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
from core.worldbook import LoreEntry
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


async def test_item_add_reveals_linked_worldbook_evidence():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:item-evidence"
    await _make(services, chat_key, "p1", "Alice")
    await services.worldbook.add(
        chat_key,
        LoreEntry(
            id="",
            title="Photograph evidence",
            content="The photograph places the suspect at the pier.",
            keys=["photograph-evidence"],
            category="clue",
        ),
    )
    await _seed(
        services,
        chat_key,
        _tpl("Old Photograph", plot_role="evidence", reveals=["photograph-evidence"]),
    )

    await router.dispatch(_player_ctx(chat_key), ".item add Old Photograph")

    from agent.clue_log import get_clue_log

    clues = await get_clue_log(services.documents, chat_key)
    assert [clue["title"] for clue in clues] == ["Photograph evidence"]


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


async def test_item_archive_shelves_item_out_of_play():
    """`.item archive` shelves an item: out of the active bag and the wire views,
    its equip slot cleared so the bonus stops — but the record survives and
    `--archived` lists it."""
    from agent.items import render_held_items, render_item_views, set_equipped

    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Bronze Mirror", kind="misc", bonus={"侦查": 5}))
    await router.dispatch(_player_ctx(chat_key), ".item add Bronze Mirror")
    doc = (await instances_for_owner(services.documents, chat_key, "Alice"))[0]
    await set_equipped(services.documents, chat_key, doc.id, "accessory")

    reply = await router.dispatch(_player_ctx(chat_key), ".item archive Bronze Mirror")

    assert "Archived" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Alice")
    assert instances[0].data.get("archived") is True
    assert instances[0].data.get("equipped_slot") is None
    # Out of the plain views and the active bag...
    assert render_held_items(instances) == []
    assert "Bronze Mirror" not in (await router.dispatch(_player_ctx(chat_key), ".item inv"))
    assert render_item_views(instances) == [] or not any(
        not view.get("archived") for view in render_item_views(instances)
    )
    # ...but the `--archived` listing shows it.
    archived_inv = await router.dispatch(_player_ctx(chat_key), ".item inv --archived")
    assert "Bronze Mirror" in archived_inv


async def test_item_views_order_newest_first():
    """Item views order holdings by acquisition time (document creation stamp),
    newest first, so the equipment-details section reads latest first; a row with
    no stamp sorts last."""
    from agent.items import render_held_items, render_item_views
    from core.documents import Document

    def instance(name: str, created: float) -> Document:
        return Document(
            id=name,
            type="item",
            schema_version=1,
            data={"name": name, "kind": "misc", "slot": "", "quantity": 1},
            meta={"created": created},
        )

    older = instance("Torch", 100.0)
    newer = instance("Sword", 200.0)
    newest = instance("Gem", 300.0)
    legacy = Document(
        id="Relic",
        type="item",
        schema_version=1,
        data={"name": "Relic", "kind": "misc", "slot": "", "quantity": 1},
        meta={},
    )

    views = render_item_views([older, newest, legacy, newer])
    assert [v["name"] for v in views] == ["Gem", "Sword", "Torch", "Relic"]
    assert render_held_items([older, newest, legacy, newer]) == ["Gem", "Sword", "Torch", "Relic"]


async def test_item_unarchive_restores_item_to_active_bag():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Bronze Mirror"))
    await router.dispatch(_player_ctx(chat_key), ".item add Bronze Mirror")
    await router.dispatch(_player_ctx(chat_key), ".item archive Bronze Mirror")

    reply = await router.dispatch(_player_ctx(chat_key), ".item unarchive Bronze Mirror")

    assert "Restored" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Alice")
    assert instances[0].data.get("archived") is False
    assert "Bronze Mirror" in (await router.dispatch(_player_ctx(chat_key), ".item inv"))
    assert "Bronze Mirror" not in (await router.dispatch(_player_ctx(chat_key), ".item inv --archived"))


async def test_item_equip_rejects_archived_item():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Sword", slot="main_hand"))
    await router.dispatch(_player_ctx(chat_key), ".item add Sword")
    await router.dispatch(_player_ctx(chat_key), ".item archive Sword")

    reply = await router.dispatch(_player_ctx(chat_key), ".item equip Sword")

    assert "archived" in reply and "unarchive" in reply


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

# ---------------------------------------------------------------------------
# .item give — improvised off-catalog lane (D6's exception)
# ---------------------------------------------------------------------------


async def test_item_give_improvised_off_catalog():
    """A catalog miss becomes an improvised one-off: universal scope, no bonus,
    and the player still cannot add it themselves."""
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")

    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 神秘护符 Bob")

    assert "improvised" in reply and "Bob" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Bob")
    assert len(instances) == 1
    data = instances[0].data
    assert data["name"] == "神秘护符"
    assert data["scope"] == "universal"
    assert data["bonus"] == {}
    assert data["improvised"] is True
    assert data["equipped_slot"] is None  # no bonus → stays in the bag
    denied = await router.dispatch(_player_ctx(chat_key, uid="p2"), ".item add 神秘护符")
    assert "not in this room's item catalog" in denied


async def test_item_give_improvised_with_desc_qty_and_secret():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")

    reply = await router.dispatch(
        _keeper_ctx(chat_key), ".item give 治疗药水 Bob --desc 红色瓶装药剂 --qty 3 --secret"
    )

    assert "improvised" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Bob")
    assert len(instances) == 1
    data = instances[0].data
    assert data["description"] == "红色瓶装药剂"
    assert data["quantity"] == 3
    assert data["secret"] is True
    inv = await router.dispatch(_player_ctx(chat_key), ".item inv Bob")
    assert "治疗药水" not in inv


async def test_item_give_improvised_small_bonus_applies_immediately():
    """A bonus-bearing improvised give is equipped automatically, so the edge
    applies without a separate equip step (the old flow required one)."""
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")

    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 幸运石 Bob --bonus 侦查=1")
    assert "improvised" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Bob")
    assert instances[0].data["equipped_slot"] == "equipped"
    sheet = await services.characters.get_character("p2", chat_key)
    assert sheet.equipped_bonuses == {"侦查": 1}

    # The holder can still move it to a real slot without losing the edge.
    reply = await router.dispatch(_player_ctx(chat_key, uid="p2"), ".item equip 幸运石 as necklace")
    assert "necklace" in reply
    sheet = await services.characters.get_character("p2", chat_key)
    assert sheet.equipped_bonuses == {"侦查": 1}


async def test_item_give_improvised_unresolvable_bonus_warns():
    """A bonus key the pack cannot resolve is kept as-is and reported — without
    the warning it would silently never apply to any check."""
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")

    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 怪石 Bob --bonus 不存在技能=1")

    assert "kept as-is" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Bob")
    assert instances[0].data["bonus"] == {"不存在技能": 1}


async def test_item_give_improvised_bonus_over_cap_rejected():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 神剑 Bob --bonus 侦查=3")
    assert "±2" in reply
    assert await _instance_names(services, chat_key, "Bob") == []


async def test_item_give_improvised_bonus_total_over_cap_rejected():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 神剑 Bob --bonus 侦查=2,意志=2,敏捷=1")
    assert "4 points" in reply
    assert await _instance_names(services, chat_key, "Bob") == []


async def test_item_give_improvised_bad_bonus_rejected():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give 神剑 Bob --bonus 侦查=abc")
    assert "Invalid give flags" in reply
    assert await _instance_names(services, chat_key, "Bob") == []


async def test_item_give_catalog_item_rejects_override_flags():
    """A catalog item's description/bonus/secret come from the module — the Keeper
    cannot override them through give flags."""
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Torch"))
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give Torch Bob --desc 照亮一切")
    assert "catalog item" in reply
    assert await _instance_names(services, chat_key, "Bob") == []


async def test_item_give_catalog_item_with_qty_merges():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Ration"))
    reply = await router.dispatch(_keeper_ctx(chat_key), ".item give Ration Bob --qty 3")
    assert "Bob" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Bob")
    assert instances[0].data.get("quantity") == 3


# ---------------------------------------------------------------------------
# .item use — consumable semantics (quantity decreases, zero removes)
# ---------------------------------------------------------------------------


async def test_item_use_consumes_quantity():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Ration"))
    await router.dispatch(_player_ctx(chat_key), ".item add Ration 3")

    reply = await router.dispatch(_player_ctx(chat_key), ".item use Ration")
    assert "2 left" in reply
    instances = await instances_for_owner(services.documents, chat_key, "Alice")
    assert instances[0].data.get("quantity") == 2

    reply = await router.dispatch(_player_ctx(chat_key), ".item use Ration 2")
    assert "last" in reply
    assert await _instance_names(services, chat_key, "Alice") == []


async def test_item_use_not_held_is_failure():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    reply = await router.dispatch(_player_ctx(chat_key), ".item use Ghost")
    assert "holds no" in reply

# ---------------------------------------------------------------------------
# room broadcast — who holds what is table talk (D5)
# ---------------------------------------------------------------------------


class _FakeHub:
    def __init__(self) -> None:
        self.published: list = []

    def members(self, chat_key: str) -> list:
        return []

    async def publish(self, chat_key: str, event: object) -> None:
        self.published.append((chat_key, event))

    async def publish_each(self, chat_key: str, event_for: object) -> None:
        return


def _narrative_texts(hub: _FakeHub) -> list[str]:
    return [e.text for _, e in hub.published if getattr(e, "kind", "") == "narrative"]


async def test_item_add_broadcasts_room_notice():
    services = _services()
    hub = _FakeHub()
    router = CommandRouter(services, hub=hub)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Torch"))

    await router.dispatch(_player_ctx(chat_key), ".item add Torch")

    texts = _narrative_texts(hub)
    assert any("Alice" in t and "Torch" in t for t in texts)


async def test_item_give_broadcasts_room_notice():
    services = _services()
    hub = _FakeHub()
    router = CommandRouter(services, hub=hub)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")
    await _seed(services, chat_key, _tpl("Bronze Key"))

    await router.dispatch(_keeper_ctx(chat_key), ".item give Bronze Key Bob")

    texts = _narrative_texts(hub)
    assert any("Bob" in t and "Bronze Key" in t for t in texts)


async def test_item_use_broadcasts_room_notice():
    services = _services()
    hub = _FakeHub()
    router = CommandRouter(services, hub=hub)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p1", "Alice")
    await _seed(services, chat_key, _tpl("Ration"))
    await router.dispatch(_player_ctx(chat_key), ".item add Ration 3")

    await router.dispatch(_player_ctx(chat_key), ".item use Ration")

    texts = _narrative_texts(hub)
    assert any("2 left" in t for t in texts)


async def test_item_secret_give_is_not_broadcast():
    services = _services()
    hub = _FakeHub()
    router = CommandRouter(services, hub=hub)
    chat_key = "cli:dm:items"
    await _make(services, chat_key, "p2", "Bob")

    await router.dispatch(_keeper_ctx(chat_key), ".item give 密信 Bob --secret")

    assert _narrative_texts(hub) == []
