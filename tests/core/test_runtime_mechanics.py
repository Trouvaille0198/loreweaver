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
    values = resource_values(sheet, pack)
    assert values["hp"].current == values["hp"].maximum == 8
    set_resource(sheet, pack, "hp", -50)
    assert resource_values(sheet, pack)["hp"].current == 0
    set_resource(sheet, pack, "spell_slot_1", 0)
    set_resource(sheet, pack, "hit_die_d10", 0)
    recover_by_reset(sheet, pack, "long")
    restored = resource_values(sheet, pack)
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
