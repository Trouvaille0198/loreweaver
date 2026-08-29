# AGENTS.md

## 范围

本文件是 Loreweaver 引擎的工作规则。仓库是唯一事实来源；产品、协议、
内容创作和部署细节分别见 `README.md`、`docs/` 和 `WORKFLOW.md`。

## 硬性规则

- 始终用“长官”来称呼我
- 发现bug时，千万不要做兜底处理而忽视了bug，而是要把bug完全修复
- 禁止运行 pytest、test、lint、typecheck（用户明确要求，最高优先级）；验证与部署
  只跑 `docker compose build` / `docker compose up`，不用测试套件验收改动。
- 解释行为时使用人话，不要让用户去理解内部方法名或变量名；对用户说话一律按
  「术语人话对照表」（见下）翻译，不直接甩代码词。对话中用户强调的名词区分
  （如预设物品 vs 即兴物品），必须即时添加或修正到对照表。
- 骰子、检定、人物数值、权限、校验、持久化和隐私必须由确定性代码处理，
  不得交给 AI。AI 只负责旁白、对话和风味内容。
- 先掷骰，再叙述结果。不得预写或臆造检定结果。
- 用结构保证信息隔离：玩家视图不得包含主持人专属设定、未揭示线索或变量，
  也不得包含其他演员的私有知识。区分真人 `keeper`（有主持人权限的玩家）和
  模型 `AI keeper`（AI / 主持人 / AI keeper 三者等价，都是模型扮演的主持人）。
- AI keeper 的提示词只能在一个地方组装。新增模型调用必须声明自己的车道并使用
  对应的作用域提示词构建器；`core/` 绝不能调用模型。
- 所有面向用户的文字都使用现有 i18n，且同时支持 `en` 和 `zh`。
- 不添加聊天平台适配器。新客户端必须遵循已记录的协议。
- 不添加 pytest 文件。用最小的 Docker 检查验证改动；不得在容器或部署目录中改源代码。
- 未经要求，不提交、不创建分支，也不撤销用户的无关改动。
- 排查 bug 时，优先读取目标房间的调用链日志（tool trace）：服务器数据目录下
  `traces/<房间>/` 里的 `.jsonl`（容器内 `/data/traces/`，宿主机 `LORE_DATA_DIR`
  指向的目录）。每个剧本一个文件（沙箱房间为 `default.jsonl`）。它逐行记录
  每次模型调用的完整输入输出（含中间轮次与工具调用标记）、AI 发出的每个工具
  调用的参数与结果、耗时与剧本 id；配合聊天历史里的最终回复，即可回放该房间
  AI 当时的完整行为链——先读日志，再猜代码。`.trace on` 开启记录。

## 术语人话对照表

对用户说话时，内部词一律翻译成人话，不直接甩代码词。

核心概念：
- pregen = 模组自带的预生成角色（剧本写好的现成人物，玩家点"认领"即得卡）
- worldbook = 世界设定库 / 剧本设定条目（NPC、线索、秘密）
- clue = 线索
- manifest / asset = 模组包的文件清单 / 素材文件（图片等）
- media jobs = 配图任务队列（后台出图）
- forge = AI 生成剧本（锻造）
- settle = 剧本结算（打完剧本后的总结，写角色记忆）
- concept = AI 建卡时生成的角色设定草案（出身、职业、属性倾向）
- pack = 打包好的完整模组（.lwpack）
- active_module = 当前剧本
- party_roster = 队伍名册（桌面"队伍"卡片的数据）
- purge = 换剧本时清空旧剧本数据
- lane = 执行通道（AI 各车道）
- provider = 模型服务商（DeepSeek / MiniMax 等）

角色与数值：
- CharacterSheet / sheet = 角色卡
- attributes = 属性（力量/敏捷/体质等）
- secondary attributes = 派生数值（护甲、速度等）
- skills = 技能（侦察、说服等）
- resources = 资源条（血量/法力/理智等条状数值）
- equipment / items = 装备 / 物品
- status effects = 状态效果（中毒、眩晕等）
- spell slots = 法术位
- known spells = 已学会的法术
- level / class / race = 等级 / 职业 / 种族
- check = 检定（掷骰判定）
- save = 豁免（抵抗判定）
- critical / fumble = 大成功 / 大失败
- advantage / disadvantage = 优势 / 劣势
- initiative = 先攻（战斗出手顺序）
- rest = 休息（回满短休/长休资源）

房间与会话：
- member = 房间成员
- keeper = 拥有主持人权限的玩家（真人，能操作管理命令）
- AI / 主持人 / AI keeper = 三者等价，都是指模型扮演的主持人
- turn = 回合
- round = 轮（战斗轮）
- session = 一次会话
- chronicle = 编年史（剧情记录）
- recap = 剧情回顾

剧本与内容：
- module / scenario = 模组 / 剧本
- world card = 世界卡（剧本核心设定文档）
- lore = 设定资料
- secret = 秘密（未揭示的设定）
- hook = 剧情钩子（引入线索的入口）
- scene = 场景
- npc = 非玩家角色
- companion = 同伴（由 AI 扮演的角色）
- variables / trackers = 剧情追踪器（恐慌值、好感度这类数值）
- module brief = 剧本简报
- opening = 开场白
- epilogue = 尾声
- timeline = 时间线

规则与配置：
- rulepack / system = 规则系统（D&D 5e / CoC 等）
- house rules = 房规
- preset = 预设（模型/提示词预设）
- skill（KP 技能）= 主持人技能（给 AI 的能力插件，可开关）
- runtime = 规则引擎（战斗/休息/升级这些确定性规则）
- capabilities = 能力门控（按规则系统决定 AI 能用哪些工具）

角色操作：
- claim = 认领（玩家领取一个角色）
- release = 释放认领
- retire = 退队（角色退出当前剧本，卡保留）
- join = 入队（角色重新加入剧本）
- active character = 当前角色
- grant = 发放（发放物品/奖励）
- 预设物品 = 模组/规则系统设计好的物品（在物品清单里，可查可反复发放，走完整校验）
- 即兴物品 = AI/主持人当场临时造的物品（不在清单里，通用范围、跟持有人走、加成封顶）

界面与通信：
- StatePanel = 状态面板（右侧桌面栏）
- StateFrame / frame = 状态数据包（服务器推给页面的数据）
- WS = 实时连接（WebSocket）
- PWA / Service Worker = 网页缓存（改了代码看不到新页面时清它）
- TUI = 终端界面（命令行跑团）

AI 侧：
- prompt = 提示词
- context = 上下文（模型每次看到的全部内容）
- tool call = 工具调用（AI 调用引擎功能来结算机制）
- prep phase = 准备阶段（回合开始前 AI 的准备动作）
- embedding / vector = 知识检索（AI 从设定库里捞相关资料）
- model / provider = 模型 / 模型服务商

数据与部署：
- documents = 文档存储（角色卡、设定都存在这里）
- room_state = 房间状态
- media store / blob = 素材存储（图片音频文件）
- data_dir = 数据目录（服务器上的数据文件夹）
- container = 容器（部署环境，网址 8787）
- volume = 数据卷（容器重启不丢数据）

命令（点号开头，用户直接输入）：
- .st / .sheet = 查看/编辑角色卡
- .cast = 施法（用法术位）
- .spells = 法术（查看/学习法术）
- .rest = 休息（恢复法术位/血量）
- .advance / .level = 升级
- .roll / .check / .save = 掷骰 / 检定 / 豁免
- .hroll = 主持人隐藏骰
- .opposed = 对抗检定（双方比骰）
- .jrrp = 今日人品骰
- .combat / .init / .attack = 战斗 / 先攻 / 攻击
- .item = 物品（查看/发放/装备）
- .party = 队伍与同伴管理
- .pc = 预生成角色管理（认领/释放/生成新角色）
- .var = 剧情追踪器（查看/调整恐慌值这类数值）
- .settle = 剧本结算（打完总结，写角色记忆）
- .forge = AI 生成剧本
- .import = 导入（剧本/卡/配置）
- .module = 剧本管理（导入/删除/查看）
- .worldbook = 世界设定库（查看/编辑设定条目）
- .clue = 线索（查看/揭示）
- .encounter = 遭遇（设计遭遇战）
- .map = 地图（绘制/查看）
- .image = 配图（生成插图）
- .audio / .bgm = 音频 / 背景音乐
- .avatar = 角色头像
- .rename = 改名
- .undo = 撤销上一步
- .ai = 切换 AI 主持人开关
- .bot = 机器人（自动回复开关）
- .panels = 面板管理（开关某类桌面卡片）
- .phase = 阶段管理（剧本进行到哪个阶段）
- .clock = 剧情时钟（时间线推进）
- .chronicle = 编年史（剧情记录）
- .habits = 习惯记录（记录玩家行为偏好）
- .trace = 调试记录开关（出问题排查用）
- .dev = 开发模式（调试房间）
- .reset = 重置房间（清进度/角色/全部）
- .backup = 备份
- .skill / .role = 主持人技能（AI 能力插件开关）

界面与机制：
- MentionCard = 角色提及卡片（点角色名弹出的卡片）
- CharacterScreen = 角色页（网页端看/改角色卡）
- ModuleScreen / ModuleDetail = 模组库页 / 模组详情页
- desk card = 桌面卡片（状态面板上的区块）
- game_clock = 剧情时钟
- party modal = 队伍成员弹窗
- panel = 面板（桌面上的一类卡片）
- clue reveal = 线索揭示（把未揭示线索亮给玩家）
- reward dice / penalty dice = 奖励骰 / 惩罚骰
- battle report = 战报（战斗结束后的小结）
- stage = 生成阶段（AI 生成剧本时走到哪一步）
- trait / tags = 角色特征 / 标签
- playstyle = 扮演风格（给 AI 同伴的角色扮演提示）
- persona = 角色人设（性格/背景描述）
- appearance = 外貌描述（用来画立绘）
- blurb = 一句话简介（名册上的一行介绍）

## 修改归属

- 规则系统放在 `rulepacks/`，优先使用数据驱动。
- 技能放在 `skills/<id>/SKILL.md`；沙箱钩子使用相同的技能 ID。
- 内容包遵循 `docs/plugins.md` 和 `.lwpack` 格式。
- 新命令必须同时修改对应的 gateway 领域模块、路由规格、
  `locales/{en,zh}/commands.json`、聚焦验证，以及网页端
  `loreweaver-web/src/features/play/commands.ts` 的注册。
- 协议变更必须同步 Python 会话版本、TypeScript 协议包、包元数据、协议 README
  和 `docs/protocol.md`。
- 房间级数据的清理（`.reset` 与换剧本 `purge_active_module`）必须走
  facet registry 单源：`.reset` 按 facet 声明的 `reset_scope` 收集目标，
  purge 清"剧本内容类（reset_scope 非 None）里属于当前模组的部分"。
  现状：`.reset` 已 registry 驱动；`purge_active_module` 仍是手写
  文档类型/状态键清单（2026-08-28 已补齐全部类别）——把 purge 改为吃
  registry 是待办重构，做之前先对齐语义（purge 保留玩家自建角色/手动
  启用的技能，reset all 连设置都清）。教训：双份清理知识必然漂移，
  两边各漏各的（实测：换剧本漏 item/clue_log/scene/note/配图索引/时钟/
  roster 条目，reset 漏 pregen_media_jobs）。新增剧本数据类 = 注册
  facet 声明 reset_scope，两条路都覆盖。
- 提出非平凡机制前先检查 `docs/notes/rejected/`；涉及生命周期、锁、provider
  或回放时，还要阅读 `docs/defensive-patterns.md`。
- 新增用户可操作的机制功能（施法、休息、战斗、升级、资源、法术管理等）必须
  同时提供 AI keeper 工具：在 `agent/kp_tools_*` 给 AI 同一条引擎结算车道
  （复用 `command_router.dispatch` 模式），不得只做玩家命令让 AI 叙事式假装
  （"休息了""命中了"却没有真实结算）。工具描述必须写明"未经引擎结算不得叙事"。
- 规则包/机制改动必须验证三层接线，缺一不可：命令车道、AI 工具、显示投影
  （`net/state` + web 渲染）与建卡链路（`agent/char_from_persona`）。用最小的
  Docker 检查验证（不添加 pytest 文件）；验证脚本要覆盖：建卡产出职业/法术位/
  已知法术、投影能显示等级与资源、AI 工具集包含对应机制工具。

## 工作流程

- 从真正负责该行为的代码路径开始，先提出局部假设，再做最小可验证改动。
- 保留现有 API 和用户改动；公开行为或创作契约变化时补充文档。
- 引擎改动通过源码仓库使用 Docker 验证。网页端常用检查是
  `docker compose up --build -d`；不要在前台运行阻塞服务器。
- 保持分层：`core/` 负责确定性逻辑，`agent/` 负责 AI 行为，`gateway/` 负责命令
  和回合编排，`net/` 负责传输。
