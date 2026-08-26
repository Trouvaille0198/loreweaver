# Pregen persona merged: `concept` folded into `background`

- **Problem:** a pregen carried two overlapping persona fields — the one-line
  `concept` (roster one-liner, `.pc list` / party modal) and the 2-4 sentence
  `background` (the claimed sheet's persona paragraph). Both answered "who is
  this character", at different lengths, from different prompts — redundant on
  the page and in the generated card.
- **Decision:** one field of truth. `background` (legacy names `notes`/`concept`)
  is the only persona field the forge asks for. The roster one-liner (`blurb`)
  is now DERIVED from `background`'s first sentence (`core.lorecard._first_sentence`,
  split on CJK/ASCII sentence punctuation and newlines), with a `concept`/`blurb`
  fallback for legacy hand-authored packs. The pack-module schema no longer
  advertises `concept`; the md-lane cards pass derives its roster blurb the same
  way. The web party modal labels the blurb "背景与人设 / Background & persona"
  instead of "剧本设定 / Scenario setting". The character page already showed
  the full `background`; nothing else changes (memory summary/entries keep their
  own labels).
- **Rule home:** `agent/forge.py` (`_PACK_MODULE_CARD_SCHEMA`, `_module_cards_pass`
  blurb); `core/lorecard.py` (`_parse_pregens`, `_first_sentence`);
  web `src/i18n/locales/{en,zh}.json` (`play.character.concept` value);
  format contract in `docs/authoring.md` / `docs/authoring.zh.md` `pregens[]`.
- **Date:** 2026-08-26.
