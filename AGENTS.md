# AGENTS.md

## 范围

本文件是 Loreweaver 引擎的工作规则。仓库是唯一事实来源；产品、协议、
内容创作和部署细节分别见 `README.md`、`docs/` 和 `WORKFLOW.md`。

## 硬性规则

- 禁止运行 pytest、test、lint、typecheck（用户明确要求，最高优先级）；验证与部署
  只跑 `docker compose build` / `docker compose up`，不用测试套件验收改动。
- 解释行为时使用人话，不要让用户去理解内部方法名或变量名。
- 骰子、检定、人物数值、权限、校验、持久化和隐私必须由确定性代码处理，
  不得交给 AI。AI 只负责旁白、对话和风味内容。
- 先掷骰，再叙述结果。不得预写或臆造检定结果。
- 用结构保证信息隔离：玩家视图不得包含守门人专属设定、未揭示线索或变量，
  也不得包含其他演员的私有知识。区分真人 `keeper` 和模型 `AI keeper`。
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
