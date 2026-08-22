# Pending: keeper-selectable media & companion content for AI-generated modules

- **Problem:** an AI-generated module (`agent.forge.generate_and_install_module`)
  produces exactly one artifact — a Markdown scenario document installed into the
  room's knowledge pool. A hand-authored module pack (`.lwpack`) ships, besides
  text, both binary media (scene art, NPC portraits, item/clue illustrations,
  maps) and structured content (skills, rulepacks, cards, worldbooks, presets, a
  presentation kit, panels) that the generated module never has — the forge path
  never touches the image model nor the other `generate_*` lanes. The keeper asked
  for a per-generation opt-in: when authoring a module, choose which of these
  "normal module" contents to also generate, and have them generated like a real
  pack would.

- **需求 (verbatim, keeper):**
  1. 配乐和音效忽略掉（no audio: BGM/ambience/SFX are out of scope）。
  2. AI 生成剧本的时候，可以勾选很多额外内容（也就是现在没有生成的内容），分
     好几类让我勾选。
  3. 勾选的内容就会像正常模组一样生成哦 —— 包括图片啊、场景啊、物品图啊、NPC
     的立绘啊什么的。提示词的设计要仔细查看现在的提示词风格措辞，添加这里的提示词。

- **Current state:**
  - `generate_and_install_module` (`agent/forge.py`) authors one `.md`, validates
    via `_extract_module_title`/`_extract_module_id`, writes it under
    `_USER_MODULE_DIR`, and ingests it through the existing module pipeline
    (`DocumentTools.upload_document`). No image-model call, no media store write.
  - A pack's `CONTENT_KINDS` (`core/pack.py`): skills, rulepacks, cards, lorebooks,
    panels, presentation, presets, prep — plus `assets:` (binary files). Only the
    scenario text is reachable by the forge path today.
  - The image-generation plumbing the proposal reuses already exists:
    `infra/imagegen.ImageGen.generate()` (OpenAI-compatible / MiniMax /
    SiliconFlow / Fake), `gateway.imagegen.allow_imagegen_request` (per-room hourly
    cap), `infra.media_store.MediaStore.register_blob`, and
    `gateway.media.publish_media` — the same lane `kp_tools_images.generate_image`
    (AI-KP handout) and `.avatar gen` ride.
  - Wire: `AdminGenerateFrame {kind: "module", description, locale}` →
    `net/admin.py::_generate` → `generate_and_install_module`; the gated KP tool
    `kp_tools_forge.generate_module(description)` mirrors it.

- **Proposal (design):**

  **1. Keeper-selectable content — two groups.** A hand-authored pack ships media
  AND structured content the generated module never has. The keeper picks, per
  generation, which of these to also produce. Two groups with different engines:

  **Group A — media (imagegen; the mechanism already exists).**

  | id | label | what gets generated | per-call cap |
  |---|---|---|---|
  | `cover` | 封面/海报 | one opening image for the module | 1 |
  | `scenes` | 场景图 | one atmospheric image per KEY scene/location | ≤ 6 |
  | `npcs` | NPC 立绘 | one portrait per KEY NPC (one per NPC — owner verdict) | ≤ 6 |
  | `items` | 物品/线索图 | one image per KEY item/clue | ≤ 6 |

  **Group B — structured content (LLM-authored text/data; different engines).**
  Each is the "normal module" counterpart AI never generates today. Feasibility
  varies by existing plumbing:

  | id | label | what gets generated | existing engine |
  |---|---|---|---|
  | `skills` | 技能 | a KP `SKILL.md` bundle | ✅ `generate_and_install_skill` (reuse) |
  | `rulepacks` | 规则包 | a `rulepacks/<id>.yaml` system | ✅ `generate_and_install_rulepack` (reuse) |
  | `cards` | 角色卡 | pre-generated investigator card(s) | ✅ `.genchar` machinery (LLM concept + deterministic sheet) |
  | `worldbook` | 世界书/背景设定 | background lore entries for the module | ❌ no generator — needs a new lane |
  | `presets` | 提示词预设 | completion presets for the room | ❌ no generator — needs a new lane |
  | `presentation` | 演出资料包 | 定妆 refs + `ui/presentation.yaml` for the Director | ❌ no generator; needs 定妆 images + subject/audio declaration — hardest |
  | `panels` | 面板 | table UI panels (with image assets) | ❌ no generator; UI + asset heavy |

  Audio categories (BGM/ambience/SFX) are deliberately absent from BOTH groups —
  keeper vetoed them. Group A is the ask's core ("图片、场景、物品图、NPC立绘");
  Group B records the full "what else a normal module has" so the scope is explicit
  and each item's feasibility is judged by the owner. Group A + the three ✅ Group B
  items are IN this change (they reuse existing engines); the four ❌ items are new
  pipelines and land later (Future work).

  **Group A — media (imagegen; the mechanism already exists):** one extra pass
  after the module is authored, described below.

  **Group B — structured (reuse the existing `generate_*` engines):** each ✅ item
  runs its OWN existing generator after the module lands — `generate_and_install_skill`,
  `generate_and_install_rulepack`, and the `.genchar` character lane — driven by a
  description the module's own content supplies, not by a new bespoke pipeline. The
  four ❌ items are explicitly OUT of this change (new lanes, owner to schedule).

  **2. Group A generation pipeline (one extra pass after the module is authored).**
  `generate_and_install_module(services, ctx, description, media: list[str] | None)`:
  when `media` is non-empty, after the `.md` is written and ingested, run a media
  pass:
  - **Shot-list call (LLM, text):** feed the module document + selected kinds to a
    new scoped prompt; the model returns a JSON shot list — one entry per desired
    image: `{kind, subject, prompt, caption}`. Prompt text is player-safe and
    grounded in the module's own scenes/NPCs/items (never keeper-only material
    beyond what the module itself discloses). Output is capped: `cover` ≤ 1,
    others ≤ their cap, total ≤ 12; a malformed/non-JSON reply degrades to zero
    images, never a failed module.
  - **Render loop (imagegen):** for each shot, `allow_imagegen_request` check
    (room hourly cap — a reached cap stops further renders, earlier ones kept),
    then `imagegen.generate(prompt, size=...)`; the provider is the room's
    `imagegen_for_room` selection, same as every other lane.
  - **Store:** each image → `MediaStore.register_blob(room=chat_key, …)` under
    `media/` with a name prefixed `module-<id>/<kind>-<n>.png` so the Keeper can
    recognize provenance in the room media deck. Not auto-broadcast: the Keeper
    pushes handouts when the table calls for them (same stance as a pack's assets).
  - **Confirmation:** `ForgeResult.detail` extends with the shot list (e.g. "4
    images generated: scene-lighthouse.png, …"), so the module-install reply also
    names the media.

  **3. Prompt design** (the keeper's explicit ask: match existing forge style).
  New i18n keys under `agent.forge.*` (`module_media_system_prompt` +
  `module_media_language_requirement`), en + zh, mirroring `module_system_prompt`'s
  register: second person, "you are …", states what the output is FOR (a
  shot list a separate render loop will execute), "Output ONLY …", grounding
  rules (pictures must depict what the module's own text depicts), caps, and
  player-safety (an image may show only what players may know). Same single-prompt
  assembly rule as the other lanes — one scoped assembler, one caller.

  **4. Wire changes (cross-repo, backward-compatible additive).** One additive
  frame field `AdminGenerateFrame.options: { media?: string[], companion?: string[] }`
  (both validated against their closed vocabularies; unknown ids ignored):
  - engine `clients/protocol/src/types.ts`: add `options` to `AdminGenerateFrame`;
    `client.ts::adminGenerate` gains an `options` parameter.
  - engine `net/admin.py::_generate`: pass `frame.get("options")` into
    `generate_and_install_module`.
  - engine `agent/kp_tools_forge.py::generate_module`: optional `media`/`companion`
    tool arguments (for the conversational path).
  - web `src/store/admin.ts::generateModule(description, options)` and
    `src/features/play/screens/ModuleScreen.tsx`: the two checkbox groups above the
    generate button; i18n labels in both locales.
  - engine locales `locales/{en,zh}/agent.json`: the new prompt keys + the
    confirmation strings (`agent.forge.module_media_*`).

  **5. Failure semantics.** The media pass never fails the module: any error
  (provider down, budget, rate limit, LLM refusal) degrades to fewer/zero images
  and is reported in the confirmation, exactly like `stage_director`'s
  IMAGE_* outcome vocabulary — a dead image provider must not cost the table its
  module.

  **6. Checkbox UI (web surface spec).** In the module-generation card, between
  the description textarea and the generate button, two checkbox groups — every
  box UNCHECKED by default, each with a one-line explanation. The UI shows Group A
  (media) and Group B (structured); a box whose engine is not yet built is
  rendered disabled with a "即将支持 / coming soon" marker rather than hidden, so
  the intended scope stays visible.

  **Group A — 媒体配图 / Media:**

  | checkbox label (zh / en) | explanation (zh / en) | cap |
  |---|---|---|
  | 封面/海报 · Cover | 为剧本生成一张开场封面图 · one opening image for the module | 1 |
  | 场景图 · Scenes | 为每个关键场景生成一张气氛图 · one atmospheric image per key scene | ≤ 6 |
  | NPC 立绘 · NPC portraits | 为主要 NPC 各生成一张肖像（每人一张）· one portrait per key NPC | ≤ 6 |
  | 物品/线索图 · Items & clues | 为关键物品/线索各生成一张图 · one image per key item/clue | ≤ 6 |

  **Group B — 配套内容 / Companion content:**

  | checkbox label (zh / en) | explanation (zh / en) | state |
  |---|---|---|
  | 技能 · Skills | 为该玩法生成一个 KP 技能 · a KP skill bundle | enabled (reuses forge) |
  | 规则包 · Rule system | 为剧本生成一套规则体系 · a rulepack for the scenario | enabled (reuses forge) |
  | 角色卡 · Character cards | 生成可用的预建调查员卡 · pre-generated investigator cards | enabled (reuses `.genchar`) |
  | 世界书 · Worldbook | 生成背景设定条目 · background lore entries | coming soon |
  | 提示词预设 · Presets | 生成提示词预设 · completion presets | coming soon |
  | 演出资料包 · Presentation kit | 自动配定妆图与 presentation.yaml · 定妆 refs + Director brief | coming soon |
  | 面板 · Panels | 生成桌面 UI 面板 · table UI panels | coming soon |

  A hint line under the groups: 勾选内容会调用模型/图像服务并产生 API 费用，生成
  也会变慢 / checking content calls the LLM/image provider (API cost) and slows
  generation. The selection is per-request only — never persisted, never carried
  into the next generation. Unchecked = the module is authored exactly as today
  (zero behavior change for the existing path).

- **Owner verdicts (2026-08-22):**
  1. **Consistency:** one portrait per NPC — the shot list never carries two
     entries for the same NPC (enforced in the prompt AND at parse time); no
     cross-shot likeness promise.
  2. **Sync vs background:** SYNCHRONOUS — the media pass runs inside the
     `admin_generate` reply; the confirmation names every generated image.
  3. **Default:** UNCHECKED — the keeper opts in per generation; media costs
     real API money.
  4. **Group B scope (THIS change):** ship Group A (media) AND the three ✅ Group B
     items — skills / rulepacks / character cards, all reusing existing engines.
     The four ❌ items are deferred to a later change (see Future work).

- **Future work (scheduled separately, not in this change):** the four Group B
  items with no existing generator — each a new lane, owner to schedule:
  - **worldbook (世界书):** extract the module's background/setting into worldbook
    lore entries the room can query. New lane: LLM reads the authored module and
    writes worldbook entries through the existing worldbook parser/pipeline.
  - **presets (提示词预设):** author completion presets for the module's tone. New
    lane: LLM writes the preset JSON the `.preset` store imports.
  - **presentation (演出资料包):** auto-build a 定妆 kit — generate subject
    reference images (imagegen) AND author `ui/presentation.yaml` (subjects, 定妆
    refs, style, audio cues) so the Stage Director can stage this generated module.
    Hardest: 定妆 consistency — generated refs must be stable for the Director's
    later generations to build on, which is precisely the 定妆-mandatory discipline.
  - **panels (面板):** author table UI panels with their image assets. Heaviest: a
    panel is a structured UI definition plus referenced images, and needs the panel
    build/validate pipeline wired to generation.

- **Impact (files):** engine `agent/forge.py`, `agent/kp_tools_forge.py`,
  `net/admin.py`, `clients/protocol/src/{types,client}.ts`, locales
  `locales/{en,zh}/agent.json`; web `src/store/admin.ts`,
  `src/features/play/screens/ModuleScreen.tsx`, `src/i18n/locales/{en,zh}.json`.
  Tests: `agent/test_forge.py` (media pass with `FakeImageGen`), web admin store
  test (frame carries `options`).

- **Date:** 2026-08-22.
