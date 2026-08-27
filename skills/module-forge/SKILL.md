---
name: Module forge
description: >
  Enable to let the Keeper author a brand-new module/scenario document from a natural-language
  description (or a keeper-provided premise), installing it straight into this room's module
  knowledge pool. Turn this on only when you want the Keeper itself to generate and install a new
  module at your request.
allowed-tools: [generate_module]
name-zh: 模组锻造
description-zh: >
  开启后，Keeper 可根据自然语言描述（或守秘人提供的设定前提）创作一份全新的模组/剧本文档，并直接安装到本房间的模组知识库中。
metadata:
  scope: room
  content-rating: ""
---

# Module forge

You can author an entirely new module/scenario, not just run modules a keeper manually uploads.
When the keeper describes a scenario they want to play -- a premise, a setting, a mystery, a
one-shot hook -- call `generate_module` with a clear, self-contained description of it. Only call
it when the keeper is explicitly asking for a new module to be authored; never speculatively, and
never in response to ordinary play, since it replaces/adds to this room's module knowledge pool.

A good description to pass along:
- the setting and player-facing premise/hook
- the tone (investigation, horror, heist, political intrigue, ...) and rule system in play, if
  relevant
- any key NPCs, threats, or twists the keeper already has in mind -- or leave it open for the
  generator to invent them
- roughly how big the scenario should be (a single tense session vs. a longer arc)

`generate_module` authors a full module document and then runs it through the SAME analysis
pipeline a manual `.module` upload uses, so the resulting scenes/NPCs/clues/timeline/truths land
directly in this room's keeper-only and player-visible knowledge pools -- there is no separate
review step, so only call it when the keeper actually wants this room's module replaced/extended
right now.

After `generate_module` responds, tell the keeper plainly what was created (or why it wasn't, if
it failed) and summarize what the room's module knowledge pool now holds.

## Difficulty and level range

Difficulty and level ranges are D&D-class concepts: they apply ONLY to rule systems with
character levels (D&D 5e). For CoC/WoD modules, never use `difficulty`/`levels` — those
systems have no level or challenge-tier model, and the engine ignores the arguments there.

When the keeper names a difficulty or a target level range for a level-based system, pass it
through `generate_module`'s `difficulty` / `levels` arguments — do not fold it into the
description as flavor and drop it:

- `difficulty` is one of `easy` / `standard` / `hard` / `deadly`. It is a DESIGN DRIVER:
  easy modules get mild surroundings, weak and sparse threats, and plentiful resources;
  hard modules get perilous surroundings, stronger threats, scarce resources and time
  pressure; deadly modules are near-uninhabitable with overwhelming threats. The authored
  scenes, threat budgets, trap density and resource distribution must all reflect it.
- `levels` is the recommended character level range (e.g. `1-3`, `5-10`) the module is
  tuned for — the engine records it (and `difficulty`, for pack modules) in the module
  metadata and the module page shows it as the difficulty identifier.

Ask the keeper for a difficulty tier / level range when they have one in mind (a harder
run of an existing premise is a legitimate request); omit both for a default-standard module.
