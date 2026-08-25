# Implemented: forge module casts ship persona-consistent, budget-sane skill profiles

- **Problem:** the pack-module forge (`generate_and_install_pack_module`) authored
  `pregens[]` with only `name` + `concept` — the world-card schema never asked for
  skills and the prompts never mentioned them — so every generated investigator
  landed on the rule system's base values (话术 5 for every CoC character), and a
  whole cast was mechanically identical.
- **Decision:** the LLM authors `pregens[].skills` (6-10 persona-fitting skills,
  signature 50-85, supporting 30-50, never above the creation max, staying within
  the system's skill-point budget); the engine then runs a deterministic sanity
  pass (`agent/forge._normalize_pregen_skills`) before the card is written: keys
  resolve through the pack (aliases become canonical, unresolvable junk is
  dropped), values floor at each skill's base and clamp to the pack's creation
  max, and — when the pack declares a budget — the points spent above base are
  scaled down to the nominal budget evaluated over the pack's DEFAULT sheet
  values (CoC: all-50 stats → INT×2 + EDU×4 = 300), preserving the author's
  relative profile.
- **Reason:** numeric constraints are deterministic engine work (iron rule #1),
  not model charity; the true budget depends on attributes rolled at import time,
  so forge normalizes against a nominal, system-agnostic ceiling instead. The
  import path (`core.lorecard._parse_pregens` + `kp_tools_charcard` cast loop)
  already applied `skills:` overrides — the gap was that forge never produced
  them.
- **Rule home:** `agent/forge.py` (`_PACK_MODULE_CARD_SCHEMA`,
  `_nominal_skill_budget`, `_normalize_pregen_skills`);
  `locales/{en,zh}/agent.json` (`pack_module_system_prompt`,
  `pack_module_skills_normalized`); format contract in `docs/authoring.md`
  `pregens[]`.
- **Date:** 2026-08-24.
