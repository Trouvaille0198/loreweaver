# Character dossier: source, memory and relationships reach the player

- **Problem:** a character's full persona lived in the engine but never reached
  players. The browser character page showed the sheet's numbers and prose, but the
  module source of a claimed pregen, the character's memory (life summary + recent
  experience lines) and the relationship tracks it holds toward other entities were
  all engine-side only — `state.character` carried none of them, and the terminal had
  no command to read them.
- **Decision:** make the whole dossier additive wire fields and surface it on both
  clients.
  1. `net.state._character_payload` now appends `source` (read off the pregen roster
     document — only pregen characters carry one), `memory` (player projection:
     settled `summary` plus the most recent 10 experience lines, newest first, text
     extracted from the `{text, turn}` entries) and `relationships` (this character
     as subject, non-default track values only, track ids sent raw for client
     labeling).
  2. `clients/protocol` `CharacterState` gains `source?`, `memory?`
     (`{summary?, entries?}`) and `relationships?` (`[{target, tracks:[{track,value}]}]`).
     Additive and optional — old clients ignore them, the protocol version is
     unchanged. The web client mirrors the types locally (`protocol-augment.d.ts`,
     the same seam as v2.4 `skills`), because it consumes the published npm package.
  3. The web character page moves the persona block (background/notes) directly
     under the character-details section, then renders three new sections: memory
     (summary + journal lines), relationships (per-target chips) and a module-source
     badge beside the system tag. Localized keys in `src/i18n/locales/{en,zh}.json`.
  4. Terminal parity: `.pc info <name>` prints the same dossier (source, memory
     summary + last 5 lines, relationships) from the same player projections.
- **Secrecy:** unchanged — memory uses the existing player projection (keeper margin
  stays keeper-side), relationships are the character's own public tracks, source is
  roster-public.
- **Rule home:** `net/state.py` (`_character_payload`); `gateway/commands/sheet.py`
  (`cmd_pc` info branch, `_pc_info`); `clients/protocol/src/types.ts`; web
  `src/protocol-augment.d.ts`, `src/features/play/screens/CharacterScreen.tsx`,
  `src/styles.css`, `src/i18n/locales/{en,zh}.json`; `locales/{en,zh}/pregen.json`.
- **Date:** 2026-08-26.
