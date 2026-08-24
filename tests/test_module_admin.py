import json
from types import SimpleNamespace

import pytest

from core.documents import DocumentStore
from core.worldbook import Worldbook
from infra.store import Store
from module_admin import ModuleAdminService
from net.room_backup import chat_key_for_room


@pytest.mark.asyncio
async def test_room_attached_worldbook_is_listed_and_selectable(tmp_path):
    store = Store(":memory:")
    worldbook = Worldbook(store)
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=worldbook,
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))
    room = "room-with-card"
    chat_key = chat_key_for_room(room)
    await worldbook.import_entries(
        chat_key,
        {"entries": [{"title": "Marsh", "content": "The fog is cold.", "keys": ["marsh"]}]},
        source="marsh-card.png",
        is_keeper=True,
    )

    root = tmp_path / "worldbooks"
    root.mkdir()
    listed = json.loads((await admin._worldbook_list(room, root))["detail"])
    assert listed["worldbooks"] == [
        {
            "name": "marsh-card.png",
            "size": 0,
            "modified": 0,
            "current": False,
            "attached": True,
            "origin": "room",
            "entry_count": 1,
            "source_kind": "attached",
        }
    ]

    detail = json.loads((await admin._worldbook_detail(room, root, "marsh-card.png"))["detail"])
    assert detail["source_kind"] == "attached"
    assert detail["entries"][0]["title"] == "Marsh"

    selected = await admin._worldbook_select(room, root, {"name": "marsh-card.png", "source_kind": "attached"}, None)
    assert selected["ok"] is True
    assert await worldbook.active_source(chat_key) == "marsh-card.png"


@pytest.mark.asyncio
async def test_module_detail_includes_only_this_modules_forge_media(tmp_path):
    """The detail reply for the room's CURRENT module lists the forge-generated illustrations
    (`module-<id>-` provenance prefix) from the room's media deck -- and nothing else. A
    non-current source gets no media list at all, same as it gets no pool."""
    from infra.media_store import ALLOWED_MEDIA_MIMES, MediaStore

    store = Store(":memory:")
    tui = SimpleNamespace(
        media_max_file_bytes=8 * 1024 * 1024,
        audio_max_file_bytes=16 * 1024 * 1024,
        media_room_quota_bytes=64 * 1024 * 1024,
        audio_room_quota_bytes=64 * 1024 * 1024,
    )
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path, tui=tui),
        store=store,
        documents=DocumentStore(store),
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))
    room = "arkham"
    chat_key = chat_key_for_room(room)

    root = tmp_path / "modules"
    root.mkdir()
    (root / "marsh-case.md").write_text("# Marsh Case\n", encoding="utf-8")
    await store.state_set(chat_key, "module_source", "marsh-case.md")

    media = MediaStore(
        store,
        tmp_path,
        max_file_bytes=tui.media_max_file_bytes,
        room_quota_bytes=tui.media_room_quota_bytes,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    await media.register_blob(room=chat_key, data=b"\x89PNG-cover", mime="image/png", name="module-marsh-case-cover-1.png", uploader="keeper")
    await media.register_blob(room=chat_key, data=b"\x89PNG-scene", mime="image/png", name="module-marsh-case-scenes-2.png", uploader="keeper")
    await media.register_blob(room=chat_key, data=b"\x89PNG-other", mime="image/png", name="module-other-module-cover-1.png", uploader="keeper")
    await media.register_blob(room=chat_key, data=b"\x89PNG-hand", mime="image/png", name="scene-handout.png", uploader="keeper")

    detail = json.loads((await admin._detail(room, root, "marsh-case.md"))["detail"])
    names = [record["name"] for record in detail["media"]]
    assert names == ["module-marsh-case-cover-1.png", "module-marsh-case-scenes-2.png"]
    assert all(record["hash"] and record["mime"] == "image/png" and record["size"] > 0 for record in detail["media"])

    (root / "other.md").write_text("# Other\n", encoding="utf-8")
    other = json.loads((await admin._detail(room, root, "other.md"))["detail"])
    assert other["current"] is False
    assert other["media"] == []


@pytest.mark.asyncio
async def test_import_pack_routes_to_world_card_import(tmp_path, monkeypatch):
    """`module_import` of an installed .lwpack pack id routes to the keeper WORLD-CARD import
    path (`CharcardTools.import_world_card`) — a binary bundle, not a Markdown scenario — and
    a pack with no world card degrades to a clean miss instead of a malformed request."""
    store = Store(":memory:")
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=Worldbook(store),
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))

    # A fake installed pack home carrying one world card.
    home = tmp_path / "packs" / "fog@1.0.0"
    (home / "cards").mkdir(parents=True)
    (home / "cards" / "fog.lorecard.json").write_text(
        json.dumps({"format": "loreweaver.card", "format_version": 1, "name": "Fog Manor", "opening": "It begins."}),
        encoding="utf-8",
    )
    fake_homes = {"fog": home}
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: fake_homes)

    calls: list[tuple[str, str]] = []

    class _FakeCharcardTools:
        def __init__(self, services) -> None:
            self._services = services

        async def import_world_card(self, ctx, file_path, **kw):
            calls.append((ctx.chat_key, file_path))
            return "world card imported"

    monkeypatch.setattr("agent.kp_tools_charcard.CharcardTools", _FakeCharcardTools)

    room = "fog-room"
    i18n = SimpleNamespace(locale="en")
    reply = await admin._import(room, tmp_path / "modules", {"name": "fog"}, i18n)
    assert reply["ok"] is True
    assert reply["kind"] == "module_import"
    assert calls == [(chat_key_for_room(room), str(home / "cards" / "fog.lorecard.json"))]

    # A pack with no world card degrades cleanly.
    bare = tmp_path / "packs" / "empty@1.0.0"
    bare.mkdir(parents=True)
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {"empty": bare})
    reply = await admin._import(room, tmp_path / "modules", {"name": "empty"}, i18n)
    assert reply["ok"] is False
    assert json.loads(reply["detail"])["error"] == "no_world_card"
