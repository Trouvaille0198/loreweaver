# Item archiving: shelf gear without losing it

- **Problem:** a character's inventory is all-or-nothing. `drop` deletes an item
  permanently; there was no way to set gear aside — keep the record, but stop it
  mattering in play and stop it riding into the next scenario.
- **Decision:** `item` instances gain an `archived` flag.
  - Shelved items drop out of `render_held_items` (the sheet's `equipment` name
    list — party panel, keeper prompt) and of `aggregate_equipped_bonuses` (no
    bonus); archiving also clears the equip slot so a shelved item can never
    keep granting its bonus. `render_item_views` still carries them (with the
    `archived` flag) so the owner's character page can list and restore them.
  - CLI: `.item archive <name>` / `.item unarchive <name>` (aliases
    归档/封存/恢复/解封/還原); `.item inv` lists active items only,
    `.item inv --archived` lists shelved ones; `equip`/`use` refuse a shelved
    item (restore first); `drop` still works on it.
  - Web: the character page's equipment section splits into active items (each
    with an Archive button) and an "已归档装备 / Archived equipment" section
    with per-item Restore buttons; `ItemView` gains additive `archived?`.
- **Rule home:** `agent/items.py` (`_instance_data`, `render_held_items`,
  `render_item_views`, `aggregate_equipped_bonuses`, `set_archived`);
  `gateway/commands/item.py`; `clients/protocol/src/types.ts` (`ItemView`);
  web `src/protocol-augment.d.ts`, `CharacterScreen.tsx` (ItemCard + sections),
  `src/i18n/locales/{en,zh}.json`; `locales/{en,zh}/commands.json`.
- **Date:** 2026-08-26.
