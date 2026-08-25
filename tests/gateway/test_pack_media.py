import json
from types import SimpleNamespace

from gateway.commands import CommandRouter
from gateway.pack_media import sync_pack_media_to_room
from net.state import _image_names
from tests.gateway.test_image_command import _keeper_ctx, _services


async def test_pack_media_is_registered_and_exposed_as_image_name(tmp_path):
    services = _services(tmp_path)
    chat_key = "tui:group:pack-media"
    home = tmp_path / "packs" / "module-demo@1.0.0"
    asset_path = home / "assets" / "module-demo-npcs-1.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"pack portrait")
    manifest = SimpleNamespace(
        id="module-demo",
        assets=(
            SimpleNamespace(
                path="assets/module-demo-npcs-1.png",
                mime="image/png",
                title="老周",
            ),
        ),
    )

    entries = await sync_pack_media_to_room(services, chat_key, home, manifest)

    assert entries[0]["subject"] == "老周"
    assert entries[0]["kind"] == "npcs"
    raw = await services.store.state_get(chat_key, "module_media_index")
    assert json.loads(raw or "[]")[0]["hash"] == entries[0]["hash"]
    names = await _image_names(services, chat_key)
    assert names == {"npcs": ["老周"]}

    await CommandRouter(services).dispatch(_keeper_ctx(chat_key), ".image portrait 老周")
    assert services.imagegen.calls[-1]["reference"] != "0"
