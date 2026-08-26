"""The `.share` command oracle: publish a player-facing module link.

The keeper triggers it; the room sees ONE system line carrying the link, and
`state.module_share` (every member's state frame) carries the public face so
any member opening the link renders the page without an admin round trip.
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


def _ctx(user_id: str, role: str) -> AgentCtx:
    return AgentCtx(
        chat_key=ROOM,
        user_id=user_id,
        platform="tui",
        locale="zh",
        extra={"role": role},
    )


async def _seed_module(services) -> None:
    await services.documents.put(
        ROOM,
        "module_brief",
        "snake-ferry",
        {"name": "蛇渡诡祭", "description": "清末光绪年间，一群飘零南洋的中国人卷入祭典。"},
    )


async def test_share_is_keeper_only():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("zh")

    reply = await router.dispatch(_ctx("p1", "player"), ".share")

    assert reply == i18n.t("rooms.denied")


async def test_share_without_an_active_module_is_a_notice():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("zh")

    reply = await router.dispatch(_ctx("kp", "keeper"), ".share")

    assert reply == i18n.t("commands.share.no_module")


async def test_share_publishes_the_link_and_persists_the_public_face():
    services = _services()
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("zh")
    await _seed_module(services)

    reply = await router.dispatch(_ctx("kp", "keeper"), ".share")

    assert reply is None, "the share line itself is the whole reply"
    assert len(hub.events) == 1
    _, event, _, _ = hub.events[0]
    assert event.kind == "system"
    assert event.text == i18n.t(
        "commands.share.done", name="蛇渡诡祭", url="/#/module-share/%E8%9B%87%E6%B8%A1%E8%AF%A1%E7%A5%AD"
    )
    # The public face persists for every member's state frame.
    raw = await services.store.state_get(ROOM, "module_share")
    assert raw is not None
    data = json.loads(raw)
    assert data["name"] == "蛇渡诡祭"
    assert "祭典" in data["description"]
