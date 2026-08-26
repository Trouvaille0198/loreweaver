# Implemented: the post-campaign settlement ritual

- **Problem:** Ending a scenario did nothing for the characters. Growth was a manual
  `.growth` roll the Keeper had to remember per skill; PC experience lived only in the
  campaign chronicle (what the TABLE did), never in the character (what SHE lived
  through) — so a character carried no past into the next module.
  - **The AI-KP's end-of-story reminder** (`prompt.settlement_notice`, a stable-head
    section in `agent/prompt_builder.py`): the AI-KP reads the whole campaign context,
    so it can recognise a clear ending itself — the notice tells it to end such a
    reply with a brief natural reminder that the keeper can run `.settle`, and
    NEVER to trigger settlement itself, draft its contents, or declare a scene over
    while it could still continue. Triggering and landing stay keeper-only.
- **Iron-rule accounting:**
  - #1 — the lane proposes; the engine disposes. The lane never rolls, never invents
    mechanics, never writes a sheet outside the validated apply path.
  - #3 — memories are built only from what the reply said aloud (same discipline as the
    auto chronicle); the `keeper` margin stays keeper-side by projection; a name not on
    a sheet is discarded structurally.
  - #5 — one new declared lane (`agent/settle.py: AUTHORING` in
    `tests/architecture/test_model_call_lanes.py`), never per turn.
- **Rule home:** `core/character_memory.py` (structure/projection/validation),
  `agent/settle.py` (lane + apply + pending + facets), `agent/scribe.py` (per-turn
  writer), `gateway/commands/world.py` (`.settle`), `gateway/commands/sheet.py`
  (`.mem`), `locales/{en,zh}/commands.json`, `net/room_lifecycle.py` (facet list).
