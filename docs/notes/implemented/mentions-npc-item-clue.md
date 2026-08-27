# Implemented: narrative mention highlights for npc / item / clue

- **Decision:** `gateway/mentions.py` binds all three tracked-record kinds
  through ONE pipeline — validated `[[name]]` marks first, then a
  case-insensitive longest-first fallback scan over a merged key table whose
  insertion order is the conflict rule (npc > item instance > catalog preset >
  clue). Links carry their kind in the scheme (`npc://`, `item://`,
  `clue://`); each mention names its `kind`. Item keys/cards come only from
  PLAYER-view sources (granted instances; non-secret catalog presets, aliases
  gated on the canonical name already being player-visible); clue keys/cards
  come only from the discovered-clue log. The KP style prompt instructs the
  model to mark every mention of anything tracked.
- **Reason:** highlight and cards are player-facing surfaces, so their inputs
  must be projections, not filters after the fact — a secret item or an
  undiscovered clue never becomes a key in the first place. One pipeline for
  three kinds keeps validation, dedupe and fallback semantics identical, so a
  name renders the same live and on replay.
- **Standing:** settled 2026-08-27 (owner). Protocol 2.8 documents the wire
  shape (`Mention.kind`, generalized card bag).
- **Rule home:** `gateway/mentions.py`; protocol contract `docs/protocol.md`
  (2.8 additive section); prompt instruction `prompt.style.mention_marks`.
- **Date:** 2026-08-27 (owner).
