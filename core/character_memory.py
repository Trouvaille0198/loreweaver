"""Character memory — one durable experience log per player character.

The campaign chronicle (M18) records what the TABLE did; this type records what
each CHARACTER lived through. A PC's memory is what makes her feel continuous
across modules: the settlement ritual folds the per-turn entries into a
life-summary paragraph that travels with the sheet, so a character imported
into the next scenario arrives with a past, not a blank card.

One document per character, keyed by the character's canonical name (the same
id space as the ``sheet`` documents). Document shape:

- ``entries`` — the raw per-turn lines the Scribe appends (player-grade text
  built from the game-master reply, exactly like the auto chronicle record:
  what the table SAW, never keeper annotations). Oldest first; capped at
  ``MAX_ENTRIES``, truncating the oldest when the cap is hit. The cap is a
  size bound, not a memory policy — settlement is what turns lines into a
  summary, and a settlement that runs before the cap is reached loses nothing.
- ``summary`` — the folded life-summary: one growing paragraph, appended by
  the settlement lane (the generative half lives in `agent.settle`; this
  module only provides the deterministic structure).
- ``keeper`` — keeper-side margin written by settlement: judgments about the
  character's growth (what the players missed, which trait is quietly
  maturing). Never crosses ``project()`` (iron rule #3).

Projection: the memory is a record of events the table shared, so every
player-grade viewer sees entries + summary whole; only the ``keeper`` margin
stays keeper-side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # no runtime import of core.documents — documents.py imports US
    from core.documents import Document, Viewer

CHARACTER_MEMORY_DOC_TYPE = "character_memory"

# One auto-written line is capped like the chronicle record — a line of one
# character's story, not a retelling of the turn.
_MAX_ENTRY_CHARS = 300
# Defensive ceiling on ONE document's raw lines: the original entries are NEVER
# dropped by settlement (the fold only ADDS a summary), so this cap exists purely
# to keep a single JSON document from growing without bound. A career that passes
# it is ancient history; the folded summary above it remains the readable record.
MAX_ENTRIES = 2_000
# The folded life-summary budget. It grows by settlement, never by per-turn
# writes, so a long career compresses instead of unboundedly expanding.
_MAX_SUMMARY_CHARS = 4_000
_MAX_KEEPER_CHARS = 2_000

_MEMORY_PLAYER_FIELDS = ("entries", "summary")


def project_character_memory(doc: Document, viewer: Viewer) -> dict[str, Any] | None:
    """Keeper sees everything; player-grade viewers get the shared record —
    never the keeper margin on the character's growth."""
    if viewer.is_keeper:
        return dict(doc.data)
    return {key: doc.data[key] for key in _MEMORY_PLAYER_FIELDS if key in doc.data}


def validate_character_memory_write(doc: Document, services: Any) -> list[str]:
    violations = []
    entries = doc.data.get("entries", [])
    if not isinstance(entries, list):
        violations.append("entries must be a list")  # i18n-exempt: validation diagnostic
    else:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                violations.append(f"entry {index} must be an object")  # i18n-exempt: diagnostic
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                violations.append(f"entry {index} requires a non-empty text")  # i18n-exempt: diagnostic
            turn = entry.get("turn")
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
                violations.append(f"entry {index} turn must be a non-negative integer")  # i18n-exempt: diagnostic
    if not isinstance(doc.data.get("summary", ""), str):
        violations.append("summary must be a string")  # i18n-exempt: validation diagnostic
    if not isinstance(doc.data.get("keeper", ""), str):
        violations.append("keeper must be a string")  # i18n-exempt: validation diagnostic
    return violations


def empty_memory() -> dict[str, Any]:
    """The canonical empty document payload."""
    return {"entries": [], "summary": "", "keeper": ""}


def append_entry(data: dict[str, Any], text: str, turn: int) -> dict[str, Any]:
    """Append one per-turn line (oldest first), truncating the oldest when the
    cap is hit. Pure: returns the new payload, never mutates ``data``."""
    entries = list(data.get("entries", []))
    entries.append({"text": str(text).strip()[:_MAX_ENTRY_CHARS], "turn": int(turn)})
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    return {**data, "entries": entries}


def append_playthrough_entry(
    data: dict[str, Any], text: str, turn: int, *, scenario: str = ""
) -> dict[str, Any]:
    """Record ONE scenario memory — the character's experience across the
    playthrough that just settled (`.settle apply`). Tagged
    ``kind: "playthrough"`` and keyed by ``scenario`` so player-facing surfaces
    can show the scenario-level memories without the raw per-turn journal, and
    a re-settle of the same scenario REPLACES the old entry instead of stacking
    a duplicate. Pure."""
    entries = [
        entry
        for entry in (data.get("entries") or [])
        if not (
            isinstance(entry, dict)
            and entry.get("kind") == "playthrough"
            and entry.get("scenario") == scenario
        )
    ]
    entries.append(
        {
            "text": str(text).strip()[:_MAX_ENTRY_CHARS],
            "turn": int(turn),
            "kind": "playthrough",
            "scenario": str(scenario)[:60],
        }
    )
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    return {**data, "entries": entries}


def fold_entries(data: dict[str, Any], summary_line: str, keeper_note: str = "") -> dict[str, Any]:
    """Settlement fold: append the folded life-summary paragraph and store the
    keeper margin. The raw per-turn entries are KEPT — the fold adds a summary,
    it never replaces the record (a character's full experience log is theirs
    for life). Pure; the generative work of turning entries into ``summary_line``
    happens in the settlement lane."""
    summary = str(data.get("summary", "") or "")
    line = str(summary_line or "").strip()
    if line:
        summary = f"{summary}\n\n{line}".strip() if summary else line
        summary = summary[-_MAX_SUMMARY_CHARS:]
    keeper = str(keeper_note or "").strip()[:_MAX_KEEPER_CHARS]
    return {"entries": list(data.get("entries", [])), "summary": summary, "keeper": keeper}
