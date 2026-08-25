# Pending: the Keeper volunteers hidden truths on a fraction of nights

- **Problem:** with secrecy model-judged (81a132e), the nightly gate sees what
  the string sentinels never could: the Keeper (deepseek-chat) discloses
  truth-tier material unearned on roughly 3-4 nights in 7. Observed 2026-08-27
  .. 09-03: an NPC volunteers the village pact at turn 2 ("refusing to keep
  paying the old price... the bargain was paid enough", run 33081461505);
  an NPC states outright that Elias Crane drowned two years ago (33185263404,
  33303739937); narration asserts the lure mechanism ("The light doesn't warn.
  It calls.", 33185263404; "it wasn't warning boats...", 33303739937). Eleven
  of the week's 43 confirmed verdicts quote truth-tier content; the rest were
  judge scope noise (see `implemented/redline-model-judged-secrecy.md`).
  The old "45.8% -> 0" measured sentinel words, not secrecy.
- **Options:** (a) Keeper-side: a secrecy directive in the prompt builder
  gated on a structural marker (a module pool with truths present), per the
  no-blanket-directives rule; (b) an end-of-turn check in `agent/turn_checks`
  that asks the model to re-read its reply against the pool's truths before
  release (one more Stop-form round inside the existing 5-round cap);
  (c) accept the rate and keep the gate red on those nights as a standing
  measurement.
- **Recommendation:** (b) first — it is the same shape as the dice-first
  corrective and stays inside the per-turn budget — with (a) only if the
  check keeps firing. Not (c): a gate that is red by design is a dead light.
- **Impact:** touches `agent/turn_checks` and possibly `agent/prompt_builder`
  (the single Keeper assembler); the ~155-call budget note must be updated if
  a round is added; nightly reds continue until it lands.
- **Date:** 2026-09-04 (raised from the first week of model-judged nightlies)
