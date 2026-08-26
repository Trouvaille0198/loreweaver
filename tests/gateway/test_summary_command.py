"""The `.summary` command oracle: a player-safe, LLM-generated campaign recap.

Written in the same style as `test_chronicle_commands.py`. The DoD surfaces: an
empty room yields a localized notice WITHOUT calling the model; a room with
material hands ONLY player projections to the authoring LLM and renders its
recap; keeper annotations cannot reach the prompt or the reply.
"""

from __future__ import annotations

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


async def test_summary_empty_room_gives_a_localized_notice_without_calling_the_model():
    services = _services()
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_player_ctx(), ".summary")

    assert reply == i18n.t("commands.summary.empty")
    assert not services.llm.calls, "no material means no model call"


async def test_summary_hands_only_player_projections_to_the_llm_and_renders_its_recap():
    llm = FakeLLM(script=[assistant_text(RECAP)])
    services = _services(llm=llm)
    router = CommandRouter(services)
    await _seed_material(services)

    reply = await router.dispatch(_player_ctx(), ".summary")

    assert RECAP in reply, "the model's recap is the reply"
    assert len(llm.calls) == 1
    prompt = "\n".join(str(message.get("content", "")) for message in llm.calls[0][0])
    assert "freed the bell ringer" in prompt, "the rolling summary feeds the recap"
    assert "camped by the pier" in prompt, "the chronicle tail feeds the recap"
    assert "search the chapel" in prompt, "the conversation tail feeds the recap"
    assert SENTINEL not in prompt, "keeper annotations never reach the model"
    assert SENTINEL not in reply, "the reply is player-safe too"


async def test_summary_works_for_the_keeper_and_accepts_the_chinese_verb():
    llm = FakeLLM(script=[assistant_text(RECAP), assistant_text(RECAP)])
    services = _services(llm=llm)
    router = CommandRouter(services)
    await _seed_material(services)

    keeper_reply = await router.dispatch(_keeper_ctx(), ".summary")
    assert RECAP in keeper_reply

    zh_reply = await router.dispatch(_player_ctx(), ".概括")
    assert RECAP in zh_reply


async def test_summary_reports_a_failed_generation():
    def _boom(messages, tools):
        raise RuntimeError("provider down")

    services = _services(llm=FakeLLM(responder=_boom))
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")
    await _seed_material(services)

    reply = await router.dispatch(_player_ctx(), ".summary")

    assert reply == i18n.t("commands.summary.failed")
