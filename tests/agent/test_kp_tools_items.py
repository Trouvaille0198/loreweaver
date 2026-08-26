"""Tests for the phase-2 item tools in agent.kp_tools_mechanics: grant/transfer/
remove/use/equip/unequip_item. Items are `item` documents (agent.items); grant
validates the room's catalog (D6) and equip slots drive bonuses (D3).

Deterministic and offline: `FakeLLM`/`FakeEmbeddings`, fresh in-memory store per test.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.items import (
    aggregate_equipped_bonuses,
    ensure_catalog,
    instances_for_owner,
    item_active,
)
from agent.kp_tools_mechanics import CharacterTools
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.sheets import sheet_value
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _build():
    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))
    ctx = AgentCtx(chat_key="cli:dm:items", user_id="keeper")
    return services, ctx


async def _make_character(services, chat_key, owner: str, name: str) -> None:
    await services.characters.save_character(owner, chat_key, CharacterSheet(name, "coc7"))


async def _seed(services, chat_key, *templates) -> None:
    await ensure_catalog(services.documents, chat_key, list(templates))


async def _instance_names(services, chat_key, owner: str) -> list[str]:
    return [doc.data.get("name") for doc in await instances_for_owner(services.documents, chat_key, owner)]


def _tpl(name: str, **extra) -> dict:
    tpl = {"name": name, "kind": "misc", "slot": "", "effect": "", "bonus": {}}
    tpl.update(extra)
    return tpl


# ---------------------------------------------------------------------------
# grant_item
# ---------------------------------------------------------------------------


async def test_grant_item_adds_to_any_member_owned_by_another_uid():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Bronze Key"))

    reply = await CharacterTools(services).grant_item(ctx, "Alice", "Bronze Key")

    assert "Bronze Key" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == ["Bronze Key"]


async def test_grant_item_merges_same_owner_same_name_quantity():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Ration"))
    tools = CharacterTools(services)

    await tools.grant_item(ctx, "Alice", "Ration", qty=2)
    await tools.grant_item(ctx, "Alice", "Ration", qty=3)

    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert len(instances) == 1
    assert instances[0].data.get("quantity") == 5


async def test_grant_item_rejects_item_not_in_catalog():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    reply = await CharacterTools(services).grant_item(ctx, "Alice", "Sword")
    assert "not in this room's item catalog" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []


async def test_grant_item_rejects_unknown_character():
    services, ctx = _build()
    await _seed(services, ctx.chat_key, _tpl("Sword"))
    reply = await CharacterTools(services).grant_item(ctx, "Nobody", "Sword")
    assert "No character" in reply


async def test_grant_item_rejects_nonpositive_qty():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Sword"))
    reply = await CharacterTools(services).grant_item(ctx, "Alice", "Sword", qty=0)
    assert "positive integer" in reply


async def test_grant_item_rejects_module_item_outside_its_module():
    """A module-scoped item cannot be granted while the room's active module is a
    different one (or none) — a plot artifact must not leak across campaigns."""
    from agent.module_lifecycle import publish_active_module

    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(
        services,
        ctx.chat_key,
        _tpl("The Bronze Mirror", scope="module", module_id="shadows-over-shanghai"),
        _tpl("Flashlight", scope="universal", module_id=""),
    )
    await publish_active_module(
        services, ctx.chat_key, {"source_id": "pack:other@1.0.0:cards/other.json", "pack_id": "other"}
    )

    reply = await CharacterTools(services).grant_item(ctx, "Alice", "The Bronze Mirror")
    assert "different module" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []

    # The universal item still works in any module.
    reply = await CharacterTools(services).grant_item(ctx, "Alice", "Flashlight")
    assert "Flashlight" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == ["Flashlight"]


async def test_grant_item_allows_module_item_in_its_module():
    from agent.module_lifecycle import publish_active_module

    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(
        services,
        ctx.chat_key,
        _tpl("The Bronze Mirror", scope="module", module_id="shadows-over-shanghai"),
    )
    await publish_active_module(
        services,
        ctx.chat_key,
        {"source_id": "pack:shadows-over-shanghai@0.1.0:cards/x.json", "pack_id": "shadows-over-shanghai"},
    )

    reply = await CharacterTools(services).grant_item(ctx, "Alice", "The Bronze Mirror")
    assert "The Bronze Mirror" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == ["The Bronze Mirror"]


# ---------------------------------------------------------------------------
# transfer_item
# ---------------------------------------------------------------------------


async def test_transfer_item_moves_between_characters():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _make_character(services, ctx.chat_key, "u2", "Bob")
    await _seed(services, ctx.chat_key, _tpl("Bronze Key"))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Bronze Key")

    reply = await tools.transfer_item(ctx, "Alice", "Bob", "Bronze Key")

    assert "Bronze Key" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []
    assert await _instance_names(services, ctx.chat_key, "Bob") == ["Bronze Key"]


async def test_transfer_item_rejects_same_source_and_target():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Sword"))
    reply = await CharacterTools(services).transfer_item(ctx, "Alice", "Alice", "Sword")
    assert "different characters" in reply


async def test_transfer_item_requires_source_to_hold_item():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _make_character(services, ctx.chat_key, "u2", "Bob")
    await _seed(services, ctx.chat_key, _tpl("Sword"))
    reply = await CharacterTools(services).transfer_item(ctx, "Alice", "Bob", "Sword")
    assert "holds no" in reply


# ---------------------------------------------------------------------------
# remove_item / use_item
# ---------------------------------------------------------------------------


async def test_remove_item_removes_existing():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Sword"), _tpl("Torch"))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Sword")
    await tools.grant_item(ctx, "Alice", "Torch")

    reply = await tools.remove_item(ctx, "Alice", "Sword")

    assert "Sword" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == ["Torch"]


async def test_remove_item_reports_when_not_held():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Sword"))
    reply = await CharacterTools(services).remove_item(ctx, "Alice", "Sword")
    assert "holds no" in reply


async def test_use_item_consumes_an_entry():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Healing Potion"), _tpl("Torch"))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Healing Potion")
    await tools.grant_item(ctx, "Alice", "Torch")

    reply = await tools.use_item(ctx, "Alice", "Healing Potion")

    assert "used" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == ["Torch"]


# ---------------------------------------------------------------------------
# equip_item / unequip_item + bonuses
# ---------------------------------------------------------------------------


async def test_equip_applies_bonus_and_unequip_drops_it():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Fencing Sword", kind="weapon", slot="main_hand", bonus={"attack": 2}))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Fencing Sword")

    # Unequipped: no bonus.
    sheet = await services.characters.get_character("u1", ctx.chat_key, "Alice")
    assert sheet.equipped_bonuses == {}
    pack = load_rulepack("coc7")
    base = sheet_value(sheet, pack, "attack")

    await tools.equip_item(ctx, "Alice", "Fencing Sword")

    sheet = await services.characters.get_character("u1", ctx.chat_key, "Alice")
    assert sheet.equipped_bonuses == {"attack": 2}
    assert sheet_value(sheet, pack, "attack") == base + 2

    await tools.unequip_item(ctx, "Alice", "Fencing Sword")

    sheet = await services.characters.get_character("u1", ctx.chat_key, "Alice")
    assert sheet.equipped_bonuses == {}
    assert sheet_value(sheet, pack, "attack") == base


async def test_equip_uses_declared_slot_when_unspecified():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Iron Shield", kind="armor", slot="off_hand"))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Iron Shield")

    reply = await tools.equip_item(ctx, "Alice", "Iron Shield")

    assert "off_hand" in reply
    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert instances[0].data.get("equipped_slot") == "off_hand"


async def test_equip_requires_held_item():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Sword", slot="main_hand"))
    reply = await CharacterTools(services).equip_item(ctx, "Alice", "Sword")
    assert "holds no" in reply


async def test_transfer_drops_source_bonus():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _make_character(services, ctx.chat_key, "u2", "Bob")
    await _seed(services, ctx.chat_key, _tpl("Fencing Sword", slot="main_hand", bonus={"attack": 2}))
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Fencing Sword")
    await tools.equip_item(ctx, "Alice", "Fencing Sword")
    assert (await services.characters.get_character("u1", ctx.chat_key, "Alice")).equipped_bonuses == {"attack": 2}

    await tools.transfer_item(ctx, "Alice", "Bob", "Fencing Sword")

    # Alice lost the item and its bonus; Bob holds it (unequipped, so no bonus yet).
    assert (await services.characters.get_character("u1", ctx.chat_key, "Alice")).equipped_bonuses == {}
    assert await _instance_names(services, ctx.chat_key, "Bob") == ["Fencing Sword"]


async def test_grant_updates_roster_equipment_for_clients():
    """The roster's `equipment` (what clients render) reflects the item instances, so
    the page shows gear without any client-side change."""
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Fencing Sword", kind="weapon", slot="main_hand", bonus={"attack": 2}))
    tools = CharacterTools(services)

    await tools.grant_item(ctx, "Alice", "Fencing Sword")
    await tools.equip_item(ctx, "Alice", "Fencing Sword")

    roster = await services.characters.get_party_roster(ctx.chat_key)
    alice = next((m for m in roster if m.get("name") == "Alice"), None)
    assert alice is not None
    assert alice.get("equipment") == ["Fencing Sword (main_hand)"]
    # Structured item views ride the roster too, for a client item-detail section.
    item_views = alice.get("items")
    assert len(item_views) == 1
    assert item_views[0]["name"] == "Fencing Sword"
    assert item_views[0]["kind"] == "weapon"
    assert item_views[0]["equipped_slot"] == "main_hand"
    # The item's bonus map rides the wire so a client can show per-stat contributions.
    assert item_views[0]["bonus"] == {"attack": 2}


async def test_secret_items_stay_out_of_client_roster():
    """Secret items never appear in the player-facing roster equipment list."""
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(services, ctx.chat_key, _tpl("Map"), _tpl("Hidden Ledger", secret=True))
    tools = CharacterTools(services)

    await tools.grant_item(ctx, "Alice", "Map")
    await tools.grant_item(ctx, "Alice", "Hidden Ledger")

    roster = await services.characters.get_party_roster(ctx.chat_key)
    alice = next((m for m in roster if m.get("name") == "Alice"), None)
    assert alice.get("equipment") == ["Map"]


async def test_aggregate_equipped_bonuses_sums_across_equipped_items():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(
        services,
        ctx.chat_key,
        _tpl("Sword", slot="main_hand", bonus={"attack": 2}),
        _tpl("Helmet", slot="armor", bonus={"ac": 1}),
    )
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Sword")
    await tools.grant_item(ctx, "Alice", "Helmet")
    await tools.equip_item(ctx, "Alice", "Sword")
    await tools.equip_item(ctx, "Alice", "Helmet")

    sheet = await services.characters.get_character("u1", ctx.chat_key, "Alice")
    assert sheet.equipped_bonuses == {"attack": 2, "ac": 1}


# ---------------------------------------------------------------------------
# module scoping — pure functions
# ---------------------------------------------------------------------------


def test_item_active_scoping():
    """Universal items (and unbound legacy items) always contribute; module-scoped
    items only while the room's active module matches their module_id (pack_id or
    source_id). No active module -> module items are inert."""
    active = {"pack_id": "aaa", "source_id": "pack:aaa@1.0.0:cards/x.json"}
    assert item_active(active, {"scope": "universal", "module_id": "ignored"})
    assert item_active(None, {"scope": "universal"})
    assert item_active(None, {"scope": "", "module_id": ""})
    assert item_active(active, {"scope": "module", "module_id": ""})
    assert item_active(active, {"scope": "module", "module_id": "aaa"})
    assert item_active(active, {"scope": "module", "module_id": "pack:aaa@1.0.0:cards/x.json"})
    assert not item_active(active, {"scope": "module", "module_id": "bbb"})
    assert not item_active(None, {"scope": "module", "module_id": "aaa"})


def test_aggregate_equipped_bonuses_skips_foreign_module_items():
    from types import SimpleNamespace

    doc = lambda data: SimpleNamespace(data=data)  # noqa: E731
    active = {"pack_id": "aaa", "source_id": "pack:aaa@1.0.0:cards/x.json"}
    items = [
        doc({"equipped_slot": "weapon", "bonus": {"STR": 1}, "scope": "universal"}),
        doc({"equipped_slot": "weapon", "bonus": {"INT": 1}, "scope": "module", "module_id": "aaa"}),
        doc({"equipped_slot": "weapon", "bonus": {"DEX": 1}, "scope": "module", "module_id": "bbb"}),
        doc({"equipped_slot": None, "bonus": {"CON": 1}, "scope": "universal"}),
    ]
    assert aggregate_equipped_bonuses(items, active) == {"STR": 1, "INT": 1}
    # No active module: module-scoped gear is inert, universal still counts.
    assert aggregate_equipped_bonuses(items, None) == {"STR": 1}

# ---------------------------------------------------------------------------
# improvise_item — the off-catalog lane (D6's exception)
# ---------------------------------------------------------------------------


async def test_improvise_item_creates_off_catalog_instance():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "神秘护符", "石质护符，刻着看不懂的符文")

    assert "improvised" in reply
    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert len(instances) == 1
    data = instances[0].data
    assert data["name"] == "神秘护符"
    assert data["description"] == "石质护符，刻着看不懂的符文"
    assert data["scope"] == "universal"
    assert data["bonus"] == {}


async def test_improvise_item_with_small_bonus():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "幸运石", bonus="spot_hidden=1")

    assert "improvised" in reply
    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert instances[0].data["bonus"] == {"spot_hidden": 1}


async def test_improvise_item_merges_quantity():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    await tools.improvise_item(ctx, "Alice", "治疗药水", qty=3)
    await tools.improvise_item(ctx, "Alice", "治疗药水", qty=2)

    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert len(instances) == 1 and instances[0].data.get("quantity") == 5


async def test_improvise_item_rejects_oversized_bonus():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "神剑", bonus="attack=3")

    assert "±2" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []


async def test_improvise_item_rejects_total_over_cap():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "神剑", bonus="attack=2,ac=2,str=1")

    assert "4 points" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []


async def test_improvise_item_rejects_malformed_bonus():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "神剑", bonus="attack=abc")

    assert "integers" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []


async def test_improvise_item_unknown_character():
    services, ctx = _build()
    tools = CharacterTools(services)
    reply = await tools.improvise_item(ctx, "Ghost", "石头")
    assert "No character" in reply


async def test_use_item_decrements_quantity_keeps_instance():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)
    await tools.improvise_item(ctx, "Alice", "治疗药水", qty=3)

    reply = await tools.use_item(ctx, "Alice", "治疗药水")

    assert "2 left" in reply
    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert instances[0].data.get("quantity") == 2


async def test_use_item_last_unit_removes_instance():
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    tools = CharacterTools(services)
    await tools.improvise_item(ctx, "Alice", "治疗药水", qty=1)

    reply = await tools.use_item(ctx, "Alice", "治疗药水")

    assert "last" in reply
    assert await _instance_names(services, ctx.chat_key, "Alice") == []


async def test_improvise_item_uses_catalog_template_when_name_exists():
    """Improvising a name the catalog already designs must grant the real template
    (kind/effect/bonus), never a stripped one-off."""
    services, ctx = _build()
    await _make_character(services, ctx.chat_key, "u1", "Alice")
    await _seed(
        services,
        ctx.chat_key,
        _tpl("撬棍", kind="weapon", slot="weapon", effect="伤害 1d8，可撬开固定物。", bonus={"attack": 1}),
    )
    tools = CharacterTools(services)

    reply = await tools.improvise_item(ctx, "Alice", "撬棍", "顺手捡的")

    assert "Gave" in reply
    instances = await instances_for_owner(services.documents, ctx.chat_key, "Alice")
    assert len(instances) == 1
    data = instances[0].data
    assert data["kind"] == "weapon"
    assert data["slot"] == "weapon"
    assert data["effect"] == "伤害 1d8，可撬开固定物。"
    assert data["bonus"] == {"attack": 1}
    assert data["scope"] == ""  # template scope inherited verbatim, not improv's universal
