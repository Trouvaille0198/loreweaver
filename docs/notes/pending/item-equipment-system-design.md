# Design (pending): item / equipment system

## Problem

`CharacterSheet.equipment` is a free-text `list[Any]` that is persisted
(`to_dict`/`from_dict`), shipped on the wire (`protocol/types.ts`) and shown to
the Keeper by `get_character_sheet`/`list_party_sheets` — but nothing can ever
WRITE it at runtime. There is no `update_character_equipment` tool, no CLI
command, no hook, no card-import path that touches it. The world layer has an
item-like concept (`clue` in the knowledge pool, dual-pool projected by
`_project_module_pool`), but clue is a *static world fact* with no "who holds
it" semantics and never enters a sheet.

Net: an AI Keeper cannot grant, transfer or remove gear; "you found a key" is
narrative only and stays off the sheet. Upstream `1A7432/loreweaver` is
identical (same 3 `equipment` references in `character_manager.py`, same
missing tool) — so this is an intentional design boundary, and building beyond
it is a fork extension, not a fix.

## Options

1. **Lightweight only** — add `update_character_equipment` (`@tool`, non
   `prep_only`) + a `.item` command; `equipment` stays a free-text list. No
   slot/weight/bonus semantics. Smallest change, satisfies "AI can hand out
   gear", but no rules.
2. **Full (architecture-native)** — item catalog in `rulepacks/*.yaml` +
   item instances as `Document`s (M17) + derived-DAG bonus integration.
   Correct long-term shape, large change.
3. **Two-step: 1 then 2** — ship the lightweight write path first (immediate
   need), then Document-ize items for rules.

## Recommendation

**Option 3**, landed as two independent changes. The two-layer insight that
drives the design: an item lives on the WORLD layer (exists, what it is, where
found → the existing `clue`/knowledge pool) AND on the CHARACTER layer (who
holds it, quantity, state, rule effects → `CharacterSheet.equipment`). The two
layers exist today but are not connected; the design connects them and adds
rules on top without inventing new storage or a new vocabulary.

## Design

### Layer 1 — item catalog: rulepack data (option 2 phase)

Declare in `rulepacks/<system>.yaml`:

```yaml
items:
  shortsword:
    slot: weapon            # weapon|armor|consumable|misc (pack-chosen enum)
    weight: 2
    tags: [finesse, light]
    bonus: { ac: "+1", attack: "+2" }   # condexpr-style exprs over the sheet DAG
  healing_potion:
    slot: consumable
    uses: 1
    effect: { hp: "2d4+2" }
```

- "what this *kind* of item is" is **data**, not code → iron rule 1: the
  deterministic half (weight, slots, bonus expressions) is pack data / real
  code; description/flavor stays the model's job.
- Follows the existing pattern of `sheet`/`resolution`/`subsystems` being
  pack-declared. The closed-set doctrine for subsystem *templates* does NOT
  apply here: item catalog is data (like `draw_table` entries), not a behavior
  template. An item-USE behavior (consume, equip) is a candidate for a new
  subsystem template (`use_item`) only if it recurs across systems.

### Layer 2 — item instances: a new `Document` type (M17)

New `item` document type registered via
`core.documents.register_document_type(DocumentType(...))`:

```python
DocumentType(
    name="item",
    schema_version=1,
    project=_project_item,
    validate_write=_validate_item,
)
```

Instance shape:

```python
{
    "template_id": "shortsword",     # rulepack catalog ref (None = one-off homebrew)
    "owner": "<character_id>|party|world|none",
    "quantity": 1,
    "state": {},                      # uses_left, durability, attuned, ...
    "description": "...",             # model-authored flavor
    "secret": false,                  # keeper-only whole-entry, mirror _project_lore
}
```

- Every room object is a `Document` in one table (M17 doctrine) → item
  instances get projection, validation, schema migration and room-lifecycle
  facets for free, no new store.
- Non-singleton (many items), so `singleton_id=None` — this is the first
  plural registered type behind the existing singletons, which is fine; the
  registry already keys on `name`.

### Layer 3 — character holding: wire `equipment` to instances

`CharacterSheet.equipment` graduates from free-text to a **list of item refs**
(`{"template_id"|"item_doc_id", "qty"}`), or stays as-is while item instances
carry `owner` — the two-step plan keeps phase 1 on the plain list and only
re-shapes it in phase 2. `to_dict`/`from_dict` and the wire
(`protocol/types.ts`) are the compatibility seam.

### Projection (phase 2; the anti-metagaming half)

Mirror the proven patterns in `core/documents.py`:

| viewer | view |
|---|---|
| Keeper | full instance + secret flag |
| owner character's own actor | its held items, non-secret |
| other players / other actors | `owner==me` subset only (like `_project_npc`), or public via `secret` |

`project(doc, viewer)` stays THE wire chokepoint (iron rule 3) — a held secret
item is invisible outside its owner/Keeper by construction. No new secrecy
surface. Sentinel tests follow `tests/documents/test_secrecy_sentinels.py`
(oracle-first).

### Bonus integration (phase 2) — the existing seam

`core/sheets.py` already reserves this: *"a stored value differing from the
derivation is a manual override (**armor changing AC**, a feature raising
passive senses) — keep it."* Equipped bonuses feed the derived DAG:

- `RulePack.compute_derived` is the modifier layer insertion point; equipped
  items contribute to the `values` namespace before derived recompute.
- "derived values are NEVER persisted": bonuses are applied read-side in
  `refresh_sheet`/`compute_derived`, never written as storage overrides —
  un-equipping just drops the term. The existing `preserve_trained` path
  already refuses to overwrite a stored value that differs from derivation,
  so no collision.
- Bonus exprs reuse `core.condexpr` / `core.resolution` arithmetic — no new
  expression engine.

### Interfaces (both phases, per project convention: major features need a CLI)

**Keeper tools** (`agent/kp_tools_mechanics.py`, non-`prep_only` so play-phase
has them):
- `grant_item(character, item_id|name, qty=1)` — add to sheet/instance
- `transfer_item(from, to, item, qty)` — move between characters/party
- `remove_item(character, item, qty)` / `use_item(character, item)` — consume/equip

**CLI** (`gateway/commands/router.py` spec row + a mixin module + i18n keys in
`locales/{en,zh}/commands.json` + tests in `tests/gateway/`):
- `.item add <name> [qty]` / `.item drop <name> [qty]` / `.item give <name> <to>` /
  `.item inv` — players manage their own; keeper-only verbs on others.

Phase 1 ships `update_character_equipment` + a minimal `.item add/drop/inv`;
phase 2 replaces the implementation behind the same verbs with the document +
catalog path.

### i18n

All user-facing strings via `infra.i18n` + `locales/{en,zh}/commands.json`
(iron rule 4). Item *data* (names, slot enums) is game data and exempt, like
existing skill/alias data.

## Test plan

- `tests/gateway/` — `.item` command: add/drop/inv/give, keeper-vs-player gating,
  error paths.
- `tests/agent/` — `grant_item`/`transfer_item`/`remove_item` tool behavior,
  phase gating (available in play phase).
- `tests/documents/` — `item` projection sentinels: secret item invisible
  outside owner/Keeper; other-player isolation (oracle-first like existing
  secrecy tests).
- `tests/core/` — catalog load/validation (bad slot, unknown bonus key);
  derived-DAG bonus: equip changes AC, un-equip restores, no persisted override.
- `tests/architecture/` — tool-phase budget unchanged (`test_tool_phase_budget.py`):
  new tools are few and non-`prep_only`, adding to play phase is a schema
  budget bump that must be checked.

## Impact

- **Phase 1:** 1 new tool + 1 command domain + i18n + tests. No storage change,
  no protocol change (`equipment` field already on the wire). Does not touch
  iron rules.
- **Phase 2:** new `item` Document type + projection + catalog schema + derived
  insertion. Adds a facet to `net/room_lifecycle.FACET_MODULES` (which reset
  scope clears items) — enforced by `tests/architecture/test_room_facets.py`.
  May bump the wire if `equipment`'s shape changes (keep `item` doc ids
  additive to avoid a protocol bump; verify against
  `tests/architecture/test_protocol_version_sync.py` if touched).
- Non-breaking for existing data: current free-text `equipment` remains valid
  in phase 1; phase 2 keeps compatibility via a schema migration.

## Open questions

1. Does the Keeper need an "equip slot" concept (e.g. one weapon + one armor),
   or is an unordered bag enough for the first cut? (Rules depth.)
2. Should `party`-shared inventory exist, or is every item individually owned?
3. Is a homebrew one-off item (no catalog template) a first-class case in phase
   2, or must every item have a catalog entry?

## Date

2026-08-25 (pending; owner review before implementation).
