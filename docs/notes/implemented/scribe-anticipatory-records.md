# Pending: the Scribe records clue content the table has not yet seen

- **Problem:** the model-judged chronicle lane caught the Scribe writing
  player-grade records that state MORE than play revealed: "found a hidden
  tide table marking March 6-8 -- the same nights..." when the transcript
  shows the players only saw scratches behind the map and Martha stopped the
  reading (run 33247221050, session 2); "Martha revealed that the north cliff
  lamp sometimes burns green, luring boats" when Martha had been interrupted
  before revealing anything (same run, session 1); "heavy drag-marks were
  found leading out of the lighthouse" without any search in the transcript
  (33731679535). This is the anticipatory-write class from the 2026-08-07 A/B
  run (records written from the keeper's trackers rather than from what was
  said at the table), now on a persistent surface players reach via `.recap`.
- **Options:** (a) tighten the Scribe's evidence contract: a chronicle line
  must be supportable by a verbatim quote from THIS turn's player-facing reply
  (the state-write rule already demands this; extend it to record text);
  (b) run the record through the player projection of the keeper pool and
  drop sentences that match only keeper-side fields; (c) leave as is and let
  the nightly report it.
- **Recommendation:** (a) — one contract for every Scribe write, and it is
  the contract that already stopped the state-write version of this error.
- **Impact:** `agent/scribe*` prompt and its offline tests; zero extra model
  calls; chronicle records get terser on turns where the Keeper implied more
  than it said.
- **Date:** 2026-09-04 (raised from the first week of model-judged nightlies)
