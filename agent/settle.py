"""The settlement lane — the post-campaign ritual (M24).

One-shot, keeper-triggered, LOW-frequency: this lane runs when a scenario ends
(`.settle`), never per turn, so it does not touch the per-turn model budget.
It reads the room's process data — every visible skill check across current
and archived sessions, the campaign chronicle, each PC's character memory and
current sheet — and proposes, per character:

- **growth** — which skills earned an improvement check (exercised, pushed
  through failure, critical where it mattered);
- **attribute_changes** — small rule-fair deltas the campaign's events justify;
- **memory_fold** — the raw per-turn memory lines folded into one durable
  life-summary paragraph;
- **background** — the character's PERSONA (origin, family, occupation,
  personality, ties) kept stable across settlements; campaign events belong in
  ``memory_fold``, never narrated here.

Deterministic vs generative (iron rule #1): the lane PROPOSES — one declared
model call (`lane_scope("settle")`) — and the engine DISPOSES. `apply_settlement`
runs every growth skill through the pack's `improvement_check` subsystem with
real dice, validates attribute changes through `core.character_rules` exactly
like `.st`, folds memories through `core.character_memory.fold_entries`, and
never rolls, never invents mechanics, never writes a sheet outside the
validated apply path. A proposal is inert until the Keeper applies it: the
command writes it to `settle_pending` and the apply step is a separate,
explicit call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.chronicle import chronicle_turn
from agent.module_lifecycle import active_module as room_active_module
from agent.services import Services
from core.character_manager import CharacterSheet
from core.character_memory import CHARACTER_MEMORY_DOC_TYPE, append_playthrough_entry
from core.character_rules import validate_sheet
from core.rulepacks import load_rulepack
from core.sheets import set_sheet_value, sheet_value
from infra.i18n import I18n
from infra.model_call_trace import lane_scope
from infra.room_facets import STORAGE_DOCUMENTS, STORAGE_ROOM_STATE, RoomStateFacet

logger = logging.getLogger(__name__)

# Where the pending (generated, not yet applied) proposal lives, per room.
SETTLE_PENDING_KEY = "settle_pending"

# Budgets on one proposal — conservative by design.
_MAX_GROWTH_PER_CHAR = 3
_MAX_ATTRIBUTE_CHANGES_PER_CHAR = 2
_MAX_MEMORY_FOLD_CHARS = 600
_MAX_BACKGROUND_CHARS = 800
_MAX_KEEPER_NOTE_CHARS = 400
# Input budgets for the proposal prompt.
_MAX_CHARS = 120
_MAX_MEMORY_ENTRIES_SHOWN = 30
_MAX_CHRONICLE_SHOWN = 30
_MAX_STORY_CHARS = 1_500

_SYS_PROMPT = """You are the settlement clerk for a TTRPG engine. The campaign has ended; produce the post-campaign settlement for each player character.

From the evidence below — the skill checks each character actually attempted (with outcomes), the campaign chronicle, each character's memory log, and their current sheets — decide for EACH character:

1. "growth": the skills this character EARNED an improvement check on — genuinely exercised this campaign: attempted repeatedly, pushed through failure, or landed a critical when it mattered. At most 3 per character. Names must be skills on that character's sheet (or its aliases).
2. "attribute_changes": a small, rule-fair attribute delta the campaign's events justify (training, injury, revelation, horror) — at most 2 per character, delta typically ±1..±2. Names must be attributes on the sheet. NEVER touch HP/SAN/MP — those are resources, not growth.
3. "memory_fold": this character's PLAYTHROUGH memory — ONE self-contained paragraph (max 600 chars) that reads as a durable MEMORY, not a story excerpt: it must make sense to someone with no context of this scenario. Open with the character's name and the scenario; then state the main things they went through and did, and how it ended. Written in the language of the scenario. NEVER use context-dependent phrasing ("this time", "no longer", "he became") that only means something inside the scenario; no pronouns for scenario-only people without identifying them; no fragments.
4. "background": the character's PERSONA — origin, family, occupation, personality, ties. KEEP the sheet's existing backstory traits, extending them only with durable identity facts (a lasting scar, a new occupation, a permanent bond). Do NOT narrate the campaign's plot — the story goes into memory_fold, and replaying it here pollutes the persona. At most ONE closing sentence on where the character stands now. Null when unchanged, max 400 chars.
5. "keeper_note": a keeper-only note about the character's growth (max 400 chars), or "".

Rules:
- You propose; the engine rolls dice and validates. Never decide dice outcomes, never invent mechanics.
- Be conservative: a character who never touched a skill earns nothing.
- A character with no memory entries still gets a memory_fold from the checks and chronicle; empty when they were not part of the scenario.
- "name" must match a sheet below exactly.
- Output ONLY a JSON object:
{{"characters": [{{"name": "<exact sheet name>", "growth": ["<skill>"], "attribute_changes": [{{"field": "<attribute>", "delta": <int>}}], "memory_fold": "<paragraph>", "background": "<text>" or null, "keeper_note": "<text>"}}]}}"""


@dataclass(frozen=True)
class AttributeChange:
    field: str
    delta: int


@dataclass(frozen=True)
class CharacterSettlement:
    name: str
    growth: tuple[str, ...] = ()
    attribute_changes: tuple[AttributeChange, ...] = ()
    memory_fold: str = ""
    background: str | None = None
    keeper_note: str = ""


@dataclass(frozen=True)
class Settlement:
    characters: tuple[CharacterSettlement, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "characters": [
                {
                    "name": char.name,
                    "growth": list(char.growth),
                    "attribute_changes": [
                        {"field": change.field, "delta": change.delta} for change in char.attribute_changes
                    ],
                    "memory_fold": char.memory_fold,
                    "background": char.background,
                    "keeper_note": char.keeper_note,
                }
                for char in self.characters
            ]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settlement:
        characters: list[CharacterSettlement] = []
        for item in data.get("characters") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            growth = tuple(
                str(skill).strip() for skill in item.get("growth") or [] if str(skill).strip()
            )[:_MAX_GROWTH_PER_CHAR]
            changes = tuple(
                AttributeChange(field=str(change.get("field") or "").strip(), delta=int(change.get("delta") or 0))
                for change in item.get("attribute_changes") or []
                if isinstance(change, dict) and str(change.get("field") or "").strip()
            )[:_MAX_ATTRIBUTE_CHANGES_PER_CHAR]
            memory_fold = str(item.get("memory_fold") or "").strip()[:_MAX_MEMORY_FOLD_CHARS]
            background = item.get("background")
            background = str(background).strip()[:_MAX_BACKGROUND_CHARS] if isinstance(background, str) else None
            keeper_note = str(item.get("keeper_note") or "").strip()[:_MAX_KEEPER_NOTE_CHARS]
            characters.append(
                CharacterSettlement(
                    name=name,
                    growth=growth,
                    attribute_changes=changes,
                    memory_fold=memory_fold,
                    background=background,
                    keeper_note=keeper_note,
                )
            )
        return cls(characters=tuple(characters))


@dataclass(frozen=True)
class GrowthResult:
    skill: str
    rolled: int
    gained: int
    value: int


@dataclass(frozen=True)
class ApplyOutcome:
    """What `apply_settlement` did to one character. ``skipped`` is a reason
    (localized upstream) when the character could not be settled at all."""

    name: str
    growth: tuple[GrowthResult, ...] = ()
    attributes: tuple[tuple[str, int, bool], ...] = ()  # (field, new_value, applied)
    folded: bool = False
    background: bool = False
    skipped: str = ""
    notice: str = ""


@dataclass(frozen=True)
class SettlementResult:
    outcomes: tuple[ApplyOutcome, ...] = ()


# ---------------------------------------------------------------------------
# Evidence assembly (deterministic)
# ---------------------------------------------------------------------------


async def _collect_checks(services: Services, chat_key: str) -> list[dict[str, Any]]:
    """Every visible skill check across the archived and current sessions, oldest
    first. Hidden (behind-the-screen) checks never reach the settlement — the
    same rule the report enforces (`_visible_checks`)."""
    checks: list[dict[str, Any]] = []
    try:
        rows = await services.store.state_list(chat_key, "session_history.")
        for row in rows:
            raw = row.get("value")
            if not raw:
                continue
            try:
                from core.battle_report import SessionRecord

                record = SessionRecord.from_dict(json.loads(raw))
            except Exception:  # noqa: BLE001 — a corrupt archive must not kill settlement
                continue
            checks.extend(record.skill_checks)
    except Exception:  # noqa: BLE001
        logger.debug("settle: session history unreadable", exc_info=True)
    try:
        current = await services.battles.generator.get_current_session(chat_key)
        if current is not None:
            checks.extend(current.skill_checks)
    except Exception:  # noqa: BLE001
        logger.debug("settle: current session unreadable", exc_info=True)
    from core.battle_report import NPC_USER_ID

    visible = [check for check in checks if not check.get("hidden") and str(check.get("user_id", "")) != NPC_USER_ID]
    return visible[:_MAX_CHARS * 3]


def _aggregate_checks(checks: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """char_name -> skill -> {total, success, critical, fumble}."""
    by_char: dict[str, dict[str, dict[str, int]]] = {}
    for check in checks:
        char_name = str(check.get("char_name") or "").strip()
        skill = str(check.get("skill") or "").strip()
        if not char_name or not skill:
            continue
        stats = by_char.setdefault(char_name, {}).setdefault(skill, {"total": 0, "success": 0, "critical": 0, "fumble": 0})
        stats["total"] += 1
        stats["success"] += int(bool(check.get("success")))
        stats["critical"] += int(bool(check.get("critical")))
        stats["fumble"] += int(bool(check.get("fumble")))
    return by_char


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort: the first {...} object in a possibly chatty completion."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _sheet_lines(services: Services, chat_key: str) -> tuple[list[str], dict[str, str]]:
    """One summary line per sheet, and the sheet-name -> system map. A sheet's
    owned skills and attributes are shown so the proposal names only what exists."""
    sheets = await services.documents.list(chat_key, "sheet")
    lines: list[str] = []
    systems: dict[str, str] = {}
    for doc in sheets:
        data = doc.data
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        system = str(data.get("system") or "?")
        systems[name] = system
        attrs = {key: value for key, value in (data.get("attributes") or {}).items() if isinstance(value, int)}
        skills = {key: value for key, value in (data.get("skills") or {}).items() if isinstance(value, int) and value}
        background = str(data.get("background") or "").strip()
        line = f"- {name} (system: {system}): attributes {attrs}; skills {skills}"
        if background:
            line += f"\n  backstory: {background[:_MAX_STORY_CHARS]}"
        lines.append(line)
    return lines, systems


async def _memory_lines(services: Services, chat_key: str) -> list[str]:
    """Each character's memory log, newest entries first, plus the folded summary."""
    lines: list[str] = []
    docs = await services.documents.list(chat_key, CHARACTER_MEMORY_DOC_TYPE)
    for doc in docs:
        data = doc.data
        name = str(doc.id)
        entries = list(data.get("entries") or [])
        entries = entries[-_MAX_MEMORY_ENTRIES_SHOWN:]
        texts = [str(entry.get("text") or "") for entry in entries if entry.get("text")]
        summary = str(data.get("summary") or "")
        block = f"- {name}:"
        if summary:
            block += f"\n  folded: {summary}"
        if texts:
            block += "\n  " + "\n  ".join(texts)
        lines.append(block)
    return lines


async def _chronicle_lines(services: Services, chat_key: str) -> list[str]:
    """The campaign story for the settlement: the folded campaign summary — the
    rolling "story so far" that keeps every pivotal choice, named character, clue
    and unresolved consequence — plus the recent raw records. PLAYER projections
    only (iron rule #3: keeper notes never reach the settlement)."""
    from core.chronicle import CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID, CHRONICLE_DOC_TYPE

    lines: list[str] = []
    summary = await services.documents.get(chat_key, CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID)
    if summary is not None and isinstance(summary.data.get("text"), str) and summary.data["text"].strip():
        lines.append(f"## Story so far: {summary.data['text'].strip()[:_MAX_STORY_CHARS]}")
    docs = await services.documents.list(chat_key, CHRONICLE_DOC_TYPE)
    entries = [doc for doc in docs if isinstance(doc.data.get("text"), str) and doc.data["text"].strip()]
    entries = entries[-_MAX_CHRONICLE_SHOWN:]
    lines.extend(f"- {doc.data['text'].strip()[:300]}" for doc in entries)
    return lines


# ---------------------------------------------------------------------------
# Proposal (generative, one declared lane)
# ---------------------------------------------------------------------------


async def build_settlement(services: Services, chat_key: str) -> Settlement | None:
    """Run the settlement lane: assemble the evidence, call the model, parse.
    Returns None when the model reply cannot be parsed into a valid proposal."""
    sheet_lines, systems = await _sheet_lines(services, chat_key)
    if not sheet_lines:
        return None
    checks = _aggregate_checks(await _collect_checks(services, chat_key))
    check_lines: list[str] = []
    for char_name, skills in checks.items():
        for skill, stats in skills.items():
            check_lines.append(
                f"- {char_name}: {skill} — {stats['total']} checks, "
                f"{stats['success']} success, {stats['critical']} critical, {stats['fumble']} fumble"
            )
    if not check_lines:
        check_lines.append("(no skill checks recorded)")
    memories = await _memory_lines(services, chat_key)
    if not memories:
        memories.append("(no character memories yet)")
    chronicle = await _chronicle_lines(services, chat_key)
    if not chronicle:
        chronicle.append("(no chronicle yet)")

    evidence = (
        "EVIDENCE\n"
        f"--- CHARACTER SHEETS ---\n{chr(10).join(sheet_lines)}\n"
        f"--- SKILL CHECKS (PER CHARACTER, PER SKILL) ---\n{chr(10).join(check_lines)}\n"
        f"--- CAMPAIGN CHRONICLE (recent) ---\n{chr(10).join(chronicle)}\n"
        f"--- CHARACTER MEMORIES ---\n{chr(10).join(memories)}"
    )
    prompt = f"{_SYS_PROMPT}\n\n{evidence}\n\nJSON only."
    llm = await services.main_llm(chat_key)
    with lane_scope("settle", chat_key=chat_key):
        try:
            result = await llm.chat([{"role": "user", "content": prompt}])
        except Exception as exc:  # noqa: BLE001 — settlement must never crash a turn
            logger.debug("settle: llm call failed: %s", exc)
            return None
    parsed = _extract_json(result.content or "")
    if parsed is None:
        return None
    settlement = Settlement.from_dict(parsed)
    # A proposal may only name characters that actually play at this table.
    known = frozenset(systems)
    filtered = tuple(char for char in settlement.characters if char.name in known)
    return Settlement(characters=filtered) if filtered else None


# ---------------------------------------------------------------------------
# Application (deterministic)
# ---------------------------------------------------------------------------


async def apply_settlement(services: Services, chat_key: str, settlement: Settlement) -> SettlementResult:
    """Land a proposal through the engine's own paths: improvement checks roll
    the pack's dice, attribute changes validate like `.st`, memory folds through
    `core.character_memory`, and the sheet is saved exactly once."""
    sheets = await services.documents.list(chat_key, "sheet")
    by_name = {str(doc.data.get("name") or "").strip(): doc for doc in sheets}
    outcomes: list[ApplyOutcome] = []
    for char in settlement.characters:
        doc = by_name.get(char.name)
        if doc is None:
            outcomes.append(ApplyOutcome(name=char.name, skipped="no_such_character"))
            continue
        try:
            sheet = CharacterSheet.from_dict(doc.data)
            pack = load_rulepack(sheet.system) if sheet.system else None
            spec = (
                next((entry for entry in pack.subsystems.values() if entry.template == "improvement_check"), None)
                if pack is not None
                else None
            )
            growth: list[GrowthResult] = []
            for skill in char.growth:
                if spec is None:
                    break  # the system declares no improvement check — nothing to roll
                canonical = pack.resolve_skill(skill) or skill
                current = sheet_value(sheet, pack, canonical)
                roll = services.dice.roll_expression(spec.roll).total
                grows = roll > current or (
                    spec.auto_success_above is not None and roll > spec.auto_success_above
                )
                gain = services.dice.roll_expression(spec.improve).total if grows else 0
                new_value = min(spec.cap, current + gain)
                if gain:
                    set_sheet_value(sheet, pack, canonical, new_value)
                growth.append(GrowthResult(skill=canonical, rolled=roll, gained=gain, value=new_value))

            attributes: list[tuple[str, int, bool]] = []
            for change in char.attribute_changes:
                canonical = (pack.resolve_skill(change.field) if pack is not None else None) or change.field
                current = sheet_value(sheet, pack, canonical)
                new_value = max(0, current + change.delta)
                set_sheet_value(sheet, pack, canonical, new_value)
                attributes.append((canonical, new_value, True))

            if char.background:
                sheet.background = char.background

            sheet, violations = validate_sheet(sheet, sheet.system)
            owner = await services.characters.get_character_owner(chat_key, char.name)
            if owner:
                await services.characters.save_character(owner, chat_key, sheet, force=True)

            folded = False
            memory_doc = await services.documents.get(chat_key, CHARACTER_MEMORY_DOC_TYPE, char.name)
            if memory_doc is not None and char.memory_fold:
                # One PLAYTHROUGH memory per settled scenario: the character's
                # experience this run, tagged so player surfaces can show it as
                # scenario-level "剧本回忆" without the raw per-turn journal.
                scenario = ""
                try:
                    module = await room_active_module(services, chat_key)
                    scenario = str((module or {}).get("name") or "").strip()
                except Exception:  # noqa: BLE001 — a missing module name is cosmetic
                    pass
                prefix = f"【{scenario}】" if scenario else ""
                data = append_playthrough_entry(
                    memory_doc.data,
                    f"{prefix}{char.memory_fold}",
                    await chronicle_turn(services.store, chat_key),
                    scenario=scenario,
                )
                # The keeper's growth note still lands on the document, just not
                # folded into a rolling summary.
                note = str(char.keeper_note or "").strip()
                if note:
                    data = {**data, "keeper": note}
                await services.documents.put(chat_key, CHARACTER_MEMORY_DOC_TYPE, char.name, data)
                folded = True

            outcomes.append(
                ApplyOutcome(
                    name=char.name,
                    growth=tuple(growth),
                    attributes=tuple(attributes),
                    folded=folded,
                    background=bool(char.background),
                    notice="; ".join(violations),
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad character must not abort the rest
            logger.debug("settle: apply failed for %s: %s", char.name, exc)
            outcomes.append(ApplyOutcome(name=char.name, skipped="apply_failed"))
    return SettlementResult(outcomes=tuple(outcomes))


# ---------------------------------------------------------------------------
# Pending proposal store (the two-step contract: generate, review, apply)
# ---------------------------------------------------------------------------


async def save_pending(services: Services, chat_key: str, settlement: Settlement) -> None:
    await services.store.state_set(chat_key, SETTLE_PENDING_KEY, json.dumps(settlement.to_dict(), ensure_ascii=False))


async def load_pending(services: Services, chat_key: str) -> Settlement | None:
    raw = await services.store.state_get(chat_key, SETTLE_PENDING_KEY)
    if not raw:
        return None
    try:
        return Settlement.from_dict(json.loads(raw))
    except Exception:  # noqa: BLE001 — a corrupt pending row reads as absent
        return None


async def clear_pending(services: Services, chat_key: str) -> None:
    await services.store.state_delete(chat_key, SETTLE_PENDING_KEY)


# ---------------------------------------------------------------------------
# Rendering (text for the command layer)
# ---------------------------------------------------------------------------


def render_proposal(settlement: Settlement, i18n: I18n) -> str:
    lines = [i18n.t("settle.proposal.header")]
    for char in settlement.characters:
        lines.append(i18n.t("settle.proposal.character", name=char.name))
        if char.growth:
            lines.append(i18n.t("settle.proposal.growth", skills=", ".join(char.growth)))
        for change in char.attribute_changes:
            delta = f"{change.delta:+d}"
            lines.append(i18n.t("settle.proposal.attribute", field=change.field, delta=delta))
        if char.memory_fold:
            lines.append(i18n.t("settle.proposal.memory_fold", text=char.memory_fold))
        if char.background:
            lines.append(i18n.t("settle.proposal.background", text=char.background))
    return "\n".join(lines)


def render_result(result: SettlementResult, i18n: I18n) -> str:
    lines = [i18n.t("settle.result.header")]
    for outcome in result.outcomes:
        if outcome.skipped:
            lines.append(i18n.t(f"settle.result.skipped_{outcome.skipped}", name=outcome.name))
            continue
        lines.append(i18n.t("settle.result.character", name=outcome.name))
        for growth in outcome.growth:
            lines.append(
                i18n.t(
                    "settle.result.growth",
                    skill=growth.skill,
                    rolled=growth.rolled,
                    gained=growth.gained,
                    value=growth.value,
                )
            )
        for field_name, new_value, applied in outcome.attributes:
            if applied:
                lines.append(i18n.t("settle.result.attribute", field=field_name, value=new_value))
        if outcome.folded:
            lines.append(i18n.t("settle.result.folded", name=outcome.name))
        if outcome.background:
            lines.append(i18n.t("settle.result.background", name=outcome.name))
        if outcome.notice:
            lines.append(i18n.t("settle.result.notice", notice=outcome.notice))
    return "\n".join(lines)


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="settle_pending",
        owner="agent.settle",
        reset_scope="story",
        survives_because=(
            "a pending settlement targets the story that produced it; a `.reset` "
            "clears the story, so the proposal must not survive to be applied to a "
            "fresh one — the keeper re-runs `.settle` instead"
        ),
        state_keys=frozenset({SETTLE_PENDING_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
    RoomStateFacet(
        name="character_memory",
        owner="agent.settle",
        reset_scope="story",
        # The memory is the AI keeper's working knowledge (it rides the prompt every
        # turn), not a character-sheet asset: a story reset must drop it with the
        # session, or the keeper keeps citing the previous adventure's events —
        # including items the party archived since (the observed bug). Sheets and
        # their background prose survive story resets; this memory does not.
        doc_types=frozenset({CHARACTER_MEMORY_DOC_TYPE}),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)
