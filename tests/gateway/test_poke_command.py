"""The `.poke` command oracle: a broadcast "poke" with an optional target nudge.

Any member may poke a party member. The handler publishes ONE system event
carrying the poke metadata (actor/target identities for the browser nudge) and
returns no separate reply, so the room sees exactly one line and the handler's
reply never double-broadcasts. The fan-out is fire-and-forget so the room's
turn lock is never held on slow members, hence the `asyncio.sleep` drains.
"""

from __future__ import annotations

import asyncio
import json

from agent.context import AgentCtx
from agent.services import build_services
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

ROOM = "tui:group:room1"


class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, session_key, event, *, exclude=None, only_user=None, exclude_user=None):
        self.events.append((session_key, event, exclude, only_user))


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _ctx(user_id: str = "p1") -> AgentCtx:
    return AgentCtx(
        chat_key=ROOM,
        user_id=user_id,
        platform="tui",
        locale="en",
        extra={"role": "player"},
    )


async def _seed_party(services, *, claimed_by: str = "Player Two", owner: str = "") -> None:
    roster = {
        "陈武": {"name": "陈武", "system": "coc7", "resources": []},
        "文秀": {"name": "文秀", "system": "coc7", "resources": []},
    }
    await services.store.state_set(ROOM, "party_roster", json.dumps(roster))
    await services.documents.put(
        ROOM, "pregen", "chen-wu", {"name": "陈武", "claimed_by": claimed_by}
    )
    await services.documents.put(
        ROOM, "sheet", "陈武", {"name": "陈武", "owner": owner or "u2", "system": "coc7"}
    )
    # The poking player's own active character.
    await services.store.state_set(ROOM, "active_character.p1", "阿理")
    await services.documents.put(
        ROOM, "sheet", "阿理", {"name": "阿理", "owner": "p1", "system": "coc7"}
    )


async def test_poke_requires_a_target():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_ctx(), ".poke")

    assert reply == i18n.t("commands.poke.usage")


async def test_poke_unknown_member_is_a_localized_notice():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")
    await _seed_party(services)

    reply = await router.dispatch(_ctx(), ".poke 不存在")

    assert reply == i18n.t("commands.poke.unknown", name="不存在")


async def test_poke_broadcasts_one_event_with_nudge_metadata_and_no_reply():
    services = _services()
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")
    await _seed_party(services, claimed_by="Player Two", owner="u2")

    reply = await router.dispatch(_ctx(), ".poke 陈武")
    await asyncio.sleep(0.05)  # the poke fan-out is fire-and-forget

    assert reply is None, "the poke itself is the only line — no separate reply"
    assert len(hub.events) == 1
    _, event, _, _ = hub.events[0]
    assert event.kind == "system"
    assert event.text == i18n.t("commands.poke.done", actor="阿理", target="陈武")
    poke = event.data["poke"]
    assert poke["actor"] == "阿理"
    assert poke["actor_user"] == "p1"
    assert poke["target"] == "陈武"
    assert poke["target_name"] == "Player Two", "pregen claim names the player for the nudge"
    assert poke["target_user"] == "u2", "sheet owner names the uid for the nudge"


async def test_poke_self_uses_the_self_text():
    services = _services()
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")
    await _seed_party(services, claimed_by="Player One", owner="p1")
    # Make the poking player's active character the target so the self branch fires.
    await services.store.state_set(ROOM, "active_character.p1", "陈武")

    await router.dispatch(_ctx(), ".poke 陈武")
    await asyncio.sleep(0.05)  # the poke fan-out is fire-and-forget

    _, event, _, _ = hub.events[0]
    assert event.text == i18n.t("commands.poke.self", actor="陈武")


async def test_poke_turn_never_falls_through_to_the_ai_keeper():
    """A silent command turn (`.poke` returns no reply text — its event IS the
    whole turn) must not re-enter the KP pipeline with the raw command line.
    Regression: `gateway.turn.run_turn` used to treat a matched-but-replyless
    command as "nothing happened" and run an AI round on ".poke <name>".
    A scriptless FakeLLM makes any AI call raise, so the assert below doubles
    as the proof that no KP consultation happened."""
    from agent.kp_tools import build_kp_toolset
    from gateway.hub import RoomHub
    from gateway.turn import run_turn

    llm = FakeLLM(script=[])
    services = build_services(Settings(), llm=llm, embeddings=FakeEmbeddings(64))
    hub = RoomHub()
    router = CommandRouter(services, hub=hub)
    await _seed_party(services, claimed_by="Player Two", owner="u2")

    result = await run_turn(
        hub,
        services,
        _ctx(),
        ".poke 陈武",
        command_router=router,
        toolset=build_kp_toolset(services),
    )

    assert result is None, "a command turn yields no KP result"
    assert not llm.calls, "the AI Keeper must never be consulted for a poke"
