"""Make titled illustration assets from an imported pack available to `.image`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent.services import Services
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore, is_image_mime

_MEDIA_INDEX_KEY = "module_media_index"
_REFERENCE_KINDS = frozenset(("npcs", "scenes", "items"))
_MEDIA_NAME_RE = re.compile(r"-(npcs|scenes|items)-\d+(?:\.[^/]+)?$")


def _asset_kind(path: str) -> str:
    match = _MEDIA_NAME_RE.search(Path(path).name)
    return match.group(1) if match else ""


async def sync_pack_media_to_room(
    services: Services, chat_key: str, home: Path, manifest: Any
) -> list[dict[str, str]]:
    """Register titled pack illustrations in the room's media store.

    Pack assets live under the installed pack home, while `.image` references are
    deliberately room-scoped. Copying through ``MediaStore.register_blob`` keeps
    the existing reference reader, quota checks, and content-addressed deduplication
    as the single runtime path.
    """
    settings = services.settings.tui
    store = MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=settings.media_max_file_bytes,
        room_quota_bytes=settings.media_room_quota_bytes,
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )
    entries: list[dict[str, str]] = []
    home = Path(home).resolve()
    for asset in getattr(manifest, "assets", ()):
        path = str(getattr(asset, "path", ""))
        title = str(getattr(asset, "title", "") or "").strip()
        mime = str(getattr(asset, "mime", "") or "").strip().lower()
        kind = _asset_kind(path)
        if not path or not title or kind not in _REFERENCE_KINDS or not is_image_mime(mime):
            continue
        asset_path = (home / Path(path)).resolve()
        try:
            asset_path.relative_to(home)
            data = asset_path.read_bytes()
            record = await store.register_blob(
                room=chat_key,
                data=data,
                mime=mime,
                name=asset_path.name,
                uploader="keeper",
            )
        except Exception:
            # A pack import remains usable when one optional illustration is
            # unreadable or exceeds the room's media quota.
            continue
        entries.append(
            {
                "kind": kind,
                "subject": title,
                "hash": record.hash,
                "name": record.name,
                "pack_id": str(getattr(manifest, "id", "") or ""),
            }
        )

    # This is the active module's reference index. Replacing it on world-card
    # import prevents a previous module's same-named NPC from being selected.
    try:
        await services.store.state_set(
            chat_key, _MEDIA_INDEX_KEY, json.dumps(entries, ensure_ascii=False)
        )
    except Exception:
        pass
    return entries
