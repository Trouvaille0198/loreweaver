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
