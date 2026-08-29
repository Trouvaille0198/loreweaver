"""Shared image-generation helpers for commands and KP tools."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from agent.services import Services
from gateway.ops import RateLimiter
from infra.imagegen import ImageGenError
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore, is_image_mime

logger = logging.getLogger(__name__)

_LIMITERS: dict[tuple[int, int], RateLimiter] = {}

# Rooms with an image generation currently in flight, keyed by store identity so
# isolated services (tests, CLI) never block each other.
_INFLIGHT: set[tuple[int, str]] = set()

# Room state keys: every media frame PUBLISHED to the room, and the forge/pack
# illustration provenance (which may name illustrations the keeper never published).
_MEDIA_HISTORY_KEY = "media_history"
_MODULE_MEDIA_INDEX_KEY = "module_media_index"


def allow_imagegen_request(services: Services, chat_key: str) -> bool:
    capacity = int(services.settings.imagegen.per_room_per_hour)
    if capacity <= 0:
        return False
    key = (id(services.store), capacity)
    limiter = _LIMITERS.get(key)
    if limiter is None:
        limiter = RateLimiter(capacity, capacity / 3600.0)
        _LIMITERS[key] = limiter
    return limiter.allow(f"imagegen:{chat_key}")


def refund_imagegen_request(services: Services, chat_key: str) -> None:
    """Return the room's last granted image slot after a FAILED render — a
    provider timeout or transient error is not a quota consumption, and without
    the refund a burst of failures silently burns the hourly budget."""
    capacity = int(services.settings.imagegen.per_room_per_hour)
    if capacity <= 0:
        return
    key = (id(services.store), capacity)
    limiter = _LIMITERS.get(key)
    if limiter is not None:
        limiter.refund(f"imagegen:{chat_key}")


def image_name(kind: str, prompt: str, *, ext: str = ".png") -> str:
    safe_kind = _slug(kind) or "image"
    safe_prompt = _slug(prompt)[:40] or "generated"
    return f"{safe_kind}-{safe_prompt}{ext}"


def reset_imagegen_limiters() -> None:
    _LIMITERS.clear()
    _INFLIGHT.clear()


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value).strip().lower()).strip("-_")


def imagegen_failure_text(i18n: Any, exc: ImageGenError, *, key_prefix: str, chat_key: str) -> str:
    """The localized failure line for `exc.code`, plus the provider's own words when it
    carried any. A relay that wraps its outage in a bare HTTP 200 answers with a body the
    parsers can only call invalid — the code alone ("无效响应") is then undiagnosable, so
    the detail rides along on the room message (truncated like the media job queue) and
    lands in the server log. `key_prefix` is the caller's error-string family
    (`commands.avatar.error` / `kp_tools.image.generate.error`)."""
    message = i18n.t(f"{key_prefix}.{exc.code}")
    detail = str(exc).strip()
    if not detail or detail == exc.code:
        return message
    logger.warning("[imagegen] %s failed in %s: %s", exc.code, chat_key, detail)
    return i18n.t("commands.imagegen.error_detail", message=message, detail=detail[:300])


async def _trace_imagegen(services: Services, chat_key: str, payload: dict[str, Any]) -> None:
    """Write one `tool: "imagegen"` row into the room's trace, stamped with the active
    module like every other row. Best-effort: the trace must never cost the call."""
    try:
        from agent.tool_trace import active_module_id, trace_event

        module = await active_module_id(services, chat_key)
        trace_event("imagegen", payload, chat_key=chat_key, module=module)
    except Exception:  # noqa: BLE001 — see module docstring of agent.tool_trace
        logger.debug("imagegen trace write failed", exc_info=True)


async def _imagegen_profile(services: Services, chat_key: str) -> str:
    """The room's selected image profile id ("" when the global generator is in play) —
    the first question any image failure begs is WHICH config was used."""
    try:
        selection = await services.room_model_selection(chat_key)
        return str(selection.get("imagegen") or "")
    except Exception:  # noqa: BLE001 — best-effort, like the row itself
        return ""


async def generate_traced(
    services: Services,
    chat_key: str,
    imagegen: Any,
    prompt: str,
    *,
    size: str,
    reference: bytes | None = None,
    reference_mime: str = "",
) -> tuple[bytes, str]:
    """One imagegen call whose outcome lands in the room's tool trace as `tool: "imagegen"`.

    The provider HTTP call is neither a model call nor an AI tool call, so before this row
    a failed generation left the JSONL blind between the prompt-expansion row and the
    room's error line — the 2026-08-29 kuaipao outage read as unexplained silence, and the
    relay-side console showed nothing either (an invalid token dies at the gateway, before
    any channel). The detail rides in the row because the room message already carries it;
    these strings are status + provider message, never the credential itself.
    `reference_bytes` tells prompt-only (0) from image-to-image (>0), the other half of
    the endpoint question."""
    started = time.monotonic()
    common: dict[str, Any] = {
        "profile": await _imagegen_profile(services, chat_key),
        "size": size,
        "reference_bytes": len(reference or b""),
        "prompt": str(prompt)[:400],
    }
    try:
        data, mime = await imagegen.generate(
            prompt, size=size, reference=reference, reference_mime=reference_mime
        )
    except ImageGenError as exc:
        await _trace_imagegen(
            services,
            chat_key,
            {**common, "outcome": "error", "ms": round((time.monotonic() - started) * 1000, 1),
             "code": exc.code, "detail": str(exc)[:300]},
        )
        raise
    except Exception as exc:  # noqa: BLE001 — trace, then re-raise unchanged
        await _trace_imagegen(
            services,
            chat_key,
            {**common, "outcome": "error", "ms": round((time.monotonic() - started) * 1000, 1),
             "code": type(exc).__name__, "detail": str(exc)[:300]},
        )
        raise
    await _trace_imagegen(
        services,
        chat_key,
        {**common, "outcome": "ok", "ms": round((time.monotonic() - started) * 1000, 1),
         "mime": mime, "bytes": len(data)},
    )
    return data, mime

def acquire_imagegen_slot(services: Services, chat_key: str) -> bool:
    """Take the room's one-image-in-flight slot; False when a generation is running.

    Async lanes must not rely on the caller's discipline for the "one image per
    scene" rule: while a background generation is underway, new requests are
    refused until ``release_imagegen_slot`` runs."""
    key = (id(services.store), chat_key)
    if key in _INFLIGHT:
        return False
    _INFLIGHT.add(key)
    return True


def release_imagegen_slot(services: Services, chat_key: str) -> None:
    """Return the room's in-flight slot once a background generation settles."""
    _INFLIGHT.discard((id(services.store), chat_key))


async def active_module_pack_id(services: Services, chat_key: str) -> str:
    """The room's active module pack id ("" when none), so reference images are
    scoped to the CURRENT module. `module_media_index` is append-only across module
    switches, so without this filter an old module's art would anchor (and slow down,
    via I2I) the new story's image requests."""
    try:
        raw = await services.store.state_get(chat_key, "active_module")
        if raw:
            value = json.loads(raw)
            if isinstance(value, dict):
                return str(value.get("pack_id") or "").strip()
    except Exception:
        pass
    return ""


async def gather_image_reference(
    services: Services,
    chat_key: str,
    kind: str,
    imagegen: Any,
    *,
    focus: str = "",
    extra: str = "",
    published_only: bool = False,
) -> tuple[bytes | None, str]:
    """Find a module illustration of this subject to reuse as the generation reference.

    Only kinds the provider can anchor (``imagegen.reference_kinds``) get a reference:
    MiniMax supports only `character` (portrait), while image-to-image providers also
    anchor scene/clue illustrations. A `portrait`/`clue` request matches its subject
    against the focused name (a Keeper's `.image portrait 老周` / `.image clue 玉蟾`,
    or the AI lane's `reference_subject`); a `scene` with no focus reuses the most
    recent scene illustration. `module_media_index` (written by the forge media pass)
    maps each illustration to the scene/NPC/item it depicts. Without an index entry
    we fall back to the room's media names by kind prefix.

    With ``published_only`` (the AI lane, iron rule #3), candidates are further
    restricted to illustrations whose hash appears in the room's media history —
    i.e. images the players have ALREADY been shown. The index and the media store
    also hold keeper-only illustrations that were never published; those must never
    reach an AI prompt or an external provider on the AI's own initiative.

    Always best-effort: ``(None, "")`` means prompt-only, never an error."""
    if kind not in getattr(imagegen, "reference_kinds", frozenset({"portrait"})):
        return None, ""
    focus = focus.strip() or extra.strip()
    pack_id = await active_module_pack_id(services, chat_key)
    published: set[str] | None = None
    if published_only:
        published = await _published_media_hashes(services, chat_key)
    try:
        raw = await services.store.state_get(chat_key, _MODULE_MEDIA_INDEX_KEY)
        entries: list[dict] = []
        if raw:
            value = json.loads(raw)
            if isinstance(value, list):
                entries = [e for e in value if isinstance(e, dict)]
        pool = []
        for e in entries:
            kind_key = {"portrait": "npcs", "scene": "scenes", "clue": "items"}.get(kind)
            if str(e.get("kind") or "") != kind_key:
                continue
            # The index accumulates every forge module this room ever ran (entries are
            # append-only, never purged on module switch). Only the CURRENT module's
            # illustrations may anchor a reference — an old module's scene art would
            # otherwise leak into the new story (and drag an I2I pass along).
            if pack_id and not str(e.get("name") or "").startswith(f"module-{pack_id}-"):
                continue
            if published is not None and str(e.get("hash") or "") not in published:
                continue
            name = str(e.get("subject") or "").strip()
            if focus:
                if focus in name or name in focus:
                    pool.append(e)
            elif kind == "scene":
                pool.append(e)
        if pool:
            return await _read_reference_bytes(services, chat_key, str(pool[-1].get("hash") or ""))
    except Exception:
        pass
    # Fallback: match room media names by kind prefix (old forge illustrations that
    # predate the index). Scene reuses the latest matching image; portrait/clue need
    # a focused name to avoid guessing the wrong subject.
    if not focus and kind != "scene":
        return None, ""
    kind_key = {"portrait": "npcs", "scene": "scenes", "clue": "items"}.get(kind)
    try:
        store = _image_store(services)
        records = await store.list_room_records(chat_key)
        matches = [
            r for r in records
            if r.name.startswith("module-") and f"-{kind_key}-" in r.name and is_image_mime(r.mime)
            and (not pack_id or r.name.startswith(f"module-{pack_id}-"))
            and (published is None or r.hash in published)
        ]
        if not matches:
            return None, ""
        target = matches[-1]
        rec, data = await store.read_bytes(chat_key, target.hash)
        return data, rec.mime
    except Exception:
        return None, ""


async def _published_media_hashes(services: Services, chat_key: str) -> set[str]:
    """Hashes of every media frame already published to the room (`media_history`).

    Anything in this list was broadcast to the players, so it is safe as an AI-lane
    reference. The history is capped for replay, so a very old publication may fall
    off — a conservative miss (prompt-only generation), never a leak."""
    try:
        raw = await services.store.state_get(chat_key, _MEDIA_HISTORY_KEY)
        history = json.loads(raw) if raw else []
    except Exception:
        return set()
    if not isinstance(history, list):
        return set()
    return {str(frame.get("hash")) for frame in history if isinstance(frame, dict) and frame.get("hash")}


def _image_store(services: Services) -> MediaStore:
    settings = services.settings.tui
    return MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=settings.media_max_file_bytes,
        room_quota_bytes=settings.media_room_quota_bytes,
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )


async def _read_reference_bytes(services: Services, chat_key: str, hash_value: str) -> tuple[bytes | None, str]:
    """Read a stored module illustration's bytes from the media store."""
    try:
        store = _image_store(services)
        rec, data = await store.read_bytes(chat_key, hash_value)
        return data, rec.mime
    except Exception:
        return None, ""
