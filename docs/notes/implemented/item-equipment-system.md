# Implemented: item / equipment system

- **Status:** both phases landed 2026-08-25. Phase 1 shipped the cross-owner item
  verbs (grant/transfer/remove/use) + the `.item` CLI on the free-text
  `equipment` list. Phase 2 replaced the implementation behind the same verbs
  with `item`/`item_catalog` Documents (M17), catalog-validated grants (D6),
  equip slots driving derived bonuses (D3), table-level projection (D5), and
  `module_initializer` seeding the catalog from a module script's `items`
  analysis (D1). One deviation from the plan below: rulepack-declared
  `items:` sections are NOT yet merged into the room catalog — the catalog is
  seeded solely from the module script's analysis (the engine still names no
  concrete item/slot category, so this is a later pack-data enhancement, not a
  vocabulary leak).
  The current scope also includes the Keeper's improv lane (D6's single
  exception: off-catalog grants become narrative trinkets, light capped bonuses
  or consumables) and D7 (the item pool holds only what the party must FIND —
  entries that read as the investigators' own starting gear are refused at the
  lorecard parser, and the generation prompts forbid listing it).

## Problem

`CharacterSheet.equipment` is a free-text `list[Any]` that is persisted
(`to_dict`/`from_dict`), shipped on the wire (`protocol/types.ts`) and shown to
the Keeper by `get_character_sheet`/`list_party_sheets` — but nothing can ever
WRITE it at runtime. There is no item tool, no CLI command, no hook, no
card-import path that touches it. Item is NOT the same concept as the
knowledge-pool `clue`: clue is the NARRATIVE/information dimension (what a
discovery reveals, which truth it points to), while an item's value is
MECHANICAL — the buffs/bonuses it grants a character. The world layer's closest
analogue is the `clue` pool (dual-pool projected by `_project_module_pool`),
but clue has no "who holds it" semantics, carries no mechanical effect, and
never enters a sheet. The engine therefore has NO mechanical item concept at
all, and conflating the two would discard exactly the buff function an item
exists to provide.

Net: an AI Keeper cannot grant, transfer or remove gear; "you found a key" is
narrative only and stays off the sheet. Upstream `1A7432/loreweaver` is
identical (same 3 `equipment` references in `character_manager.py`, same
missing tool) — so this is an intentional design boundary, and building beyond
it is a fork extension, not a fix.

## Decisions (owner-confirmed)

- **D1 — Items are a first-class module-analysis output.** `module_initializer`
  gains `items` as a top-level category alongside scenes/npcs/clues/truths.
- **D2 — The AI Keeper grants items during play.** `grant_item`/`transfer_item`/
  `remove_item`/`use_item` (non-`prep_only`) are the verbs.
- **D3 — Equip slots exist.** A character has declared slots (pack-named); an
  equipped item occupies one and its buff applies; unequipped items sit in the
  bag with no buff.
- **D4 — No shared party bag.** Every item is owned by exactly one character.
- **D5 — Any viewer may SEE any character's holdings.** The read view is
  table-level (each character's list is visible to the table); only
  `secret`-flagged items stay isolated (Keeper/owner only).
- **D6 — Catalog-gated grants, with one Keeper's exception.** Every player-side
  grant comes from a template in the room's catalog (seeded by the module
  script). The single exception is the Keeper's improv lane: an off-catalog
  grant creates an improvised one-off — a narrative trinket (no bonus), a light
  edge (per-stat cap ±2, total 4 points) or a consumable (quantity) — capped so
  it can never rival a designed item. Players cannot add off-catalog items.
- **D7 — The item pool holds what the party must FIND.** Module analysis, forge
  cards and lorecard `items:` never list the investigators' own starting gear;
  the lorecard parser skips entries whose origin reads as starter gear
  (调查员随身携带/自备, "starter"/"investigator") with a warning.

## Conceptual model: narrative vs mechanical

- **`clue`** — the narrative/information dimension. A discovery that pushes the
  plot or reveals a secret (`leads_to` a scene/truth/NPC). Lives in the
  knowledge pool. No holder, no mechanical effect.
- **`item`** — the mechanical/state dimension. Something a character holds that
  grants buffs, consumes, or otherwise changes sheet state. This is the concept
  the engine currently lacks entirely.

The two are **independent** and may overlap: a magic sword can be BOTH a clue
(the plot reveals the wielder's identity) and an item (grants +2 attack) — but
they are separate slots, and the item's worth is its mechanical buff, not its
place in a mystery. Design never reduces an item to a clue, and never makes a
clue the vehicle for buffs.

## Shared pattern: discover-to-hold state transfer

The deeper reason the first draft reached for clue is that clue and item share
a mechanism — BOTH are entities that transfer from "exists in the world" to
"the player/character has it", and the engine already implements that transfer
for information:

- `unlock_for_player` moves a clue from the keeper pool (full, spoiler) to the
  player pool (spoiler-free); projection decides what each viewer sees. That is
  the anti-metagaming ledger (iron rule 3).
- An item is the SAME state-transfer pattern applied to the mechanical
  dimension: an instance flips `owner` from `none`/`world` to a character;
  projection decides who may see it. The transfer is identical — only the
  payload differs (a buff-bearing item vs an information-bearing clue).

Design consequence: item instances REUSE this ledger shape (owner + projection
+ the document table), not a second state machine. `grant_item` is
`unlock_for_player`'s sibling — both mean "a room entity becomes visible to /
held by a viewer". Slot, weight and buff are then just fields on the transferred
payload, not a new subsystem. This keeps the two-step plan small: phase 1 adds
the transfer verbs; phase 2 attaches mechanical payload.

## Design

### Layer 0 — items as first-class module-analysis output

`module_initializer` gains `items` as a top-level analysis category (add to
`_LIST_FIELDS`), because an item is a structural part of a module, not a
runtime afterthought.

#### The generation prompt (crafted)

Add an `items` block to `_ANALYSIS_JSON_SCHEMA` (the fixed, non-i18n JSON
contract — same machine-format rationale as every other category):

```json
"items": [
    {
        "name": "item name (e.g. 'The Sunken Bell')",
        "kind": "weapon/armor/consumable/gem/tool/quest/misc - pick the single best fit",
        "description": "short player-visible intro (what it is, how it looks)",
        "lore": "background story - ONLY for notable/powerful items, else leave empty",
        "effect": "the mechanical effect (e.g. '+2 attack', 'heals 1d4', '+1 to Spot Hidden') - leave empty for purely narrative items",
        "origin": "the scene or NPC where it is found - be specific (a scene name, an NPC name)",
        "original_holder": "who held it before, if the module states it",
        "clue": "the narrative significance it reveals, if any (links to a clue/truth name)"
    }
]
```

And an analysis-instruction note, so the model extracts the right things:

> Only list items that MATTER to the module — things the investigators can
> acquire that carry mechanical or plot significance. Pure scene dressing (a
> chair, a vase) is not an item. Never assign an item to a specific character:
> who ends up holding it is decided in play, not by the script. Make
> `origin`/`original_holder` concrete, straight from the module text. Only
> notable/powerful items get a `lore`; ordinary items make do with
> `description`. An item's `clue` field links to a clue/truth entry when the
> item carries plot significance — but the item is NOT a clue; it is a thing
> with an effect.

The markdown fallback extracts an `物品`/`item` section exactly as it already
does `线索`/`clue` and `真相`/`truth`.

**Boundaries (kept honest):**

- **Script DESIGNS items (template + narrative linkage); it does NOT instance
  them.** The analysis declares kind, effect, origin and lore — it never grants
  one to a specific character. Pre-assigning who ends up with an item would
  spoil the reveal and discard the state-transfer semantics that make the
  runtime grant meaningful (D1).
- **Item linkage mirrors the clue DAG.** Each item may name a `clue`/`truth` it
  connects to, weaving into the module structure rather than a flat list.

### Layer 1 — item catalog: rulepack data

`module_initializer` seeds a per-room catalog from the script's `items`
(D1/D6), and `rulepacks/<system>.yaml` may declare additional catalog entries
or patch the slot vocabulary:

```yaml
items:
  shortsword:
    kind: weapon
    slot: main_hand        # which equip slot this occupies (D3)
    weight: 2
    bonus: { attack: "+2", ac: "+1" }   # condexpr-style exprs over the sheet DAG
  healing_potion:
    kind: consumable
    slot: bag              # non-equippable; sits in the bag
    effect: { hp: "2d4+2" }             # deferred use effect (optional)
```

- "what this *kind* of item is" is **data**, not code → iron rule 1.
- `slot` and `kind` are pack-named enums (the engine names no concrete category
  — the "vocabulary belongs to the pack" rule from `subsystems`). D3's slot
  set per system (e.g. dnd5e: `main_hand|off_hand|armor|two trinkets`) is pack
  data too.

### Layer 2 — item instances + slots: a new `Document` type (M17)

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
    "template_id": "shortsword",     # REQUIRED (D6): must exist in the catalog; no None
    "owner": "<character_id>",       # exactly one character (D4); no party bag
    "quantity": 1,
    "state": {},                     # uses_left, durability, attuned, ...
    "equipped_slot": "main_hand",    # None = in the bag (D3); buff applies only when set
}
```

- `template_id` is REQUIRED (D6) — no homebrew. Every item traces to a script/
  catalog template. `grant_item` refuses unknown ids.
- Inheritance from the template: `description`, optional `lore`, `kind`,
  `effect`, provenance (`origin`/`original_holder`) and `slot` are copied from
  the catalog at grant time; an instance may override them if the story
  develops. `owner`, `quantity`/`state` and `equipped_slot` are the runtime,
  mutable parts.
- Non-singleton (many items), `singleton_id=None` — the first plural registered
  type behind the existing singletons; the registry already keys on `name`.
- **Equip slots (D3)** are per-character, declared by the pack. `equip_item`
  sets `equipped_slot`; the buff contribution then flows into the derived DAG
  (below). Unequipped items sit in the bag with no buff. A slot already taken
  refuses a second occupant (or swaps — pack rule).

### Projection — table-level read, secret isolation (D5)

Any viewer sees any character's holdings; only `secret` items stay isolated:

| viewer | non-`secret` items | `secret` items |
|---|---|---|
| Keeper | all, incl. owner | all |
| any player | every character's list (table-level read) | none |
| owner's own actor | its own | its own `secret` |
| other actors | table-level non-`secret` | none |

`project(doc, viewer)` stays THE wire chokepoint (iron rule 3) — the table can
see who holds what (D5), but a `secret` item is invisible outside its owner/
Keeper by construction. Sentinel tests follow `tests/documents/
test_secrecy_sentinels.py` (oracle-first). This mirrors `_project_pregen`'s
"who exists is table talk, payload is restricted" split, applied to items.

### Bonus integration — the existing seam

`core/sheets.py` already reserves this: *"a stored value differing from the
derivation is a manual override (**armor changing AC**, a feature raising
passive senses) — keep it."* Equipped bonuses feed the derived DAG:

- `RulePack.compute_derived` is the modifier-layer insertion point; items with
  `equipped_slot` set contribute to the `values` namespace before derived
  recompute. Bagged items contribute nothing.
- "derived values are NEVER persisted": bonuses apply read-side in
  `refresh_sheet`/`compute_derived`, never written as storage overrides —
  un-equipping just drops the term. The existing `preserve_trained` path
  already refuses to overwrite a stored value that differs from derivation, so
  no collision.
- Bonus exprs reuse `core.condexpr` / `core.resolution` arithmetic — no new
  expression engine.

### Interfaces — the grant prompt (crafted)

**Keeper tools** (`agent/kp_tools_mechanics.py`, non-`prep_only` so play-phase
has them):

```python
@tool
async def grant_item(self, ctx: AgentCtx, character: str, item_id: str, qty: int = 1) -> str:
    """Grant a real item to a character once the party has ACTUALLY obtained it in
    play (picked up, looted, bought, rewarded).

    Args:
        character: the target character's name
        item_id: the item's template id from the room's catalog (module script)
        qty: quantity, default 1

    Rules:
    - Call ONLY when the item is genuinely in that character's hands in the story —
      never pre-award, never as a reward for narration alone.
    - The item MUST exist in the room's catalog (from the module script). You cannot
      invent a template; if the script has no such item, use improvise_item for a
      light off-catalog one-off, or keep it narrative/a clue until it becomes real.
    - After granting, your narration must reflect that the character now holds it
      ("You pick up X").
    - Check the current holder first (items may have moved via transfer).
    """
```

plus `transfer_item(from, to, item, qty)` / `remove_item(character, item, qty)` /
`use_item(character, item)` (consumes one unit — quantity decreases, zero removes) /
`equip_item(character, item, slot)` — and `improvise_item(character, name,
description, bonus, qty)`, the off-catalog lane: narrative trinkets, light capped
bonuses and consumables, capped so an improvised item never rivals a designed one.

**Item discipline block** — a third Keeper-discipline clause alongside
`prompt.keeper_discipline` / `prompt.module_fidelity` (which already fold into
`core.prompt_sections.inject_document_context_prompt`), keyed
`prompt.item_discipline` in `locales/{en,zh}/prompt.json`:

> Item discipline: grant items only when a character has genuinely acquired
> them in play (picked up, looted, bought, rewarded). Never pre-award, never for
> narration alone. The item must come from the room's catalog (module script) —
> do not invent templates; a missing item may become a light improvised one-off
> (improvise_item), else keep it narrative/clue until it becomes real. After
> granting, narrate that the character now holds it. Track current holders;
> avoid double grants or wrong targets.

The block is injected only when the room has an item-enabled catalog (same
gating style as keeper_discipline — a room with no items contributes no block).

**CLI** (`gateway/commands/router.py` spec row + a mixin module + i18n keys in
`locales/{en,zh}/commands.json` + tests in `tests/gateway/`):
- `.item inv [name]` — see any character's list (table-level read, D5)
- `.item add <name> [qty]` / `.item drop <name> [qty]`
  — players manage their own bag (catalog-validated, D6)
- `.item give <name> <to> [--desc <text> --bonus stat=n,… --qty n --secret]`
  — keeper-only; off-catalog names become improvised one-offs (D6 exception)
- `.item use <name> [qty]` — consume a held item (quantity decreases, zero removes)
- `.item equip <name> [slot]` / `.item unequip <name>` — slot control (D3)

Phase 1 ships the verbs + `.item inv/add/drop/give`; phase 2 adds
`equip/unequip` + the document/catalog path behind the same verbs.

### i18n

All user-facing strings via `infra.i18n` + `locales/{en,zh}/commands.json`
(iron rule 4). Item *data* (names, kind/slot enums, lore) is game data and
exempt, like existing skill/alias data. The generation schema and tool/docstring
prompt text are machine-format, model-facing, and not user-visible (same
exemption as `_ANALYSIS_JSON_SCHEMA`).

## Test plan

- `tests/agent/test_module_initializer.py` — `items` is first-class: LLM path
  produces kind/effect/origin/original_holder/clue linkage; markdown fallback
  extracts an `物品`/`item` section; items NEVER carry an owner.
- `tests/gateway/` — `.item` inv/add/drop/give/equip/unequip, table-level read
  (D5), keeper-vs-player gating, unknown-template rejection (D6).
- `tests/agent/` — `grant_item`/`transfer_item`/`remove_item`/`equip_item`
  behavior, play-phase availability, unknown-id refusal, slot conflicts (D3).
- `tests/documents/` — `item` projection sentinels: table-level non-`secret`
  view (D5), `secret` invisible outside owner/Keeper; other-player isolation
  (oracle-first like existing secrecy tests).
- `tests/core/` — catalog load/validation (bad slot, unknown bonus key);
  derived-DAG bonus: equip changes AC, unequip restores, no persisted override.
- `tests/architecture/` — tool-phase budget unchanged (`test_tool_phase_budget.py`):
  new tools are few and non-`prep_only`, adding to play phase is a schema
  budget bump that must be checked.

## Impact

- **Phase 1:** item verbs + `.item` + i18n + tests. No storage change, no
  protocol change (`equipment` field already on the wire). Does not touch iron
  rules.
- **Phase 2:** `item` Document type + projection + catalog schema + slot +
  derived insertion. Adds a facet to `net/room_lifecycle.FACET_MODULES` (which
  reset scope clears items) — enforced by `tests/architecture/
  test_room_facets.py`. May bump the wire if `equipment`'s shape changes (keep
  `item` doc ids additive to avoid a protocol bump; verify against
  `tests/architecture/test_protocol_version_sync.py` if touched).
- Non-breaking for existing data: current free-text `equipment` remains valid
  in phase 1; phase 2 keeps compatibility via a schema migration.

## Date

2026-08-25 (pending; owner review before implementation).
