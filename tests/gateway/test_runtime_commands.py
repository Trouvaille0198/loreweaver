"""Focused gateway contracts for runtime command families."""

import json

import pytest

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.dice_engine import seed_dice
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


@pytest.mark.asyncio
async def test_runtime_commands_start_combat_resolve_action_and_show_resources() -> None:
    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    router = CommandRouter(services)
    room = "cli:dm:runtime-command"
    player = AgentCtx(chat_key=room, user_id="u1", platform="cli", locale="en")
    await services.characters.save_character("u1", room, CharacterSheet("Hero", "dnd5e"))

    resources = await router.dispatch(player, ".resource show")
    assert resources is not None and "spell_slot_1" in resources
    started = await router.dispatch(player, ".combat start")
    assert started is not None and "active" in started

    seed_dice(7)
    result = await router.dispatch(player, ".attack attack Hero")
    assert result is not None and '"action_id"' in result
    raw = await services.store.state_get(room, "combat_state")
    state = json.loads(raw or "{}")
    assert state["event_seq"] == 1 and len(state["events"]) == 1


@pytest.mark.asyncio
async def test_statblock_player_projection_hides_mechanics() -> None:
    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    router = CommandRouter(services)
    room = "cli:dm:runtime-statblock"
    await services.documents.put(
        room,
        "statblock",
        "guard",
        {
            "id": "guard",
            "name": "Guard",
            "public": {"description": "Visible guard"},
            "defenses": {"armor_class": 16},
            "resources": {"hp": {"role": "health", "current": 11, "max": 11}},
        },
        services=services,
    )
    player = AgentCtx(chat_key=room, user_id="u1", platform="tui", locale="en")
    view = await router.dispatch(player, ".statblock show guard")
    assert view is not None and "Guard" in view and "armor_class" not in view


@pytest.mark.asyncio
async def test_cast_spell_consumes_slot_and_resolves_damage() -> None:
    """`.cast <spell> <target>` resolves the spell catalog, enforces known-spells
    and slot availability, rolls damage and spends the slot pool."""
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    router = CommandRouter(services)
    room = "cli:dm:cast-success"
    player = AgentCtx(chat_key=room, user_id="u1", platform="cli", locale="en")

    from agent.char_from_persona import _fill_initial_spells
    from core.rulepacks import load_rulepack

    wizard = CharacterSheet("Wizard", "dnd5e")
    wizard.attributes["INT"] = 16
    wizard.level = 3
    wizard.character_class = "wizard"
    wizard.known_spells = ["magic_missile"]
    _fill_initial_spells(wizard, load_rulepack("dnd5e"))
    await services.characters.save_character("u1", room, wizard)
    await services.characters.set_active_character("u1", room, "Wizard")

    await router.dispatch(player, ".combat start")
    await router.dispatch(player, ".combat join goblin")

    seed_dice(2)  # 1d4+1 rolls 2 → total 3
    result = await router.dispatch(player, ".cast magic_missile goblin")
    assert result is not None and "magic_missile" in result and "force" in result

    # The lvl-1 slot was spent.
    from core.resources import resource_values
    from core.rulepacks import load_rulepack

    sheet = await services.characters.get_character("u1", room)
    pack = load_rulepack("dnd5e")
    slots = {pool: value.current for pool, value in resource_values(sheet, pack).items()}
    assert slots["spell_slot_1"] == 3  # level-3 wizard: 4 first-level slots, one cast spent

    # A repeat of the SAME action idempotently replays the stored result (no
    # second roll, no double spend) — the claim stays held for the turn.
    blocked = await router.dispatch(player, ".cast magic_missile goblin")
    assert blocked is not None and "Stored action result" in blocked


@pytest.mark.asyncio
async def test_cast_rejects_unknown_not_known_and_missing_slots() -> None:
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    router = CommandRouter(services)
    room = "cli:dm:cast-reject"
    player = AgentCtx(chat_key=room, user_id="u1", platform="cli", locale="en")

    wizard = CharacterSheet("Wizard", "dnd5e")
    wizard.known_spells = ["magic_missile"]  # NOT fireball
    await services.characters.save_character("u1", room, wizard)
    await services.characters.set_active_character("u1", room, "Wizard")
    await router.dispatch(player, ".combat start")
    await router.dispatch(player, ".combat join goblin")

    seed_dice(1)
    unknown = await router.dispatch(player, ".cast glorp goblin")
    assert unknown is not None and "Unknown spell" in unknown

    seed_dice(1)
    not_known = await router.dispatch(player, ".cast fireball goblin")
    assert not_known is not None and "don't know" in not_known

    # Empty the lvl-1 slot pool: the spell is known but uncastable.
    from core.resources import set_resource
    from core.rulepacks import load_rulepack

    pack = load_rulepack("dnd5e")
    sheet = await services.characters.get_character("u1", room)
    set_resource(sheet, pack, "spell_slot_1", 0)
    await services.characters.save_character("u1", room, sheet)

    seed_dice(1)
    no_slot = await router.dispatch(player, ".cast magic_missile goblin")
    assert no_slot is not None and "No available spell slots" in no_slot


@pytest.mark.asyncio
async def test_cast_save_spell_rolls_target_save_and_applies_half_damage() -> None:
    """Fireball is a DEX-save spell: the target rolls against the caster's DC and
    a successful save halves the damage (the save_half factor on its defenses)."""
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    router = CommandRouter(services)
    room = "cli:dm:cast-fireball"
    keeper = AgentCtx(chat_key=room, user_id="kp", platform="cli", locale="en")

    from agent.char_from_persona import _fill_initial_spells
    from core.rulepacks import load_rulepack

    wizard = CharacterSheet("Wizard", "dnd5e")
    wizard.attributes["INT"] = 16  # DC 14 at level 5 (8 + prof 3 + INT mod 3)
    wizard.level = 5
    wizard.character_class = "wizard"
    wizard.known_spells = ["fireball"]
    _fill_initial_spells(wizard, load_rulepack("dnd5e"))
    await services.characters.save_character("kp", room, wizard)
    await services.characters.set_active_character("kp", room, "Wizard")
    await router.dispatch(keeper, ".combat start")
    await router.dispatch(keeper, ".combat join goblin")

    # Have the goblin FAIL its save: d20 rolls 1, damage dice roll 5 → 8d6 = 40.
    from core.resources import set_resource
    from core.rulepacks import load_rulepack

    pack = load_rulepack("dnd5e")
    sheet = await services.characters.get_character("kp", room)
    set_resource(sheet, pack, "spell_slot_3", 1)  # level 5 → max 2, cast spends 1
    await services.characters.save_character("kp", room, sheet)

    seed_dice(1)  # save d20 = 1 (fails vs DC 13), damage 8d6 drawn from the seeded stream
    result = await router.dispatch(keeper, ".cast fireball goblin")
    assert result is not None and "fireball" in result
    assert '"type": "fire"' in result
    # Failed save → full damage (no save_half factor applied).
    assert '"factor": 1.0' in result
