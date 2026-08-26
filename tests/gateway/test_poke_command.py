"""The `.poke` command oracle: a broadcast "poke" with an optional target nudge.

Any member may poke a party member. The handler publishes ONE system event
carrying the poke metadata (actor/target identities for the browser nudge) and
returns no separate reply, so the room sees exactly one line and the handler's
reply never double-broadcasts.
"""

from __future__ import annotations

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
    # The poking player's own active character is 阿理; poke their own 阿理? No —
    # make the actor's active character the target so the self branch fires.
    await services.store.state_set(ROOM, "active_character.p1", "陈武")

    await router.dispatch(_ctx(), ".poke 陈武")

    _, event, _, _ = hub.events[0]
    assert event.text == i18n.t("commands.poke.self", actor="陈武")
