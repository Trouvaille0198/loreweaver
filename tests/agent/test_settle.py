"""The settlement lane: propose (one declared model call) and apply (deterministic).

Offline and reproducible per repo convention: FakeLLM/FakeEmbeddings, seeded dice.
The apply half is the iron-rule-#1 half — the proposal never rolls; the engine does.
"""

from __future__ import annotations

import json

from agent.services import Services, build_services
from agent.settle import (
    AttributeChange,
    CharacterSettlement,
    Settlement,
    apply_settlement,
    build_settlement,
    clear_pending,
    load_pending,
    save_pending,
)
from core.character_manager import CharacterSheet
from core.character_memory import CHARACTER_MEMORY_DOC_TYPE, empty_memory
from core.dice_engine import DiceRoller, seed_dice
from core.rulepacks import load_rulepack
from core.sheets import sheet_value
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import ChatResult, FakeLLM

ROOM = "tui:group:settle-room"

PROPOSAL = json.dumps(
    {
        "characters": [
            {
                "name": "Vera",
                "growth": ["侦查"],
                "attribute_changes": [{"field": "敏捷", "delta": 1}],
                "memory_fold": "Vera uncovered the sunken bell's secret and hardened.",
                "background": "A librarian who survived the chapel.",
                "keeper_note": "grew into the lead investigator",
            },
            {"name": "Ghost", "growth": ["侦查"]},  # not a real sheet — must be filtered
        ]
    }
)


def _services(llm: FakeLLM | None = None) -> Services:
    return build_services(Settings(), llm=llm or FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def _make_character(services, name: str = "Vera", system: str = "coc7") -> None:
    await services.characters.save_character("u1", ROOM, CharacterSheet(name, system))


async def _seed_memory(services, name: str = "Vera") -> None:
    await services.documents.put(ROOM, CHARACTER_MEMORY_DOC_TYPE, name, empty_memory())


async def test_build_settlement_parses_the_proposal_and_filters_unknown_names():
    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content=PROPOSAL, tool_calls=[]))
    services = _services(llm)
    await _make_character(services)

    settlement = await build_settlement(services, ROOM)

    assert settlement is not None
    assert [char.name for char in settlement.characters] == ["Vera"]
    vera = settlement.characters[0]
    assert vera.growth == ("侦查",)
    assert vera.attribute_changes == (AttributeChange(field="敏捷", delta=1),)
    assert "sunken bell" in vera.memory_fold
    assert vera.background == "A librarian who survived the chapel."
    assert vera.keeper_note == "grew into the lead investigator"


async def test_build_settlement_returns_none_when_the_reply_is_garbage_or_room_is_empty():
    services = _services(FakeLLM(script=[]))
    assert await build_settlement(services, ROOM) is None  # no sheets at all

    llm = FakeLLM(responder=lambda messages, tools: ChatResult(content="not json at all", tool_calls=[]))
    services = _services(llm)
    await _make_character(services)
    assert await build_settlement(services, ROOM) is None


async def test_apply_settlement_rolls_the_pack_improvement_check_with_real_dice():
    services = _services()
    await _make_character(services)
    pack = load_rulepack("coc7")
    spec = next(entry for entry in pack.subsystems.values() if entry.template == "improvement_check")

    seed_dice(20260701)
    expected_roll = DiceRoller().roll_expression(spec.roll).total
    seed_dice(20260701)
    result = await apply_settlement(
        services,
        ROOM,
        Settlement(
            characters=(CharacterSettlement(name="Vera", growth=("侦查",)),)
        ),
    )

    outcome = result.outcomes[0]
    assert outcome.skipped == ""
    assert outcome.growth[0].rolled == expected_roll
    # 侦查 starts at 25 (pack-fixed); the outcome reflects the improvement rule:
    # grow only when the roll beats the current value (or the auto-success line).
    sheet = await services.characters.get_character("u1", ROOM)
    grown = outcome.growth[0].gained > 0
    assert sheet_value(sheet, pack, "侦查") == outcome.growth[0].value
    assert (sheet_value(sheet, pack, "侦查") > 25) == grown


async def test_apply_settlement_applies_attribute_changes_and_validation():
    services = _services()
    await _make_character(services)
    pack = load_rulepack("coc7")
    before = sheet_value(
        (await services.characters.get_character("u1", ROOM)), pack, "敏捷"
    )

    seed_dice(11)
    result = await apply_settlement(
        services,
        ROOM,
        Settlement(
            characters=(
                CharacterSettlement(name="Vera", attribute_changes=(AttributeChange(field="敏捷", delta=1),)),
            )
        ),
    )

    assert result.outcomes[0].attributes == (("敏捷", before + 1, True),)
    sheet = await services.characters.get_character("u1", ROOM)
    assert sheet_value(sheet, pack, "敏捷") == before + 1


async def test_apply_settlement_folds_memories_and_updates_background():
    services = _services()
    await _make_character(services)
    await _seed_memory(services)
    await services.documents.put(
        ROOM,
        CHARACTER_MEMORY_DOC_TYPE,
        "Vera",
        {"entries": [{"text": "she found the ledger", "turn": 3}], "summary": "", "keeper": ""},
    )

    result = await apply_settlement(
        services,
        ROOM,
        Settlement(
            characters=(
                CharacterSettlement(
                    name="Vera",
                    memory_fold="Vera uncovered the sunken bell's secret.",
                    background="A librarian who survived the chapel.",
                    keeper_note="she doubts the mayor",
                ),
            )
        ),
    )

    outcome = result.outcomes[0]
    assert outcome.folded and outcome.background
    memory = await services.documents.get(ROOM, CHARACTER_MEMORY_DOC_TYPE, "Vera")
    # The per-turn journal is kept, and ONE playthrough memory is appended —
    # scenario-level, tagged, NOT folded into a rolling summary.
    assert [entry["text"] for entry in memory.data["entries"]] == [
        "she found the ledger",
        "Vera uncovered the sunken bell's secret.",
    ]
    assert memory.data["entries"][1]["kind"] == "playthrough"
    assert "sunken bell" not in memory.data["summary"]
    assert memory.data["keeper"] == "she doubts the mayor"
    sheet = await services.characters.get_character("u1", ROOM)
    assert sheet.background == "A librarian who survived the chapel."


async def test_apply_settlement_skips_unknown_characters_and_no_improvement_systems():
    services = _services()
    await _make_character(services)

    result = await apply_settlement(
        services,
        ROOM,
        Settlement(characters=(CharacterSettlement(name="Nobody", growth=("侦查",)),)),
    )
    assert result.outcomes[0].skipped == "no_such_character"

    # D&D 5e declares no improvement_check subsystem: growth is skipped, not invented.
    await _make_character(services, name="Thorin", system="dnd5e")
    result = await apply_settlement(
        services,
        ROOM,
        Settlement(characters=(CharacterSettlement(name="Thorin", growth=("运动",)),)),
    )
    assert result.outcomes[0].skipped == ""
    assert result.outcomes[0].growth == ()


async def test_pending_proposal_roundtrips_and_clears():
    services = _services()
    proposal = Settlement(
        characters=(CharacterSettlement(name="Vera", growth=("侦查",), memory_fold="arc"),)
    )
    await save_pending(services, ROOM, proposal)
    loaded = await load_pending(services, ROOM)
    assert loaded == proposal
    await clear_pending(services, ROOM)
    assert await load_pending(services, ROOM) is None


async def test_build_settlement_includes_the_folded_campaign_summary():
    """The settlement reads the rolling "story so far" — the folded summary that
    keeps every pivotal fact — not only the recent raw chronicle tail."""
    seen: list[str] = []

    def responder(messages, tools):
        seen.append(str(messages[-1]["content"]))
        return ChatResult(content=PROPOSAL, tool_calls=[])

    llm = FakeLLM(responder=responder)
    services = _services(llm)
    await _make_character(services)
    from core.chronicle import CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID, CHRONICLE_DOC_TYPE

    await services.documents.put(
        ROOM, CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID,
        {"text": "The party traced the ledger to the chapel and learned the bell's secret.", "keeper": "hidden", "through_turn": 40, "fold_count": 2},
    )
    await services.documents.put(
        ROOM, CHRONICLE_DOC_TYPE, "c1", {"text": "They camped by the pier.", "keeper": "hidden", "turn": 41},
    )

    settlement = await build_settlement(services, ROOM)

    assert settlement is not None
    assert "Story so far" in seen[0]
    assert "chapel" in seen[0]
    assert "camped by the pier" in seen[0]
    assert "hidden" not in seen[0], "keeper annotations must never reach the settlement"


async def test_build_settlement_shows_the_old_backstory_so_it_is_extended_not_replaced():
    """The new backstory must ABSORB the old one — the model needs to see the
    sheet's existing background or it would rewrite the character from scratch."""
    seen: list[str] = []

    def responder(messages, tools):
        seen.append(str(messages[-1]["content"]))
        return ChatResult(content=PROPOSAL, tool_calls=[])

    services = _services(FakeLLM(responder=responder))
    sheet = CharacterSheet("Vera", "coc7")
    sheet.background = "A Chaozhou clerk from a family of three generations of scribes."
    await services.characters.save_character("u1", ROOM, sheet)

    settlement = await build_settlement(services, ROOM)

    assert settlement is not None
    assert "Chaozhou clerk" in seen[0]
