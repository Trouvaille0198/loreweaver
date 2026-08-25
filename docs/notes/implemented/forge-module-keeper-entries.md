# Implemented: forge world cards must ship the keeper-only skeleton (ending plan + knowledge boundary)

- **Problem:** comparing an AI-forged module (1940npc) against the hand-authored reference
  (`loreweaver-module-xipu-songdeng`) showed the forge cast's secrets were incomplete: it carried
  the truths (who did it, how, where the victim is) and the antagonist motives, but had no
  **ending plan** (xipu's `【守密人】结局门与信物` — each ending, its trigger condition and
  consequence) and no **NPC knowledge boundary** (`【守密人】人物所知边界` — what each key NPC
  knows and must not let slip). A keeper could run the middle of the module but had no structural
  guidance on how to finish it or how to avoid over-sharing.
- **Decision:** the pack-module forge contract now demands those two entries. The
  `_PACK_MODULE_CARD_SCHEMA` worldbook example names keeper-only secret entries (ending plan,
  NPC knowledge boundary) and widens `category` to `lore|npc|clue|truth|secret`; the
  `pack_module_system_prompt` (en/zh) requires every generated module to include two
  `secret: true` keeper-only entries — one titled like 「结局门与信物」 (each ending, trigger,
  consequence) and one titled like 「人物所知边界」 (per-NPC knowledge, never over-share) — and
  marks every truth entry `secret: true`.
- **Reason:** the reference module encodes endings and anti-metagaming distribution as secret
  worldbook entries with a title convention, not as separate fields. Following that same authoring
  shape (prompt-driven, no protocol/format change) keeps forge output structurally complete with
  zero cross-repo cost.
- **Rule home:** `agent/forge.py` (`_PACK_MODULE_CARD_SCHEMA`);
  `locales/{en,zh}/agent.json` (`pack_module_system_prompt`); contract pinned by
  `tests/agent/test_forge_module.py::test_pack_module_schema_and_prompts_require_keeper_only_entries`.
- **Date:** 2026-08-24.
