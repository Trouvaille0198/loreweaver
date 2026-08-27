# Top-level meters come from ungrouped pools only

## Problem

`dnd5e` declared its meters twice: the legacy `sheet.resources` row
(`{id: hp, label: HP, source: hit_points}`) and the runtime `resources.pools`
block (`hp` + `temp_hp` + grouped `hit_die_*` / `spell_slot_*`). Both wire
lanes shipped — top-level `state.character.resources` from the legacy row, and
`resource_groups` from the runtime projection — so a D&D sheet showed HP twice
(`HP` from the legacy row, `生命值` from the pool) plus temporary HP.

## Decision

The runtime pools contract is the single source for a runtime pack's meters.

- `core.sheets.wire_resources` (the shared top-level meters entry point used
  by the state frame, party roster sync, and AI prompt lanes) feeds runtime
  packs from `core.resources.resource_projection`, keeping only UNGROUPED
  pools — the top-level vitals (HP / temp-HP style). Grouped pools ride the
  `resource_groups` lane exclusively.
- `net.state._character_payload` drops blank `resource_groups` entries
  (ungrouped pools already ride `resources`), so nothing renders twice.
- `core.character_manager.resource_label_map` reads runtime pool labels too,
  so roster re-labeling stays viewer-localized for runtime packs.
- `core.runtime.resolve_display_label` is the single label-resolution helper
  shared by the pool spec and the value projection.
- `rulepacks/dnd5e.yaml` no longer declares legacy `sheet.resources`; legacy
  packs (`coc7`) keep their declaration and the fallback path.

## Result

A D&D character shows 生命值 and 临时生命值 exactly once, plus the
`hit_dice` / `spell_slots` groups; coc7 is unchanged.
