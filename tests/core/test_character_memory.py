"""Character memory document (M24): append/cap/fold structure, projection isolation."""

from __future__ import annotations

from core.character_memory import (
    CHARACTER_MEMORY_DOC_TYPE,
    MAX_ENTRIES,
    append_entry,
    empty_memory,
    fold_entries,
    project_character_memory,
    validate_character_memory_write,
)
from core.documents import KEEPER_VIEWER, PLAYER_VIEWER, Document


def _doc(data: dict) -> Document:
    return Document(id="vera", type=CHARACTER_MEMORY_DOC_TYPE, schema_version=1, data=data)


def test_append_entry_appends_oldest_first_and_truncates_only_at_the_defensive_cap():
    data = empty_memory()
    for turn in range(50):
        data = append_entry(data, f"entry {turn}", turn)
    entries = data["entries"]
    assert len(entries) == 50  # well under the defensive ceiling: nothing is dropped
    assert entries[0]["text"] == "entry 0"
    data = empty_memory()
    for turn in range(MAX_ENTRIES + 5):
        data = append_entry(data, f"entry {turn}", turn)
    entries = data["entries"]
    assert len(entries) == MAX_ENTRIES
    assert entries[0]["text"] == "entry 5"  # only the very oldest are truncated
    assert entries[-1]["turn"] == MAX_ENTRIES + 4


def test_append_entry_caps_a_single_line():
    data = append_entry(empty_memory(), "x" * 500, 1)
    assert len(data["entries"][0]["text"]) <= 300


def test_fold_entries_grows_the_summary_and_keeps_the_raw_entries():
    """The fold ADDS a life-summary; the original experience lines are never
    dropped — a character's full log is theirs for life."""
    data = append_entry(append_entry(empty_memory(), "she found the ledger", 3), "she burned it", 4)
    folded = fold_entries(data, "Vera hardened into the lead investigator.", "she doubts the mayor")
    assert [entry["text"] for entry in folded["entries"]] == ["she found the ledger", "she burned it"]
    assert folded["summary"] == "Vera hardened into the lead investigator."
    assert folded["keeper"] == "she doubts the mayor"


def test_fold_entries_appends_to_an_existing_summary_and_caps_it():
    data = {"entries": [], "summary": "first arc", "keeper": ""}
    folded = fold_entries(data, "second arc", "")
    assert folded["summary"] == "first arc\n\nsecond arc"
    huge = fold_entries({"entries": [], "summary": "", "keeper": ""}, "z" * 5000, "")
    assert len(huge["summary"]) <= 4000


def test_project_keeps_keeper_margin_out_of_player_views():
    data = append_entry(empty_memory(), "public line", 1)
    data["keeper"] = "secret judgment"
    data["summary"] = "life so far"
    keeper_view = project_character_memory(_doc(data), KEEPER_VIEWER)
    player_view = project_character_memory(_doc(data), PLAYER_VIEWER)
    assert keeper_view["keeper"] == "secret judgment"
    assert "keeper" not in player_view
    assert player_view["entries"][0]["text"] == "public line"
    assert player_view["summary"] == "life so far"


def test_validate_rejects_bad_entries_and_accepts_wellformed():
    assert validate_character_memory_write(_doc(empty_memory()), None) == []
    bad = _doc({"entries": [{"text": "", "turn": -1}], "summary": 5, "keeper": None})
    violations = validate_character_memory_write(bad, None)
    assert any("text" in v for v in violations)
    assert any("turn" in v for v in violations)
    assert any("summary" in v for v in violations)
    assert any("keeper" in v for v in violations)
