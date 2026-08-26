"""The player-facing `.hint` command enters the normal Keeper turn lane."""

from __future__ import annotations

from agent.context import AgentCtx
from agent.history import DEFAULT_HISTORY_KEY, load_chain
from agent.kp_tools import build_kp_toolset
from agent.services import build_services
from gateway.commands import CommandRouter
from gateway.hub import Event, RoomHub
from gateway.turn import run_turn
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text


class _Member:
    transport = "tui"
    locale = "en"

    def __init__(self, member_id: str = "p1") -> None:
        self.id = member_id
        self.user_key = f"user:{member_id}"
        self.name = member_id
        self.events: list[Event] = []

    async def deliver(self, event: Event) -> None:
        self.events.append(event)


def _ctx(room: str = "tui:group:hint") -> AgentCtx:
    return AgentCtx(chat_key=room, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


async def test_hint_alias_prepares_a_localized_keeper_turn_request() -> None:
    services = build_services(Settings(locale="zh"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(8))
    router = CommandRouter(services)
    ctx = AgentCtx(
        chat_key="tui:group:hint-zh",
        user_id="p1",
        platform="tui",
        locale="zh",
        extra={"role": "player"},
    )

    reply = await router.dispatch_reply(ctx, "。提示 封死的钟楼门")

    assert reply is not None
    assert reply.text is None
    assert reply.turn_message is not None
    assert "封死的钟楼门" in reply.turn_message
    assert "不剧透" in reply.turn_message


async def test_hint_runs_as_a_real_keeper_turn_and_reaches_the_whole_table() -> None:
    room = "tui:group:hint-e2e"
    llm = FakeLLM(script=[assistant_text("The salt-stained ledger points to the old lighthouse." )])
    services = build_services(Settings(locale="en"), llm=llm, embeddings=FakeEmbeddings(8))
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)
    hub = RoomHub()
    member = _Member()
    await hub.subscribe(room, member)

    result = await run_turn(
        hub,
        services,
        _ctx(room),
        ".hint the sealed lighthouse door",
        command_router=router,
        toolset=toolset,
    )

    assert result is not None
    kp_lines = [event for event in member.events if event.kind == "narrative" and event.speaker == "kp"]
    assert [event.text for event in kp_lines] == ["The salt-stained ledger points to the old lighthouse."]
    assert not any(event.kind == "player_action" for event in member.events)

    sent_messages = llm.calls[0][0]
    prompt = sent_messages[-1]["content"]
    assert "Player focus: the sealed lighthouse door" in prompt
    assert "spoiler-safe hint" in prompt
    assert ".hint the sealed lighthouse door" not in prompt

    chain = await load_chain(services, room, DEFAULT_HISTORY_KEY)
    user_lines = [message["content"] for message in chain if message["role"] == "user"]
    assert len(user_lines) == 1
    assert "the sealed lighthouse door" in user_lines[0]
