"""Focused contract for runtime packs using one combat-state authority."""

import json

import pytest

from agent.context import AgentCtx
from agent.kp_tools_mechanics import InitiativeTools
from agent.services import build_services
from core.character_manager import CharacterSheet
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


@pytest.mark.asyncio
async def test_runtime_initiative_uses_combat_state_without_legacy_rows() -> None:
    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    room = "cli:dm:runtime-initiative"
    ctx = AgentCtx(chat_key=room, user_id="u1", platform="cli", locale="en")
    await services.characters.save_character("u1", room, CharacterSheet("Hero", "dnd5e"))
    tracker = InitiativeTools(services)

    added = await tracker.initiative_tracker(ctx, action="add", initiative=15)
    assert "Hero" in added
    keys = {row["key"] for row in await services.store.state_list(room)}
    assert "initiative" not in keys and "initiative_meta" not in keys
    pending = json.loads(await services.store.state_get(room, "combat_state") or "{}")
    assert pending["phase"] == "pending" and pending["order"] == ["Hero"]

    router = CommandRouter(services)
    started = await router.dispatch(ctx, ".combat start")
    assert started is not None and "active" in started
    advanced = await tracker.initiative_tracker(ctx, action="next")
    assert "Hero" in advanced
    current = json.loads(await services.store.state_get(room, "combat_state") or "{}")
    assert current["phase"] == "active" and current["round"] == 2

    await tracker.initiative_tracker(ctx, action="clear")
    ended = json.loads(await services.store.state_get(room, "combat_state") or "{}")
    assert ended["phase"] == "ended"


@pytest.mark.asyncio
async def test_director_claims_only_the_current_companion_turn(monkeypatch) -> None:
    from agent.npc import companion_uid, create_companion
    from core.combat import CombatManager, create_combat, start_combat
    from gateway import director
    from gateway.hub import RoomHub

    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(16))
    room = "cli:dm:runtime-director"
    ctx = AgentCtx(chat_key=room, user_id="keeper", platform="cli", locale="en")
    companion = await create_companion(services.documents, room, "Ada", stat_char="Ada")
    await services.characters.save_character(companion_uid(companion.id), room, CharacterSheet("Ada", "dnd5e"))
    state = start_combat(
        create_combat(
            "encounter",
            {
                "Ada": {
                    "id": "Ada",
                    "name": "Ada",
                    "initiative": 20,
                    "controller": "companion",
                    "controller_id": companion_uid(companion.id),
                },
                "Hero": {
                    "id": "Hero",
                    "name": "Hero",
                    "initiative": 10,
                    "controller": "human",
                    "controller_id": "u1",
                },
            },
            budget={"action": 1},
        ),
        budget={"action": 1},
    )
    await CombatManager(services.store, room).save(state, expected_raw=None)
    await services.store.state_set(room, "party_auto", "1")
    calls: list[str] = []

    async def fake_companion_turn(*args, **kwargs):
        calls.append(args[2].name)
        return None

    monkeypatch.setattr(director, "run_companion_turn", fake_companion_turn)
    result = await director.run_director(
        RoomHub(),
        services,
        ctx,
        command_router=CommandRouter(services),
    )
    assert result == [(companion.id, None)]
    assert calls == ["Ada"]
    finished = await CombatManager(services.store, room).get()
    assert finished is not None and finished.current == "Hero" and finished.claim is None
