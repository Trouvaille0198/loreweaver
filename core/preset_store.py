"""ST completion presets on disk — discovery + load.

Two tiers of presets:

- **System presets** live in the engine's own ``presets/`` directory (the repo
  checkout, or ``TRPG_SYSTEM_PRESET_DIR`` when the deployment relocates it — the
  Docker image copies it to ``/srv/presets``). They ship with the engine, are
  read-only (no delete/overwrite), and carry the engine's own gate markers such as
  ``x_loreweaver_content_rating`` (mature mode).
- **User presets** live under ``data_dir/presets/`` — a keeper's ``.preset import
  <path>`` lands the file VERBATIM there (the imported file is the source of truth;
  normalization happens on every load). They support create/overwrite/delete and
  import/export.

Rooms enable ONE preset id (the ``preset_enabled.<chat_key>`` store flag, managed by
`gateway.ops`), and `agent.prompt_builder` folds `core.preset.style_segments` of the
enabled preset into the assembled system prompt. A system id always wins over a
same-named user file — a generated/imported preset can never shadow ``mature-mode``.

Load failures ALWAYS degrade to ``None`` — a deleted, corrupt, oversized or
unparseable preset file must never break a room's turn; the prompt builder depends on
that contract the same way it tolerates a missing skill id.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.preset import MAX_PRESET_BYTES, StPreset, parse_st_preset

PRESET_DIR_NAME = "presets"

#: Optional deployment override for where the engine's own presets live. The Docker
#: image sets this to ``/srv/presets``; the default is the repo checkout's ``presets/``.
_SYSTEM_PRESET_DIR_ENV = "TRPG_SYSTEM_PRESET_DIR"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def system_presets_dir() -> Path:
    """The engine-owned presets directory (read-only tier)."""
    override = os.environ.get(_SYSTEM_PRESET_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / PRESET_DIR_NAME


def presets_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / PRESET_DIR_NAME


def _read_preset_file(directory: Path, preset_id: str) -> str | None:
    """Raw text of ``<directory>/<id>.json``; ``None`` on any failure."""
    if not _ID_RE.match(preset_id):
        return None
    path = directory / f"{preset_id}.json"
    try:
        if path.stat().st_size > MAX_PRESET_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def sanitize_preset_id(name: str) -> str:
    """A filesystem-safe preset id from a filename (or stem): lowercased, every run of
    characters outside ``[a-z0-9]`` collapsed to one dash, capped at 64 chars. A stem
    that leaves nothing usable (e.g. a fully-CJK title) falls back to ``"preset"``;
    an empty input stays ``""`` so callers can reject it."""
    stem = Path(str(name)).stem.strip().lower()
    if not stem:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:64]
    return slug if slug and _ID_RE.match(slug) else "preset"


def list_preset_ids(data_dir: str | Path) -> list[str]:
    """Installed preset ids (sorted, system first); tolerates a missing/unreadable directory.

    A user file with the same id as a system preset is shadowed — the system tier wins —
    so the list contains each id at most once."""
    def _scan(directory: Path) -> list[str]:
        try:
            return sorted(
                path.stem
                for path in directory.glob("*.json")
                if path.is_file() and _ID_RE.match(path.stem)
            )
        except OSError:
            return []

    ids: list[str] = []
    seen: set[str] = set()
    for directory in (system_presets_dir(), presets_dir(data_dir)):
        for preset_id in _scan(directory):
            if preset_id not in seen:
                ids.append(preset_id)
                seen.add(preset_id)
    return ids


def preset_source(data_dir: str | Path, preset_id: str) -> str:
    """``"system"`` when the id resolves to the engine tier, ``"user"`` otherwise."""
    if not _ID_RE.match(preset_id):
        return "user"
    if (system_presets_dir() / f"{preset_id}.json").is_file():
        return "system"
    return "user"


def save_preset_text(data_dir: str | Path, preset_id: str, text: str) -> Path:
    """Persist an ALREADY-PARSED preset's raw text under ``presets/<id>.json`` in the
    USER tier.

    Callers must run `core.preset.parse_st_preset` first (the command surface does) —
    this function only writes. Raises ``ValueError`` on a malformed id or on a collision
    with a read-only SYSTEM preset id; ``OSError`` propagates to the caller's localized
    error path."""
    if not isinstance(preset_id, str) or not _ID_RE.match(preset_id):
        raise ValueError(f"not a preset id: {preset_id!r}")  # i18n-exempt: wrapped by the command's localized reply
    if preset_source(data_dir, preset_id) == "system":
        raise ValueError(f"preset id is a read-only system preset: {preset_id!r}")  # i18n-exempt
    directory = presets_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{preset_id}.json"
    path.write_text(text, encoding="utf-8")
    return path


def load_preset(data_dir: str | Path, preset_id: str) -> StPreset | None:
    """Parse ``<id>.json`` (system tier first, then user) through the real parser;
    ``None`` on ANY failure."""
    if not isinstance(preset_id, str) or not _ID_RE.match(preset_id):
        return None
    text = _read_preset_file(system_presets_dir(), preset_id) or _read_preset_file(presets_dir(data_dir), preset_id)
    if text is None:
        return None
    try:
        return parse_st_preset(text, preset_id)
    except ValueError:
        return None


def load_preset_text(data_dir: str | Path, preset_id: str) -> str | None:
    """The VERBATIM file text of a preset (system tier first) — the export surface.

    ``None`` on a missing/oversized/unreadable file. Unlike :func:`load_preset` this
    does not parse, so an export never fails on a preset the parser would reject.
    """
    if not isinstance(preset_id, str) or not _ID_RE.match(preset_id):
        return None
    return _read_preset_file(system_presets_dir(), preset_id) or _read_preset_file(presets_dir(data_dir), preset_id)


def delete_preset(data_dir: str | Path, preset_id: str) -> bool:
    """Remove the USER-tier ``presets/<id>.json``; ``False`` when it did not exist, the
    id is malformed, or the id names a read-only SYSTEM preset.

    Callers own the housekeeping around a deleted preset: rooms that had it enabled
    degrade to no style layer on their next prompt build (load failures always do),
    and the current room's ``preset_enabled`` flag is best cleared explicitly.
    """
    if not isinstance(preset_id, str) or not _ID_RE.match(preset_id):
        return False
    if preset_source(data_dir, preset_id) == "system":
        return False
    path = presets_dir(data_dir) / f"{preset_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
