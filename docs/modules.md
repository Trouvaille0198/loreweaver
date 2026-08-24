*English · [中文](modules.zh.md)*

# From a module file to a played campaign

*For players, Keepers, and authors who want the whole lifecycle. For a quick start, read
[Playing](play.md); to build one, continue with [Authoring](authoring.md).*

“Import a module” sounds like opening one file. In Loreweaver it covers at least four distinct
acts: installing content on a server, selecting a world for a room, admitting material to the
Keeper context, and letting rules and state operate on every turn. Keeping those layers separate
makes both normal operation and failures much easier to understand.

---

## 1. What is a module?

A Loreweaver **module is executable adventure content**. It may contain prose, but it can also
declare rules, secrets, a cast, state, UI, and turn-time behavior. It gives the Keeper a world to
run and the deterministic engine a ledger to maintain.

Three carriers are often called a “module” even though they are not interchangeable:

| Carrier | What it is | What it adds to a room |
|---|---|---|
| Source scenario document | `.txt`, `.md`, `.pdf`, `.doc`, or `.docx` prose | An analyzed Keeper knowledge pool and player-grade knowledge projection |
| World card | Native `*.lorecard.json`, or SillyTavern JSON / PNG with world machinery | Structured lore, variables, hooks, pregens, cast, and rule-system selection |
| Content pack | A `.lwpack` with a `pack.yaml` manifest | An installation and distribution container; it may hold one, many, or no world cards |

A **`.lwpack` is therefore not necessarily a module**. It can carry a complete adventure,
several selectable adventures, or extensions only. Conversely, an unpackaged prose scenario can
enter a room through `.module`.

### What a complete content pack can carry

```text
harbour-bell/
  pack.yaml                    identity, version, authors, licence, content manifest
  cards/*.lorecard.json        world cards and character cards
  rulepacks/*.yaml             rule systems or patches over a base system
  lorebooks/*                  standalone lorebooks
  skills/<id>/SKILL.md         Keeper procedure and tool constraints
  skills/<id>/hooks.js         sandboxed turn-lifecycle behavior
  ui/panels.yaml               variable panels, sidebars, and modals
  ui/presentation.yaml         title cards, letters, clippings, map pins
  presets/*                    prompt-style presets
  prep/*                       scripts a Keeper explicitly runs during preparation
  assets/*                     images, audio, and other media
```

Every directory is optional except the manifest and whatever files that manifest declares. A
small native module can be one manifest plus one world card. A prose file is sufficient when the
table needs knowledge but no structured machinery.

---

## 2. From a file to a room

First separate installation from room activation:

```text
authoring tree ──pack──> .lwpack ──install──> server data directory
                                                   │
                              Keeper selects and enables content
                                                   ▼
                                     room data, rules, UI, state
                                                   │
                                         ordinary player input
                                                   ▼
                                      turn assembly and resolution
```

### Route A: import a source scenario document

The Keeper runs:

```text
.module <file>
```

The server:

1. reads text or extracts it from PDF / Word;
2. chunks and indexes the source;
3. uses the dedicated module-analysis lane to identify scenes, NPCs, clues, secrets, pacing,
   and rule hints;
4. builds separate projections: the Keeper pool retains answers, while analysis excludes
   Keeper-marked secrets from the player pool;
5. marks initialization playable, or records an explicit deterministic fallback when analysis
   cannot complete.

The room refuses ordinary player actions while analysis is in progress. When it finishes, the
module pool becomes a stable part of every Keeper prompt; indexed source chunks are recalled only
when relevant.

This route is useful for an existing conventional scenario. It does **not invent** typed
variables, hooks, panels, pregens, or rulepacks. A line saying “alert runs from 0 to 5” remains
prose unless an author declares that tracker as structured data.

### Route B: install and enable a content pack

At the server terminal:

```bash
uv run python -m app --install <local-path|https-url|github-url|gh:owner/repo[@tag]>
```

This validates the manifest, hashes, and trust disclosure, then places content in the server data
directory. It does not choose anything for a room.

A Keeper can instead install from inside a remote room:

```text
.pack install <local-path|https-url|github-url|gh:owner/repo[@tag]>
```

That room command first installs the pack. If the pack contains exactly one world card, the card,
same-source lorebooks, panels, presentation kit, and KP skills become the room module together. If
it contains several world cards, the receipt lists explicit choices and enables no module-owned
switch before the Keeper selects one. An extension-only pack occupies no module slot and enables
the KP skills and UI it actually declares.

Activation differs by content kind:

| Content | Server `--install` only | Room `.pack install` | Manual entry point |
|---|---:|---:|---|
| World card | Installed | Imported when unique; choice when multiple | `.import <pack/card> world` |
| Character card | Installed | Not claimed | Player `.import <pack/card> pc` |
| Rulepack | Installed and discoverable | Available to the world card | Selected by the card or character creation |
| KP skill | Installed and discoverable | Enabled for one world card or an extension pack; waits on a multi-card choice | `.skill enable <id>` |
| Panels and presentation | Installed and discoverable | Enabled for one world card or an extension pack; waits on a multi-card choice | `.panels enable <pack-id>` |
| Standalone lorebook | Installed | Imported as same-source module material for one world card; otherwise not imported | `.lore import <pack/path>` |
| Prompt preset | Installed | Not enabled | `.preset import …`, then `.preset enable <id>` |
| Prep script | Installed | Not run | Explicit Keeper action during prep |
| Asset | Installed and served by hash | Used through references | Referenced by cards, panels, hooks, or Director |

Trust this matrix more than the phrase “the pack is installed.” Executable content has room-level
admission points so files on disk do not silently reshape a campaign already in progress.

### Route C: import a world card

The Keeper runs:

```text
.import <file-or-pack-reference> [rule-system] world
```

Players cannot use the `world` route. The character import route structurally removes world
scripts, variable schemas, and EJS; only the Keeper can admit machinery and secrets that change the
whole table.

A world-card import processes:

1. card parsing and the split between character content and world machinery;
2. explicit, pack-declared, and room-default rule-system selection;
3. worldbook entries and compatible InitVar / MVU state;
4. room hooks and EJS template capability;
5. module identity and the Keeper-only brief;
6. typed variable definitions;
7. pregens and card-native cast records;
8. the room-system pin and KP skills, panels, presentation material, and standalone lorebooks
   shipped by the same pack.

The whole operation is an atomic replacement: each room has exactly one module, and prose modules
and world cards are mutually exclusive. Any failure restores the current module and room switches
to their pre-import state. An import receipt means preparation landed; it is not opening narration.
Confirm characters, rules, and panels. The next ordinary player action then enters the normal turn
pipeline.

### Route D: live mounting while authoring

With a development source root enabled, an author can run:

```text
.dev mount <source-directory>
```

The sandbox room reloads cards, lorebooks, and UI after saves. This is an authoring workflow, not a
distribution mechanism; players should receive a built and verified `.lwpack`.

---

## 3. What does the room gain?

An imported module is not one large message pasted into chat. It adds two groups of data.

### Static content supplied by the author

- **Keeper knowledge:** answers, hidden motives, scene structure, clue relationships, and running
  instructions;
- **worldbook entries:** selected by keys, conditions, probability, priority, delay, cooldown, and
  sticky state;
- **rulepacks:** checks, result ranks, sheet shapes, derived values, resources, and commands;
- **typed variables:** trackers with kinds, ranges, enums, and viewer visibility;
- **MVU state:** hierarchical state from the SillyTavern ecosystem, player-hidden by default;
- **pregens and cast:** claimable characters plus NPC / companion records and sheets;
- **KP skills:** Keeper procedure, style, tool constraints, and optional hooks;
- **hooks and templates:** responses to turn events, state updates, blocks, and rendered content;
- **panels, presentation, and assets:** the table-facing display and media layer.

### Campaign data produced by play

- claims, player sheets, and resource changes;
- game clock, scene, combat order, relationships, and module-variable state;
- Scribe proposals derived from narration and accepted through engine validation;
- turn chronicles, campaign summary, open threads, and recalled history;
- learned habits of this particular table;
- narrative log, media events, panel state, and reports.

The second group belongs to **this room's campaign**, not to the static pack. A backup must carry it.
Importing the same pack into another room does not copy the story this group played.

### Who can see what

The Keeper receives the complete module truth because a mystery's host must know the answer.
Players, NPCs, companions, and Director receive their respective document projections. Keeper-only
lore, NPC secrets, Keeper trackers, and unexposed MVU leaves never enter a player-grade transport.

This does not prove that the main Keeper model will never reveal something it has seen. Structure
prevents player-grade actors from receiving unauthorized data; restraint by the main Keeper is a
behavioral property measured with live-model evaluations.

---

## 4. How a module is played on each turn

Players do not need to “start the script.” They state what their characters do:

```text
player intent
  │
  ├─ engine reads the character, room state, relevant lore, and campaign memory
  ├─ the single Keeper assembler constructs this turn's context
  ├─ Keeper narrates or calls tools
  │    ├─ checks: engine rolls first and computes the result rank
  │    ├─ state: engine validates types, bounds, permissions, and rules
  │    └─ NPCs: scoped actors receive only their own records and knowledge
  ├─ reply streams into the narrative log
  ├─ Scribe reconciles objective facts and writes the chronicle
  └─ on a story beat, Director emits presentation from the player projection
```

### Module material present in a Keeper turn

The stable prompt head carries rule expertise, interaction policy, the analyzed prose pool, the
enabled prompt preset, KP skills, and campaign summary. The volatile tail carries context-matched
worldbook entries, game state, relationships, variables, MVU, hook injections, recent threads, and
recalled history.

The whole worldbook is not inserted every time. Recent dialogue and state select entries under item
and character budgets. `constant` entries stay resident; keyed and conditional entries arrive when
relevant. A module can therefore exceed a single model context without unrelated lore drowning the
current scene.

### What each data type does during play

| Data | Consumer | Table effect |
|---|---|---|
| Scenes, clues, secrets | Keeper prompt and retrieval | Informs narration and clue placement; secrets are not sent directly to players |
| Rulepack | Deterministic engine | Chooses dice, result ranks, sheet shape, and legal writes |
| Worldbook triggers | Worldbook engine | Admits entries only for relevant topics, states, or timings |
| Typed variables | Engine, Keeper, panels | Tracks alert, dates, progress; invalid or out-of-range writes are rejected or clamped |
| MVU tree | Hooks, EJS, Keeper, panels | Carries compatible hierarchy; `.var expose` admits selected branches to players |
| Pregens | Character and sheet systems | Exclusive player claim; release restores the pristine module sheet |
| NPC / companion records | Scoped actors | Bounds each actor's persona, agenda, knowledge, and numerical ability |
| KP skills | Keeper prompt and tool filter | Tells the Keeper how to run the material and may unlock tools or hooks |
| Hooks | Sandboxed runtime | Reacts to turns, checks, and variable changes with declared effects |
| Panels | Server projection and client | Renders permitted state as bars, lists, sidebars, or modals |
| Presentation kit | Director | Produces title cards, letters, clippings, map pins, images, and audio cues on beats |
| Assets | Client media channel | Displays handouts, maps, portraits; plays BGM, ambience, and SFX |
| Chronicle and summary | Folding and `.recap` | Sustains long campaigns; the player recap excludes Keeper annotations |

The central boundary is: **models tell the story; code owns facts**. Dice, result ranks, sheets,
variable bounds, permissions, projections, and random tables cannot be improvised by the model.

---

## 5. Preflight for a table

For a structured module, a Keeper should inspect the room rather than treating one success line as
the whole preflight:

```text
.import list                 list installed cards
.pack install <ref>         install and enable a pack
.pc                         inspect the pregen pool
.skill status               inspect Keeper skills
.panels                     inspect module UI
.var list                   inspect trackers and hidden variables
.phase                      inspect prep / play Keeper tool phase
```

A successful prose-module or world-card import makes automatic tool-phase selection enter `play`.
Only an explicit `.phase` pin overrides that automatic choice.

Also verify that:

- the selected rule system matches the character sheets;
- intended MVU branches are exposed and secrets remain hidden;
- required panels and KP skills are enabled;
- prompt presets and prep scripts are explicitly enabled or run as the receipt directs;
- same-module lorebooks are present and extra material is handled as the author directs;
- every player has claimed a pregen or created a legal character;
- an important campaign is backed up before its first live-model turn.

### Switching to another module

Independent campaigns usually deserve independent rooms. Logs, player characters, chronicles,
habits, and manual settings are durable campaign state and are not owned by module import.

Importing another prose module or world card in the same room replaces the current module's
worldbooks, knowledge pools, brief, hooks, variable schemas, MVU tree, pregens, module NPCs, module
vectors, and module-owned skill and UI switches. Manually enabled general skills, non-module
material, player characters, and campaign records remain. Back up an important campaign first; a
failed import leaves the room in its pre-import state.

---

## 6. How players play well

### The simplest interface

State three things in ordinary language: **your intent, your method, and the risk you accept**.

> I circle along the warehouse wall and listen at the rear window. If it sounds empty, I try the
> lock with wire, but I do not want to alert the dock guards.

That gives the Keeper enough shape to retrieve the right scene material, select an appropriate
skill, and attach a meaningful consequence to failure.

### Commands worth knowing

| Goal | Command |
|---|---|
| Inspect or claim a module character | `.pc`, `.pc claim <name>` |
| Roll dice directly | `.r <expression>` |
| Check a sheet skill | `.ra <skill>` |
| Inspect / edit an allowed sheet value | `.st`, `.st <field>=<value>` |
| Render a module panel as text | `.panel <id>` |
| Read a spoiler-free recap | `.recap` |
| Export the session report | `.report` |
| List every command available at this table | `.help` |

### Six practical habits

1. **Separate player inference from character knowledge.** A suspicious panel value does not tell
   the character its hidden cause.
2. **Describe the method before reaching for a check.** Let the Keeper decide whether uncertainty
   calls for dice; direct `.ra` is useful when the rule and target are already clear.
3. **Change approach after failure instead of rerolling in place.** Failure should change position,
   cost, or route; use that change to continue.
4. **Inspect handouts and panels.** Images, letters, pins, and progress bars can be clues rather
   than decoration.
5. **Let companions act from their own knowledge.** NPCs and AI companions are not omniscient;
   choosing whom to ask and what to share is part of play.
6. **Start a returning session with `.recap`.** Restore shared facts from the player projection
   instead of guessing from chat memory.

If narrative, panel, and rules disagree, point to the exact value and ask the Keeper to reconcile
it. “The story says day three while the tracker says day one” is a reportable defect, not something
the model should cover with more prose.

---

## 7. Trust, privacy, and failure boundaries

- Pack installation verifies the manifest and hashes and discloses scripts, EJS, assets, and
  possible image-generation spend. It remains third-party content you have chosen to run.
- Hooks and EJS run in QuickJS sandboxes, but may change room state and output within declared
  capabilities.
- A remote model receives the prompt for its lane. The main Keeper prompt deliberately includes
  Keeper secrets; use a local model backend when content must not leave the host.
- Player-grade actors receive document projections. Character-card import strips world scripts,
  variable schemas, and EJS.
- Do not continue play by assumption after an import, analysis, or install failure. Inspect the
  receipt and room state, then retry, restore a backup, or move to a clean room.

---

## 8. Implementation contract and regression boundaries

Every module entry point follows these contracts. Changes to module import must preserve them:

| # | Contract | Observable result |
|---:|---|---|
| 1 | Each room has one module; prose modules and world cards are mutually exclusive. | The Keeper never receives prompt material from two modules. |
| 2 | Every module artifact has stable provenance and replacement cleans by source. | Hooks, variables, briefs, MVU, pregens, NPCs, and UI do not leak across modules. |
| 3 | Skill and UI switches record module ownership. | Replacement disables only module-owned entries and preserves manual general-purpose choices. |
| 4 | Admission, cleanup, system pinning, and switch activation share one transaction. | Any failure restores the complete pre-import room state. |
| 5 | Command and admin entry points return the real transaction result. | A body-level failure cannot accompany `ok: true`, and partial content is not current. |
| 6 | Pack cards are resolved from manifest paths and `kind`. | Nested native JSON and Tavern JSON / PNG work, while multiple world cards require a choice. |
| 7 | Admin lists only packs that can be modules and matches current by stable identity. | Extension packs do not masquerade as modules and same-name cards do not collide. |
| 8 | Admin import and room commands share the transaction, turn lock, and activation rules. | Every entry point creates the same room and cannot interleave writes with a player turn. |
| 9 | A world card and its pack's standalone lorebooks share one module source. | Authors can compose structured cards and supplemental lorebooks into one module. |
| 10 | Install receipts distinguish activated content from manual follow-up. | Presets, prep scripts, and inactive lorebooks are never implied to be playable. |
| 11 | Source documents have stable ids; same-name reimport replaces vectors and delete covers every same-name record. | Retrieval cannot recall stale text from the same path. |
| 12 | Module identity includes type, pack id, pack version, and card path provenance. | Same-name modules switch safely and admin can match current reliably. |
| 13 | Keeper expertise uses the room-pinned rule system. | It cannot load unrelated system guidance before a player character is claimed. |
| 14 | Either successful module form makes automatic tool phase enter `play`. | World-card rooms do not retain prep-only bulk tools. |
| 15 | The player pool begins with the opening scene and scene clues unlock through discovery. | Player-visible does not mean player-known, and undiscovered clues stay out of player projection. |
| 16 | NPC / companion calls without a room key use the deployment default model. | Standalone actor calls do not fail in room-model selection before the model request. |

The rule ownership and transaction boundary are recorded in
[`notes/implemented/module-lifecycle-consistency.md`](notes/implemented/module-lifecycle-consistency.md).
Implementation entry points include `agent/module_lifecycle.py`, `agent/kp_tools_charcard.py`,
`agent/kp_tools_knowledge.py`, `module_admin.py`, and `gateway/commands/panels.py`.

---

## Where to go next

| Goal | Document |
|---|---|
| Player keys, dice, sheets, and recaps | [play.md](play.md) |
| Running, backup, reset, and playtesting | [operating.md](operating.md) |
| Build a `.lwpack` from zero | [authoring.md](authoring.md) |
| Cards, card split, and SillyTavern compatibility | [cards.md](cards.md) |
| Pack fields, limits, and trust contract | [plugins.md](plugins.md) |
| Hook events and sandbox API | [hooks.md](hooks.md) |
