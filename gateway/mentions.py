"""Annotate narrative text with mention links + player-visible cards.

Three kinds of room records feed the highlight — NPCs, items (granted
instances plus non-secret catalog presets), and clues actually discovered by
the table — all ending in the SAME deterministic binding:

1. **LLM-nominated marks** — the KP prompt asks the model to wrap a tracked
   name in `[[名字]]` on every mention. The annotator VALIDATES each mark
   against the room's records (canonical names + explicit `aliases`): a match
   binds the mark to its record's id and player-visible card, keeping its own
   wording; a miss (a name the model invented, a format drift) is stripped to
   plain text — never a dead link.
2. **Fallback scan** — names still present verbatim in the text (the model
   forgot to mark them) are linked by the same case-insensitive scan, longest
   key first; single-character keys stay mark-only so prose can't light up on
   stray glyphs.

Player safety is structural, not a filter bolted on top: match keys and cards
come only from PLAYER-view projections (`core.documents`), so a secret item,
an undiscovered clue, or an NPC's inner life never yields a key, a link, or a
card field. When two records claim the same surface form, first insertion
wins — NPC > item instance > catalog preset > clue, settled by construction.

The card list per mention is exactly what a projection exposes; nothing else
travels. Applied at the narrative event's construction point (`gateway.turn`
for live KP replies) and at KP-reply replay (`net.session`), so live and
replayed lines render the same highlight. Streaming deltas are NOT annotated
— only the final line, exactly once, at its source.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from agent.items import item_name_key
from agent.npc import NPC_DOC_TYPE
from core.documents import CLUE_LOG_ID, PLAYER_VIEWER
from core.documents import project as _project
from core.relationships import TRACKS, RelationshipManager

# The model-side mark: `[[名字]]` around any tracked name (see prompt.style.mention_marks).
_MARK_RE = re.compile(r"\[\[([^\]]{1,64})\]\]")
# Placeholder seat for a validated mark while the fallback scan runs, so the
# fallback never double-links a name the mark already claimed.
_PH = "\x00"
# Fallback-scan floor: one-character keys would light up everywhere in CJK
# prose. Marks still validate against short names — this floor gates scanning.
_MIN_SCAN_KEY = 2

_ITEM_DOC_TYPE = "item"
_CATALOG_DOC_TYPE = "item_catalog"
_CLUE_DOC_TYPE = "clue_log"

_NPC_CARD_FIELDS = ("name", "public_description", "location", "status", "avatar", "public_memory", "relationships")
_ITEM_CARD_FIELDS = ("name", "kind", "slot", "description", "effect", "quantity", "equipped_slot")
_CLUE_CARD_FIELDS = ("title", "content", "found_turn")


def _restrict(view: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """The player-visible card subset of one projected record (None when nothing projects)."""
    if view is None:
        return None
    return {key: view[key] for key in fields if key in view}


def _href(kind: str, record_id: str) -> str:
    """ASCII-only destination: safe for markdown parens and HTML quoting at
    every rendering layer; decoded back only at click time."""
    return f"{kind}://{quote(record_id, safe='')}"


async def annotate_mentions(
    services: Any, chat_key: str, text: str
) -> tuple[str, list[dict[str, Any]]]:
    """Validate `[[name]]` marks against the room's tracked records, bind them
    to `<kind>://<id>` links, strip invalid marks, then fall back to exact
    matching for unmarked mentions. Returns `(annotated_text, mentions)` with
    one entry per DISTINCT mentioned record —
    `{id, kind: "npc"|"item"|"clue", name, card?}` — each carrying its
    player-visible card."""
    if not text:
        return text, []
    try:
        by_key = await _mention_table(services, chat_key)
    except Exception:  # noqa: BLE001 — annotation is never load-bearing
        return text, []
    if not by_key:
        return text, []

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    holders: list[str] = []

    def _bind(kind: str, record_id: str, link_text: str, display: str, card: dict[str, Any] | None) -> str:
        """One deterministic binding: dedupe into `seen` (recorded under the
        canonical `display`) and emit a link rendered with `link_text` —
        validated marks keep the wording the model chose."""
        seen.setdefault(
            (kind, record_id),
            {"id": record_id, "kind": kind, "name": display, **({"card": card} if card else {})},
        )
        return f"[{link_text}]({_href(kind, record_id)})"

    def _mark(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        entry = by_key.get(inner.casefold())
        if entry is None:
            # A mark the model invented or drifted — strip to plain text, never a dead link.
            return inner
        kind, record_id, display, card = entry
        holders.append(_bind(kind, record_id, inner, display, card))
        return f"{_PH}{len(holders) - 1}{_PH}"

    # 1) LLM-nominated marks: validate, bind, or strip.
    staged = _MARK_RE.sub(_mark, text)

    # 2) Fallback: names still present verbatim (unmarked mentions). A scan hit
    # binds to the record but KEEPS the wording the model chose — a narration
    # that calls the guard "格伦" stays "格伦", never re-expanded to the
    # canonical full name (the mention card still shows the record's name).
    ordered = sorted((key for key in by_key if len(key) >= _MIN_SCAN_KEY), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(key) for key in ordered), re.IGNORECASE)

    def _fallback(match: re.Match[str]) -> str:
        entry = by_key.get(match.group(0).casefold())
        if entry is None:
            return match.group(0)
        kind, record_id, display, card = entry
        return _bind(kind, record_id, match.group(0), display, card)

    staged = pattern.sub(_fallback, staged)

    # 3) Restore validated marks.
    for index, link in enumerate(holders):
        staged = staged.replace(f"{_PH}{index}{_PH}", link)

    return staged, list(seen.values())


async def _mention_table(services: Any, chat_key: str) -> dict[str, tuple[str, str, str, dict[str, Any] | None]]:
    """casefolded surface form -> (kind, record id, canonical display, player card).

    Every read goes through `services.documents`; entries enter ONLY from
    player-visible projections (raw payloads feed alias harvesting exclusively
    behind the visibility gate below), keeping the keeper's secrets out of both
    the highlight and the cards.
    """
    docs = services.documents
    by_key: dict[str, tuple[str, str, str, dict[str, Any] | None]] = {}

    def _put(key: Any, kind: str, record_id: str, display: str, card: dict[str, Any] | None) -> None:
        stripped = str(key or "").strip()
        if stripped:
            # setdefault IS the conflict rule: npc > instance > preset > clue by insertion order.
            by_key.setdefault(stripped.casefold(), (kind, record_id, display, card))

    def _entry_card(entry: Any, fields: tuple[str, ...]) -> dict[str, Any]:
        return {field: entry[field] for field in fields if field in entry}

    # NPCs — every registered record, matched by name or explicit alias.
    # The card also carries the NPC's deterministic relationship tracks toward
    # other entities (好感/情欲), read straight off the room state the same way
    # `net.state` projects a player character's tracks: only non-default values,
    # only the player-visible half of the pair. Tracks are mechanical state, not
    # keeper secrets — an NPC's public feelings are as visible as a sheet's.
    try:
        rel_state = await RelationshipManager(services.store).load(chat_key)
    except Exception:  # noqa: BLE001 — a broken relationship store must not kill annotation
        rel_state = {}
    for doc in await docs.list(chat_key, NPC_DOC_TYPE):
        data = doc.data
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        card = _restrict(_project(doc, PLAYER_VIEWER), _NPC_CARD_FIELDS)
        tracks = rel_state.get(name)
        if tracks:
            rendered = []
            for target, values in tracks.items():
                pairs = []
                for track_id, value in values.items():
                    spec = TRACKS.get(track_id)
                    if spec is None or value == spec.default:
                        continue
                    pairs.append({"track": str(track_id), "value": int(value)})
                if pairs:
                    rendered.append({"target": str(target), "tracks": pairs})
            if rendered:
                card = {**card, "relationships": rendered}
        _put(name, "npc", doc.id, name, card)
        for alias in data.get("aliases") or []:
            _put(str(alias), "npc", doc.id, name, card)

    # Granted item instances — secret ones project to None, so they never
    # become a key, a link, or a card. Instances carried over from a PREVIOUS
    # scenario stay on their holder (player property) but must not highlight in
    # this one: only items of the current module — or legacy rows with no origin
    # stamp — are mentionable here.
    current_module = ""
    try:
        from agent.module_lifecycle import active_module

        active = await active_module(services, chat_key)
        current_module = str(active.get("pack_id") or active.get("source_id") or "") if active else ""
    except Exception:  # noqa: BLE001 — a broken module lookup must not kill annotation
        current_module = ""
    for doc, view in await docs.list_views(chat_key, _ITEM_DOC_TYPE, PLAYER_VIEWER):
        name = str(view.get("name") or "").strip()
        if not name:
            continue
        origin = str(view.get("source_module_id") or doc.data.get("source_module_id") or "")
        if origin and current_module and origin != current_module:
            continue
        _put(name, "item", doc.id, name, _entry_card(view, _ITEM_CARD_FIELDS))

    # Catalog presets the table already knows exist (non-secret templates). A preset
    # belonging to a PREVIOUS scenario must not highlight here either — same scoping
    # as instances. The player projection strips provenance, so the origin module is
    # read from the raw template (`_doc.data`) instead of the view.
    for _doc, view in await docs.list_views(chat_key, _CATALOG_DOC_TYPE, PLAYER_VIEWER):
        _raw_by_name = {
            str(e.get("name") or "").casefold(): e
            for e in (getattr(_doc, "data", {}) or {}).get("items") or []
            if isinstance(e, dict)
        }
        for entry in view.get("items") or []:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            _raw = _raw_by_name.get(name.casefold()) or {}
            entry_module = str(_raw.get("module_id") or "").strip()
            if entry_module and current_module and entry_module != current_module:
                continue
            record_id = f"tpl-{item_name_key(name)}"
            _put(name, "item", record_id, name, _entry_card(entry, _ITEM_CARD_FIELDS))

    # Template aliases live only in raw payloads; harvest them behind a
    # visibility gate — a template whose canonical name is invisible gets no
    # aliases either, so its existence stays unknown to players.
    visible_presets = {
        key: (value[1], value[3])
        for key, value in by_key.items()
        if value[0] == "item" and value[1].startswith("tpl-")
    }
    if visible_presets:
        for doc in await docs.list(chat_key, _CATALOG_DOC_TYPE):
            for entry in doc.data.get("items") or []:
                name = str(entry.get("name") or "").strip()
                preset = visible_presets.get(name.casefold())
                if preset is None:
                    continue
                for alias in entry.get("aliases") or []:
                    _put(str(alias), "item", preset[0], name, preset[1])

    # Discovered clues — every log entry was found by the table (the log is
    # identical for keepers and players); identity is the title itself.
    clue_view = await docs.get_view(chat_key, _CLUE_DOC_TYPE, CLUE_LOG_ID, PLAYER_VIEWER)
    for entry in (clue_view or {}).get("clues") or []:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        entry_module = str(entry.get("module") or "").strip()
        if entry_module and current_module and entry_module != current_module:
            continue
        _put(title, "clue", title, title, _entry_card(entry, _CLUE_CARD_FIELDS))

    return by_key
