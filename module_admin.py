"""Web-only keeper admin extension for room module source files.

The engine's published protocol predates this surface. The bridge deliberately
uses the existing ``admin_generated`` reply lane so older clients keep parsing
all responses; ``serve_both.py`` installs it on the web server's AdminService.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import core.pack as core_pack
import yaml
from agent.context import AgentCtx, LocalFs
from agent.kp_tools_knowledge import DocumentTools
from agent.module_lifecycle import active_module
from core.documents import KEEPER_VIEWER, MODULE_POOL_ID
from core.skills import parse_skill_text
from core.worldbook import LORE_DOC_TYPE
from core.yaml_safety import safe_load_no_aliases
from infra.media_store import ALLOWED_MEDIA_MIMES, MediaStore
from net.admin import AdminService, _error
from net.room_backup import chat_key_for_room

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_WORLDBOOK_SUFFIXES = frozenset({".json"})
_BUNDLE_SUFFIX = ".zip"
_CUSTOM_KINDS = frozenset(
    {
        "module_list",
        "module_detail",
        "module_upload",
        "module_update",
        "module_pregen_update",
        "module_pack_export",
        "module_delete",
        "module_bundle_upload",
        "module_pack_upload",
        "module_import",
        "module_media_generate",
        "pregen_avatar",
        "worldbook_list",
        "worldbook_detail",
        "worldbook_upload",
        "worldbook_select",
        "worldbook_disable",
    }
)
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_FILES = 128


def install_module_admin(admin: AdminService) -> AdminService:
    """Wrap the engine admin service with module-source operations."""
    if isinstance(admin, ModuleAdminService):
        return admin
    return ModuleAdminService(admin)


class ModuleAdminService:
    """Intercept module-source actions and delegate every other admin frame."""

    def __init__(self, inner: AdminService) -> None:
        self.inner = inner
        self.services = inner.services
        self.keystore = inner.keystore
        self.fs = inner.fs
        self.hub = inner.hub

    async def dispatch(
        self,
        role: str,
        caller_room: str,
        frame: dict[str, Any],
        i18n: Any,
        *,
        reauthorize: Any = None,
        emit_frame: Any = None,
    ) -> dict[str, Any]:
        kind = frame.get("kind") if frame.get("type") == "admin_generate" else None
        if kind not in _CUSTOM_KINDS:
            return await self.inner.dispatch(
                role,
                caller_room,
                frame,
                i18n,
                reauthorize=reauthorize,
                emit_frame=emit_frame,
            )
        # `module_detail` is a pure read: the shared module link renders the same page to
        # the room's players, so any member may fetch it. Every WRITE (upload, update,
        # export, delete, generate, import) stays keeper-only.
        if role != "keeper" and kind != "module_detail":
            return _error("forbidden", i18n)
        try:
            # NO room lock here: the transport choke point (`net.session._on_frame`) already
            # holds the room's `turn_lock` around the ENTIRE `admin_generate` dispatch. That
            # lock is a plain (non-reentrant) `asyncio.Lock`; re-acquiring the same one in
            # this task self-deadlocks the import and strands the room lock forever, which
            # also wedges every later module request for the room (the import never answers
            # and never releases). Taking the lock exactly once, at the choke point, is what
            # keeps module_import serialized against concurrent turns.
            return await self._dispatch_module(caller_room, frame, i18n)
        except ValueError:
            return _error("bad_request", i18n)
        except OSError as exc:
            return _module_reply(str(kind), False, "", {"error": str(exc)})

    async def _dispatch_module(self, caller_room: str, frame: dict[str, Any], i18n: Any) -> dict[str, Any]:
        kind = str(frame.get("kind"))
        root = self._root()
        root.mkdir(parents=True, exist_ok=True)
        payload = self._payload(frame)
        if kind == "module_list":
            return await self._list(caller_room, root)
        if kind == "module_detail":
            return await self._detail(caller_room, root, str(payload.get("name") or ""))
        if kind == "module_upload":
            return await self._upload(root, payload)
        if kind == "module_pregen_update":
            return await self._update_pregen(payload)
        if kind == "module_pack_export":
            return await self._export_pack(payload)
        if kind == "module_delete":
            return await self._delete(caller_room, root, payload)
        if kind == "module_bundle_upload":
            return await self._bundle_upload(root, payload)
        if kind == "module_pack_upload":
            return await self._pack_upload(root, payload)
        if kind == "module_import":
            requested_locale = str(payload.get("locale") or "").replace("_", "-").split("-", 1)[0].casefold()
            import_i18n = i18n.with_locale(requested_locale) if requested_locale in {"en", "zh"} else i18n
            return await self._import(caller_room, root, payload, import_i18n)
        if kind == "module_media_generate":
            return await self._media_generate(caller_room, payload, i18n)
        if kind == "pregen_avatar":
            return await self._pregen_avatar(caller_room, payload, i18n)
        worldbook_root = self._worldbook_root()
        worldbook_root.mkdir(parents=True, exist_ok=True)
        if kind == "worldbook_list":
            return await self._worldbook_list(caller_room, worldbook_root)
        if kind == "worldbook_detail":
            return await self._worldbook_detail(caller_room, worldbook_root, str(payload.get("name") or ""))
        if kind == "worldbook_upload":
            return await self._worldbook_upload(worldbook_root, payload)
        if kind == "worldbook_select":
            return await self._worldbook_select(caller_room, worldbook_root, payload, i18n)
        if kind == "worldbook_disable":
            return await self._worldbook_disable(caller_room)
        raise ValueError("unknown admin action")

    def _root(self) -> Path:
        return Path(self.services.settings.data_dir).resolve() / "modules"

    def _worldbook_root(self) -> Path:
        return Path(self.services.settings.data_dir).resolve() / "worldbooks"

    @staticmethod
    def _pack_dir_size(home: Path) -> int:
        """Total bytes of an installed pack home (cards + assets + manifest), for the library row."""
        total = 0
        try:
            for entry in home.rglob("*"):
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            pass
        return total

    @staticmethod
    def _pack_world_cards(home: Path) -> tuple[Any | None, list[tuple[Any, Path]]]:
        """Manifest-declared world cards, preserving nested paths and file formats."""
        manifest_path = home / core_pack.MANIFEST_NAME
        if not manifest_path.is_file():
            return None, []
        text = manifest_path.read_text(encoding="utf-8")
        manifest = None
        for expect_trust in (True, False):
            try:
                manifest = core_pack.parse_manifest_text(text, expect_trust=expect_trust)
                break
            except Exception:
                continue
        if manifest is None:
            return None, []
        cards: list[tuple[Any, Path]] = []
        is_dev = home in core_pack.DEV_PACK_HOMES.values()
        base = home.resolve()
        for card in manifest.card_entries:
            try:
                path = (home / card.path).resolve(strict=True)
                path.relative_to(base)
            except (OSError, ValueError):
                continue
            kind = card.kind
            if is_dev:
                try:
                    kind = core_pack.detect_card_kind(card.path, path.read_bytes())
                except Exception:
                    continue
            if kind == "world" and path.is_file():
                cards.append((card, path))
        return manifest, cards

    async def _active_module(self, caller_room: str) -> dict[str, Any]:
        from agent.module_lifecycle import active_module

        return await active_module(self.services, chat_key_for_room(caller_room)) or {}
    @staticmethod
    def _payload(frame: dict[str, Any]) -> dict[str, Any]:
        raw = frame.get("description")
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"name": raw.strip()}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _safe_name(raw: str) -> str:
        name = raw.strip()
        path = Path(name)
        if not name or path.name != name or name in {".", ".."}:
            raise ValueError("invalid module filename")
        if any(ord(char) < 32 for char in name):
            raise ValueError("invalid module filename")
        if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
            raise ValueError("unsupported module filename")
        return name

    @classmethod
    def _path(cls, root: Path, raw: str) -> tuple[str, Path]:
        name = cls._safe_name(raw)
        target = root / name
        if target.is_symlink():
            raise ValueError("symlink module source")
        resolved = target.resolve()
        if resolved.parent != root.resolve():
            raise ValueError("module path escapes source directory")
        return name, target

    @classmethod
    def _files(cls, root: Path) -> list[tuple[str, Path]]:
        root.mkdir(parents=True, exist_ok=True)
        return [
            (path.name, path)
            for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in _ALLOWED_SUFFIXES
        ]

    @staticmethod
    def _title(content: str, name: str) -> str:
        for line in content.splitlines():
            candidate = line.strip()
            if candidate.startswith("# "):
                title = candidate[2:].strip()
                if title:
                    return title
        return Path(name).stem

    async def _pack_detail(self, caller_room: str, raw_name: str) -> dict[str, Any]:
        """Detail for an installed .lwpack content pack, read directly from its bundled world
        card (no room import needed): the card's name/description/scenario/opening, its worldbook
        entries, typed variables, and pregen cast."""
        from gateway.panels import installed_pack_homes

        pack_id, _, selected_path = raw_name.strip().partition("/")
        homes = installed_pack_homes(Path(self.services.settings.data_dir))
        home = homes.get(pack_id)
        if home is None:
            return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        manifest, world_cards = self._pack_world_cards(home)
        if manifest is None or not world_cards:
            return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        if selected_path:
            world_cards = [pair for pair in world_cards if pair[0].path == selected_path]
            if not world_cards:
                return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        title = dict(manifest.name).get("en") or pack_id
        # The card's own description is the AUTHORED pitch ("死亡级难度的 1-2 级短模组…"),
        # which carries the difficulty/level the keeper chose; the manifest template
        # description is only the fallback for hand-written packs that skip it.
        description = ""

        # Read the pack's own world card(s): its lore, variables, pregens, and prose. The world
        # card is the pack's module content — showing it needs no room import.
        entries: list[dict[str, Any]] = []
        variables: list[dict[str, Any]] = []
        pregens: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        scenario = ""
        opening = ""
        levels = ""
        difficulty = ""
        for _card_entry, card_path in world_cards:
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — skip an unreadable card
                continue
            if not isinstance(card, dict):
                continue
            for entry in card.get("worldbook") or []:
                if not isinstance(entry, dict):
                    continue
                keys = entry.get("keys") or []
                if isinstance(keys, str):
                    keys = [keys]
                keys = [str(k) for k in keys if str(k).strip()]
                category = str(entry.get("category") or "lore")
                entry_title = str(entry.get("title") or entry.get("comment") or entry.get("name") or "").strip()
                if not entry_title:
                    entry_title = keys[0] if keys else category
                entries.append(
                    {
                        "title": entry_title,
                        "content": str(entry.get("content") or ""),
                        "keys": keys,
                        "secret": bool(entry.get("secret", False)),
                        "category": category,
                        # The entry's bound illustration (NPC portraits etc.) —
                        # rendered beside the entry, not in the material gallery.
                        "image": str(entry.get("image") or ""),
                    }
                )
            variables.extend([dict(v) for v in (card.get("variables") or []) if isinstance(v, dict)])
            for pregen_index, p in enumerate(card.get("pregens") or []):
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                # Keep the complete editable character payload visible to the keeper. The
                # stable card path/index pair lets the update lane target a character even
                # after its display name is changed.
                persona = str(
                    p.get("background") or p.get("notes") or p.get("concept") or p.get("blurb") or ""
                )
                # `character_class` / `race` are first-class identity fields the detail
                # page renders (localized); everything else unknown rides `extra`.
                known = {"name", "background", "notes", "concept", "blurb", "appearance", "occupation", "character_class", "race", "aliases", "skills", "avatar"}
                extra = {key: value for key, value in p.items() if key not in known}
                pregens.append(
                    {
                        "id": f"{_card_entry.path}#{pregen_index}",
                        "card_path": _card_entry.path,
                        "index": pregen_index,
                        "name": str(p.get("name", "")),
                        "concept": persona,
                        "background": persona,
                        "appearance": str(p.get("appearance") or ""),
                        "occupation": str(p.get("occupation") or ""),
                        "character_class": str(p.get("character_class") or ""),
                        "race": str(p.get("race") or ""),
                        "aliases": [str(a) for a in (p.get("aliases") or []) if str(a).strip()],
                        "skills": dict(p.get("skills") or {}) if isinstance(p.get("skills"), dict) else {},
                        "avatar": str(p.get("avatar") or ""),
                        **({"extra": extra} if extra else {}),
                    }
                )
            # The pack's designed items (the catalog templates `.item grant` hands out).
            for it in card.get("items") or []:
                if not isinstance(it, dict) or not str(it.get("name", "")).strip():
                    continue
                items.append(
                    {
                        "name": str(it.get("name", "")),
                        "kind": str(it.get("kind") or ""),
                        "slot": str(it.get("slot") or ""),
                        "scope": str(it.get("scope") or ""),
                        "description": str(it.get("description") or ""),
                        "effect": str(it.get("effect") or ""),
                        "lore": str(it.get("lore") or ""),
                        "origin": str(it.get("origin") or ""),
                        "original_holder": str(it.get("original_holder") or ""),
                        "plot_role": str(it.get("plot_role") or ""),
                        "quantity": it.get("quantity", 1) if isinstance(it.get("quantity"), int) else 1,
                        "bonus": dict(it.get("bonus") or {}),
                    }
                )
            scenario = str(card.get("scenario") or "") or scenario
            opening = str(card.get("opening") or "") or opening
            # The forge stamps a machine difficulty tier on the card; legacy packs only
            # carry it as a tag (e.g. "死亡级" for deadly) — fall back to tag matching so
            # the detail UI's difficulty chip works for both.
            description = str(card.get("description") or "") or description
            levels = str(card.get("recommended_levels") or "") or levels
            difficulty = str(card.get("difficulty") or "") or difficulty
            if not difficulty:
                for _tag in card.get("tags") or []:
                    _t = str(_tag).strip().casefold()
                    if _t in {"easy", "standard", "hard", "deadly"}:
                        difficulty = _t
                        break
                    if _t in {"简单", "轻松", "容易"}:
                        difficulty = "easy"
                        break
                    if _t in {"标准", "普通"}:
                        difficulty = "standard"
                        break
                    if _t in {"困难", "艰难"}:
                        difficulty = "hard"
                        break
                    if _t in {"死亡级", "极限", "致命"}:
                        difficulty = "deadly"
                        break

        # The pack's bundled rulepack(s) and KP skill(s) install into the shared discovery dirs
        # (`data/rulepacks/`, `data/skills/`) — not the pack home — so resolve them by the
        # manifest's declared `contents` and read from those dirs.
        data_dir = Path(self.services.settings.data_dir)
        rulepacks_dir = data_dir / "rulepacks"
        skills_dir = data_dir / "skills"
        rulepacks: list[dict[str, Any]] = []
        for declared in manifest.contents["rulepacks"]:
            name = PurePosixPath(declared).name
            rp_file = rulepacks_dir / name
            if not rp_file.is_file():
                continue
            try:
                rp_text = rp_file.read_text(encoding="utf-8")
            except OSError:
                continue
            names: list[str] = []
            try:
                rp = safe_load_no_aliases(rp_text)
                if isinstance(rp, dict):
                    names = [str(n) for n in (rp.get("names") or []) if str(n).strip()]
            except Exception:  # noqa: BLE001
                pass
            rulepacks.append(
                {
                    "name": rp_file.stem,
                    "title": names[0] if names else rp_file.stem,
                    "content": rp_text[:8000],
                }
            )
        skills: list[dict[str, str]] = []
        for declared in manifest.contents["skills"]:
            skill_id = PurePosixPath(declared).name
            skill_file = skills_dir / skill_id / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                skill_text = skill_file.read_text(encoding="utf-8")[:8000]
            except OSError:
                continue
            # The directory id is the stable install handle; the human title lives in the
            # SKILL.md frontmatter `name` (e.g. a forge-authored "瘴雨巫蛊"). Show that.
            try:
                display_name = parse_skill_text(skill_id, skill_text).name
            except Exception:  # noqa: BLE001 — unparseable frontmatter falls back to the id
                display_name = skill_id
            skills.append({"name": display_name or skill_id, "content": skill_text})

        content_parts = [part for part in (description, scenario, opening) if part]
        content = "\n\n".join(content_parts)
        # The pack's bundled asset illustrations (installed under the pack home, content-addressed
        # by path) — the images a module ships with, shown in the detail view.
        media: list[dict[str, Any]] = []
        for asset_path in manifest.assets:
            p = home / asset_path.path
            if not p.is_file():
                continue
            # Figure kind from the provenance name (`module-<id>-<kind>-<n>.jpg`): cover/scenes/npcs/items.
            kind = "asset"
            stem = p.stem
            for token in ("cover", "scenes", "npcs", "clue", "items", "item", "pregens"):
                if token in stem:
                    kind = token
                    break
            # NO inline `data` payload: it was truncated at 512KB base64, which cut every qwen
            # image (~1.4MB) down to a partial picture. Clients fetch the FULL bytes through the
            # content-addressed asset channel (`assetFetch(hash)`), which the server resolves from
            # installed packs even before the pack is enabled in the room.
            media.append(
                {
                    "name": p.name,
                    "hash": asset_path.sha256,
                    "mime": asset_path.mime,
                    "size": asset_path.size,
                    "kind": kind,
                    **({"subject": asset_path.title} if asset_path.title else {}),
                }
            )
        # Live illustration job state: pending/generating plates the detail page renders as
        # "正在生成中" placeholders; failed jobs keep their prompt for a one-click retry.
        from gateway.module_media import load_jobs

        jobs_doc = load_jobs(home)
        # A failed fresh-shot plan persists its localized reason in the sidecar; the detail
        # page shows it so the keeper knows why nothing was queued.
        media_plan_error = str(jobs_doc.get("plan_error") or "")
        media_jobs = [
            {
                "id": str(job.get("id") or ""),
                "kind": str(job.get("kind") or ""),
                "subject": str(job.get("subject") or ""),
                "prompt": str(job.get("prompt") or ""),
                "caption": str(job.get("caption") or ""),
                "status": str(job.get("status") or "pending"),
                "asset": str(job.get("asset") or ""),
                "hash": str(job.get("hash") or ""),
                "mime": str(job.get("mime") or ""),
                "error": str(job.get("error") or ""),
            }
            for job in jobs_doc.get("jobs", [])
            if isinstance(job, dict) and job.get("id")
        ]
        return _module_reply(
            "module_detail",
            True,
            raw_name,
            {
                "name": raw_name,
                "title": title,
                "size": self._pack_dir_size(home),
                "modified": int(home.stat().st_mtime * 1000),
                "content": content,
                "source_kind": "pack",
                "current": (
                    (await self._active_module(caller_room)).get("pack_id") == pack_id
                    and (
                        not selected_path
                        or (await self._active_module(caller_room)).get("card_path") == selected_path
                    )
                ),
                "status": "ready",
                "import_status": "",
                "importing": False,
                "pool": None,
                "media": media,
                "media_jobs": media_jobs,
                "media_plan_error": media_plan_error,
                "worldbook_entries": entries,
                "variables": variables,
                "pregens": pregens,
                "items": items,
                "rulepacks": rulepacks,
                "skills": skills,
                "levels": levels,
                "difficulty": difficulty,
            },
        )

    @staticmethod
    def _write_pack_file(path: Path, content: bytes) -> None:
        """Replace one installed-pack file atomically after its content is validated."""
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    @classmethod
    def _update_pack_manifest(cls, home: Path, relative_path: str, content: bytes) -> None:
        """Keep the installed pack's file digest aligned with an edited world card."""
        manifest_path = home / core_pack.MANIFEST_NAME
        raw = safe_load_no_aliases(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid pack manifest")
        files = raw.get("files")
        if not isinstance(files, list):
            files = []
            raw["files"] = files
        digest = hashlib.sha256(content).hexdigest()
        for entry in files:
            if isinstance(entry, dict) and entry.get("path") == relative_path:
                entry["sha256"] = digest
                entry["size"] = len(content)
                break
        else:
            files.append({"path": relative_path, "sha256": digest, "size": len(content)})
        cls._write_pack_file(
            manifest_path,
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False).encode("utf-8"),
        )

    async def _update_pregen(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist one claimable investigator back into its installed world card."""
        from gateway.panels import installed_pack_homes

        raw_name = str(payload.get("name") or "").strip()
        pack_id, _, selected_path = raw_name.partition("/")
        home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
        if home is None:
            return _module_reply("module_pregen_update", False, raw_name, {"error": "source_not_found"})
        card_path = str(payload.get("card_path") or "").strip()
        index = payload.get("index")
        updates = payload.get("pregen")
        if not card_path or not isinstance(index, int) or index < 0 or not isinstance(updates, dict):
            return _module_reply("module_pregen_update", False, raw_name, {"error": "bad_request"})
        if selected_path and selected_path != card_path:
            return _module_reply("module_pregen_update", False, raw_name, {"error": "bad_request"})
        _manifest, world_cards = self._pack_world_cards(home)
        card_match = next(((entry, path) for entry, path in world_cards if entry.path == card_path), None)
        if card_match is None:
            return _module_reply("module_pregen_update", False, raw_name, {"error": "source_not_found"})
        _entry, path = card_match
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _module_reply("module_pregen_update", False, raw_name, {"error": "invalid_world_card"})
        pregens = card.get("pregens") if isinstance(card, dict) else None
        if not isinstance(pregens, list) or index >= len(pregens) or not isinstance(pregens[index], dict):
            return _module_reply("module_pregen_update", False, raw_name, {"error": "source_not_found"})

        current = dict(pregens[index])
        name = str(updates.get("name") or "").strip()[:60]
        if not name:
            return _module_reply("module_pregen_update", False, raw_name, {"error": "invalid_pregen_name"})
        if any(
            offset != index
            and isinstance(other, dict)
            and str(other.get("name") or "").strip().casefold() == name.casefold()
            for offset, other in enumerate(pregens)
        ):
            return _module_reply("module_pregen_update", False, raw_name, {"error": "pregen_name_taken"})

        def clean_text(key: str, limit: int) -> str:
            return str(updates.get(key) or "").strip()[:limit]

        aliases_raw = updates.get("aliases")
        aliases = (
            [str(alias).strip()[:64] for alias in aliases_raw if str(alias).strip()][:8]
            if isinstance(aliases_raw, list)
            else []
        )
        skills_raw = updates.get("skills")
        skills: dict[str, int] = {}
        if isinstance(skills_raw, dict):
            for key, value in list(skills_raw.items())[:32]:
                try:
                    skills[str(key).strip()[:60]] = int(value)
                except (TypeError, ValueError):
                    return _module_reply("module_pregen_update", False, raw_name, {"error": "invalid_pregen_skills"})
        # Identity ids are first-class editable fields on the keeper's pregen editor;
        # a client that never sends them (older builds) must not wipe existing values.
        for identity_key in ("character_class", "race"):
            if updates.get(identity_key) is not None:
                current[identity_key] = clean_text(identity_key, 60)
        # Canonicalize legacy persona spellings so an old `notes`/`concept` value cannot
        # silently override the keeper's edited `background` on the next import.
        for legacy_key in ("notes", "concept", "blurb"):
            current.pop(legacy_key, None)
        extra = updates.get("extra")
        if isinstance(extra, dict):
            reserved = {"name", "background", "appearance", "occupation", "character_class", "race", "aliases", "skills", "avatar"}
            current.update({str(key): value for key, value in extra.items() if str(key) not in reserved})
        pregens[index] = current
        encoded = json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_pack_file(path, encoded)
        self._update_pack_manifest(home, card_path, encoded)
        return _module_reply(
            "module_pregen_update",
            True,
            raw_name,
            {"card_path": card_path, "index": index, "name": name},
        )

    async def _export_pack(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the edited installed pack for download or replacement of its source archive."""
        from gateway.panels import installed_pack_homes

        raw_name = str(payload.get("name") or "").strip()
        overwrite = bool(payload.get("overwrite"))
        pack_id, _, _selected_path = raw_name.partition("/")
        home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
        if home is None:
            return _module_reply("module_pack_export", False, raw_name, {"error": "source_not_found"})
        manifest, world_cards = self._pack_world_cards(home)
        if manifest is None or not world_cards:
            return _module_reply("module_pack_export", False, raw_name, {"error": "source_not_found"})

        export_dir = Path(self.services.settings.data_dir).resolve() / "module_exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        expiry = time.time() - 3600
        for old_file in export_dir.glob("*.lwpack"):
            try:
                if old_file.stat().st_mtime < expiry:
                    old_file.unlink()
            except OSError:
                continue

        token = secrets.token_urlsafe(32)
        output = export_dir / f"{token}.lwpack"
        try:
            # Installed homes contain the built manifest, while build_pack expects an author
            # manifest. Work on a temporary copy so the downloaded archive is validated by the
            # normal pack builder without mutating the installed source or its provenance.
            with tempfile.TemporaryDirectory(prefix=".pack-export-", dir=export_dir) as source_name:
                source = Path(source_name)
                shutil.copytree(home, source, dirs_exist_ok=True)
                data_dir = Path(self.services.settings.data_dir).resolve()

                # `install_pack` keeps cards/assets in the pack home but extracts skills,
                # rulepacks and presets into their shared discovery directories. Restore
                # those declared files into the temporary source so the exported archive
                # remains complete for a later install on another server.
                for relative in manifest.contents["skills"]:
                    target = source / PurePosixPath(relative)
                    if target.is_dir():
                        continue
                    shared = data_dir / "skills" / PurePosixPath(relative).name
                    if shared.is_dir():
                        shutil.copytree(shared, target, dirs_exist_ok=True)
                for relative in manifest.contents["rulepacks"]:
                    target = source / PurePosixPath(relative)
                    if target.is_file():
                        continue
                    shared = data_dir / "rulepacks" / PurePosixPath(relative).name
                    if shared.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(shared, target)
                        pack_id = PurePosixPath(relative).stem
                        shared_scripts = data_dir / "rulepacks" / pack_id
                        if shared_scripts.is_dir():
                            shutil.copytree(shared_scripts, target.parent / pack_id, dirs_exist_ok=True)
                for relative in manifest.contents["presets"]:
                    target = source / PurePosixPath(relative)
                    if target.is_file():
                        continue
                    from core.preset_store import sanitize_preset_id

                    shared = data_dir / "presets" / f"{sanitize_preset_id(PurePosixPath(relative).name)}.json"
                    if shared.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(shared, target)
                source_manifest = safe_load_no_aliases(
                    (source / core_pack.MANIFEST_NAME).read_text(encoding="utf-8")
                )
                if not isinstance(source_manifest, dict):
                    raise ValueError("invalid pack manifest")
                contents = source_manifest.get("contents")
                if isinstance(contents, dict) and isinstance(contents.get("cards"), list):
                    for card in contents["cards"]:
                        if isinstance(card, dict):
                            card.pop("kind", None)
                source_manifest.pop("files", None)
                source_manifest.pop("trust", None)
                (source / core_pack.MANIFEST_NAME).write_text(
                    yaml.safe_dump(source_manifest, sort_keys=False, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8",
                )
                built = core_pack.build_pack(source, output)
        except Exception:  # noqa: BLE001 — return a safe operation error to the keeper
            logger.exception("module pack export failed for %s", raw_name)
            output.unlink(missing_ok=True)
            return _module_reply("module_pack_export", False, raw_name, {"error": "pack_export_failed"})

        filename = f"{built.manifest.id}-{built.manifest.version}{core_pack.PACK_SUFFIX}"
        if overwrite:
            original = Path(self.services.settings.data_dir).resolve() / "modules" / filename
            if not original.is_file() or original.is_symlink():
                output.unlink(missing_ok=True)
                return _module_reply("module_pack_export", False, raw_name, {"error": "source_archive_not_found"})
            temporary_original = original.with_name(f".{original.name}.{token}.tmp")
            try:
                shutil.copyfile(output, temporary_original)
                temporary_original.replace(original)
                output.unlink(missing_ok=True)
            except OSError:
                temporary_original.unlink(missing_ok=True)
                logger.exception("module pack source overwrite failed for %s", original)
                output.unlink(missing_ok=True)
                return _module_reply("module_pack_export", False, raw_name, {"error": "pack_export_failed"})
            return _module_reply(
                "module_pack_export",
                True,
                raw_name,
                {"filename": filename, "overwritten": True},
            )
        return _module_reply(
            "module_pack_export",
            True,
            raw_name,
            {
                "download_url": f"/__module-download/{token}/{quote(filename)}",
                "filename": filename,
                "size": output.stat().st_size,
                "overwritten": False,
            },
        )

    async def _current_name(self, caller_room: str, root: Path, files: list[tuple[str, Path]]) -> str:
        chat_key = chat_key_for_room(caller_room)
        active = await self._active_module(caller_room)
        if active.get("kind") == "text":
            source = str(active.get("source") or "")
            if any(candidate == source for candidate, _ in files):
                return source
        recorded = str(await self.services.store.state_get(chat_key, "module_source") or "")
        if recorded:
            try:
                name, _ = self._path(root, recorded)
            except ValueError:
                name = ""
            if name and any(candidate == name for candidate, _ in files):
                return name
        fulltext = str(await self.services.store.state_get(chat_key, "module_fulltext") or "")
        if not fulltext:
            return ""
        for name, path in files:
            try:
                if path.read_text(encoding="utf-8") == fulltext:
                    return name
            except (OSError, UnicodeDecodeError):
                continue
        return ""

    async def _list(self, caller_room: str, root: Path) -> dict[str, Any]:
        files = self._files(root)
        current = await self._current_name(caller_room, root, files)
        chat_key = chat_key_for_room(caller_room)
        active = await self._active_module(caller_room)
        import_status = str(await self.services.store.state_get(chat_key, "module_import_status") or "")
        importing_name = str(await self.services.store.state_get(chat_key, "module_import_name") or "")
        modules = []
        for name, path in files:
            stat = path.stat()
            modules.append(
                {
                    "name": name,
                    "title": self._title(path.read_text(encoding="utf-8"), name),
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime * 1000),
                    "source_kind": "text",
                    "current": name == current,
                    "importing": name == importing_name and import_status == "processing",
                }
            )
        # Installed .lwpack content packs are module sources too — list them alongside the
        # Markdown text sources so the library shows every importable module in one place,
        # distinguished by `source_kind`.
        from gateway.panels import installed_pack_homes

        for pack_id, home in sorted(installed_pack_homes(Path(self.services.settings.data_dir)).items()):
            manifest, world_cards = self._pack_world_cards(home)
            if manifest is None or not world_cards:
                continue
            display = dict(manifest.name).get("en") or pack_id
            stat = home.stat()
            # Summarize the pack's content for the library row: lore entry count + pregen cast size.
            entry_count = 0
            pregen_count = 0
            for _card_entry, card_path in world_cards:
                if card_path.suffix.casefold() == ".json":
                    try:
                        card = json.loads(card_path.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001 — skip an unreadable card
                        continue
                    if not isinstance(card, dict):
                        continue
                    entry_count += len([e for e in (card.get("worldbook") or []) if isinstance(e, dict)])
                    pregen_count += len([p for p in (card.get("pregens") or []) if isinstance(p, dict)])
            for card_entry, _card_path in world_cards:
                module_name = pack_id if len(world_cards) == 1 else f"{pack_id}/{card_entry.path}"
                modules.append(
                    {
                    "name": module_name,
                    "title": display if len(world_cards) == 1 else f"{display} — {PurePosixPath(card_entry.path).stem}",
                    "size": self._pack_dir_size(home),
                    "modified": int(stat.st_mtime * 1000),
                    "source_kind": "pack",
                    "entry_count": entry_count,
                    "pregen_count": pregen_count,
                    "current": bool(
                        active.get("pack_id") == pack_id
                        and active.get("card_path") == card_entry.path
                    ),
                    "importing": False,
                    }
                )
        # Generations currently running in this room show as placeholder sources so the keeper
        # sees them in the library while they author/render — and after a refresh, because the
        # in-flight stages are persisted by `net.admin._progress` and cleared on completion.
        # One row per generation id (`generation_progress:<id>`): parallel forges each keep a
        # live placeholder instead of overwriting one shared key.
        generating_rows = await self.services.store.state_list(chat_key, prefix="generation_progress:")
        placeholders: list[dict[str, Any]] = []
        for row in sorted(generating_rows, key=lambda r: str(r.get("key") or "")):
            raw = row.get("value")
            if not raw:
                continue
            try:
                gen = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(gen, dict):
                continue
            gen_id = str(row.get("key") or "").partition(":")[2]
            placeholders.append(
                {
                    "name": f"__generating__:{gen_id}",
                    "title": "",
                    "size": 0,
                    "modified": 0,
                    "source_kind": "generating",
                    "generating": True,
                    "generation_kind": str(gen.get("kind") or ""),
                    "stage": str(gen.get("stage") or ""),
                    "detail": str(gen.get("detail") or ""),
                    "current": False,
                    "importing": False,
                }
            )
        if placeholders:
            modules = [*placeholders, *modules]
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        visible_current = current
        if not visible_current and active.get("kind") == "world_card":
            pack_id = str(active.get("pack_id") or "")
            card_path = str(active.get("card_path") or "")
            if pack_id:
                active_home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
                cards = self._pack_world_cards(active_home)[1] if active_home is not None else []
                visible_current = (
                    pack_id if len(cards) == 1 else f"{pack_id}/{card_path}" if card_path else pack_id
                )
        return _module_reply(
            "module_list",
            True,
            visible_current,
            {
                "modules": modules,
                "current": visible_current,
                "status": status or ("ready" if active else ""),
            },
        )

    async def _detail(self, caller_room: str, root: Path, raw_name: str) -> dict[str, Any]:
        try:
            name, path = self._path(root, raw_name)
        except ValueError:
            name, path = "", None
        if path is None or not path.is_file():
            # Not a Markdown source — it may be an installed .lwpack content pack, which has a
            # detail too (its name, description, size; plus its worldbook entries if the room
            # has imported it). A pack detail is still a valid module detail.
            return await self._pack_detail(caller_room, raw_name)
        content = path.read_text(encoding="utf-8")
        files = self._files(root)
        current = await self._current_name(caller_room, root, files)
        chat_key = chat_key_for_room(caller_room)
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        import_status = str(await self.services.store.state_get(chat_key, "module_import_status") or "")
        import_name = str(await self.services.store.state_get(chat_key, "module_import_name") or "")
        pool = None
        media_records: list[dict[str, Any]] = []
        if current == name:
            pool = await self.services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, KEEPER_VIEWER)
            # Phase 2: the module's designed items ride the module detail alongside the
            # knowledge pool (they live in the room's `item_catalog`), so the module page
            # shows what items the script designed — category/effect/lore/origin, no holders.
            if isinstance(pool, dict) and isinstance(pool.get("keeper"), dict):
                catalog = await self.services.documents.get_singleton(chat_key, "item_catalog")
                if catalog is not None and isinstance(catalog.data.get("items"), list):
                    pool["keeper"]["items"] = catalog.data["items"]
            # Forge-generated illustrations carry the `module-<id>-` provenance prefix; they are
            # room-scoped like the pool, so they surface exactly where the pool does.
            module_id = name[:-3] if name.endswith(".md") else name
            prefix = f"module-{module_id}-"
            tui = self.services.settings.tui
            store = MediaStore(
                self.services.store,
                self.services.settings.data_dir,
                max_file_bytes=max(tui.media_max_file_bytes, tui.audio_max_file_bytes),
                room_quota_bytes=max(tui.media_room_quota_bytes, tui.audio_room_quota_bytes),
                allowed_mimes=ALLOWED_MEDIA_MIMES,
            )
            subject_by_name: dict[str, str] = {}
            raw_media_index = await self.services.store.state_get(chat_key, "module_media_index")
            if raw_media_index:
                try:
                    media_index = json.loads(raw_media_index)
                    if isinstance(media_index, list):
                        subject_by_name = {
                            str(entry.get("name")): str(entry.get("subject"))
                            for entry in media_index
                            if isinstance(entry, dict)
                            and str(entry.get("name") or "").strip()
                            and str(entry.get("subject") or "").strip()
                        }
                except (json.JSONDecodeError, TypeError):
                    subject_by_name = {}
            media_records = []
            for record in await store.list_room_records(chat_key):
                if not record.name.startswith(prefix):
                    continue
                media_records.append(
                    {
                        "name": record.name,
                        "hash": record.hash,
                        "mime": record.mime,
                        "size": record.size,
                        **({"subject": subject_by_name[record.name]} if record.name in subject_by_name else {}),
                    }
                )
        stat = path.stat()
        return _module_reply(
            "module_detail",
            True,
            name,
            {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "content": content,
                "title": self._title(content, name),
                "source_kind": "text",
                "current": current == name,
                "status": status if current == name else "",
                "import_status": import_status if import_name == name else "",
                # The room is unavailable while ANY module is being imported;
                # mark the current source too so a refreshed page cannot show its
                # stale pool during the transition.
                "importing": import_status == "processing",
                "pool": pool,
                "media": media_records,
            },
        )

    async def _upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty module content")
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("module source too large")
        path.write_bytes(raw)
        return _module_reply("module_upload", True, name, {"name": name})

    async def _update(self, caller_room: str, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        if not path.is_file():
            return _module_reply("module_update", False, name, {"error": "source_not_found"})
        current_before = await self._current_name(caller_room, root, self._files(root))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return _module_reply("module_update", False, name, {"error": "empty_module_content"})
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            return _module_reply("module_update", False, name, {"error": "module_source_too_large"})
        path.write_bytes(raw)
        if current_before == name:
            # Keep the room/source association while the edited file waits for
            # an explicit re-import.  Without this marker, a generated module
            # whose source was discovered by content comparison would look
            # inactive after its file changed, making deletion unsafe.
            await self.services.store.state_set(chat_key_for_room(caller_room), "module_source", name)
        stat = path.stat()
        return _module_reply(
            "module_update",
            True,
            name,
            {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "current": current_before == name,
            },
        )

    async def _bundle_upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        import pypdf

        name = str(payload.get("name") or "content-library.zip").strip()
        if Path(name).suffix.casefold() != ".zip" or Path(name).name != name:
            raise ValueError("invalid bundle filename")
        encoded = payload.get("archive")
        if not isinstance(encoded, str):
            raise ValueError("empty module bundle")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > _MAX_BUNDLE_BYTES:
            raise ValueError("module bundle too large")
        sections: list[str] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) > _MAX_BUNDLE_FILES:
                raise ValueError("too many bundle files")
            for member in members:
                candidate = Path(member.filename)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("bundle path escapes archive")
                suffix = candidate.suffix.casefold()
                if suffix not in {".md", ".markdown", ".txt", ".pdf"}:
                    continue
                data = archive.read(member)
                if suffix == ".pdf":
                    reader = pypdf.PdfReader(io.BytesIO(data))
                    text = "\n".join(page.extract_text() or "" for page in reader.pages)
                else:
                    text = data.decode("utf-8")
                if text.strip():
                    sections.append(f"# Source: {member.filename}\n\n{text.strip()}")
        if not sections:
            raise ValueError("bundle has no readable module sources")
        output_name = Path(name).stem + ".md"
        _, output = self._path(root, output_name)
        content = "\n\n---\n\n".join(sections)
        if len(content.encode("utf-8")) > _MAX_SOURCE_BYTES:
            raise ValueError("module source too large")
        output.write_text(content, encoding="utf-8")
        return _module_reply(
            "module_bundle_upload",
            True,
            output_name,
            {"name": output_name, "files": len(sections)},
        )

    async def _pack_upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        """Land an uploaded `.lwpack` on the server's disk — the file half of a pack install.

        The web module screen has no server filesystem, so the browser carries the whole
        archive here (base64 in one admin frame, `_MAX_BUNDLE_BYTES` cap). This only
        STORES the bytes under ``data_dir/modules/`` and hands back the path: the actual
        install runs through the keeper's ordinary `.pack install <path>` command, so
        verification, extraction, room switching and the trust card stay ONE code path
        instead of a second one drifting beside it."""
        name = str(payload.get("name") or "").strip()
        path = Path(name)
        if (
            not name
            or path.name != name
            or path.suffix.casefold() != ".lwpack"
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError("invalid pack filename")
        encoded = payload.get("archive")
        if not isinstance(encoded, str):
            raise ValueError("empty pack archive")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > _MAX_BUNDLE_BYTES:
            raise ValueError("module pack too large")
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        if target.is_symlink():
            raise ValueError("symlink module source")
        # Write-then-rename: a module listing that races the upload never sees a torn file.
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(raw)
        tmp.replace(target)
        return _module_reply("module_pack_upload", True, name, {"path": str(target), "bytes": len(raw)})

    async def _import(self, caller_room: str, root: Path, payload: dict[str, Any], i18n: Any) -> dict[str, Any]:
        raw_name = str(payload.get("name") or "").strip()
        # An installed .lwpack content pack imports through its WORLD CARD (the keeper world
        # import path) — it is a binary bundle, not a Markdown scenario, so it is handled before
        # the `.md`-only `_path`/`_safe_name` checks reject its suffix-less pack id. The world
        # card loads the pack's lorebook, variables, pregen cast and its declared rule system
        # into the room.
        from gateway.panels import installed_pack_homes

        pack_id, _, selected_card = raw_name.partition("/")
        pack_home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
        if pack_home is not None:
            return await self._import_pack(caller_room, pack_id, pack_home, i18n, selected_card=selected_card)
        name, path = self._path(root, raw_name)
        if not path.is_file():
            return _module_reply("module_import", False, name, {"error": "source_not_found"})
        source_text = path.read_text(encoding="utf-8")
        chat_key = chat_key_for_room(caller_room)
        ctx = AgentCtx(
            chat_key=chat_key,
            user_id="keeper",
            platform="tui",
            locale=i18n.locale,
            fs=LocalFs(root),
            extra={"role": "keeper"},
        )
        receipt = await DocumentTools(self.services).upload_document(ctx, file_path=name, doc_type="module")
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        installed_text = str(await self.services.store.state_get(chat_key, "module_fulltext") or "")
        ok = status in {"ready", "ready_fallback"} and installed_text == source_text
        if ok:
            await self.services.store.state_set(chat_key, "module_source", name)
        else:
            # DocumentTools returns a localized failure string instead of raising when
            # vector extraction/initialization fails before publishing a final state.
            await self.services.store.state_set(chat_key, "module_init_error", receipt[-1000:])
        return _module_reply(
            "module_import",
            ok,
            name,
            {"receipt": receipt, "status": status, "current": ok},
        )

    async def _import_pack(
        self,
        caller_room: str,
        pack_id: str,
        home: Path,
        i18n: Any,
        *,
        selected_card: str = "",
    ) -> dict[str, Any]:
        """Import an installed .lwpack content pack into the room through its bundled world
        card (`core.pack` + `CharcardTools.import_world_card`): the keeper world-import path
        loads the pack's lorebook, typed variables, pregen cast, bundled skill auto-enable and
        its declared rule system. Nothing is imported for a pack with no world card."""
        from agent.kp_tools_charcard import CharcardTools

        _manifest, world_cards = self._pack_world_cards(home)
        if selected_card:
            world_cards = [pair for pair in world_cards if pair[0].path == selected_card]
        if not world_cards:
            return _module_reply("module_import", False, pack_id, {"error": "no_world_card"})
        if len(world_cards) > 1:
            return _module_reply(
                "module_import",
                False,
                pack_id,
                {
                    "error": "multiple_world_cards",
                    "choices": [f"{pack_id}/{card.path}" for card, _path in world_cards],
                },
            )
        card_path = str(world_cards[0][1])
        chat_key = chat_key_for_room(caller_room)
        ctx = AgentCtx(
            chat_key=chat_key,
            user_id="keeper",
            platform="tui",
            locale=i18n.locale,
            fs=LocalFs(home.parent),
            extra={"role": "keeper"},
        )
        try:
            receipt = await CharcardTools(self.services).import_world_card(
                ctx, file_path=card_path, raise_on_failure=True
            )
        except Exception as exc:  # noqa: BLE001 — a failed import degrades to a clean reply
            return _module_reply("module_import", False, pack_id, {"error": f"import_failed: {exc}"})
        if self.hub is not None:
            from gateway.panels import publish_ui_manifests

            await publish_ui_manifests(self.hub, self.services, chat_key)
        # A pack imported mid-generation: remember this room for completion registration and
        # resume the background worker if its illustrations are still pending.
        from gateway.module_media import attach_importing_room

        attach_importing_room(self.services, home, chat_key)
        return _module_reply("module_import", True, pack_id, {"receipt": receipt, "current": True})


    async def _media_generate(self, caller_room: str, payload: dict[str, Any], i18n: Any) -> dict[str, Any]:
        """Trigger or retry illustration generation for an installed pack, WITHOUT re-running
        module generation. ``retry`` (a job-id list) re-queues failed jobs with their persisted
        prompts verbatim; otherwise ``kinds`` (media ids) plans fresh shots through the LLM
        shot-list lane. Both persist the jobs and schedule the background worker, returning
        immediately — the module detail page shows live status."""
        from gateway.module_media import (
            append_jobs,
            plan_media_jobs,
            record_plan_error,
            requeue_jobs,
            schedule_pack_media,
        )
        from gateway.panels import installed_pack_homes

        pack_id = str(payload.get("name") or "").strip()
        homes = installed_pack_homes(Path(self.services.settings.data_dir))
        home = homes.get(pack_id)
        if home is None:
            return _module_reply("module_media_generate", False, pack_id, {"error": "source_not_found"})
        requested_locale = str(payload.get("locale") or "").replace("_", "-").split("-", 1)[0].casefold()
        media_i18n = i18n.with_locale(requested_locale) if requested_locale in {"en", "zh"} else i18n

        retry_ids = payload.get("retry")
        if isinstance(retry_ids, list) and retry_ids:
            retry_ids = [str(i) for i in retry_ids if str(i).strip()]
            # A retry must use the same public visual-world contract as a fresh plan. Older
            # jobs may contain only the authored shot description, so load the world card here
            # and let requeue_jobs repair that persisted prompt before rendering it.
            visual_source: dict[str, Any] | None = None
            _manifest, world_cards = self._pack_world_cards(home)
            if world_cards:
                try:
                    candidate = json.loads(world_cards[0][1].read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        visual_source = candidate
                except (OSError, json.JSONDecodeError):
                    visual_source = None
            requeued = requeue_jobs(
                home,
                retry_ids,
                visual_source=visual_source,
                locale=media_i18n.locale,
            )
            if requeued:
                schedule_pack_media(self.services, pack_id)
            return _module_reply(
                "module_media_generate", True, pack_id, {"queued": requeued, "retry": len(retry_ids)}
            )

        kinds_raw = payload.get("kinds")
        if not isinstance(kinds_raw, list) or not kinds_raw:
            return _module_reply("module_media_generate", False, pack_id, {"error": "bad_request"})
        from agent.forge import MEDIA_OPTION_IDS

        kinds = [str(k) for k in kinds_raw if str(k) in MEDIA_OPTION_IDS]
        if not kinds:
            return _module_reply("module_media_generate", False, pack_id, {"error": "bad_request"})
        # Fresh-shot planning runs the LLM shot-list call, which can take tens of seconds —
        # NEVER inside the room's turn lock (the admin dispatch choke point holds it): the
        # whole plan+queue happens in a detached task, and the reply says "planning" — the
        # detail page polls and the queued jobs surface when they exist.
        _manifest, world_cards = self._pack_world_cards(home)
        if not world_cards:
            return _module_reply("module_media_generate", False, pack_id, {"error": "no_world_card"})
        try:
            card = json.loads(world_cards[0][1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _module_reply("module_media_generate", False, pack_id, {"error": "no_world_card"})
        chat_key = chat_key_for_room(caller_room)

        async def _plan_and_queue() -> None:
            try:
                jobs, note = await plan_media_jobs(
                    self.services,
                    pack_id,
                    home,
                    card,
                    kinds,
                    chat_key,
                    media_i18n,
                    force=payload.get("force") is True,
                )
                if jobs and append_jobs(home, jobs, room=chat_key):
                    schedule_pack_media(self.services, pack_id)
                elif note:
                    # The detached plan's failure must reach the keeper: persist the
                    # localized reason (shot_list_failed / no_shots / …) for the detail
                    # page instead of dropping it into silence.
                    record_plan_error(home, note)
            except Exception:  # noqa: BLE001 — a failed plan leaves the pack untouched
                logger.exception("module media planning failed for %s", pack_id)
                from agent.forge import _option_reason

                record_plan_error(
                    home,
                    media_i18n.t(
                        "agent.forge.module_media_none",
                        reason=_option_reason(media_i18n, "shot_list_failed"),
                    ),
                )

        asyncio.get_running_loop().create_task(_plan_and_queue())
        return _module_reply("module_media_generate", True, pack_id, {"queued": 0, "planning": True})


    async def _pregen_avatar(self, caller_room: str, payload: dict[str, Any], i18n: Any) -> dict[str, Any]:
        """Generate a roster character's portrait through the SAME async illustration
        lane the module detail page uses: one room-scoped job (prompt built from the
        character's appearance + persona), rendered by the background worker. The
        chat-side "生成头像" entry — works for module-imported AND `.pc gen`-born
        characters. Returns immediately; the portrait lands on the pregen document
        and any claimed party member when the worker finishes."""
        from gateway.hub import Event
        from gateway.module_media import queue_pregen_avatar

        name = str(payload.get("name") or "").strip()
        if not name:
            return _module_reply("pregen_avatar", False, "", {"error": "bad_request"})
        chat_key = chat_key_for_room(caller_room)
        ok, detail = await queue_pregen_avatar(self.services, chat_key, name)
        if ok:
            # An avatar render runs off the turn lock — the table sees a pending
            # spinner right away, then the outcome when the worker finishes.
            logger.warning("[pregen-avatar] hub=%s room=%s name=%s", self.hub is not None, chat_key, name)
            if self.hub is not None:
                await self.hub.publish(
                    chat_key,
                    Event(
                        kind="system",
                        text=i18n.t("agent.forge.pregen_avatar_pending", name=name),
                        data={"level": "info", "spinner": True},
                    ),
                )
            asyncio.get_running_loop().create_task(
                self._publish_after_pregen_avatar(caller_room, chat_key, name, i18n)
            )
        return _module_reply("pregen_avatar", ok, name, {} if ok else {"error": detail})

    async def _publish_after_pregen_avatar(self, caller_room: str, chat_key: str, name: str, i18n: Any) -> None:
        """Poll the room's pregen portrait job until it settles, then retire the pending
        spinner in place, broadcast the outcome, and push a fresh room-state frame (the
        pregen document and any claimed party member now carry the new portrait).
        Bounded: a hung render stops being polled after ~4 minutes, and the portrait
        still lands whenever the worker eventually finishes."""
        from gateway.hub import Event
        from gateway.module_media import load_room_pregen_jobs

        try:
            outcome = "done"
            for _ in range(120):
                await asyncio.sleep(2)
                jobs = await load_room_pregen_jobs(self.services, chat_key)
                job = next((j for j in jobs if j.get("subject") == name), None)
                if job is None or job.get("status") in ("done", "failed"):
                    outcome = str(job.get("status") or outcome) if job else outcome
                    break
            if self.hub is not None:
                from gateway.turn import publish_state

                await self.hub.publish(
                    chat_key,
                    Event(
                        kind="system",
                        text=i18n.t("agent.forge.pregen_avatar_pending", name=name),
                        data={"level": "info", "spinner": False},
                    ),
                )
                await self.hub.publish(
                    chat_key,
                    Event(
                        kind="system",
                        text=i18n.t(
                            "agent.forge.pregen_avatar_failed" if outcome != "done" else "agent.forge.pregen_avatar_done",
                            name=name,
                        ),
                        data={"level": "info"},
                    ),
                )
                await publish_state(self.hub, self.services, AgentCtx(chat_key=chat_key))
        except Exception:  # noqa: BLE001 — state push must never break the admin reply
            logger.exception("pregen avatar state push failed for %s in %s", name, caller_room)


    async def _delete(self, caller_room: str, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        source_kind = str(payload.get("source_kind") or "").strip().casefold()
        # An installed .lwpack content pack is deleted by its pack id (not a module file).
        # A Markdown text source is deleted by filename; only `source_kind == "pack"` routes
        # here. Testing `/` in the name is wrong: text filenames never contain `/`, so that
        # predicate routed every text delete into pack deletion and reported `source_not_found`.
        if source_kind == "pack":
            ok, resolved, error = await delete_installed_pack(
                self.services, name.partition("/")[0], caller_room=caller_room
            )
            return _module_reply("module_delete", ok, resolved, {} if ok else {"error": error})
        name, path = self._path(root, name)
        if not path.is_file():
            return _module_reply("module_delete", False, name, {"error": "source_not_found"})
        current = await self._current_name(caller_room, root, self._files(root))
        if current == name:
            return _module_reply("module_delete", False, name, {"error": "module_in_use"})
        path.unlink()
        return _module_reply("module_delete", True, name, {"name": name})

    @staticmethod
    def _safe_worldbook_name(raw: str) -> str:
        name = raw.strip()
        path = Path(name)
        if not name or path.name != name or name in {".", ".."}:
            raise ValueError("invalid worldbook filename")
        if any(ord(char) < 32 for char in name):
            raise ValueError("invalid worldbook filename")
        if path.suffix.casefold() not in _WORLDBOOK_SUFFIXES:
            raise ValueError("unsupported worldbook filename")
        return name

    @classmethod
    def _worldbook_path(cls, root: Path, raw: str) -> tuple[str, Path]:
        name = cls._safe_worldbook_name(raw)
        target = root / name
        if target.is_symlink():
            raise ValueError("symlink worldbook source")
        if target.resolve().parent != root.resolve():
            raise ValueError("worldbook path escapes source directory")
        return name, target

    @classmethod
    def _worldbook_files(cls, root: Path) -> list[tuple[str, Path]]:
        return [
            (path.name, path)
            for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in _WORLDBOOK_SUFFIXES
        ]

    @staticmethod
    def _worldbook_entries(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict) and "entries" not in data:
            book = data.get("character_book") or (data.get("data") or {}).get("character_book")
            if isinstance(book, dict):
                data = book
        raw_entries = data.get("entries", []) if isinstance(data, dict) else data
        return [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []

    async def _attached_worldbooks(self, chat_key: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for document in await self.services.documents.list(chat_key, LORE_DOC_TYPE):
            if not document.source:
                continue
            grouped.setdefault(document.source, []).append(dict(document.data))
        return grouped

    @staticmethod
    def _entry_views(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "title": str(entry.get("title") or entry.get("comment") or entry.get("name") or "Untitled"),
                "content": str(entry.get("content") or ""),
                "keys": entry.get("keys") or entry.get("key") or [],
                "secret": bool(entry.get("secret", False)),
            }
            for entry in entries
        ]

    async def _worldbook_list(self, caller_room: str, root: Path) -> dict[str, Any]:
        chat_key = chat_key_for_room(caller_room)
        current = await self.services.worldbook.active_source(chat_key)
        # The current MODULE is the `world_import` marker (the card name the keeper imported as
        # the room's running module) — use it as the authoritative "current worldbook" so the
        # keeper UI shows which scenario is live.
        imported_module = str(await self.services.store.state_get(chat_key, "world_import") or "")
        attached = await self._attached_worldbooks(chat_key)
        books_by_name: dict[str, dict[str, Any]] = {}
        for name, path in self._worldbook_files(root):
            stat = path.stat()
            books_by_name[name] = {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "current": bool(current == name or (imported_module and name == imported_module)),
                "attached": name in attached,
                "origin": "room" if name in attached else "library",
                "entry_count": len(attached.get(name, [])),
                "source_kind": "file",
            }
        for name, entries in attached.items():
            if name in books_by_name:
                continue
            books_by_name[name] = {
                "name": name,
                "size": 0,
                "modified": 0,
                "current": bool(current == name or (imported_module and name == imported_module)),
                "attached": True,
                "origin": "room",
                "entry_count": len(entries),
                "source_kind": "attached",
            }
        books = sorted(books_by_name.values(), key=lambda book: str(book["name"]).casefold())
        visible_current = current if current != "__disabled__" else ""
        return _module_reply(
            "worldbook_list",
            True,
            visible_current,
            {"worldbooks": books, "current": visible_current, "enabled": current != "__disabled__"},
        )

    async def _worldbook_detail(self, caller_room: str, root: Path, raw_name: str) -> dict[str, Any]:
        name = raw_name.strip()
        chat_key = chat_key_for_room(caller_room)
        attached = await self._attached_worldbooks(chat_key)
        path: Path | None = None
        try:
            _, candidate = self._worldbook_path(root, name)
            if candidate.is_file():
                path = candidate
        except ValueError:
            path = None
        current = await self.services.worldbook.active_source(chat_key)
        if path is not None:
            content = path.read_text(encoding="utf-8-sig")
            entries = self._worldbook_entries(json.loads(content))
            stat = path.stat()
            source_kind = "file"
            size = stat.st_size
            modified = int(stat.st_mtime * 1000)
        elif name in attached:
            content = ""
            entries = attached[name]
            source_kind = "attached"
            size = 0
            modified = 0
        else:
            return _module_reply("worldbook_detail", False, name, {"error": "source_not_found"})
        return _module_reply(
            "worldbook_detail",
            True,
            name,
            {
                "name": name,
                "size": size,
                "modified": modified,
                "content": content,
                "current": current == name,
                "attached": name in attached,
                "source_kind": source_kind,
                "entry_count": len(entries),
                "entries": self._entry_views(entries),
            },
        )

    async def _worldbook_upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._worldbook_path(root, str(payload.get("name") or ""))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty worldbook content")
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("worldbook source too large")
        json.loads(content)
        path.write_bytes(raw)
        return _module_reply("worldbook_upload", True, name, {"name": name})

    async def _worldbook_select(
        self, caller_room: str, root: Path, payload: dict[str, Any], i18n: Any
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        chat_key = chat_key_for_room(caller_room)
        attached = await self._attached_worldbooks(chat_key)
        source_kind = str(payload.get("source_kind") or "")
        if source_kind == "attached" and name in attached:
            await self.services.worldbook.set_active_source(chat_key, name)
            return _module_reply(
                "worldbook_select",
                True,
                name,
                {"count": len(attached[name]), "current": name, "source_kind": "attached"},
            )
        _, path = self._worldbook_path(root, name)
        if not path.is_file():
            return _module_reply("worldbook_select", False, name, {"error": "source_not_found"})
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        count = await self.services.worldbook.import_entries(chat_key, data, source=name, is_keeper=True)
        if count == 0:
            return _module_reply("worldbook_select", False, name, {"error": "worldbook_empty"})
        await self.services.worldbook.set_active_source(chat_key, name)
        return _module_reply("worldbook_select", True, name, {"count": count, "current": name, "source_kind": "file"})

    async def _worldbook_disable(self, caller_room: str) -> dict[str, Any]:
        await self.services.worldbook.set_active_source(chat_key_for_room(caller_room), "__disabled__")
        return _module_reply("worldbook_disable", True, "", {"enabled": False})



async def delete_installed_pack(
    services: Any,
    pack_id: str,
    *,
    caller_room: str,
) -> tuple[bool, str, str]:
    """Delete an installed .lwpack content pack from the server.

    Returns ``(ok, name, error_code)`` — ``error_code`` is ``""`` on success and one of
    ``source_not_found`` / ``dev_mount`` / ``module_in_use`` otherwise. Removes the
    installed home under ``packs/<id>@<version>/``, the forge build artifacts under
    ``modules/``, strips the pack's admitted skills/panels from every room, removes skill
    directories no remaining installed pack declares, and refreshes skill/rulepack discovery.
    ``pack_id`` is matched against the newest installed home; a dev-mount source tree is never
    deleted.
    """
    from core.pack import DEV_PACK_HOMES
    from gateway.panels import installed_pack_homes

    data_dir = Path(services.settings.data_dir)
    home = installed_pack_homes(data_dir).get(pack_id)
    if home is None:
        return False, pack_id, "source_not_found"
    if home.resolve() in {Path(p).resolve() for p in DEV_PACK_HOMES.values()}:
        return False, pack_id, "dev_mount"
    active = await active_module(services, chat_key_for_room(caller_room))
    if active and active.get("kind") == "world_card" and str(active.get("pack_id") or "") == pack_id:
        return False, pack_id, "module_in_use"
    # The pack's own skills (by id) are removed from every room's enabled list alongside
    # the pack-id entries in `panels_enabled`. Shared skill files are removed separately
    # only when no remaining installed pack declares them.
    skill_ids = _pack_skill_ids(home)
    _remove_pack_artifacts(services, pack_id, home, skill_ids=skill_ids)
    await _strip_pack_from_rooms(services, pack_id, skill_ids=skill_ids)
    return True, pack_id, ""


def _pack_skill_ids(home: Path) -> list[str]:
    """The skill ids a pack ships (its ``contents.skills`` directory names), or ``[]``.

    Mirrors the installer's id derivation (`core.pack` extracts each skill under
    ``skills/<PurePosixPath(skill_dir).name>/``): the LAST path component is the
    skill id, not its parent. Taking ``parent.name`` here returned ``"skills"``
    for every declared skill, so deleting a pack never removed its skills.
    """
    manifest_path = home / core_pack.MANIFEST_NAME
    if not manifest_path.is_file():
        return []
    text = manifest_path.read_text(encoding="utf-8")
    manifest = None
    for expect_trust in (True, False):
        try:
            manifest = core_pack.parse_manifest_text(text, expect_trust=expect_trust)
            break
        except Exception:
            continue
    if manifest is None:
        return []
    from pathlib import PurePosixPath

    ids: list[str] = []
    for path in (manifest.contents or {}).get("skills", []):
        skill_id = PurePosixPath(path).name
        if skill_id and skill_id not in ids:
            ids.append(skill_id)
    return ids


def _remove_pack_artifacts(
    services: Any, pack_id: str, home: Path, *, skill_ids: list[str] | None = None
) -> None:
    """Delete an installed pack and its orphaned skill files, then refresh discovery.

    Skills are extracted into a shared directory, so a skill can only be removed when no
    remaining installed pack declares the same id. A manually copied skill cannot be
    distinguished from a pack-owned file; the pack manifest is the ownership boundary used
    by the installer everywhere else.
    """
    import shutil

    data_dir = Path(services.settings.data_dir)
    shutil.rmtree(home, ignore_errors=True)
    # Forge-generated modules leave a `.lwpack` and a `.pack-src` source tree under
    # `modules/`; a hand-installed pack never has them, and both are non-load-bearing
    # once the pack is installed, so removing them is safe either way.
    for entry in (data_dir / "modules").glob(f"{pack_id}-*.lwpack"):
        entry.unlink(missing_ok=True)
    for entry in (data_dir / "modules").glob(f"{pack_id}.pack-src"):
        shutil.rmtree(entry, ignore_errors=True)
    _remove_orphaned_pack_skills(data_dir, skill_ids or [])
    # A just-removed skill/rulepack must stop being discoverable without a restart.
    try:
        from core import rulepacks as core_rulepacks
        from core import skills as core_skills

        core_skills.reload_skills()
        core_rulepacks.reload_rulepacks()
    except Exception:  # noqa: BLE001 — discovery refresh is best-effort
        logger.exception("pack delete: discovery refresh failed")


def _remove_orphaned_pack_skills(data_dir: Path, skill_ids: list[str]) -> None:
    """Remove deleted-pack skill directories that no remaining installed pack owns."""
    import shutil

    if not skill_ids:
        return
    from gateway.panels import installed_pack_homes

    retained_ids: set[str] = set()
    for remaining_home in installed_pack_homes(data_dir).values():
        retained_ids.update(_pack_skill_ids(remaining_home))

    skills_dir = data_dir / "skills"
    for skill_id in set(skill_ids) - retained_ids:
        skill_home = skills_dir / skill_id
        if skill_home.is_symlink() or not skill_home.is_dir():
            continue
        shutil.rmtree(skill_home, ignore_errors=True)


async def _strip_pack_from_rooms(services: Any, pack_id: str, *, skill_ids: list[str] | None = None) -> None:
    """Remove a deleted pack's references from every room: the pack id from each room's
    `panels_enabled`, and the pack's own skill ids from each room's `skills_enabled`."""
    skill_ids = skill_ids or []
    for chat_key in await services.store.state_rooms():
        for state_key, drop in (
            ("panels_enabled", {pack_id}),
            ("skills_enabled", set(skill_ids)),
        ):
            raw = await services.store.state_get(chat_key, state_key)
            if not raw:
                continue
            try:
                items = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(items, list):
                continue
            filtered = [item for item in items if item not in drop]
            if filtered != items:
                await services.store.state_set(
                    chat_key, state_key, json.dumps(filtered, ensure_ascii=False)
                )

def _module_reply(kind: str, ok: bool, name: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "admin_generated",
        "kind": kind,
        "ok": ok,
        "id": name,
        "name": name,
        "error": "" if ok else str(detail.get("error") or "module_operation_failed"),
        "detail": json.dumps(detail, ensure_ascii=False),
    }
