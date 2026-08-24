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
uv run python -m app --install <local-path|https-url|gh:owner/repo[@tag]>
```

This validates the manifest, hashes, and trust disclosure, then places content in the server data
directory. It does not choose anything for a room.

A Keeper can instead install from inside a remote room:

```text
.pack install <local-path|https-url|gh:owner/repo[@tag]>
```

That room command installs the pack and enables its panels / presentation kit and KP skills. If
the pack contains exactly one world card, it imports that card. If it contains several, the receipt
lists explicit choices and leaves the fork to the Keeper.

Activation differs by content kind:

| Content | Server `--install` only | Room `.pack install` | Manual entry point |
|---|---:|---:|---|
| World card | Installed | Imported when unique; choice when multiple | `.import <pack/card> world` |
| Character card | Installed | Not claimed | Player `.import <pack/card> pc` |
| Rulepack | Installed and discoverable | Available to the world card | Selected by the card or character creation |
| KP skill | Installed and discoverable | Enabled | `.skill enable <id>` |
| Panels and presentation | Installed and discoverable | Enabled | `.panels enable <pack-id>` |
| Standalone lorebook | Installed | Not imported | `.lore import <pack/path>` |
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
8. the room-system pin and KP skills shipped by the same pack.

An import receipt means preparation landed; it is not opening narration. Confirm characters,
rules, panels, and the tool phase. The next ordinary player action then enters the normal turn
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

When a room uses only a world card and no `.module` source, automatic tool-phase selection does not
recognize that world import. Before play, explicitly run:

```text
.phase play
```

Also verify that:

- the selected rule system matches the character sheets;
- intended MVU branches are exposed and secrets remain hidden;
- required panels, KP skills, and prompt preset are enabled;
- standalone lorebooks are imported as the author directs;
- every player has claimed a pregen or created a legal character;
- an important campaign is backed up before its first live-model turn.

### Switching to another module

The safest course is **a new room for a new campaign**. Logs, characters, chronicles, habits, UI
switches, and prompt settings are durable room state and should not be erased by guesswork during an
import.

If a room must be reused, back it up, apply the reset scope appropriate to the campaign, and inspect
`.skill`, `.panels`, `.preset`, `.var`, and `.phase`. Do not treat another `.import … world` as a
complete cleanup operation. The implementation audit below confirms that the replacement path does
not yet own every piece of module machinery.

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

## 8. Implementation audit awaiting maintainer decisions

This is the 2026-08-24 audit of the working-tree implementation, targeted tests, and service-level
simulations. No finding was fixed as part of this documentation work. **Reproduced** means observed
through real stores and tool paths; **code review** means confirmed from control flow and state
contracts and still deserving a regression test.

| # | Evidence | Finding | Impact |
|---:|---|---|---|
| 1 | Reproduced | `.module` and `.import … world` coexist; neither clears the other route. | The Keeper can receive one prose module pool and another world card's lore in the same prompt. |
| 2 | Reproduced | World-card replacement removes only part of the prior lore / pregens. Prior hooks, typed variables, and Keeper briefs remain; MVU, UI, presets, and module NPCs have no complete provenance cleanup. | Machinery and state from the prior module may keep firing. |
| 3 | Reproduced | Replacement disables **every** enabled skill, rather than skills owned by the prior pack. | Manually enabled general-purpose Keeper skills are lost. |
| 4 | Reproduced | World import is not atomic. Lore, hooks, and the module marker land before later rulepack or pregen failures, with no rollback. | The receipt says failure while the room is partially imported and may have already purged prior content. |
| 5 | Reproduced | The admin pack-import path does not promote a failed world-card import to a failed result. | It returns `ok: true` while the receipt says failure and no playable module landed. |
| 6 | Code review | Admin import sorts `cards/*.lorecard.json` and selects the first, ignoring manifest paths, `kind`, and multi-world-card forks; ST JSON / PNG and nested declarations are missed. | It can choose a character card, the wrong world card, or report a usable pack as unusable. |
| 7 | Code review | Admin listing treats every installed pack as a module; `detail.current` is fixed false, while list current compares pack display name with card name. | Studio's current-module marker and source picker can be inaccurate. |
| 8 | Code review | Admin pack import does not mirror `.pack install` panel / presentation enablement, progress locking, or multi-card selection. | The same pack produces different room features by entry point and may compete with a player turn. |
| 9 | Code review | Activating a world card filters lore to `source == card name`; standalone `.lore import` uses a filename source. | A card and its pack's standalone lorebook cannot naturally be active together. |
| 10 | Code review | `.pack install`'s “every switch / playable” contract does not cover standalone lorebooks, prompt presets, or prep scripts. | The receipt can imply that all declared pack content is live when it is not. |
| 11 | Code review | Source documents receive random ids on each upload; same-name reimports accumulate vectors, while delete removes only the first filename match. | Retrieval tools can recall prior imports of the scenario. |
| 12 | Code review | Active module identity uses a repeatable card display name rather than pack id, version, and card path provenance. | Same-name modules can bypass cleanup and admin cannot reliably determine current. |
| 13 | Code review | Runtime rule resolution reads `room_system`, but Keeper expertise uses the deployment default when no player character is active. | Before a character is claimed, the Keeper may be taught a different system from the card's room pin. |
| 14 | Code review | Automatic tool phase recognizes only `.module` initialization, not a successful world-card import. | World-card-only rooms remain in `prep` and retain bulk tools unless the Keeper runs `.phase play`. |
| 15 | Contract risk | The analyzed player pool includes “player-visible clues” from every scene, and fallback analysis copies source paragraphs. The normal player state frame currently consumes only a bounded overview, but the pool itself has no discovery state. | A future consumer of the full player pool could reveal undiscovered scene clues; “player-safe” and “player-known” need separate contracts. |
| 16 | Test reproduction | NPC / companion actors permit callers to omit `chat_key`, while room-model selection unconditionally calls `.startswith()` on it. | These calls fail with `AttributeError` before reaching the model; 11 full-suite tests reproduce it. |

Representative service-level observations:

```text
after replacing A -> B: hooks = [A, B], modvars = [a_only, b_only], briefs = [A, B]
after a card with a missing rulepack fails: world_import and its lore are written; its hook is installed
```

Full-repository verification has three further baseline blockers unrelated to module lifecycle but
retained in this delivery record: PDF tests assume `pypdf` is absent although it is installed in
this environment (three tests); example system names in `agent/forge.py` trip the zero-system-token
architecture check (one test); and four Chinese demo-data strings in `adapters/cli/demo.py` trip the
i18n check and its two tests. This documentation change does not edit those files.

Review map: world-card import and replacement live in `agent/kp_tools_charcard.py`; the admin path
is `module_admin.py`; room commands are in `gateway/commands/world.py` and
`gateway/commands/panels.py`; source filtering is in `core/worldbook.py`; prose indexing is in
`agent/document_manager.py` / `agent/kp_tools_knowledge.py`; phase selection is in
`agent/tool_phase.py`; runtime rule selection and prompt expertise are in `agent/services.py` /
`core/prompt_sections.py`; and player-pool construction is in `agent/module_initializer.py`.

The maintainer decision entry is
[`notes/pending/module-lifecycle-consistency.md`](notes/pending/module-lifecycle-consistency.md).
The first decisions should be whether a prose pool and world card may coexist, which artifacts a
module source owns, and where import transaction boundaries sit; the remaining findings depend on
those answers.

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
