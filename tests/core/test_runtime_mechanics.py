"""Focused contracts for the deterministic runtime foundation."""

import json

import pytest

from core.character_manager import CharacterSheet
from core.combat import TurnOwnershipError, claim_turn, create_combat, end_turn, start_combat
from core.damage import apply_damage
from core.encounters import calculate_budget, parse_encounter
from core.resources import recover_by_reset, resource_values, set_resource
from core.rulepacks import load_rulepack, parse_rulepack_text
from core.runtime import RuntimeSpecError
from core.statblocks import parse_statblock, project_statblock
from infra.store import Store


def test_runtime_pack_is_loaded_and_unknown_fields_fail_closed() -> None:
    pack = load_rulepack("dnd5e")
    assert pack.runtime_spec is not None
    assert set(pack.runtime_spec.pools) >= {"hp", "temp_hp", "hit_die_d10"}
    with pytest.raises(RuntimeSpecError):
        parse_rulepack_text("broken", "runtime:\n  version: 1\n  typo: true\n")


def test_resource_pool_bounds_and_reset_tags() -> None:
    pack = load_rulepack("dnd5e")
    sheet = CharacterSheet("Hero", "dnd5e")
    sheet.character_class = "wizard"  # level-1 full caster: 2 first-level slots
    values = resource_values(sheet, pack)
    assert values["hp"].current == values["hp"].maximum == 8
    set_resource(sheet, pack, "hp", -50)
    assert resource_values(sheet, pack)["hp"].current == 0
    set_resource(sheet, pack, "spell_slot_1", 0)
    set_resource(sheet, pack, "hit_die_d10", 0)
    recover_by_reset(sheet, pack, "long")
    restored = resource_values(sheet, pack)
    # Spell slots recover to their level-table maximum on a long rest (a level-1
    # caster has 2 first-level slots). Hit dice recover the same way.
    assert restored["spell_slot_1"].current == 2
    assert restored["hit_die_d10"].current == 1


def test_combat_claim_budget_and_round_progression() -> None:
    state = start_combat(
        create_combat(
            "encounter",
            [
                {"id": "a", "initiative": 20, "controller_id": "u1"},
                {"id": "b", "initiative": 10, "controller_id": "u2"},
            ],
            budget={"action": 1, "reaction": 1},
        ),
        budget={"action": 1, "reaction": 1},
    )
    with pytest.raises(TurnOwnershipError):
        claim_turn(state, "a", "u2")
    claimed = claim_turn(state, "a", "u1")
    next_state = end_turn(claimed, "a", claim_token=str(claimed.claim["token"]))
    assert next_state.current == "b"
    assert next_state.combatants["b"]["budget"]["action"] == 1


def test_damage_defense_order_and_temporary_health() -> None:
    outcome = apply_damage(
        9,
        "fire",
        defenses={"resistance": ["fire"]},
        temporary_health=2,
    )
    assert outcome.adjusted == 4
    assert outcome.temporary_absorbed == 2
    assert outcome.health_damage == 2


def test_statblock_projection_and_encounter_budget() -> None:
    stat = parse_statblock(
        "dnd5e",
        {
            "id": "guard",
            "name": "Guard",
            "public": {"description": "A visible guard"},
            "resources": {"hp": {"role": "health", "current": 11, "max": 11}},
            "defenses": {"armor_class": 16},
            "challenge_weight": 1,
        },
    )
    public = project_statblock(stat)
    assert public is not None and "defenses" not in public
    encounter = parse_encounter({"id": "patrol", "name": "Patrol", "entries": [{"ref": "guard", "count": 2}]})
    result = calculate_budget(
        encounter,
        {"guard": stat},
        party_size=4,
        declaration={"count_multipliers": {"2": 2}, "party_thresholds": {"4": {"easy": 2, "hard": 4}}},
    )
    assert result.adjusted_weight == 4 and result.band == "hard"


@pytest.mark.asyncio
async def test_room_cas_commits_state_and_documents_together() -> None:
    store = Store()
    assert await store.compare_and_swap_room(
        "room",
        expected_state=[("marker", None)],
        state_updates=[("marker", "1")],
        expected_documents=[("sheet", "Hero", None)],
        document_updates=[{"type": "sheet", "id": "Hero", "data": {"name": "Hero"}}],
    )
    assert not await store.compare_and_swap_room(
        "room",
        expected_state=[("marker", "wrong")],
        state_updates=[("marker", "2")],
        document_updates=[{"type": "sheet", "id": "Hero", "data": {"name": "Changed"}}],
    )
    assert await store.state_get("room", "marker") == "1"
    assert json.loads((await store.doc_get("room", "sheet", "Hero"))["data"])["name"] == "Hero"
    store.close()


def test_dnd_spell_catalog_loads_the_full_srd() -> None:
    pack = load_rulepack("dnd5e")
    catalog = pack.spells
    assert catalog is not None and len(catalog) > 300  # full SRD 5.1 list

    fireball = catalog.get("Fireball")
    assert fireball is not None and fireball.level == 3
    assert fireball.save == {"ability": "敏捷", "success": "half"}
    assert [component.roll for component in fireball.damage] == ["8d6"]
    assert fireball.scaling == {"every": 1, "add": [{"roll": "1d6", "type": "fire"}]}
    assert fireball.dc_ability == "智力"

    # Localized display names and ids both resolve.
    assert catalog.get("火球术").id == "fireball"
    assert catalog.get("magic_missile").level == 1
    # Class spellbook drives creation-time known spells.
    assert "fire_bolt" in catalog.spellbook.get("wizard", ())
    assert "cure_wounds" in catalog.spellbook.get("cleric", ())


def test_half_and_pact_casters_use_their_own_slot_tables() -> None:
    pack = load_rulepack("dnd5e")
    # Half caster: a level-3 paladin unlocks 2 first-level slots (full casters
    # would already have 4x1 + 2x2).
    paladin = CharacterSheet("Knight", "dnd5e")
    paladin.character_class = "paladin"
    paladin.level = 3
    slots = {pid: value.maximum for pid, value in resource_values(paladin, pack).items() if pid.startswith("spell_slot_") and value.maximum}
    assert slots == {"spell_slot_1": 2}
    # Pact caster: a level-5 warlock holds only 3rd-ring pact slots.
    warlock = CharacterSheet("Lock", "dnd5e")
    warlock.character_class = "warlock"
    warlock.level = 5
    slots = {pid: value.maximum for pid, value in resource_values(warlock, pack).items() if pid.startswith("spell_slot_") and value.maximum}
    assert slots == {"spell_slot_3": 2}
    # A non-caster has no spell slots at all.
    fighter = CharacterSheet("Fighter", "dnd5e")
    fighter.character_class = "fighter"
    fighter.level = 5
    assert not {pid for pid, value in resource_values(fighter, pack).items() if pid.startswith("spell_slot_") and value.maximum}


def test_pact_caster_slots_recover_on_short_rest() -> None:
    pack = load_rulepack("dnd5e")
    warlock = CharacterSheet("Lock", "dnd5e")
    warlock.character_class = "warlock"
    warlock.level = 5
    set_resource(warlock, pack, "spell_slot_3", 0)
    recover_by_reset(warlock, pack, "short")
    assert resource_values(warlock, pack)["spell_slot_3"].current == 2
    # A full caster never recovers slots on a short rest — only on a long one.
    wizard = CharacterSheet("Mage", "dnd5e")
    wizard.character_class = "wizard"
    wizard.level = 5
    set_resource(wizard, pack, "spell_slot_1", 0)
    recover_by_reset(wizard, pack, "short")
    assert resource_values(wizard, pack)["spell_slot_1"].current == 0
    recover_by_reset(wizard, pack, "long")
    assert resource_values(wizard, pack)["spell_slot_1"].current == 4
