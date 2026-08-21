---
name: loreweaver-upstream-sync
description: 实时跟踪 loreweaver 原项目（上游）仓库变化——fetch、对比、pull、合理解决冲突、跑测试验证，最后向用户汇报本次同步的具体改动。用于"看看上游最近有没有新东西"或定期同步上游。
---

# Loreweaver 上游同步

跟踪原项目仓库 `1A7432/loreweaver` 的更新，同步到本 fork（`Trouvaille0198/loreweaver`），解决冲突，验证健康，汇报改动。

## 前置要求

1. 本仓库是 fork：`origin = Trouvaille0198/loreweaver`，`upstream = 1A7432/loreweaver`
2. 开发依赖齐全（跑全量测试需要）：`uv sync --extra anthropic --extra gemini --extra ejs`
3. 客户端测试：`cd clients/tui && bun test`

## 仓库拓扑（重要）

```
upstream (1A7432/loreweaver)   ← 作者原仓库，唯一的上游来源
origin  (Trouvaille0198/loreweaver) ← 本 fork（推送目标）
local main
```

**fork 特有提交（不在上游，merge 时必须保留）**：
- `feat(web): --web transport for the browser client`（loreweaver-web 依赖的 fork 特性）
- WS / protocol 修复提交

## 同步流程

### 1. 确保 upstream remote 存在

```bash
git remote -v | grep upstream || git remote add upstream git@github.com:1A7432/loreweaver.git
```

### 2. 拉取上游并对比

```bash
git fetch upstream
# 分叉情况：左=本地领先数，右=落后数
git rev-list --left-right --count main...upstream/main
git log --oneline main..upstream/main   # 上游新提交
```

### 3. 评估冲突风险（关键步骤）

```bash
# 上游动了哪些文件
git diff --name-only main...upstream/main
# 本地未提交改动
git status --short
```

**冲突规则**：
- 上游文件与本地**未提交改动**零重叠 → 直接 pull，安全
- 有重叠 → 先 commit/stash 本地改动，再 pull，解决冲突
- 上游**永远不会**有 fork 的 `--web` 特性文件冲突（上游没有这些文件）

### 4. 执行 pull（merge 方式，不用 rebase）

```bash
git pull upstream main --no-edit --no-rebase
```

- 用 merge 而非 rebase：保留 fork 特有提交历史
- pull 失败提示 "divergent branches" 时加 `--no-rebase` 显式指定

### 5. 冲突解决原则

如果 merge 报冲突（罕见——上游很少碰 `clients/tui/` 和 `gateway/commands/`）：
- **fork 特性（--web、客户端修复）以本地为准**——上游没有这些功能，上游版本是旧的
- **引擎核心（core/agent/net/infra）以上游为准**——作者的新修复/功能优先
- 双向改动：逐块看语义，保留双方意图

### 6. 验证合并后健康

```bash
# Python 侧
uv run pytest -q
# 客户端侧
cd clients/tui && bun test
```

**已知的环境性测试失败（不是代码问题，不要误报为回归）**：
- `tests/test_doctor.py` 的 3 个测试：本地 `.env` 配了 `TRPG_SCRIBE__*`（便宜记账模型）时，"Scribe 用旗舰价"警告不触发 → 断言失败。CI 干净环境会过。验证方法：临时 `mv .env /tmp/...` 后测试全过。
- 缺 extras 时 `test_providers.py` 报 `ModuleNotFoundError: No module named 'google'/'anthropic'` → `uv sync --extra anthropic --extra gemini --extra ejs`

### 7. 汇报改动（必须告知用户）

汇报格式：
- 上游新增提交列表（`git log --oneline main@{1}..main` 或 fetch 前的对比）
- 改动文件分类（核心引擎 / 客户端 / 测试 / 文档）
- 影响面：是否动了协议、pack 格式、铁律相关（查 `AGENTS.md` 的 red lines）
- 合并后测试结果
- 有无冲突及解决方式

## 注意事项

- **NEVER 前台阻塞服务器**：验证用测试，不用 `python -m app --serve`
- 本地有未提交改动时 pull 要格外小心——先分析重叠，必要时 stash
- 上游 tag（如 `v1.0.0`）fetch 时会一起拉下来，不影响 main
- 汇报时用中文，列具体 commit（短 hash + 标题），别只说"已同步"
