# Implemented: unified LLM credential book + import/export integrity + room-dangling references

Date: 2026-08-25. Implemented (was `docs/notes/pending/llm-credentials-unification.md`).

## Problem

Three coupled defects in the LLM configuration system:

1. **Two credential books duplicated the same keys.** `llm_profiles`
   (`runtime_config.llm_profiles`, keyed `provider::[kind::]model`) was the typed
   new path; `llm_credentials` (`runtime_config.credentials`, keyed `provider`)
   was the legacy path. Same `CredentialBook` class, different Store key, and
   `MutableLLM._with_saved_credentials` fell back across both — pure historical
   baggage, not a design need.
2. **Import/export did not round-trip everything.** `_export_llm_config` carried
   the credential boxes but (until a prior fix) NOT the live image-generation
   runtime selection; room-level selections were never part of it. And the v1
   import WIPED typed profiles: it called `replace_all(profiles)` then
   `replace_all(credentials)`, the second replacing the whole book with just the
   legacy entries.
3. **Deleting an LLM profile left rooms silently dangling.** `room_llm` fell back
   to the global default when a room's selected profile was gone, and
   `_delete_llm_profile` cleared neither the referencing rooms nor surfaced the
   fallback.

## 1. Unify to a single book

- `llm_profiles` (`LLM_PROFILES_KEY`) is now the ONE book; `llm_credentials`
  (`CREDENTIALS_KEY`) no longer backs any live LLM credential.
- **One-shot boot migration** `infra/runtime_config.migrate_llm_credentials`
  (called from `build_services`): folds any legacy `runtime_config.credentials`
  into `runtime_config.llm_profiles` — a chat_model-ed entry lands under
  `model_profile_id(provider, chat_model)`, a bare entry (OAuth / keyless) stays
  keyed by provider — then drops the legacy key. Idempotent.
- `MutableLLM` drops its `profiles` param; `_with_saved_credentials` is a single
  book lookup (bare provider key, then a scan of that provider's chat profiles).
  `Services.llm_credentials` is removed; every read/write moved to
  `llm_profiles`.
- `build_imagegen` param renamed `llm_credentials` → `credentials` (the same
  unified book supplies supergrok's OAuth token).
- `_set_llm_profile` no longer mirrors chat profiles into a bare provider entry
  (the pre-unification duplicate) — one write per profile.
- `_llm_profiles` (admin view) drops its second merge loop (was double-deriving
  `provider::model::model` ids from the same book). It also skips a bare
  `provider` entry (written by the `_set_model` "set live default" path) when a
  typed `provider::model` profile for the same provider+model already exists —
  otherwise the bare live-credential mirror renders as a duplicate of the typed
  profile in the Model screen.

## 2. Import/export round-trip completeness

- Export (`_export_llm_config`) carries `llm_profiles` (unified) + `runtime`
  (live-merged) + `imagegen_credentials` + `imagegen_runtime`. No more
  `llm_credentials` field. `_LLM_EXPORT_VERSION` = 2.
- Import accepts v1 AND v2; a v1 document's legacy `llm_credentials` entries are
  folded into the unified book (same mapping as the boot migration). The v1
  double-`replace_all` wipe is fixed by a single `replace_all(profiles)`.
- `clients/protocol/src/types.ts` export/import config types updated (dropped
  `llm_credentials`, added `imagegen_runtime`). Protocol version unchanged (an
  admin-frame shape, not the wire protocol).
- `scripts/playtest.py` reads the unified book (falls back to the legacy key for
  pre-unification eval DBs) and selects the provider's chat profile.

## 3. Deleting a profile no longer leaves rooms dangling

- **On delete** (`_delete_llm_profile`): after forgetting the profile, calls the
  new `services.clear_room_model_profile(profile_id)`, which scans every room via
  `store.state_rooms()`, clears each `llm_selection` lane referencing the deleted
  profile, and logs the affected rooms.
- **On read** (`room_llm`, `imagegen_for_room`, `room_llm_model`): a lane whose
  profile no longer resolves logs a loud WARNING naming the room and the deleted
  profile, then falls back to the global default — no longer silent.

## Tests

- New: `test_migrate_llm_credentials_merges_legacy_into_unified_book`,
  `test_admin_import_llm_config_accepts_v1_and_merges_legacy`,
  `test_admin_delete_llm_profile_clears_rooms_referencing_it`,
  `test_admin_set_llm_profile_writes_only_the_unified_book`; rewrote the
  dual-book `MutableLLM` tests to single-book semantics.
- Updated: export/import round-trip (version 2, no `llm_credentials`), all
  `services.llm_credentials` → `services.llm_profiles` references, imagegen
  `credentials=` param, `test_commands` book-key assertions to `LLM_PROFILES_KEY`.

## Notes / pre-existing

- `MutableLLM` gained a `base` parameter: `build_services` passes the deployment
  baseline captured BEFORE persisted runtime overrides are applied, so
  `apply({})` (used when deleting the live-default profile) resets to the true
  default instead of the stale startup-overridden snapshot. Without it, deleting
  every profile left the running Keeper reporting the old model as the default.
- The web frontend's `isAdditiveServerFrame` validation for `admin_llm_export`
  required the legacy `llm_credentials` field; the unified export no longer
  sends it, so the web Model screen silently dropped the frame and the export
  button stayed stuck on "processing". Fixed in the web repo
  (`webTransport.ts` + `protocol-augment.d.ts` + test) to validate `version`,
  `llm_profiles`, `runtime`, `imagegen_credentials`, `imagegen_runtime`.
- `test_new_model_endpoint_never_receives_or_remembers_the_old_key` still fails:
  an unrelated uncommitted `_set_model` change stores `chat_model` in the bare
  provider entry, making the test's `== {"base_url": ...}` assertion stale. Not
  touched by this work; pending user decision.
- `test_panels_wire` (utf-32-be) and `test_tui_server` (pregen claimed_by) remain
  pre-existing failures, unrelated to this change.
