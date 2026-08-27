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
