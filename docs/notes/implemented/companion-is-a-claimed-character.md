# Implemented: a companion is a CLAIMED character — the roster is the only door in

- **Problem:** companions were born in a vacuum. `add_companion` minted a fresh record
  AND rolled a fresh sheet from a tool argument — a character nobody could play, claim
  or see before the AI invented it — while the room's claimable cast (module pregens)
  sat in a separate roster that only PLAYERS could claim (`.pc claim`). Two parallel
  character universes: players claimed, the AI created. A module-imported investigator
  could never become a party companion, a `.pc gen`-style room-born character did not
  exist, and "is this name a player's seat or an AI's seat" had to be guessed per door.
- **Verdict:** one roster, one claim action, two kinds of holder. The roster (module
  imports AND the new keeper command `.pc gen`, which authors a claimable character
  fitted to the module's summary) holds every claimable character. A PLAYER claims with
  `.pc claim` and plays it; the AI claims with `.party add` / `add_companion` / the
  charcard `as companion` door — and ONLY an AI claim makes a companion. A companion
  never precedes its character: the record derives from the roster entry (`pregen_id`
  back-references it), the sheet copy materializes under the companion's virtual uid,
  and no door invents a new character anymore. Releasing an AI claim takes it whole —
  record, sheet and roster marker — through the same whole-or-nothing discipline
  `companion-record-and-sheet.md` established.
- **Shape:** the roster entry carries `claimed_by_kind` (`player` | `ai`) stored verbatim
  — core stores it and never interprets it; the agent layer owns the "ai" shape (holder
  id = companion record id, owner uid = `companion:<id>`), passed as `owner_uid` to
  `pregen_claim`/`pregen_release`. `agent.npc.companion_from_pregen` mints the record
  from the entry's own data (blurb → persona, name → sheet identity, deterministic
  pronoun inference), idempotently reusing an existing companion record. The tool
  `add_companion` kept its name but lost `persona`/`system`/`generate` — it now claims.
  The lifecycle edges follow: `.pc gen` characters are `source="room"` and survive a
  module swap (the purge deletes only module-imported pregens); the story-reset
  companion-sheet hook also clears the roster marker, so a claim is never left dangling
  without its companion.
- **Not changed on purpose:** player claims (`pregen_claim` default path) behave exactly
  as before — `claimed_by_kind` defaults to `player` and every player-facing string and
  status is untouched. Keeper NPCs are still never convertible into companions (their
  names are not roster characters, so an AI claim simply finds nothing; the writer's
  `KeeperNpcNameTakenError` still guards a direct `create_companion`). Legacy companions
  created before claims existed keep working with `pregen_id=""` — only new paths are
  constrained. The future NPC→character converter stays out of scope; the roster is the
  single door in, so a converter only has to produce a roster entry to reuse every
  claim path.
- **Rule home:** `core/pregen_roster.py` (claim holder kinds, `owner_uid`);
  `agent/npc.py` (`companion_from_pregen`, `pregen_id`); `agent/kp_tools_companion.py`
  (`claim_pregen_as_companion`, `release_pregen_companion`, `add_companion`);
  `gateway/commands/sheet.py` (`.pc gen`); `agent/module_lifecycle.py` (room-born
  pregens survive a module swap).
- **Date:** 2026-08-27.
