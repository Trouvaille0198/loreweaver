# Implemented: the item-change gate matches tracked names, not verbs

- **Problem:** `item_forged` gated on a verb dictionary (`_ITEM_ACTION_RE`) plus a generic
  item-word list — both open sets that cannot be enumerated in either language.
  2026-08-27 it failed live: 沈铁's mirror landed in his hands for the third time
  ("一把将铜镜扯了出来" — 扯 is not in the list), the forged-item check never fired, the
  AI was never re-asked to call `grant_item`, and the item documents drifted from the
  story for a whole session. The same gap had already produced a duplicate 铜钱 grant in
  another room the day before.
- **Decision:** `reply_claims_item_action` matches ONLY the closed set of names the room
  actually tracks (catalog templates + live item documents), via plain casefolded
  substring match. Both regex dictionaries are deleted. Whether a mention actually
  claims a change is now the check round's own LLM call to decide — the check table
  already runs in Stop form with per-turn-budgeted model calls, so the semantics move to
  the one place that can understand them. The gate's only job is to guarantee the model
  gets asked whenever a tracked item appears in the reply.
- **Reason:** semantics cannot be enumerated; names can. Mention-only cases (NPC
  dialogue, scenery) now trip the gate and cost one bounded confirmation ("no change")
  — cheap, and correct where the old regex was silently blind. `turn_checks` stays
  deterministic at the condition layer (the predicate is still code over the reply and
  the tool trace); what moved is only where the semantic question is answered.
- **Budget:** no new model-call lane. The check round occupies the existing
  `item_forged` row (`max_rounds=1`) inside the existing per-turn check-round caps
  (`MAX_ROUNDS_PER_TURN`). It fires more often than before (any mention), each firing is
  one bounded confirmation, and the turn ceiling is unchanged.
- **Rule home:** `agent/turn_checks.py` (`reply_claims_item_action` docstring);
  `locales/{en,zh}/loop.json` `loop.check.item_forged` (the confirmation wording).
- **Date:** 2026-08-27.
