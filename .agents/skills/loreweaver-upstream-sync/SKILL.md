---
name: loreweaver-upstream-sync
description: 跟踪 loreweaver 原项目（上游 1A7432/loreweaver）更新，rebase 到本 fork（Trouvaille0198/loreweaver），解决冲突、修复合并暴露的问题、验证健康、部署，最后汇报。用于"看看上游最近有没有新东西"或同步上游。
---

# Loreweaver 上游同步

跟踪原项目仓库 `1A7432/loreweaver` 的更新，同步到本 fork（`Trouvaille0198/loreweaver`），解决冲突，修复合并暴露的问题，验证健康，部署，汇报改动。

## 前置要求

1. 本仓库是 fork：`origin = Trouvaille0198/loreweaver`，`upstream = 1A7432/loreweaver`
2. 开发依赖齐全：`uv sync --extra anthropic --extra gemini --extra ejs`
3. 客户端测试：`cd clients/tui && bun test`；web 前端 `cd loreweaver-web && bun run test`
4. 部署：`cd loreweaver-web && docker compose up --build -d`（`.env` 已配 `ENGINE_CONTEXT=/home/melon/pros/loreweaver`、`LW_UID/LW_GID=1000`、`LORE_DATA_DIR=/home/melon/loreweaver-data-local`）

## 仓库拓扑

```
upstream (1A7432/loreweaver)   ← 作者原仓库，唯一的上游来源
origin  (Trouvaille0198/loreweaver) ← 本 fork（推送目标）
local main
```

**fork 特有提交（不在上游，同步时必须保留）**：`--web` 浏览器传输、WS/protocol 修复、p2p_ticket（Iroh ticket 进 welcome 帧）、`imagegen_for_room`（per-room imagegen 检测）、typed provider profiles（chat/image 分模型、catalog 带 `image_default_base_url`）、item 系统（items/归档/improvised）、settle 结算、角色 dossier/记忆/关系、`.share`、本地中文 AGENTS.md。

## 流程（用户已定：REBASE，不是 merge！）

### 1. 观察（只读，先汇报给用户，用户点头才动）

```bash
git fetch upstream
MB=$(git merge-base main upstream/main)
git rev-list --left-right --count main...upstream/main   # 左=本地领先，右=上游新提交
git log --oneline $MB..upstream/main                     # 上游新提交
# 上游改动文件 vs 本地改动文件（重叠 = 合并冲突点）
git diff --name-only $MB..upstream/main | sort > /tmp/up.txt
git diff --name-only <本地基线commit> <本地HEAD> | sort > /tmp/local.txt   # 注意 main 会移动，别用 git diff main <HEAD>
comm -12 /tmp/up.txt /tmp/local.txt
```

**汇报格式**：上游提交按主题分类（短 hash + 标题）、改动文件分类、与本地重叠文件、预算数字分歧（本地 `MAX_ROUNDS_PER_TURN=6` 是本地 item 伪造检查，保留本地 162/6）、合并风险点。**用户拍板后再动**。

### 2. 执行 rebase

本地有未提交改动时先处理：

```bash
git stash push -m "wip" <被挡的文件...>    # 只 stash 挡路的，不丢数据
git rebase --autostash upstream/main       # 自动暂存未提交改动，完成后恢复
```

- rebase 会把本地全部提交重放到 upstream/main 之上；`--autostash` 自动处理未提交改动
- 冲突时：`git status` 看冲突文件，逐个解决后 `git add` + `git rebase --continue`
- 卡住可 `git rebase --abort` 回退

### 3. 冲突解决原则

- **fork 特性以本地为准**：`--web`/WS、`p2p_ticket`、`imagegen_for_room`、typed profiles、item 系统、settle——上游没有这些，上游版本是旧的。例如 iroh_server 的 welcome 帧：本地结构（`_write_line` 错误处理）+ 本地 `p2p_ticket` + 本地 `imagegen_for_room`，三者都要。
- **引擎核心以上游为准**：上游的新修复/功能优先。例如 room_backup 的 `skip_unreadable`（坏 blob 不挡修复）、`result.turn <= 0` 守卫（失败回合不跑书记官）。
- **双向改动逐块看语义**：本地 `services.room_lane_enabled(chat_key, lane)` 内部已含 `settings.<lane>.enabled` 检查——可覆盖上游的全局开关，但上游新增的 `result.turn <= 0` 要保留：`if ctx.platform == "companion" or result.turn <= 0 or not await services.room_lane_enabled(...)`。
- **纯本地新增参数**（上游 HEAD 侧为空）：取本地侧。
- **语言冲突**（AGENTS.md 中文 vs 上游英文）：保留本地中文版，数字/语义以上游融合。
- 合并会暴露本地"该修"的问题：`RoomStateFacet` 缺失（新 room_state key 必须声明 facet，`net/room_lifecycle.FACET_MODULES` 注册）、i18n 硬编码、架构红线（agent/ 零系统 token——系统显示名放 `core.rulepacks.rule_display_name`）。

### 4. 验证：全量 pytest + 客户端

```bash
uv run pytest -q -p no:cacheprovider   # 后台跑，结果写文件
cd clients/tui && bun test
```

**失败甄别（关键）**：合并后失败多为"本地半成品功能 vs 上游更严检查"的交互，不是上游破坏。逐类判断：
- **逻辑对、测试过时 → 改测试**：功能迁移（mature-mode skill→preset）、新字段（scribe verdict 加 `memories`、error 帧加 `private`）、本地特性（minimax catalog 加 `image_default_base_url`、admin profile 加 `chat_model`、`llm_profiles` 带 `chat_model`、MutableLLM 加 `base` 参数要同步测试 mock）。
- **测试对、逻辑错 → 改代码**：合并暴露的真 bug。本次案例：
  - item 动作正则 `hand(?:ed)?` 误匹配名词 "hand"（"shaky hand"）→ 删掉 `hand(?:ed)?`
  - media get 的 installed-pack asset fallback 未设 keeper 门控 = blob oracle 漏洞 → `if getattr(member, "role", "") == "keeper"`
  - demo locale 检测基于整个 prompt（被 en analysis prompt 的中文 guidance 干扰）→ 改为基于模块文本语言
  - forge 硬编码系统名（coc/dnd/wod）违反 agent/ 零系统 token 红线 → 移 `core.rulepacks`
  - 本地新 play 工具超 schema 预算 → 低频工具标 `prep_only=True`（settle 例外：测试钉住它必须 play 阶段可用），或按需提高预算（本地 play 工具集 40 个是真实需求）
  - `.dev` status 分支在合并中丢失 → 补回上游版本
  - BehaviorSmokeLLM 匹配：加 speaker-label 容忍（en `": "` / zh `"："`），且**不 break**（原版 last-match-wins——chain 里最早 user 消息是历史回合）

### 5. 提交与推送

```bash
git add -A && git commit -m "fix: reconcile fork features with upstream after rebase"
git push --force-with-lease origin main    # rebase 改写历史，必须 force
```

### 6. 部署

```bash
cd loreweaver-web && docker compose up --build -d
# 验证：curl http://127.0.0.1:8787/ 返回 200；docker logs 看 Web/Iroh 就绪
```

### 7. 汇报

- 上游新增提交列表（主题分类）
- 合并/冲突处理方式（哪些本地优先、哪些上游优先）
- 失败修复清单（每类：改测试 or 改代码 + 原因）
- 测试结果（全量 pytest、TUI、web）
- 部署状态

## 注意事项

- **先观察汇报，用户拍板才执行**（用户明确要求谨慎，遇到难题让用户抉择）
- **用户要 REBASE，不是 skill 旧版的 merge**（2026-08-26 用户明确否决 merge）
- 本地未提交改动挡路 → `git stash push <文件>` 再操作，完成后 `git stash pop`（不丢数据）
- 已知环境性失败（非回归）：`tests/test_doctor.py` 3 个（本地 `.env` Scribe 配置）、`test_providers.py` ModuleNotFoundError（缺 extras）、`test_panels_wire` utf-32-be（已修，2026-08-26）
- 预算数字：本地 `MAX_ROUNDS_PER_TURN=6`（item 伪造检查），上游=5；合并保留本地 162/6，`test_turn_call_budget.py` 期待 162/22/6 + 中文 AGENTS.md 段
- 汇报用中文，列具体 commit（短 hash + 标题），别只说"已同步"
