# Deterministic runtime foundation

## Decision

Runtime mechanics are declared by an optional, versioned `runtime:` section on each
rulepack. `core.runtime` validates the closed generic contract, compiles arithmetic
expressions through `core.condexpr`, and keeps pack-owned ids and labels as data. A pack
without this section retains its check and sheet behavior and reports runtime commands as
unsupported.

Persistent counters use `core.resources.ResourceLedger`. Pools carry current/max values,
roles, groups, reset tags, dice, display metadata, and an optimistic revision. Runtime
sheets persist the pool state without changing the field-backed meter behavior of
non-runtime packs.

`core.combat.CombatState` is the single room-state authority for active order, round,
turn claims, budgets, reactions, and encounter-local mutable combatants. Pure transitions
perform no I/O. `infra.store.Store.compare_and_swap_room` is the cross-state/document
transaction boundary, while `commit_idempotent_room_mutation` records action results by
opaque action id so retries return the stored result without another roll.

The same foundation contains typed action, damage, condition, dying, rest, stat-block, and
encounter contracts. Document projections expose stat-block identity to players while
keeping mechanics and encounter definitions keeper-grade. Room facets claim combat action
results and stat-block/encounter documents for lifecycle cleanup.

## Source boundary

The built-in D&D data is limited to mechanics and short labels compatible with the
Creative Commons SRD 5.1. The licensing and attribution source is the official SRD
material distributed at:

- https://dnd.wizards.com/resources/systems-reference-document
- https://creativecommons.org/licenses/by/4.0/legalcode

No proprietary adventure or catalog text is bundled.
