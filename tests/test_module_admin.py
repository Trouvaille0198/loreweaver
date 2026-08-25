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
    await store.state_set(
        chat_key,
        "module_media_index",
        json.dumps(
            [
                {"kind": "cover", "subject": "Marsh manor", "name": "module-marsh-case-cover-1.png"},
                {"kind": "scenes", "subject": "The ferry crossing", "name": "module-marsh-case-scenes-2.png"},
            ]
        ),
    )

    detail = json.loads((await admin._detail(room, root, "marsh-case.md"))["detail"])
    names = [record["name"] for record in detail["media"]]
    assert names == ["module-marsh-case-cover-1.png", "module-marsh-case-scenes-2.png"]
    assert [record["subject"] for record in detail["media"]] == ["Marsh manor", "The ferry crossing"]
    assert all(record["hash"] and record["mime"] == "image/png" and record["size"] > 0 for record in detail["media"])

    (root / "other.md").write_text("# Other\n", encoding="utf-8")
    other = json.loads((await admin._detail(room, root, "other.md"))["detail"])
    assert other["current"] is False
    assert other["media"] == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_module_list_includes_in_flight_generation(tmp_path):
    """A generation running in this room shows as a placeholder source in the module library (the
    in-flight stage is persisted by `net.admin._progress`; `module_admin._list` merges it), so a
    keeper sees the running forge — and still sees it after a refresh — instead of nothing."""
    store = Store(":memory:")
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=Worldbook(store),
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))
    room = "gen-room"
    chat_key = chat_key_for_room(room)
    root = tmp_path / "modules"
    root.mkdir()

    empty = json.loads((await admin._list(room, root))["detail"])
    assert all(not m.get("generating") for m in empty["modules"])

    # What `net.admin._progress` persists during a forge.
    await store.state_set(
        chat_key,
        "generation_progress",
        json.dumps({"kind": "pack", "stage": "media", "detail": "rendering cover"}),
    )
    listed = json.loads((await admin._list(room, root))["detail"])
    first = listed["modules"][0]
    assert first["generating"] is True
    assert first["stage"] == "media"
    assert first["detail"] == "rendering cover"
    assert first["source_kind"] == "generating"


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
        json.dumps(
            {
                "format": "loreweaver.card",
                "format_version": 1,
                "name": "Fog Manor",
                "opening": "It begins.",
                "worldbook": [{"title": "[InitVar]", "content": '{"fog": 1}'}],
            }
        ),
        encoding="utf-8",
    )
    card_size = (home / "cards" / "fog.lorecard.json").stat().st_size
    (home / "pack.yaml").write_text(
        """manifest_version: 2
id: fog
version: 1.0.0
name: Fog
description: Fog fixture
authors: [tester]
license: MIT
contents:
  cards:
    - path: cards/fog.lorecard.json
      kind: world
trust:
  cards: 1
  world_cards: 1
files:
  - path: cards/fog.lorecard.json
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    size: """
        + str(card_size)
        + "\n",
        encoding="utf-8",
    )
    fake_homes = {"fog": home}
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: fake_homes)

    calls: list[tuple[str, str, bool]] = []

    class _FakeCharcardTools:
        def __init__(self, services) -> None:
            self._services = services

        async def import_world_card(self, ctx, file_path, **kw):
            calls.append((ctx.chat_key, file_path, bool(kw.get("raise_on_failure"))))
            return "world card imported"

    monkeypatch.setattr("agent.kp_tools_charcard.CharcardTools", _FakeCharcardTools)

    room = "fog-room"
    i18n = SimpleNamespace(locale="en")
    reply = await admin._import(room, tmp_path / "modules", {"name": "fog"}, i18n)
    assert reply["ok"] is True
    assert reply["kind"] == "module_import"
    assert calls == [
        (chat_key_for_room(room), str(home / "cards" / "fog.lorecard.json"), True)
    ]

    # A pack with no world card degrades cleanly.
    bare = tmp_path / "packs" / "empty@1.0.0"
    bare.mkdir(parents=True)
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {"empty": bare})
    reply = await admin._import(room, tmp_path / "modules", {"name": "empty"}, i18n)
    assert reply["ok"] is False
    assert json.loads(reply["detail"])["error"] == "no_world_card"


def _pack_services(tmp_path):
    """A `services`-shaped namespace backed by a real store + tmp data dir, plus a fake
    installed pack home, matching `test_import_pack_routes_to_world_card_import`."""
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
    (home / "skills" / "fog-skill").mkdir(parents=True)
    (home / "skills" / "fog-skill" / "SKILL.md").write_text("---\nname: Fog Skill\n---\nBody\n", encoding="utf-8")
    (home / "cards" / "fog.lorecard.json").write_text(
        json.dumps(
            {
                "format": "loreweaver.card",
                "format_version": 1,
                "name": "Fog Manor",
                "opening": "It begins.",
            }
        ),
        encoding="utf-8",
    )
    card_size = (home / "cards" / "fog.lorecard.json").stat().st_size
    skill_size = (home / "skills" / "fog-skill" / "SKILL.md").stat().st_size
    (home / "pack.yaml").write_text(
        """manifest_version: 2
id: fog
version: 1.0.0
name: Fog
description: Fog fixture
authors: [tester]
license: MIT
contents:
  cards:
    - path: cards/fog.lorecard.json
      kind: world
  skills:
    - skills/fog-skill
trust:
  cards: 1
  world_cards: 1
  skills: 1
files:
  - path: cards/fog.lorecard.json
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    size: """
        + str(card_size)
        + """
  - path: skills/fog-skill/SKILL.md
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    size: """
        + str(skill_size)
        + "\n",
        encoding="utf-8",
    )
    return services, admin, home


@pytest.mark.asyncio
async def test_delete_pack_module_removes_home_artifacts_and_room_refs(tmp_path, monkeypatch):
    """`module_delete` removes the pack's orphaned KP skills as well as its home and refs."""
    services, admin, home = _pack_services(tmp_path)
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {home.name.split("@")[0]: home})
    skill_home = tmp_path / "skills" / "fog-skill"
    skill_home.mkdir(parents=True)
    (skill_home / "SKILL.md").write_text("---\nname: Fog Skill\n---\nBody\n", encoding="utf-8")
    # Forge build artifacts + a room that admitted this pack.
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "fog-0.1.0.lwpack").write_bytes(b"lwpack")
    (tmp_path / "modules" / "fog.pack-src").mkdir()
    room_key = chat_key_for_room("fog-room")
    await services.store.state_set(room_key, "skills_enabled", '["fog-skill","keep-me"]')
    await services.store.state_set(room_key, "panels_enabled", '["fog","other-pack"]')

    reply = await admin._delete("fog-room", tmp_path / "modules", {"name": "fog", "source_kind": "pack"})

    assert reply["ok"] is True
    assert not home.exists(), "installed pack home removed"
    assert not (tmp_path / "modules" / "fog-0.1.0.lwpack").exists(), "forge lwpack removed"
    assert not (tmp_path / "modules" / "fog.pack-src").exists(), "forge source tree removed"
    assert not skill_home.exists(), "orphaned pack skill removed"
    assert json.loads(await services.store.state_get(room_key, "skills_enabled")) == ["keep-me"]
    assert json.loads(await services.store.state_get(room_key, "panels_enabled")) == ["other-pack"]


@pytest.mark.asyncio
async def test_delete_pack_module_keeps_skill_declared_by_another_pack(tmp_path, monkeypatch):
    """A skill shared by two installed packs survives deletion of either pack."""
    services, admin, home = _pack_services(tmp_path)
    other_home = tmp_path / "packs" / "other@1.0.0"
    other_home.mkdir(parents=True)
    (other_home / "pack.yaml").write_text(
        (home / "pack.yaml").read_text(encoding="utf-8").replace("id: fog", "id: other"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gateway.panels.installed_pack_homes",
        lambda data_dir: {"fog": home, "other": other_home},
    )
    skill_home = tmp_path / "skills" / "fog-skill"
    skill_home.mkdir(parents=True)
    (skill_home / "SKILL.md").write_text("---\nname: Fog Skill\n---\nBody\n", encoding="utf-8")

    reply = await admin._delete("fog-room", tmp_path / "modules", {"name": "fog", "source_kind": "pack"})

    assert reply["ok"] is True
    assert not home.exists()
    assert skill_home.is_dir(), "skill retained while another pack declares it"


@pytest.mark.asyncio
async def test_delete_pack_module_removes_single_segment_skill_dirs(tmp_path, monkeypatch):
    """A pack whose manifest lists skills as `skills/<id>` (single segment — the
    forge's shape) must still have those skills removed on delete.

    Regression: `_pack_skill_ids` derived ids from the path's PARENT, so the
    single-segment form yielded `"skills"` for every declared skill and deletion
    never removed them. The installer derives ids from the LAST component.
    """
    services, admin, home = _pack_services(tmp_path)
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {home.name.split("@")[0]: home})
    skill_home = tmp_path / "skills" / "fog-skill"
    skill_home.mkdir(parents=True)
    (skill_home / "SKILL.md").write_text("---\nname: Fog Skill\n---\nBody\n", encoding="utf-8")

    reply = await admin._delete("fog-room", tmp_path / "modules", {"name": "fog", "source_kind": "pack"})

    assert reply["ok"] is True
    assert not home.exists()
    assert not skill_home.exists(), "single-segment skill removed with its pack"


@pytest.mark.asyncio
async def test_delete_pack_module_refuses_current_module(tmp_path, monkeypatch):
    """A pack that is the room's current module is refused (`module_in_use`), like a text
    source in use — deleting the running module would strand the table."""
    from agent.module_lifecycle import publish_active_module

    services, admin, home = _pack_services(tmp_path)
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {home.name.split("@")[0]: home})
    room_key = chat_key_for_room("fog-room")
    await publish_active_module(
        services,
        room_key,
        {
            "kind": "world_card",
            "source_id": "pack:fog@1.0.0:cards/fog.lorecard.json",
            "name": "Fog Manor",
            "source": "cards/fog.lorecard.json",
            "pack_id": "fog",
            "pack_version": "1.0.0",
            "card_path": "cards/fog.lorecard.json",
        },
    )

    reply = await admin._delete("fog-room", tmp_path / "modules", {"name": "fog", "source_kind": "pack"})

    assert reply["ok"] is False
    assert json.loads(reply["detail"])["error"] == "module_in_use"
    assert home.exists(), "in-use pack home untouched"


@pytest.mark.asyncio
async def test_delete_text_module_unlinks_file(tmp_path):
    """`module_delete` on a Markdown text source removes the file (not the pack path).

    Regression: the `or "/" not in name` predicate routed every text filename into
    `delete_installed_pack`, which reported `source_not_found` and left the file behind.
    """
    store = Store(":memory:")
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=Worldbook(store),
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))
    root = tmp_path / "modules"
    root.mkdir()
    source = root / "guangyuan-waterline.md"
    source.write_text("# Waterline\n\nIt begins.", encoding="utf-8")

    reply = await admin._delete("fog-room", root, {"name": "guangyuan-waterline.md", "source_kind": "text"})

    assert reply["ok"] is True
    assert json.loads(reply["detail"])["name"] == "guangyuan-waterline.md"
    assert not source.exists(), "text source file removed"


@pytest.mark.asyncio
async def test_delete_text_module_refuses_current_module(tmp_path):
    """A text source that is the room's current module is refused (`module_in_use`)."""
    from agent.module_lifecycle import publish_active_module

    store = Store(":memory:")
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=Worldbook(store),
    )
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=None))
    root = tmp_path / "modules"
    root.mkdir()
    source = root / "guangyuan-waterline.md"
    source.write_text("# Waterline\n\nIt begins.", encoding="utf-8")
    room_key = chat_key_for_room("fog-room")
    await publish_active_module(
        services,
        room_key,
        {
            "kind": "text",
            "source_id": "text:waterline",
            "name": "guangyuan-waterline.md",
            "source": "guangyuan-waterline.md",
            "lore_sources": [],
            "enabled_skills": [],
            "enabled_panel_packs": [],
        },
    )

    reply = await admin._delete("fog-room", root, {"name": "guangyuan-waterline.md", "source_kind": "text"})

    assert reply["ok"] is False
    assert json.loads(reply["detail"])["error"] == "module_in_use"
    assert source.exists(), "in-use text source untouched"

@pytest.mark.asyncio
async def test_module_import_does_not_self_deadlock_under_session_lock(tmp_path, monkeypatch):
    """Regression: the transport choke point (`net.session._on_frame`) holds the room's
    `turn_lock` around the ENTIRE `admin_generate` dispatch, and that lock is a plain
    (non-reentrant) `asyncio.Lock`. `module_import` must NOT re-acquire the same lock —
    doing so self-deadlocks the import and strands the room lock forever, wedging every
    later module request for that room (the browser's "import to this room" button then
    never answers). The dispatch must return while the session-style lock is held."""
    import asyncio

    from gateway.hub import RoomHub
    from infra.i18n import get_i18n

    store = Store(":memory:")
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        store=store,
        documents=DocumentStore(store),
        worldbook=Worldbook(store),
    )
    hub = RoomHub()
    admin = ModuleAdminService(SimpleNamespace(services=services, keystore=None, fs=None, hub=hub))
    room = "fog-room"
    chat_key = chat_key_for_room(room)

    # A fake installed pack home carrying one world card (same fixture shape as
    # `test_import_pack_routes_to_world_card_import`).
    home = tmp_path / "packs" / "fog@1.0.0"
    (home / "cards").mkdir(parents=True)
    (home / "cards" / "fog.lorecard.json").write_text(
        json.dumps(
            {
                "format": "loreweaver.card",
                "format_version": 1,
                "name": "Fog Manor",
                "opening": "It begins.",
                "worldbook": [{"title": "[InitVar]", "content": '{"fog": 1}'}],
            }
        ),
        encoding="utf-8",
    )
    card_size = (home / "cards" / "fog.lorecard.json").stat().st_size
    (home / "pack.yaml").write_text(
        """manifest_version: 2
id: fog
version: 1.0.0
name: Fog
description: Fog fixture
authors: [tester]
license: MIT
contents:
  cards:
    - path: cards/fog.lorecard.json
      kind: world
trust:
  cards: 1
  world_cards: 1
files:
  - path: cards/fog.lorecard.json
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    size: """
        + str(card_size)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.panels.installed_pack_homes", lambda data_dir: {"fog": home})

    class _FakeCharcardTools:
        def __init__(self, services) -> None:
            self._services = services

        async def import_world_card(self, ctx, file_path, **kw):
            return "world card imported"

    monkeypatch.setattr("agent.kp_tools_charcard.CharcardTools", _FakeCharcardTools)

    i18n = get_i18n("en")

    async def session_style_dispatch():
        # Exactly what `net.session._on_frame` does for an `admin_generate` frame: hold the
        # room's turn lock, then hand the frame to the admin service.
        async with hub.turn_lock(chat_key):
            frame = {
                "type": "admin_generate",
                "kind": "module_import",
                "description": json.dumps({"name": "fog", "locale": "en"}),
            }
            return await admin.dispatch("keeper", room, frame, i18n)

    # If the inner lock comes back, this times out (self-deadlock) instead of returning.
    reply = await asyncio.wait_for(session_style_dispatch(), timeout=10)
    assert reply["ok"] is True
    assert reply["kind"] == "module_import"
