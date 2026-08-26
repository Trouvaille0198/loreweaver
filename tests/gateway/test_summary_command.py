"""The `.summary` command oracle: a keeper-only, non-blocking LLM campaign recap.

Written in the style of `test_image_command.py`: the model call runs in a
tracked background task OUTSIDE the room's turn lock, the command returns a
"started" notice immediately, and the recap lands as a room system message.
The DoD surfaces: players are denied; the recap is assembled ONLY from player
projections (keeper annotations cannot reach the prompt or the reply); the
empty-room notice and failures surface as system messages without blocking.
"""

from __future__ import annotations

import asyncio

from agent.chronicle import CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID, CHRONICLE_DOC_TYPE
from agent.context import AgentCtx
from agent.history import DEFAULT_HISTORY_KEY, append_turn
from agent.services import build_services
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text

ROOM = "tui:group:room1"
SENTINEL = "THE SUNKEN BELL MUST NEVER RING"
RECAP = (
    "Where we are: the party is searching the flooded chapel. "
    "Story so far: the bell ringer was freed."
)


class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, session_key, event, *, exclude=None):
        self.events.append((session_key, event, exclude))


def _services(*, llm: FakeLLM | None = None):
    return build_services(
        Settings(), llm=llm or FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )


def _keeper_ctx() -> AgentCtx:
    return AgentCtx(chat_key=ROOM, user_id="kp", platform="cli", locale="en")


def _player_ctx() -> AgentCtx:
    return AgentCtx(chat_key=ROOM, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


async def _seed_material(services) -> None:
    await services.documents.put(
        ROOM,
        CAMPAIGN_SUMMARY_DOC_TYPE,
        CAMPAIGN_SUMMARY_ID,
        {
            "text": "Previously: the party freed the bell ringer.",
            "keeper": SENTINEL,
            "through_turn": 40,
            "fold_count": 2,
        },
    )
    await services.documents.put(
        ROOM,
        CHRONICLE_DOC_TYPE,
        "c00041",
        {
            "text": "They camped by the pier.",
            "keeper": SENTINEL,
            "turn": 41,
            "pcs": [],
            "scene": "",
            "folded": False,
            "tokens": 30,
        },
    )
    await append_turn(
        services,
        ROOM,
        DEFAULT_HISTORY_KEY,
        user_message="Let us search the chapel.",
        reply="The chapel door groans open.",
        turn=42,
    )


async def _settle(router: CommandRouter) -> None:
    """Wait for the dispatch's background `.summary` task to finish.

    FakeLLM completes without real IO, so the tracked-task set drains within a
    few event-loop ticks; poll it instead of assuming timing.
    """
    tasks = getattr(router, "_summary_background_tasks", None)
    for _ in range(200):
        if not tasks:
            return
        await asyncio.sleep(0)
    raise AssertionError("background summary task did not settle")


async def test_summary_is_keeper_only():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_player_ctx(), ".summary")

    assert reply == i18n.t("commands.summary.denied")
    assert not services.llm.calls, "a denied player must not trigger a model call"


async def test_summary_returns_started_and_publishes_the_recap_as_a_system_message():
    llm = FakeLLM(script=[assistant_text(RECAP)])
    services = _services(llm=llm)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    await _seed_material(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_keeper_ctx(), ".summary")

    assert reply == i18n.t("commands.summary.started"), "the command never blocks"
    await _settle(router)

    system_events = [event for _, event, _ in hub.events if event.kind == "system"]
    assert system_events[0].text == i18n.t("commands.summary.generating"), "spinner line first"
    assert system_events[-1].text == RECAP, "the recap lands as a room system message"

    assert len(llm.calls) == 1
    prompt = "\n".join(str(message.get("content", "")) for message in llm.calls[0][0])
    assert "freed the bell ringer" in prompt, "the rolling summary feeds the recap"
    assert "camped by the pier" in prompt, "the chronicle tail feeds the recap"
    assert "search the chapel" in prompt, "the conversation tail feeds the recap"
    assert SENTINEL not in prompt, "keeper annotations never reach the model"
    assert SENTINEL not in system_events[-1].text, "the reply is player-safe too"


async def test_summary_empty_room_publishes_the_empty_notice():
    services = _services()
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")

    await router.dispatch(_keeper_ctx(), ".summary")
    await _settle(router)

    system_events = [event for _, event, _ in hub.events if event.kind == "system"]
    assert system_events[-1].text == i18n.t("commands.summary.empty")
    assert not services.llm.calls, "no material means no model call"


async def test_summary_publishes_the_failed_notice_when_the_model_errors():
    def _boom(messages, tools):
        raise RuntimeError("provider down")

    services = _services(llm=FakeLLM(responder=_boom))
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")
    await _seed_material(services)

    await router.dispatch(_keeper_ctx(), ".summary")
    await _settle(router)

    system_events = [event for _, event, _ in hub.events if event.kind == "system"]
    assert system_events[-1].text == i18n.t("commands.summary.failed")
