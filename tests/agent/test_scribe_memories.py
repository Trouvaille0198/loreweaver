"""The Scribe's character-memory lane: per-turn PC experience lines at zero extra
model calls, player-grade and name-gated (a memory for a character nobody plays is
a hallucination, not a record)."""

from __future__ import annotations

import json

from agent.context import AgentCtx
from agent.scribe import MAX_MEMORIES, run_scribe
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.character_memory import CHARACTER_MEMORY_DOC_TYPE
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import ChatResult, FakeLLM

CHAT = "mem-room"
MEMORY = "Vera traced the ledger's watermark back to the chapel."


def _services(payload: dict):
    services = build_services(
        Settings(locale="en"),
        llm=FakeLLM(responder=lambda messages, tools: ChatResult(content=json.dumps(payload), tool_calls=[])),
        embeddings=FakeEmbeddings(64),
    )
    services.settings.scribe.enabled = True
    return services


def _ctx() -> AgentCtx:
    return AgentCtx(chat_key=CHAT, user_id="u1", locale="en")


async def _make_character(services, name: str) -> None:
    await services.characters.save_character("u1", CHAT, CharacterSheet(name, "coc7"))


async def test_scribe_writes_a_named_characters_memory_line():
    services = _services(
        {
            "ops": [],
            "whispers": [],
            "chronicle": "",
            "memories": [{"name": "Vera", "text": MEMORY}],
        }
    )
    await _make_character(services, "Vera")

    await run_scribe(services, _ctx(), "I trace the watermark", "Vera finds the ledger's mark.", [], 3)

    doc = await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Vera")
    assert doc is not None
    assert doc.data["entries"] == [{"text": MEMORY, "turn": 3}]
    assert doc.data["summary"] == ""


async def test_memories_for_names_not_at_the_table_are_discarded():
    services = _services(
        {
            "ops": [],
            "whispers": [],
            "chronicle": "",
            "memories": [{"name": "Ghost", "text": MEMORY}],
        }
    )
    await _make_character(services, "Vera")

    await run_scribe(services, _ctx(), "I look around", "Nothing moves.", [], 1)

    assert await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Ghost") is None
    assert await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Vera") is None


async def test_no_memories_field_writes_nothing_and_makes_no_documents():
    services = _services({"ops": [], "whispers": [], "chronicle": "", "memories": []})
    await _make_character(services, "Vera")

    await run_scribe(services, _ctx(), "I rest", "The night passes quietly.", [], 1)

    assert await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Vera") is None


async def test_more_than_the_per_turn_budget_is_truncated():
    services = _services(
        {
            "ops": [],
            "whispers": [],
            "chronicle": "",
            "memories": [
                {"name": "Vera", "text": "first"},
                {"name": "Vera", "text": "second"},
                {"name": "Vera", "text": "third"},
            ],
        }
    )
    await _make_character(services, "Vera")

    await run_scribe(services, _ctx(), "I act", "Vera acts twice over.", [], 2)

    doc = await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Vera")
    assert doc is not None
    assert len(doc.data["entries"]) == MAX_MEMORIES
    assert [entry["text"] for entry in doc.data["entries"]] == ["first", "second"]


async def test_a_line_is_capped_in_length():
    services = _services(
        {
            "ops": [],
            "whispers": [],
            "chronicle": "",
            "memories": [{"name": "Vera", "text": "x" * 5000}],
        }
    )
    await _make_character(services, "Vera")

    await run_scribe(services, _ctx(), "I act", "Vera does something long.", [], 2)

    doc = await services.documents.get(CHAT, CHARACTER_MEMORY_DOC_TYPE, "Vera")
    assert len(doc.data["entries"][0]["text"]) <= 300
