# SDLC Tickets (`sdlc-tickets`)

面向 **Antigravity** 与 **Codex** 的端到端 **SDLC 研发流程插件**：用长期存在的 **Ticket 账本** 替代一次性 session 快照，让需求与修复在多 session、多 Git worktree、并行分支、合入主干与上下文压缩之间保持连续；内置 `Intent → Spec → Plan` 三阶段人工门禁、规范化原子提交以及研发可视化看板。

## 解决什么问题

在 AI 辅助的 `intent → spec → plan → task` 研发流程里，session 会越开越多。如果按 session 记任务状态，很容易出现：

- 每次新开 session 都要人工回忆“这轮对话应该接上之前哪个 plan”；
- 多 session / 多 worktree 并行实施时，单一状态文件互相覆盖，合并时产生冲突；
- 删掉 worktree 或切换分支后，进度和交接上下文容易丢失。

`sdlc-tickets` 把管理单位从 **session** 提升为 **Ticket**（1 条长期工作线 = 1 张 Ticket）：

```text
启动新 Session（Antigravity / Codex Hook 只读注入开放 Ticket 简表）
  └─▶ 修改代码或文档前先做归属路由：
        ├─ 纯问答 / 闲聊 / 问别的项目 ──▶ 保持未绑定，不写任何文件
        ├─ 延续已有工作线             ──▶ attach <ticket-id>（或在父 Ticket 下挂 Follow-up FU-001）
        └─ 开启全新工作线             ──▶ new <ticket-id>
              ├─ feature：Intent ──▶ intent-readiness ──(人工批准)──▶ Spec
              │           Spec   ──▶ spec-readiness   ──(人工批准)──▶ Plan
              │           Plan   ──▶ plan-readiness   ──(人工批准)──▶ Executing
              │           Executing ──▶ 开 worktree/分支 ──▶ TASK 随代码提交 ──▶ Verifying ──▶ integrate ──▶ Done
              └─ fix：    Executing ──▶ 跳过 readiness，直接实施验证 ──▶ Done
```

## 插件包含内容

| 组件 | 作用 |
|---|---|
| `plugin.json` | Antigravity 插件清单（`sdlc-tickets`）。 |
| `hooks.json` | Antigravity `SessionStart` Hook：根据 stdin 中的 `cwd` / `workspacePaths` 定位目标仓库并注入当前绑定 Ticket 或开放 Ticket 简表；非 Git 目录静默跳过。 |
| `rules/AGENTS.md` | 通用 SDLC 路由、阶段门禁与提交约束规则（启用插件即生效）。 |
| `skills/manage-tickets/` | Ticket 账本 CLI（`scripts/tickets.py`，零第三方依赖、纯 Python 标准库）、worktree 清理前检查 `doctor`、以及自包含 HTML 研发看板（`assets/board.html`）。 |
| `skills/intent-readiness/` | 意图访谈、结构化校验与人工批准门禁（产出 `intent.md`）。 |
| `skills/spec-readiness/` | 技术规格工作坊、一致性校验与人工批准门禁（产出 `spec.md`）。 |
| `skills/plan-readiness/` | 依赖拆解、并行 lane 规划与人工批准门禁（产出 `plan.md`）。 |
| `skills/commit-code-changes/` | 原子化本地 Git 提交，将新产生的 Ticket 事件文件与对应代码/文档放在同一个 commit。 |
| `codex/hooks.json.template` | Codex `SessionStart`（`startup\|resume\|compact`）与 `PreCompact` Hook 模板。 |
| `scripts/install.sh` | 一键安装脚本，支持**单项目工作区安装**（推荐）与全局安装。 |

## 两层存储与 Worktree 连续性保证

业务仓库内的数据严格分为两层，互不污染、不丢不乱：

1. **提交到 Git 的账本层** — `docs/plans/<ticket-id>/`
   - `ticket.md`：静态头信息（ID、类型、标题、父 Ticket、目标），创建后基本不改。
   - `ticket-events/<时间戳>-<随机hex>.json`：**一条不可变事件一个文件**（`created`、`phase`、`mandate`、`branch`、`task`、`followup`、`integrated`、`handoff`、`decision`）。
   - 当前 Ticket 状态由脚本实时汇总**所有 worktree 工作区（含未提交事件）与所有本地分支树（`git ls-tree`）**中的事件文件并按文件名去重折叠得出。
   - **合并不冲突**：同一张 Ticket 在多个 worktree 并行推进时只新增不同事件文件；事件随代码一起合入主干（自动识别 `main` / `master`）。
2. **本地共享的会话交接层（不提交）** — `<主 worktree>/.codex/tickets/`
   - 通过 `git rev-parse --git-common-dir` 解析到主 worktree，所有 worktree 共享同一份 `sessions/`、`assignments/`、`checkpoints/`，清理 worktree 不会丢失交接记录。

## 安装方式

### 1. 安装到指定项目（推荐，仅对该仓库生效）

将插件安装到目标仓库的 `.agents/plugins/sdlc-tickets/`，Antigravity 只在该工作区加载，团队成员和新建 worktree 也能直接共用同一版本：

```bash
git clone https://github.com/unirain/sdlc-tickets.git
./sdlc-tickets/scripts/install.sh --project /path/to/your-repo --with-codex-hooks
```

- **Antigravity**：自动发现并启用 `.agents/plugins/sdlc-tickets/`（含 5 个 skill、规则和 `SessionStart` Hook）。
- **Codex**：加 `--with-codex-hooks` 会同步生成 `.codex/hooks.json` 指向已安装的插件脚本；首次在 Codex 打开该仓库时运行一次 `/hooks` 信任即可。

### 2. 全局安装（可选，仅 Antigravity）

如果希望本机所有项目统一启用：

```bash
./sdlc-tickets/scripts/install.sh --global
```

安装到 `~/.gemini/config/plugins/sdlc-tickets/`。

## 常用命令

所有命令仅依赖系统 `python3`（无需安装额外依赖），可在目标仓库任意 worktree 下直接运行：

```bash
TICKETS="python3 .agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py"

# 查看 Ticket
$TICKETS list                     # 查看所有开放中的 Ticket
$TICKETS list --all               # 查看全部（含 Done / Cancelled）
$TICKETS show <ticket-id>         # 查看详情、Mandate、任务清单与最近交接

# 建单、绑定与交接
$TICKETS new <slug> --title "..." --goal "..." --attach --session <sid>
$TICKETS new <slug> --kind fix --parent <parent-id> --title "..."
$TICKETS attach <ticket-id> --session <sid> --first-request "..."
$TICKETS handoff --session <sid> --summary "..." --set-next-step "..." --milestone

# 里程碑与 Worktree 生命周期
$TICKETS phase <id> Spec|Plan|Executing|Verifying|Done
$TICKETS mandate <id> --doc docs/plans/<id>/spec.md --task TASK-001 --task TASK-002
$TICKETS branch <id> --task TASK-001 --purpose "..."
$TICKETS task <id> TASK-001 --status Done --commit <sha>
$TICKETS followup <id> --open "修复边界问题"
$TICKETS integrate <id> --branch <feat-branch>
$TICKETS doctor --before-cleanup /path/to/worktree

# 研发看板
$TICKETS board                    # 生成自包含静态 HTML（默认 .codex/tickets/board/index.html）
$TICKETS serve --port 8765        # 启动本地实时看板 http://127.0.0.1:8765/
```

## 研发可视化看板

内置看板（`board` / `serve`）采用从上到下四行可折叠泳道布局：**计划中（Intent / Spec / Plan）**、**进行中（Executing / Verifying）**、**已完成（Done）**、**暂停 · 阻塞（Paused / Blocked）**：

- 支持行内阶段筛选、关键词搜索、按类型（`feature` / `fix`）、进行中分支和风险提示筛选；
- **已完成**泳道针对大量历史 Ticket 做了可扩展设计：支持近 30 天 / 近 90 天 / 全部时间筛选、卡片分页展开（默认 12 张，点击“再显示”增量加载）、按月分组的紧凑列表视图，以及“含已取消”开关；
- 点击任意卡片或列表行可打开右侧详情抽屉，查看 Mandate 文档 SHA-256 指纹、TASK 进度、分支合入状态、最近本地交接记录与完整事件时间线。

## 本地测试

```bash
pytest tests/test_manage_tickets_skill.py -q
```

离线单测覆盖：事件折叠、跨 worktree 未合入进度可见性、多 lane 无冲突合并、`doctor` 清理前保护、`master` 默认分支识别、Antigravity 与 Codex Hook 协议、看板渲染以及 `install.sh` 端到端安装。
