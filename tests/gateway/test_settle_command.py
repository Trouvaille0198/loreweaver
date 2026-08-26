"""The `.settle` / `.mem` command surface: the keeper-only two-step ritual and the
player-facing memory reader. Written in the style of `test_summary_command.py`.
"""

from __future__ import annotations

import json

from agent.context import AgentCtx
from agent.services import build_services
from agent.settle import SETTLE_PENDING_KEY
from core.character_manager import CharacterSheet
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import ChatResult, FakeLLM

ROOM = "tui:group:settle-cmd"

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


def _services(llm: FakeLLM | None = None):
    return build_services(Settings(), llm=llm or FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _keeper_ctx() -> AgentCtx:
    return AgentCtx(chat_key=ROOM, user_id="kp", platform="cli", locale="en")

class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, session_key, event, *, exclude=None, only_user=None, exclude_user=None):
        self.events.append((session_key, event, exclude, only_user))


def _player_ctx() -> AgentCtx:
    return AgentCtx(chat_key=ROOM, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


async def _make_character(services) -> None:
    await services.characters.save_character("u1", ROOM, CharacterSheet("Vera", "coc7"))


async def test_settle_is_keeper_only():
    services = _services()
    router = CommandRouter(services)
    reply = await router.dispatch(_player_ctx(), ".settle")
    # The GROUP_ADMIN gate fires at the router before the handler's own check.
    assert "Keeper" in reply and "command" in reply
    assert not services.llm.calls

async def test_settle_broadcasts_an_in_progress_notice_while_analyzing():
    """The analysis call is slow; the room must see it is running instead of silence."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")

    await router.dispatch(_keeper_ctx(), ".settle")

    assert any(
        event.kind == "system" and event.text == i18n.t("commands.settle.generating")
        for _, event, _, _ in hub.events
    )


async def test_settle_retires_the_in_progress_spinner_when_done():
    """The notice must not spin forever: a same-text spinner:false frame retires it."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    i18n = services.i18n.with_locale("en")

    await router.dispatch(_keeper_ctx(), ".settle")

    text = i18n.t("commands.settle.generating")
    start = [e for _, e, _, _ in hub.events if e.kind == "system" and e.text == text and e.data.get("spinner") is True]
    stop = [e for _, e, _, _ in hub.events if e.kind == "system" and e.text == text and e.data.get("spinner") is False]
    assert start and stop, f"spinner must be started and retired, got {[(e.text, e.data) for _, e, _, _ in hub.events]}"


async def test_settle_generates_a_proposal_and_stores_it_pending():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)

    reply = await router.dispatch(_keeper_ctx(), ".settle")

    # The proposal went out as an ordinary room message: no command reply line,
    # one broadcast narrative, persisted in the chat log (a refresh keeps it).
    assert reply is None
    assert any(
        event.kind == "narrative" and event.speaker == "system" and "Vera" in event.text
        for _, event, _, _ in hub.events
    )
    from agent.history import DEFAULT_HISTORY_KEY, load_chain

    chain = await load_chain(services, ROOM, DEFAULT_HISTORY_KEY)
    assert any("Vera" in str(message.get("content") or "") for message in chain)
    raw = await services.store.state_get(ROOM, SETTLE_PENDING_KEY)
    assert raw is not None and "Vera" in raw
    # Nothing changed yet: the sheet is untouched and no memory was folded.
    sheet = await services.characters.get_character("u1", ROOM)
    assert sheet.background is None or sheet.background == ""


async def test_settle_proposal_is_broadcast_not_private():
    """The settlement is table news: the proposal goes to every seat and lands in
    the chat log, so a refresh keeps it."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)

    await router.dispatch(_keeper_ctx(), ".settle")

    assert any(
        event.kind == "narrative" and event.speaker == "system" and "Vera" in event.text
        for _, event, _, _ in hub.events
    )


async def test_settle_apply_lands_the_pending_proposal():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)

    await router.dispatch(_keeper_ctx(), ".settle")
    reply = await router.dispatch(_keeper_ctx(), ".settle apply")

    assert reply is None
    assert any(
        event.kind == "narrative" and event.speaker == "system" and ("Growth" in event.text or "侦查" in event.text)
        for _, event, _, _ in hub.events
    )
    assert await services.store.state_get(ROOM, SETTLE_PENDING_KEY) is None  # consumed


async def test_settle_apply_broadcasts_the_outcome_to_the_whole_room():
    """The result is table news — every seat sees what the characters earned."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)

    await router.dispatch(_keeper_ctx(), ".settle")
    await router.dispatch(_keeper_ctx(), ".settle apply")

    assert any(
        event.kind == "narrative" and event.speaker == "system" and ("Growth" in event.text or "侦查" in event.text)
        for _, event, _, _ in hub.events
    )




async def test_settle_replays_an_existing_pending_proposal_without_reanalyzing():
    """A private reply is gone after a page refresh, so `.settle` must re-show the
    stored proposal instead of silently regenerating (which would replace it)."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    await router.dispatch(_keeper_ctx(), ".settle")
    calls_before = len(llm.calls)
    reply = await router.dispatch(_keeper_ctx(), ".settle")

    assert "Vera" in reply
    assert i18n.t("settle.proposal.header") in reply
    assert len(llm.calls) == calls_before, "re-showing a pending proposal must not call the model"
    raw = await services.store.state_get(ROOM, SETTLE_PENDING_KEY)
    assert raw is not None


async def test_settle_cancel_discards_the_pending_proposal():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    router = CommandRouter(services)

    await router.dispatch(_keeper_ctx(), ".settle")
    reply = await router.dispatch(_keeper_ctx(), ".settle cancel")

    assert reply == services.i18n.with_locale("en").t("commands.settle.cancelled")
    assert await services.store.state_get(ROOM, SETTLE_PENDING_KEY) is None




async def test_settle_distinguishes_a_failed_analysis_from_an_empty_table():
    """A table WITH sheets whose model produced no proposal must not read as
    "no characters at this table" — the real-process bug the CLI check caught."""
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content="not json", tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_keeper_ctx(), ".settle")

    assert reply == i18n.t("commands.settle.failed")
    assert await services.store.state_get(ROOM, SETTLE_PENDING_KEY) is None


async def test_mem_shows_a_characters_memory_and_reports_empty_without_one():
    services = _services()
    await _make_character(services)
    from core.character_memory import CHARACTER_MEMORY_DOC_TYPE

    await services.documents.put(
        ROOM,
        CHARACTER_MEMORY_DOC_TYPE,
        "Vera",
        {"entries": [{"text": "she found the ledger", "turn": 3}], "summary": "life so far", "keeper": ""},
    )
    router = CommandRouter(services)
    i18n = services.i18n.with_locale("en")

    reply = await router.dispatch(_player_ctx(), ".mem Vera")
    assert "she found the ledger" in reply
    # The retired folded life-summary is not shown anymore.
    assert "life so far" not in reply
    assert "secret" not in reply  # the keeper margin never reaches a player

    empty = await router.dispatch(_player_ctx(), ".mem")
    assert empty == i18n.t("commands.mem.usage")  # no active character


async def test_st_shows_the_characters_backstory():
    """The backstory is part of the card: `.st` renders it for the player, not
    only in the keeper's roster line."""
    services = _services()
    sheet = CharacterSheet("Vera", "coc7")
    sheet.background = "A Chaozhou clerk from a family of scribes."
    await services.characters.save_character("p1", ROOM, sheet)
    router = CommandRouter(services)

    reply = await router.dispatch(_player_ctx(), ".st")

    assert "Chaozhou clerk" in reply
