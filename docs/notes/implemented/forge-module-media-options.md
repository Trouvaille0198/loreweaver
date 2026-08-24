# Implemented: keeper-selectable media & companion content for AI-generated modules

- **Problem:** an AI-generated module (`agent.forge.generate_and_install_module`)
  produced exactly one artifact — a Markdown scenario document installed into the
  room's knowledge pool — while a hand-authored pack also ships binary media (scene
  art, NPC portraits, item/clue illustrations) and structured content (skills,
  rulepacks, cards). The forge path touched neither the image model nor the other
  `generate_*` lanes.

- **Verdict (shipped 2026-08-23):** per-generation keeper opt-ins on the module
  forge, in two groups — the keeper picks per generation, everything UNCHECKED by
  default (media costs real API money), and the selection is never persisted:

  - **Group A — media** (`media:` `cover` ≤1 / `scenes` ≤6 / `npcs` ≤6 / `items` ≤6,
    hard total 12): after the module is installed, one scoped shot-list call (LLM
    JSON `{kind, subject, prompt, caption}`) plans the images, then the room's own
    imagegen lane renders them (`allow_imagegen_request` hourly cap honored — a
    reached cap stops further renders, earlier ones kept) and each lands in the
    room's media deck via `MediaStore.register_blob` under a
    `module-<id>-<kind>-<n>.png` provenance name. Not auto-broadcast: the keeper
    pushes handouts when the table calls for them, same stance as a pack's assets.
    One portrait per NPC, enforced in the prompt AND at parse time; no cross-shot
    likeness promise.
  - **Group B — companion** (`companion:` `skills` / `rulepacks` / `cards`): each
    rides its OWN existing engine, no bespoke pipelines — `generate_and_install_skill`,
    `generate_and_install_rulepack`, and the `.genchar` sheet pipeline
    (`build_sheet_from_description` + `validate_sheet` + `core.pregen_roster`), the
    last fed by one scoped concept call (≤4 player-safe PC concepts) and landing on
    the room's claimable roster (`.pc claim`).

  Both passes run SYNCHRONOUSLY inside the `admin_generate` reply, AFTER the module
  itself is safely installed, and NEVER fail the module: any error (provider down,
  rate limit, unparseable model reply) degrades to fewer/zero artifacts and is
  reported as extra lines in `ForgeResult.detail` — the confirmation names every
  generated artifact. The selection folds into the repeat-request hash, so
  re-asking with different options is a real new request, not a suppressed duplicate.

- **Wire (protocol 2.5, additive):** `admin_generate` gains
  `options?: {media?: string[], companion?: string[]}`, honored for `kind:"module"`
  only; unknown ids are ignored server-side, a malformed shape is dropped, an absent
  field is exactly the old behavior. The conversational path mirrors it:
  `kp_tools_forge.generate_module(description, media?, companion?)`.

- **Shot-list/concept prompts** live in `locales/{en,zh}/agent.json`
  (`agent.forge.module_media_*`, `agent.forge.module_cards_*`), mirroring
  `module_system_prompt`'s register: what the output is FOR, "Output ONLY …",
  grounding in the module's own text, caps, and player-safety (an image/concept may
  show only what players may know). One scoped assembler per lane, AUTHORING lane in
  the model-call registry — no new registry entry needed (all calls live in
  `agent/forge.py`).

- **Audio (BGM/ambience/SFX) is deliberately absent from both groups** — keeper veto
  (2026-08-22).

- **Deferred (was "coming soon" in the UI):** `worldbook`, `presets`, `presentation`,
  `panels` — four companion kinds with no existing generator; each is a new lane the
  owner schedules separately. The web UI renders them disabled with a "coming soon"
  marker so the intended scope stays visible.

- **Rule home:** `agent/forge.py` (module docstring + `generate_and_install_module`,
  `_module_media_pass`, `_module_companion_pass`); wire contract in
  `docs/protocol.md` (`admin_generate`); client checkboxes in loreweaver-web
  `ModuleScreen.tsx`. Tests: `tests/agent/test_forge_module.py` (e),
  `tests/net/test_admin.py` (options wire test), `clients/protocol/src/client.test.ts`.

- **Date:** 2026-08-23 (proposed 2026-08-22, owner verdicts same day).
