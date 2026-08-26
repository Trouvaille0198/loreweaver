"""The keeper-side settlement proposal tool: keeper-only, propose-only, never auto.

The tool must (a) be offered in the ordinary Keeper toolset — play-phase included,
since a settlement is proposed exactly when play winds down, (b) be keeper-only, and
(c) change nothing: the proposal lands only via `.settle apply`.
"""

from __future__ import annotations

import json

from agent.context import AgentCtx
from agent.kp_tools import build_kp_toolset
from agent.services import build_services
from agent.settle import SETTLE_PENDING_KEY, load_pending
from core.character_manager import CharacterSheet
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import ChatResult, FakeLLM

ROOM = "tui:group:kp-settle"

PROPOSAL = json.dumps(
    {
        "characters": [
            {
                "name": "Vera",
                "growth": ["侦查"],
                "attribute_changes": [],
                "memory_fold": "Vera uncovered the sunken bell's secret.",
                "background": None,
                "keeper_note": "",
            }
        ]
    }
)


def _toolset(llm: FakeLLM | None = None):
    services = build_services(Settings(), llm=llm or FakeLLM(script=[]), embeddings=FakeEmbeddings(64))
    return services, build_kp_toolset(services)


def _ctx() -> AgentCtx:
    return AgentCtx(chat_key=ROOM, user_id="kp", platform="cli", locale="en")


async def _make_character(services) -> None:
    await services.characters.save_character("u1", ROOM, CharacterSheet("Vera", "coc7"))


def test_propose_settlement_is_keeper_only_and_play_phase():
    _, toolset = _toolset()
    assert toolset.is_keeper_only("propose_settlement") is True
    entry = toolset._entries["propose_settlement"]  # noqa: SLF001 — test access to the meta
    assert entry.meta.prep_only is False, "a settlement is proposed when play winds down"


async def test_propose_settlement_proposes_and_stores_pending_without_changing_sheets():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services, toolset = _toolset(llm)
    await _make_character(services)

    reply = await toolset.dispatch("propose_settlement", _ctx(), {})

    assert "Vera" in reply
    assert "侦查" in reply
    pending = await load_pending(services, ROOM)
    assert pending is not None
    assert pending.characters[0].name == "Vera"
    # Propose-only: the sheet is untouched.
    sheet = await services.characters.get_character("u1", ROOM)
    assert sheet.background is None or sheet.background == ""


async def test_propose_settlement_reports_no_data_without_sheets():
    services, toolset = _toolset()
    i18n = services.i18n.with_locale("en")
    reply = await toolset.dispatch("propose_settlement", _ctx(), {})
    assert reply == i18n.t("commands.settle.no_data")
    assert await services.store.state_get(ROOM, SETTLE_PENDING_KEY) is None


async def test_propose_settlement_reports_failure_when_the_model_produces_no_proposal():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content="not json", tool_calls=[]))
    services, toolset = _toolset(llm)
    await _make_character(services)
    i18n = services.i18n.with_locale("en")

    reply = await toolset.dispatch("propose_settlement", _ctx(), {})

    assert reply == i18n.t("commands.settle.failed")
    assert await services.store.state_get(ROOM, SETTLE_PENDING_KEY) is None
