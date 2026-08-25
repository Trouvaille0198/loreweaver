"""The room's discovered-clue log (structural clue tracking).

A clue the party has actually found — a worldbook entry the Keeper/AI
registered at discovery time — is snapshotted into the room's singleton
`clue_log` document: title, trigger keys, the entry text, its illustration,
and the turn it was found. The log is what players see (their `clues` on the
wire come from here), so an unrevealed secret clue never reaches a player —
it is simply not in the log until the table discovers it.

Keeper-only verbs: reveal (register a discovery) and remove. Players read.
"""

from __future__ import annotations

from typing import Any

from core.documents import DocumentStore
from infra.room_facets import STORAGE_DOCUMENTS, RoomStateFacet

CLUE_LOG_ID = "clue_log"

# Room lifecycle: the log is room content that ships with the module's world —
# cleared on `reset all`, never on a narrower scope.
ROOM_FACETS = (
    RoomStateFacet(
        name="clue_log",
        owner="agent.clue_log",
        reset_scope="all",
        doc_types=frozenset({"clue_log"}),
        state_keys=frozenset(),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)


async def get_clue_log(documents: DocumentStore, chat_key: str) -> list[dict[str, Any]]:
    """The room's discovered clues, in discovery order (empty when none)."""
    doc = await documents.get_singleton(chat_key, CLUE_LOG_ID)
    if doc is None:
        return []
    clues = doc.data.get("clues")
    return clues if isinstance(clues, list) else []


async def reveal_clue(
    documents: DocumentStore,
    chat_key: str,
    *,
    title: str,
    content: str,
    keys: list[str] | None = None,
    image: str = "",
    found_turn: int = 0,
) -> bool:
    """Register one discovered clue (idempotent by title). Snapshots the worldbook
    entry at discovery time — a later module re-import cannot rewrite what the
    table already found. Returns whether a NEW entry was added."""
    title = (title or "").strip()[:120]
    if not title:
        return False
    existing = await get_clue_log(documents, chat_key)
    if any(str(e.get("title", "")).casefold() == title.casefold() for e in existing):
        return False
    entry = {
        "title": title,
        "keys": [str(k).strip() for k in (keys or []) if str(k).strip()][:16],
        "content": (content or "").strip()[:4000],
        "image": (image or "").strip()[:200],
        "found_turn": int(found_turn or 0),
    }
    await documents.put_singleton(chat_key, CLUE_LOG_ID, {"clues": [*existing, entry]})
    return True


async def remove_clue(documents: DocumentStore, chat_key: str, title: str) -> bool:
    """Remove one discovered clue by (case-insensitive) title. Returns whether any
    entry was actually removed."""
    existing = await get_clue_log(documents, chat_key)
    folded = (title or "").casefold()
    kept = [e for e in existing if str(e.get("title", "")).casefold() != folded]
    if len(kept) == len(existing):
        return False
    await documents.put_singleton(chat_key, CLUE_LOG_ID, {"clues": kept})
    return True


async def find_worldbook_clue(worldbook, chat_key: str, name: str) -> dict[str, Any] | None:
    """Find one worldbook CLUE entry (category == ``clue``) by exact title or
    trigger-key match, projected to a clue-log snapshot shape. Only `clue` entries
    are candidates — the worldbook also carries setting/NPC/truth/secret rows that
    are not the party's clue list. Shared by the `.clue` command and the Keeper's
    `reveal_clue` tool."""
    folded = (name or "").casefold()
    for entry in await worldbook.list(chat_key):
        if entry.category != "clue":
            continue
        if entry.title.casefold() == folded or any(str(k).casefold() == folded for k in entry.keys):
            return {
                "title": entry.title,
                "keys": list(entry.keys),
                "content": entry.content,
                "image": entry.image,
            }
    return None
