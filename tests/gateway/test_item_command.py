"""Tests for the `.item` command (phase 2): view/grant/drop/equip gear on `item`
documents, keeper-gated cross-character `give`, catalog-validated `add` (D6), and
table-level `inv` reads (D5).

Deterministic and offline (FakeLLM/FakeEmbeddings, fresh in-memory store per test).
A networked player is `platform="tui", extra={"role": "player"}`; a keeper is the
trusted local `cli` platform — matching `_is_keeper`.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.items import ensure_catalog, instances_for_owner
from agent.services import build_services
from core.character_manager import CharacterSheet
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _player_ctx(chat_key: str, uid: str = "p1") -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id=uid, platform="tui", locale="en", extra={"role": "player"})


def _keeper_ctx(chat_key: str) -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id="k1", platform="cli", locale="en")


async def _make(services, chat_key, owner: str, name: str) -> None:
    await services.characters.save_character(owner, chat_key, CharacterSheet(name, "coc7"))


def _tpl(name: str, **extra) -> dict:
    tpl = {"name": name, "kind": "misc", "slot": "", "effect": "", "bonus": {}}
    tpl.update(extra)
    return tpl


async def _seed(services, chat_key, *templates) -> None:
    await ensure_catalog(services.documents, chat_key, list(templates))


async def _instance_names(services, chat_key, owner: str) -> list[str]:
    return [doc.data.get("name") for doc in await instances_for_owner(services.documents, chat_key, owner)]


# ---------------------------------------------------------------------------
# .item inv — table-level read (D5)
# ---------------------------------------------------------------------------


async def test_item_usage_when_bare():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    reply = await router.dispatch(_player_ctx(chat_key), ".item")
    assert "Usage:" in reply


async def test_item_inv_shows_own_character():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Torch"))

    await router.dispatch(_player_ctx(chat_key), ".item add Torch")
    reply = await router.dispatch(_player_ctx(chat_key), ".item inv")

    assert "Alice" in reply and "Torch" in reply


async def test_item_inv_shows_any_member_table_level_read():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Bronze Key"))

    # Keeper gives Bob a key, then a player reads Bob's list (D5).
    await router.dispatch(_keeper_ctx(chat_key), ".item give Bronze Key Bob")
    reply = await router.dispatch(_player_ctx(chat_key), ".item inv Bob")

    assert "Bob" in reply and "Bronze Key" in reply


async def test_item_inv_unknown_character_is_failure():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    reply = await router.dispatch(_player_ctx(chat_key), ".item inv Ghost")
    assert "No character" in reply and "Ghost" in reply


# ---------------------------------------------------------------------------
# .item add / drop — catalog-validated (D6), the player's own character
# ---------------------------------------------------------------------------


async def test_item_add_to_own_active_character():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Sword"))

    reply = await router.dispatch(_player_ctx(chat_key), ".item add Sword")

    assert "Sword" in reply and "Alice" in reply
    assert await _instance_names(services, chat_key, "Alice") == ["Sword"]


async def test_item_add_rejects_item_not_in_catalog():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    reply = await router.dispatch(_player_ctx(chat_key), ".item add Sword")
    assert "not in this room's item catalog" in reply


async def test_item_add_with_qty_merges_quantity():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Ration"))

    await router.dispatch(_player_ctx(chat_key), ".item add Ration 5")

    instances = await instances_for_owner(services.documents, chat_key, "Alice")
    assert len(instances) == 1 and instances[0].data.get("quantity") == 5


async def test_item_drop_removes():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Sword"), _tpl("Torch"))
    await router.dispatch(_player_ctx(chat_key), ".item add Sword")
    await router.dispatch(_player_ctx(chat_key), ".item add Torch")

    reply = await router.dispatch(_player_ctx(chat_key), ".item drop Sword")

    assert "Sword" in reply
    assert await _instance_names(services, chat_key, "Alice") == ["Torch"]


async def test_item_drop_not_held_is_failure():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Sword"))
    reply = await router.dispatch(_player_ctx(chat_key), ".item drop Ghost")
    assert "holds no" in reply


# ---------------------------------------------------------------------------
# .item give — keeper-only, cross-character, catalog-validated
# ---------------------------------------------------------------------------


async def test_item_give_denied_for_player():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Sword"))
    reply = await router.dispatch(_player_ctx(chat_key), ".item give Sword Bob")
    assert "keeper" in reply


async def test_item_give_keeper_grants_to_other_character():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Bronze Key"))

    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give Bronze Key Bob")

    assert "Bob" in reply and "Bronze Key" in reply
    assert await _instance_names(services, chat_key, "Bob") == ["Bronze Key"]


async def test_item_give_keeper_unknown_target_is_failure():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _seed(services, chat_key, _tpl("Sword"))
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give Sword Ghost")
    assert "No character" in reply and "Ghost" in reply


# ---------------------------------------------------------------------------
# .item equip / unequip — slot control
# ---------------------------------------------------------------------------


async def test_item_equip_marks_equipped_and_shows_in_inv():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Fencing Sword", kind="weapon", slot="main_hand"))
    await router.dispatch(_player_ctx(chat_key), ".item add Fencing Sword")

    reply = await router.dispatch(_player_ctx(chat_key), ".item equip Fencing Sword")

    assert "main_hand" in reply
    inv = await router.dispatch(_player_ctx(chat_key), ".item inv")
    assert "equipped: main_hand" in inv


async def test_item_unequip_drops_slot():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Fencing Sword", kind="weapon", slot="main_hand"))
    await router.dispatch(_player_ctx(chat_key), ".item add Fencing Sword")
    await router.dispatch(_player_ctx(chat_key), ".item equip Fencing Sword")

    reply = await router.dispatch(_player_ctx(chat_key), ".item unequip Fencing Sword")

    assert "unequipped" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Alice")
    assert instances[0].data.get("equipped_slot") is None


async def test_item_equip_not_held_is_failure():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Sword", slot="main_hand"))
    reply = await router.dispatch(_player_ctx(chat_key), ".item equip Sword")
    assert "holds no" in reply
