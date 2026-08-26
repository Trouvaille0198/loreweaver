# Forge pregen persona: characters get real backgrounds

- **Problem:** generated modules shipped pregens with only a one-line `concept` — and even
  that never reached the sheet. The pack-module schema asked for `name` + `concept` +
  `occupation` + `skills` only; `core.lorecard._parse_pregens` supported a 400-char `notes`
  field but the schema never requested it, and the world-import cast loop
  (`kp_tools_charcard`) dropped it: the persona lived solely on the roster entry's `blurb`
  (`.pc list`), the claimed sheet's `background`/`notes` were empty, and the keeper's
  game-state prompt rendered claimed characters as `name | meters | status` — no persona at
  all. NPCs were richer only by accident (the analysis schema asks for outward description;
  the model wrote backstory on its own).
- **Decision:** make the persona contract explicit end-to-end.
  1. The pack-module schema now asks every pregen for `background`: "2-4 sentences of
     persona: their history, personality, manner of speech, and a secret or flaw"
     (`agent/forge._PACK_MODULE_CARD_SCHEMA`). The md-lane cards prompt
     (`agent.forge.module_cards_system_prompt`) says the same thing in its description
     field.
  2. `core.lorecard._parse_pregens` reads `background` into `notes` (legacy `notes` name
     still accepted for hand-authored packs; 400-char cap unchanged).
  3. The world-import cast loop writes the persona onto the sheet's `background`, so a
     player who claims the pregen can read and play it.
  4. The keeper's game-state roster panel appends a truncated (80-char) `Persona:` line
     per claimed character who has one (`core.prompt_sections`, localized
     `prompt.game_state.roster_background`).
  Keeper secrecy is untouched: the persona is the player's own character, player-visible
  by design; keeper-only secrets still belong in secret worldbook entries.
- **Rule home:** `agent/forge.py` (`_PACK_MODULE_CARD_SCHEMA`);
  `core/lorecard.py` (`_parse_pregens`); `agent/kp_tools_charcard.py` (cast loop);
  `core/prompt_sections.py` (roster panel); `locales/{en,zh}/prompt.json`
  (`roster_background`), `locales/{en,zh}/agent.json` (`module_cards_system_prompt`);
  format contract in `docs/authoring.md`/`docs/authoring.zh.md` `pregens[]`.
- **Date:** 2026-08-26.
