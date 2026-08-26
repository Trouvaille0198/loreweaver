# AGENTS.md — 贡献者与 AI 编码代理指南

Loreweaver 是自托管的跑团（TTRPG）**AI 主持人 / 守门人（Keeper）**：一个"世界/剧情优先"的引擎（结构化世界 + 模块 + 规则 + 持久状态），不是人设聊天前端。Iroh p2p 之上的终端优先，规则系统无关（D&D 5e SRD + CoC 7e），英文优先 + 运行时 `en`/`zh` i18n，MIT。本文件为在本仓库工作的人类与 AI 编码代理提供定向——它是唯一事实来源（CLAUDE.md 只是导入它）。面向用户的文档在 `README.md`；开放客户端协议在 `docs/protocol.md`；扩展性契约在 `docs/plugins.md`。

## 架构（分层，每层只有一个职责）
- `core/` — **确定性引擎**（绝不 AI 生成），M16 起规则无关：骰子、`resolution`（包编译的检定阶梯 + QuickJS `rules_script` 车道）、`sheets`（包声明的人物卡形状：规范名桥接、派生 DAG——派生值绝不持久化——生命状态、线上资源）、`character_rules`（每条属性写入路径上的包约束校验）、`game_clock`、`rulepacks`（数据驱动的规则系统：resolution/sheet/subsystems/commands/expertise 全是包数据；`tests/architecture/` 把 agent/ 钉在零系统 token）、`skills`（SKILL.md 加载器）、确定性关系轨道、`modvars`（确定性模块变量跟踪器）、worldbook、`charcard`（SillyTavern 卡解析器）、`table_habits` — M20 E 程序性记忆类型（"这张桌子"的实际玩法；仅守门人侧，玩家投影返回 None，仅索引驻留）、`chronicle` — M18 战役历史类型（`chronicle`/`campaign_summary`/`thread`：投影让守门人批注不出现在任何玩家级视图）加确定性折叠策略（0.60 触发 / 0.40 下限 / 0.85 紧急，4 回合无未来滞后窗口）、`documents` — M17 元类型：房间内全部内容（lore/NPC/卡/预生成/modvars/MVU 树/笔记/池）都是同一张表里的 `Document`，每种类型的 `project(doc, viewer)` 是铁律 #3 的唯一线上收口点；以及 `luck`（对已掷出并记录的检定做确定性"花费运气"重定级——自身不掷骰、不认系统）、`battle_recording`/`battle_report`（从中立 `check_outcome` 契约写结构化战斗记录）、`presentation`（呈现套件 `ui/presentation.yaml` 的唯一 schema 权威，作者期严格校验）与 `preset`/`preset_store`（提示词预设）、`pregen_roster`（预生成角色名册）、`svg_map`（SVG 地图）。
- `infra/` — 管道：SQLite 存储（`documents` + `room_state` + kv 表——内容文档、房间作用域运行时状态、非房间遗留）、pydantic-settings 配置 + 热运行时覆盖/凭据簿、i18n、`llm`/`embeddings`（各有 `Fake*` 离线替身）、vector、`providers`（多厂商 LLM 工厂）、`media_store`（不透明媒体 blob 存储：只存转发、绝不解析）、`usage_stats`（token/缓存计量）、`model_call_trace`（模型调用追踪）、`oauth_flows`、`svg`。
- `agent/` — AI-KP 大脑：`AgentCtx`、`tools`（`@tool` schema 生成 + 门控）、`kp_tools*`（守门人工具）、`forge`（自扩展生成器：从一段描述生成 skill/rulepack/module）、`prompt_builder`、`loop`（函数调用循环）、`services`（装配束）、`chronicle`（M18 折叠流：用量计越过触发线时把旧 chronicle 记录批量折叠进滚动的 `campaign_summary`；被折叠记录进入嵌入索引做主题召回——记录本身由 Scribe 自动书写（M21），每个实质回合一行玩家级文字、零额外模型调用，所以持久记忆和骑在其上的历史裁剪从不依赖守门人记得调 `record_chronicle`；守门人剧透余量保持为那个自愿工具专属）、知识作用域演员家族——`npc_actor`（单个 NPC 的知识封闭子演员）、`companion_actor`（同伴）、`stage_director`（M19 玩家侧呈现演员：输入只有已投影的玩家可见流 + 模块呈现套件）、`scribe`（回合后书记官）、三条模型驱动的准备车道——`module_initializer`（全文模块分析进守门人/玩家池）、`document_manager`（分块 + RAG 问答）、`char_from_persona`（人设 → 规则合法卡；校验一半留在 `core.character_rules`）；以及 `history`（回放历史的 append-only 树：回退只是指针移动，折叠后的裁剪在 `trim_folded`）、`undo`（回合边界快照 + 浅回退，覆盖 `room_state` 与 `documents` 两个非追加半区）、`clue_log`（已发现线索日志：玩家看到的 `clues` 从这来，未揭示的秘密线索根本不在日志里）、`tool_trace`、`module_lifecycle`（一房一模的事务化导入契约）。
- `gateway/` — 平台无关：session/events/turn/member/房间状态、`commands/`（双方言 + 斜杠——一个包：`router` 持有规格表与分发，每个领域是一个 mixin 模块 `checks`/`sheet`/`rules`/`rooms`/`cast`/`world`/`panels`/`media`/`llm`/`clues`/`item`；新命令落在它的领域模块加一行规格，绝不进回合管线）、`ops`（限流/审查/权限）、`hub`（跨传输 RoomHub）、`director`（同伴/战斗子回合调度）、媒体与呈现面（`imagegen`、`media`/`audio`/`avatar`、`pack_media`、`attachment_fs`、`render_chat`、`presentation`、`ui_media`）、`dev_room`（`.dev mount` 的热重载沙盒房）。
- `net/` — `session`（传输无关 SessionCore）、`iroh_server`（p2p QUIC，默认载具）、`tui_server`（WebSocket，离线测试/回环）、`web_server`（浏览器载具：`TuiServer` + SPA 静态托管——浏览器跑不了 Iroh 的自定义 ALPN QUIC，所以 Web 客户端说同一线协议的 WebSocket 版，同端口同源无 CORS；`python -m app --web [--static-dir <dist>]`）、`state`（WS `state` 帧的房间只读快照）、`keystore`、`admin`、`room_backup`、`updater`（守门人触发的原地自更新：跑操作员配置的命令后 re-exec 自己——同 PID、同 Iroh 节点号、无需监督器重启）。
- `adapters/` — 只有 `cli`（本地操作 REPL）。不要为聊天平台（Discord/QQ/Telegram/飞书/OneBot）添加适配器：UI 方向是带深度可定制 UI 扩展层（`ui` 帧、面板）的协议客户端，文本聊天平台在结构上渲染不了。要接新客户端就对 `docs/protocol.md` 建。
- `clients/` — TypeScript：`protocol`（共享类型 + `WsClient`；以 `loreweaver-protocol` 发布到 npm，其 `major.minor` 跟随线协议版本）、`tui`（OpenTUI 终端客户端；`IrohClient` 和一键建房在这里）。两者都说 `docs/protocol.md`。另有两个独立仓库的客户端：桌面端 **Loreweaver Studio**（Tauri：Rust 传输 + React UI，游玩模式的双层面板、守门人界面、卡/包工作室）是推荐玩家客户端，从 npm 消费 `loreweaver-protocol`，并用跨仓库往返门（那边的 `bun run roundtrip`）与引擎对齐；浏览器端 **loreweaver-web**（React SPA）说同一线协议的 WebSocket 版，由 `--web --static-dir` 一端口托管。在这里改协议或包格式，意味着那两边的门都要重跑。升协议版本要一次升五处——`net/session.py`（权威）、`protocol/src/types.ts`、npm manifest 的版本 + 描述、`protocol/README.md`、`docs/protocol.md`——由 `tests/architecture/test_protocol_version_sync.py` 钉住。
- 仓库根 — `app.py`（CLI 入口：`--cli` REPL / `--serve` Iroh / `--web` 浏览器 WS / `--doctor` / `--pack` / `--install`）；`module_admin.py`（Web 端守门人管理扩展：房间模块源码文件的上传/编辑/打包，走既有 `admin_generated` 回复车道以兼容旧客户端，由 `loreweaver-web` 的 `serve_both.py` 挂到 Web 服务器的 AdminService 上）；`lw_versioning.py`（Python 包版本直接跟随线协议常量——以文本方式读取 `net/session.py`，避免构建后端导入运行时依赖）。

## 铁律 / 红线（不可破坏）
1. **确定性 vs 生成性分工。** 骰子、成功等级、人物数学、随机表、权限、审查 = 真代码。旁白 / NPC / 风味 = 模型。绝不 AI 化确定性内核。
2. **骰子先行。** 一次检定先掷真骰子，再按成功等级旁白结果——绝不预写结果。
3. **信息隔离（反 metagaming）。** 玩家知识与每个 NPC/同伴的私有知识按构造隔离：NPC/同伴演员只由它自己的记录 + 卡构建（绝不碰守门人池）。结构上这是 M17 文档投影契约——每个出站面都消费 `core.documents.project(doc, viewer)` 视图（秘密 lore、NPC 秘密、守门人专属 modvars 和未暴露的 MVU 叶子绝不穿过玩家级投影；哨兵测试在 `tests/documents/`）。主守门人按设计且永久持有完整模块真相——晚知道答案的守门人无法埋伏笔、种线索、维持主谋连贯，这也是每本出版模组都把一切先交给守门人的原因。这个不对称是刻意为之，不是一个等待结构性修复的缺口：绝不提议对守门人知识做作用域化、分层或分阶段，哪怕作为实验也不行。守门人克制（绝不向玩家引用守门人专属材料）是行为属性，live 模型评测是它正确且最终的保证。拆卡是本规则的一部分：导入卡里的世界机制——hooks、`[InitVar]` schema、EJS——只能经守门人的 `.import … world` 进房间（玩家导入在结构上剥离，`core.card_split`），导入的 MVU 变量叶只有在守门人暴露（`.var expose`）之后才上玩家面板。结构测试和行为门都是红线；保持绿。
4. **英文优先 + i18n，不硬编码自然语言。** 标识符/注释/提交信息用英文。每个面向用户的字符串走 `infra.i18n` + `locales/{en,zh}/*.json`（客户端：`clients/tui/src/i18n.ts` 里带类型的 `tt()`/`messages` 字典，两种语言都要）。`scripts/i18n_lint.py` 是门禁。（CJK 游戏 DATA——技能名、别名——豁免，同现有数据模块。）
5. **单一提示装配，按车道。** 守门人的上下文只有一个装配器和一个调用者：守门人看到的每个片段——`core.prompt_sections` 构建器、世界 lore、预设层、技能正文、hook 注入、书记官低语、chronicle——都在 `agent/prompt_builder.py` 装配、作为一个对象返回、且只由 `agent/loop.py` 发送；没有别的模块构建或扩展守门人上下文。其他每一次模型调用都是一个声明过的车道，有自己的作用域装配器：知识作用域演员（NPC、同伴、Director、Scribe——各自只从自己的记录出提示，这本身就是铁律 #3）、chronicle 折叠（记忆）、创作/准备车道（forge、模块分析、RAG 问答、人设→卡）。`core/` 完全不做模型调用。`tests/architecture/test_model_call_lanes.py` 钉住这一切：新的 `.chat()` 调用点会让构建失败，直到它报出自己的车道——那正是该问"它该不该存在"的时刻。守门人对象怎么上线（一条系统消息，或一条稳定系统消息 + 一条尾随状态消息）是为缓存行为选的实现细节，不是规则——不变量是每车道一个装配器，不是消息条数。

## 单回合模型调用预算
一个玩家回合最坏 **~155 次模型调用**；新的模型驱动车道必须装得进这个预算，加一条就要同步更新这个数字和 `tests/agent/test_turn_call_budget.py`。每个 KP 回合（`agent/loop.py`）：≤3 次 chronicle 折叠 + `max_rounds` 12 个工具轮 + 5 个回合末检查轮（`agent/turn_checks.MAX_ROUNDS_PER_TURN`——对规则包可声明的表跑一个 Stop 形运行器，上限夹紧，内容包不能拉长回合；或改为 1 次禁工具的 max-rounds 终结器）+ 1 次**上下文溢出重试**（M23 WS2——provider 以提示过长拒收时触发恢复折叠并只重发一次，且仅当那次折叠真的折掉了记录；折叠本身不是新增项，因为 `fold_for_overflow` 花的是同一 ≤3 批的剩余额度）= **21**。一个玩家回合再加：1 次 Scribe 通行、仅在节拍上 1 次 Director 调用（外加 imagegen：1 张图 + 后台预热的 `pregen_per_beat`），然后至多 `MAX_COMPANION_TURNS` 6 个同伴子回合，每个 = 1 次演员调用 + 一个嵌套 KP 回合。同伴子回合自己不跑 Scribe/Director——这个守卫是结构性的，落在三处（`gateway/turn.py` ×2、`gateway/director.py`）。唯一无界的项：模型每发出一个 `speak_as_npc` / `companion_act` 就花一次子调用，没有东西封顶它一轮发多少个。整个玩家回合独占其房间：`hub.turn_lock(session_key)` 是每房间一把 `asyncio.Lock`，在传输收口点（`net/session.py`、`gateway/runner.py`）获取，所以同一房间的并发输入按到达顺序排队（锁的等待者是 FIFO），其他房间自由运行。`run_kp_turn` 和同伴 director 故意不取这把锁——正是这一点让嵌套的同伴/director 子回合不会自死锁。别把它和 `hub.begin_turn`/`end_turn` 混淆：后者不锁任何东西，是决定房间何时发布 busy/idle 的嵌套计数器。占用时间包含重试睡眠：`infra/llm_retry.py` 重试 3×、指数退避上限 20s（采信 provider 冷却提示时上限 60s），并与 SDK 自带重试复合（只有 ChatGPT 客户端传 `max_retries=0`；OpenAI 兼容、Anthropic、Gemini 客户端都不传），所以一次逻辑调用最多可以是 9 次 HTTP 尝试。Scribe/Director 通行是唯一在锁外的车道。

## 开发 / 测试 / 运行
```bash
uv sync --extra anthropic --extra gemini --extra ejs   # 环境 + 依赖；`dev` 组（pytest/ruff）默认装上；`ejs` = QuickJS 沙箱内的完整 SillyTavern EJS 模板（缺了则相关测试跳过）。（pip 兜底：python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,anthropic,gemini,ejs]")
uv run pytest -q               # 离线：FakeLLM/FakeEmbeddings + seed_dice，无网络无密钥
uv run ruff check core infra agent gateway net adapters app.py lw_versioning.py scripts
uv run python scripts/i18n_lint.py    # 不带参数（误传路径会扫到 .venv）
uv run python -m app --cli     # 试玩：r 3d6+2 / /roll 4d6kh3 / .ra 侦查 / .setcoc 2
uv run python -m app --web --static-dir <loreweaver-web 构建产物>   # 浏览器载具：WS + 托管 Web 客户端
uv run python -m app --doctor  # 体检 locales/rulepacks/skills 发现
# clients: cd clients/<protocol|tui> && bun install && bun test
```
测试是确定性且离线的。要跑真守门人，在 `.env` 里设 `TRPG_LLM__*`（见 `.env.example`）。

## 如何扩展
- **规则系统** → 加一个 `rulepacks/<system>.yaml`（defaults/derived/alias/st_show/set_keys + 可选的按语言 `display` 名）；数据驱动的部分零代码改动。
- **KP 技能** → 一个 `skills/<id>/SKILL.md`（Claude-Code 形状：YAML frontmatter `name`/`description`/`allowed-tools` + Markdown 正文）；按房间启用走 `.skill enable <id>`。可选的同名 `hooks.js` 添加沙箱化的回合生命周期事件处理器（Layer C.1——见 `docs/plugins.md`）。
- **内容包** → 把一整部作品（skills + rulepacks + cards + lorebooks + panels + 呈现套件 + 提示词预设 + prep 脚本 + 资产）打包成一个带 `pack.yaml` 清单的 `.lwpack` zip：`python -m app --pack <src-dir>` 构建，`--install <path|https|gh:owner/repo[@tag]> [--yes]` 安装（Git release 就是注册表；见 `docs/plugins.md`）。卡声明 `kind: world|character`（按真实载荷检测强制）；包内 rulepack 可用 `extends:` 补丁一个基础系统。作者阶段可完全跳过构建循环：`.dev mount <src-dir>`（守门人命令，限制在 `TRPG_DEV__SOURCE_ROOT` 之下，未设置则关闭）每次保存都把源码树热重载进一个沙盒房（`gateway/dev_room.py`；`docs/authoring.md` §8）。
- **LLM provider** → 多数厂商走 OpenAI 兼容路径 + `infra/providers.py` 里的一个 `PRESETS` 条目即可；只有非 OpenAI 形状的 API 才加原生类（见 `AnthropicLLM`/`GeminiLLM`）。
- **KP 工具** → provider 类上一个 `async def name(self, ctx, ...) -> str`，加 `@tool` 装饰器；把 provider 加进 `agent/kp_tools.build_kp_toolset`。批量准备活也可以脚本化：`run_prep_plan`（仅准备阶段）跑沙箱 JS、产出一个操作 LIST，引擎把每条经同一条 `@tool` 路径落地——`core/prep_script.py`。读秘密的工具标 `keeper_only=True`；技能解锁的工具标 `gated=True`；批量/低频的标 `prep_only=True` 让游玩阶段摘掉它（M20 B——`agent/tool_phase.py` 判定房间所处阶段，`.phase` 钉住，`tests/architecture/test_tool_phase_budget.py` 守游玩阶段的 schema 预算）；一个工具若对不同参数值碰不同文档，标 `concurrent_by="<arg>"`（`speak_as_npc` 按 `npc`：循环把一轮里的这类调用与只读调用按调用顺序并发，其余串行——见 `agent/loop._concurrency_groups`）；一个工具若依赖某些房间缺的存储，标 `needs="<capability>"`（`agent/tool_phase.room_capabilities`，每回合重算——世界卡房间没有模块池，所以池工具不会被提供）。不标 = 两个阶段、所有房间都可用。
- **客户端** → 对着 `docs/protocol.md`（带版本的 WS/Iroh 协议）建 + 复用 `loreweaver-protocol` 类型。
- **房间作用域状态**（新的 `room_state` 键、文档类型或向量车道）→ 在写它的模块里声明一个 `RoomStateFacet`，并把该模块列进 `net/room_lifecycle.FACET_MODULES`（M23 WS1——`infra/room_facets.py`）。facet 说明哪个 `.reset` 作用域会清掉它，或它为何在所有作用域下都幸存；`tests/architecture/test_room_facets.py` 会让无 facet 认领的状态挂掉构建。注册表回答"清什么"——`net/room_backup.py` 仍然独占四个生命周期操作的顺序与原子性。

## AI 代理工作约定
- **叶子并行、合并串行。** 独立的新模块可以并行地单独构建 + 测试；对共享文件（`build_kp_toolset`、`services`、`commands`、`prompt_builder`）的接线是一次小心、顺序的通过。
- **圈定测试范围。** 别人可能同时在改时，只跑你模块的测试，不跑全套。
- **绝不前台跑阻塞服务器**（`python -m app --serve`、`--web`、开发服务器）——会挂住。用测试验证（它们自旋临时的进程内服务器）；实在要跑就后台 + `timeout` + `kill`。
- **决策记录：** 在提出某机制前先查 `docs/notes/rejected/`——驳回是绑定的；非平凡改动要在同一 PR 里加或更新 `docs/notes/` 里的一条记录。
- **做生命周期/锁/provider/重放相关的事之前，** 先读 `docs/defensive-patterns.md`——付过学费的规则都在那。
- **部署以本仓库为基准**（见 `WORKFLOW.md`）：所有引擎代码改动落在本仓库；绝不在部署目录或容器里改代码。
- **改动之后：** 按改动的面验证。纯文档改动不需要跑测试。代码改动优先跑最小相关的 lint、类型检查或聚焦测试命令；默认不跑两个仓库的全套。只在用户明确要求、或高风险改动确有必要时才跑全量/跨仓库验证，且先说明原因。CI 仍是权威的全套门。
