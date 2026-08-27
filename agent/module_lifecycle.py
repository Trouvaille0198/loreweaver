"""One-room/one-module lifecycle contract.

Every module entry point records the same stable provenance and performs its
room mutations inside :class:`ModuleImportTransaction`.  A failed import
restores the module-owned document/state/vector families; a successful import
publishes exactly one ``active_module`` record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet

ACTIVE_MODULE_KEY = "active_module"
ACTIVE_MODULE_SCHEMA = 1

MODULE_DOCUMENT_TYPES = frozenset(
    {"lore", "media", "module_pool", "module_brief", "modvars", "mvu_tree", "pregen", "npc"}
)
MODULE_STATE_KEYS = frozenset(
    {
        ACTIVE_MODULE_KEY,
        "world_import",
        "room_system",
        "worldbook_active_source",
        "worldbook_timers",
        "room_hooks",
        "module_fulltext",
        "module_source",
        "module_init_status",
        "module_init_error",
        "module_import_status",
        "module_import_name",
        "skills_enabled",
        "panels_enabled",
        "preset_enabled",
    }
)

_IMPORT_LOCKS: dict[str, asyncio.Lock] = {}


def _import_lock(chat_key: str) -> asyncio.Lock:
    lock = _IMPORT_LOCKS.get(chat_key)
    if lock is None:
        lock = asyncio.Lock()
        _IMPORT_LOCKS[chat_key] = lock
    return lock


def _path_id(prefix: str, path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def identity_for_text(path: Path, *, name: str | None = None) -> dict[str, Any]:
    """Stable identity for a text module: its confined source path, not its title."""
    return {
        "schema": ACTIVE_MODULE_SCHEMA,
        "kind": "text",
        "source_id": _path_id("text", path),
        "name": str(name or path.name),
        "source": str(name or path.name),
        "lore_sources": [],
        "enabled_skills": [],
        "enabled_panel_packs": [],
    }


def identity_for_world_card(data_dir: Path, path: Path, *, display_name: str = "") -> dict[str, Any]:
    """Stable identity for a world card, including pack provenance when available."""
    from core.pack import DEV_PACK_HOMES, MANIFEST_NAME, pack_home_of, parse_manifest_text

    home = pack_home_of(data_dir, path)
    pack_id = ""
    pack_version = ""
    relative = ""
    if home is not None:
        relative = path.resolve().relative_to(home.resolve()).as_posix()
        manifest_path = home / MANIFEST_NAME
        try:
            manifest = parse_manifest_text(
                manifest_path.read_text(encoding="utf-8"),
                expect_trust=home not in DEV_PACK_HOMES.values(),
            )
            pack_id = manifest.id
            pack_version = manifest.version
        except Exception:
            pack_id = ""
    source_id = (
        f"pack:{pack_id}@{pack_version}:{relative}"
        if pack_id
        else _path_id("world-card", path)
    )
    return {
        "schema": ACTIVE_MODULE_SCHEMA,
        "kind": "world_card",
        "source_id": source_id,
        "name": display_name or path.stem,
        "source": relative or str(path.resolve()),
        "pack_id": pack_id,
        "pack_version": pack_version,
        "card_path": relative,
        "lore_sources": [source_id],
        "enabled_skills": [],
        "enabled_panel_packs": [],
    }


async def active_module(services: Any, chat_key: str) -> dict[str, Any] | None:
    raw = await services.store.state_get(chat_key, ACTIVE_MODULE_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not str(value.get("source_id") or ""):
        return None
    return value


async def publish_active_module(services: Any, chat_key: str, identity: dict[str, Any]) -> None:
    payload = dict(identity)
    payload["schema"] = ACTIVE_MODULE_SCHEMA
    await services.store.state_set(chat_key, ACTIVE_MODULE_KEY, json.dumps(payload, ensure_ascii=False))


def _json_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _module_vector(point: dict[str, Any], chat_key: str) -> bool:
    payload = point.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("chat_key") == chat_key and payload.get("document_type") in {"module", "story"}:
        return True
    return payload.get("collection") == "worldbook" and payload.get("namespace") == chat_key


class ModuleImportTransaction:
    """Rollback guard for all module-owned persistence families.

    The transport's room turn lock prevents gameplay from observing the staging
    writes.  This additional lock serializes non-transport callers such as the
    web admin and restores only explicitly owned rows on failure.
    """

    def __init__(self, services: Any, chat_key: str) -> None:
        self.services = services
        self.chat_key = chat_key
        self._lock = _import_lock(chat_key)
        self._documents: list[dict[str, Any]] = []
        self._state: list[dict[str, Any]] = []
        self._vectors: list[dict[str, Any]] = []

    async def __aenter__(self) -> ModuleImportTransaction:
        await self._lock.acquire()
        try:
            rows = await self.services.store.doc_list(self.chat_key)
            self._documents = [row for row in rows if row.get("type") in MODULE_DOCUMENT_TYPES]
            state = await self.services.store.state_list(self.chat_key)
            self._state = [row for row in state if row.get("key") in MODULE_STATE_KEYS]
            vector_store = getattr(getattr(self.services, "vector_db", None), "vector_store", None)
            if vector_store is not None:
                self._vectors = [
                    point for point in await vector_store.dump() if _module_vector(point, self.chat_key)
                ]
            await self.services.store.state_set(self.chat_key, "module_import_status", "processing")
            return self
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        try:
            if exc is not None:
                await self.services.store.replace_room_subset(
                    self.chat_key,
                    document_types=MODULE_DOCUMENT_TYPES,
                    state_keys=MODULE_STATE_KEYS,
                    documents=self._documents,
                    state=self._state,
                )
                vector_store = getattr(getattr(self.services, "vector_db", None), "vector_store", None)
                if vector_store is not None:
                    await vector_store.replace_filtered(
                        filters=[
                            {"chat_key": self.chat_key, "document_type": "module"},
                            {"chat_key": self.chat_key, "document_type": "story"},
                            {"collection": "worldbook", "namespace": self.chat_key},
                        ],
                        points=[
                            (str(point["id"]), list(point["vector"]), dict(point["payload"]))
                            for point in self._vectors
                        ],
                    )
            else:
                await self.services.store.state_delete(self.chat_key, "module_import_status")
                await self.services.store.state_delete(self.chat_key, "module_import_name")
        finally:
            self._lock.release()
        return False


async def purge_active_module(services: Any, chat_key: str) -> dict[str, Any] | None:
    """Remove the current module and only the switches it admitted to this room."""
    previous = await active_module(services, chat_key)
    legacy_name = str(await services.store.state_get(chat_key, "world_import") or "")
    lore_sources = list(previous.get("lore_sources") or []) if previous else []
    if legacy_name and legacy_name not in lore_sources:
        lore_sources.append(legacy_name)
    for source in lore_sources:
        await services.worldbook.remove_by_source(chat_key, str(source))

    for doc_type in ("module_pool", "module_brief", "modvars", "mvu_tree"):
        await services.documents.delete_type(chat_key, doc_type)
    # Room-born roster characters (`source="room"`, created by `.pc gen`) are this
    # table's own asset: a module swap must not strand them. Only module-imported
    # pregens (documents whose source column names the module) leave with it.
    for doc in await services.documents.list(chat_key, "pregen"):
        if str(doc.source or "") != "room":
            await services.documents.delete(chat_key, "pregen", doc.id)
    if previous:
        source_id = str(previous.get("source_id") or "")
        for document in await services.documents.list(chat_key, "npc"):
            if source_id and document.source == source_id:
                await services.documents.delete(chat_key, "npc", document.id)

    vector_store = getattr(getattr(services, "vector_db", None), "vector_store", None)
    if vector_store is not None:
        for doc_type in ("module", "story"):
            await vector_store.delete_by_filter(filter={"chat_key": chat_key, "document_type": doc_type})

    old_skills = set(previous.get("enabled_skills") or []) if previous else set()
    enabled_skills = _json_ids(await services.store.state_get(chat_key, "skills_enabled"))
    await services.store.state_set(
        chat_key,
        "skills_enabled",
        json.dumps([item for item in enabled_skills if item not in old_skills], ensure_ascii=False),
    )
    old_panels = set(previous.get("enabled_panel_packs") or []) if previous else set()
    enabled_panels = _json_ids(await services.store.state_get(chat_key, "panels_enabled"))
    await services.store.state_set(
        chat_key,
        "panels_enabled",
        json.dumps([item for item in enabled_panels if item not in old_panels], ensure_ascii=False),
    )

    for key in (
        ACTIVE_MODULE_KEY,
        "world_import",
        "room_system",
        "worldbook_active_source",
        "worldbook_timers",
        "room_hooks",
        "module_fulltext",
        "module_source",
        "module_init_status",
        "module_init_error",
    ):
        await services.store.state_delete(chat_key, key)
    return previous


ROOM_FACETS = (
    RoomStateFacet(
        name="active_module",
        owner="agent.module_lifecycle",
        reset_scope="all",
        state_keys=frozenset({ACTIVE_MODULE_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
