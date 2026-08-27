*English · [中文](items.zh.md)*

# Items: granting, using, removing gear

Item records are the mechanical half of "you found a key": narration alone never puts an object
on a sheet. This page is the full mechanism — what an item is, who can grant one, how it is used,
equipped, transferred, removed and archived, and the engine rules that keep the item system honest.

## 1. What an item is

Two layers:

- **Catalog** — an `item_catalog` document, seeded when a module loads (from the lorecard's
  `items` analysis; a rulepack may declare more). Each template is the designed truth: name, kind,
  effect, bonus, scope, secret flag.
- **Instances** — one `item` document per holding, owned by exactly one character (there is no
  shared party bag). An instance copies its template's identity and tracks quantity, equip slot,
  state and the archived flag.

Terminology: items split by origin into **scenario items** (designed goods from the module's
catalog, `improvised` false — the client chip reads "剧本") and **improvised items** (off-catalog
trinkets/mementos/consumables a keeper makes up on the spot, `improvised` true — chip "即兴").

Every item belongs to exactly one character. Read access is table-level: any member can see any
character's items, except `secret`-flagged ones which stay keeper/owner-only.

## 2. Who can grant

| Actor | Channel | Scope |
|---|---|---|
| AI Keeper | `grant_item` | catalog templates only |
| AI Keeper | `improvise_item` | off-catalog trinkets, capped bonuses, consumables |
| Keeper | `.item give <item> <to> [--desc <text>] [--bonus stat=n,…] [--qty n] [--secret]` | any character; off-catalog names become improvised one-offs |
| Player | `.item add <name> [qty]` | own active character, catalog templates only |

The AI Keeper always SEES the catalog: the room's designed item names ride the stable
system prompt (`inject_item_catalog_prompt`), and `list_item_catalog` returns their full
mechanics (kind/slot/effect/bonus) on demand — so it grants designed gear by exact name
instead of improvising a mechanics-less substitute.

## 3. Granting rules

- `grant_item(character, item_id, qty=1)` — only once the party has ACTUALLY obtained the item in
  play (picked up, looted, bought, rewarded); never pre-award, never for narration alone. The item
  must exist in the catalog. A character already holding a non-consumable cannot be granted it
  again — handovers use `transfer_item`, losses use `remove_item`.
- `improvise_item(character, name, description="", bonus="", qty=1)` — the light channel: narrative
  trinkets, small rewards, consumables. The bonus is capped (each stat ±2, 4 points total) and the
  cap is enforced. Bonus keys resolve through the character's rulepack aliases (any spelling —
  `侦查`, `spot hidden`, `STR` — lands on the real skill/attribute key), and a bonus-bearing
  improvised item is equipped automatically (slot `equipped`) so its edge applies immediately; a
  purely narrative trinket stays unequipped in the bag. If `name` hits a catalog template, the real
  template is granted instead — improvising a designed name never degrades the designed item.
- When a player **explicitly takes, picks up, pockets or keeps** something, the item is committed
  in that same turn, before narration. Ownership is decided by the player's action, not by the
  wording of the narration.

## 4. Seeing items

- `.item inv [name]` — your own bag, or any character's; `--archived` lists shelved items.
- Character page / party panel — active items with quantity, slot, description, effect.
- Narrative mentions — `[[item name]]` renders a player-visible item card.

## 5. Using

- `use_item(character, item, qty=1)` / `.item use <name> [qty]` — consumes a held item: quantity
  decreases, zero removes the instance. A shelved (archived) item refuses use — restore it first.

## 6. Equipping

- `equip_item(character, item, slot)` / `.item equip <name> [slot]` — an equipped item occupies a
  slot and its bonus applies, summed into the sheet's `equipped_bonuses`; `.item unequip <name>`
  removes the bonus. Unequipped items sit in the bag with no bonus. Archiving clears the slot, so a
  shelved item can never keep granting its bonus.

## 7. Transfer

- `transfer_item(source, target, item, qty=1)` / `.item give <item> <to>` — moves a real item
  between two characters (handed over, sold, given away). The source must hold it; both characters
  must exist. Never re-grant an item that is simply moving around.

## 8. Removing

- `remove_item(character, item, qty=1)` / `.item drop <name> [qty]` — permanent. To shelf gear
  without losing the record, archive it first.

## 9. Archiving

- `.item archive <name>` — shelves an item: it drops out of the active bag, grants no bonus, and no
  longer rides into the next scenario; the record is kept. `.item unarchive <name>` restores it.
  `equip`/`use` refuse a shelved item; `drop` still works on it.

## 10. The engine keeps the system honest

Narration alone never changes inventory. Two guards:

- **End-of-turn check (`item_forged`)** — if the reply mentions a tracked item (catalog template or
  held instance) **or writes a possession claim** (收下/收起/带走/捡起/物品栏, pick up / put away /
  pocket / inventory …) but no item tool committed a change this turn, the turn is held open and
  the model is re-asked: call the matching item tool now (`grant_item`, `improvise_item`,
  `transfer_item`, `remove_item`, `use_item`, `equip_item`, …), wait for its success, then narrate.
  A mention-only reply (NPC dialogue, scenery) is confirmed with `NONE` and the prose stands.
- **Prompt discipline** — the standing instruction tells the AI Keeper that a player's explicit
  take is committed that same turn through the item tools, and that a possession claim in prose
  requires a matching successful tool call.

## 11. Data model

`item_catalog` template (author-facing):

- `name`, `kind` (`tool`/`weapon`/`consumable`/`misc`/`quest`), `slot`, `description`, `lore`,
  `effect`, `bonus`, `scope` (`universal`/`module`), `quantity`, `secret`, `plot_role`,
  `reveals`/`reveal_targets`, `origin`, `original_holder`

`item` instance (runtime):

- `template_id`, `name`, `owner` (sheet name, unique in room), `quantity`, `equipped_slot`, `state`,
  `scope`, `module_id`, `description`, `lore`, `effect`, `bonus`, `origin`, `original_holder`,
  `secret`, `improvised`, `archived`

Sheet wiring: `equipment` (active item names), `items` (structured views, including archived),
`equipped_bonuses` (sum of equipped active bonuses). Every mutation refreshes the sheet through the
shared bonus-refresh wiring, so a failed mutation can never roll back half an update.

---

*Next: [how modules become a played campaign](modules.md) · [running a table](operating.md) ·
[authoring a module](authoring.md)*
