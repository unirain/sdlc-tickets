# SDLC Tickets (`sdlc-tickets`)

An end-to-end **SDLC workflow plugin** for **Antigravity** and **Codex** that replaces disposable per-session task notes with a durable **Ticket ledger** — keeping work continuous across sessions, Git worktrees, parallel branches, merges, and context compaction, paired with phase-gated `Intent → Spec → Plan` workshops, clean atomic commits, and a built-in R&D kanban board.

## Why

In an AI-assisted `intent → spec → plan → task` workflow, sessions proliferate quickly. Tracking state per session forces humans to manually remember which session belongs to which plan, breaks across Git worktrees, and causes merge conflicts on shared state files.

`sdlc-tickets` shifts the unit of continuity from **session** to **Ticket**:

```text
Session starts (Antigravity / Codex Hook)
  └─▶ Route session before editing code/docs:
        ├─ Pure Q&A / unrelated chat ──▶ Stay unbound (no files written)
        ├─ Existing work line        ──▶ attach <ticket-id> (or open Follow-up FU-001)
        └─ New work line             ──▶ new <ticket-id>
              ├─ feature: Intent ──▶ intent-readiness ──(approve)──▶ Spec
              │            Spec  ──▶ spec-readiness   ──(approve)──▶ Plan
              │            Plan  ──▶ plan-readiness   ──(approve)──▶ Executing
              │            Executing ──▶ worktree/branch ──▶ TASK commits ──▶ Verifying ──▶ integrate ──▶ Done
              └─ fix:      Executing ──▶ implement & verify ──▶ Done
```

## What's Inside

| Component | Purpose |
|---|---|
| `plugin.json` | Antigravity plugin manifest (`sdlc-tickets`). |
| `hooks.json` | Antigravity `SessionStart` hook — injects open Tickets / bound Ticket context (auto-detects target workspace from `cwd` / `workspacePaths`, silently no-ops outside Git repos). |
| `rules/AGENTS.md` | Concise, always-on SDLC routing & phase-gating rules. |
| `skills/manage-tickets/` | Ticket ledger CLI (`scripts/tickets.py`, Python stdlib only), worktree `doctor`, and interactive HTML kanban board (`assets/board.html`). |
| `skills/intent-readiness/` | Structured intent workshop + structural evaluator + human approval gate (`intent.md`). |
| `skills/spec-readiness/` | Technical specification workshop + evaluator + approval gate (`spec.md`). |
| `skills/plan-readiness/` | Dependency-aware execution planning workshop + evaluator + approval gate (`plan.md`). |
| `skills/commit-code-changes/` | Atomic local Git commits that keep new Ticket event files together with the code/docs they describe. |
| `codex/hooks.json.template` | Codex `SessionStart` (`startup\|resume\|compact`) + `PreCompact` hook template. |
| `scripts/install.sh` | Workspace-scoped (or opt-in global) installer. |

## Architecture & Continuity Guarantees

State is split into two clean layers inside each target repository:

1. **Committed ledger (Git)** — `docs/plans/<ticket-id>/`
   - `ticket.md`: static header (id, kind, title, parent, goal).
   - `ticket-events/<timestamp>-<hex>.json`: **one immutable JSON file per milestone event** (`created`, `phase`, `mandate`, `branch`, `task`, `followup`, `integrated`, `handoff`, `decision`).
   - Current Ticket state is folded deterministically across all worktrees (including uncommitted files) and all local branches (`git ls-tree`), de-duplicated by event ID.
   - **Zero merge conflicts**: parallel lanes only add new event files; events travel into `main`/`master` with the code they describe.
2. **Local shared session memory (gitignored)** — `<main-worktree>/.codex/tickets/`
   - Resolved via `git rev-parse --git-common-dir`, so every worktree of a clone shares the same `sessions/`, `assignments/`, and `checkpoints/` — removing a worktree never loses handoffs.

## Installation

### Project-scoped install (recommended)

Installs the plugin strictly into a single repository (`.agents/plugins/sdlc-tickets/`), so Antigravity discovers it only for that workspace and every worktree/teammate gets the exact same version:

```bash
git clone https://github.com/unirain/sdlc-tickets.git
./sdlc-tickets/scripts/install.sh --project /path/to/your-repo --with-codex-hooks
```

- **Antigravity**: automatically loads `.agents/plugins/sdlc-tickets/` (skills, `rules/AGENTS.md`, and `hooks.json`).
- **Codex**: `--with-codex-hooks` writes `.codex/hooks.json` pointing to the installed plugin script. Run `/hooks` once in Codex to trust the project hooks.

### Global install (optional, Antigravity only)

```bash
./sdlc-tickets/scripts/install.sh --global
```

Installs into `~/.gemini/config/plugins/sdlc-tickets/` so all workspaces on the machine inherit it.

## Common Commands

All commands run with system `python3` (zero third-party runtime dependencies) from any worktree of the target repository:

```bash
TICKETS="python3 .agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py"

# Inspect tickets
$TICKETS list                     # open tickets
$TICKETS list --all               # include Done / Cancelled
$TICKETS show <ticket-id>         # full ticket, mandate, tasks, latest assignment

# Create, route, and track
$TICKETS new <slug> --title "..." --goal "..." --attach --session <sid>
$TICKETS new <slug> --kind fix --parent <parent-id> --title "..."
$TICKETS attach <ticket-id> --session <sid> --first-request "..."
$TICKETS handoff --session <sid> --summary "..." --set-next-step "..." --milestone

# Milestones & worktree lifecycle
$TICKETS phase <id> Spec|Plan|Executing|Verifying|Done
$TICKETS mandate <id> --doc docs/plans/<id>/spec.md --task TASK-001 --task TASK-002
$TICKETS branch <id> --task TASK-001 --purpose "..."
$TICKETS task <id> TASK-001 --status Done --commit <sha>
$TICKETS followup <id> --open "Fix edge case"
$TICKETS integrate <id> --branch <feat-branch>
$TICKETS doctor --before-cleanup /path/to/worktree

# Kanban board
$TICKETS board                    # static self-contained HTML (.codex/tickets/board/index.html)
$TICKETS serve --port 8765        # live local board at http://127.0.0.1:8765/
```

## R&D Kanban Board

The bundled kanban board (`board` / `serve`) renders four stacked, collapsible lanes — **计划中 (Intent / Spec / Plan)**, **进行中 (Executing / Verifying)**, **已完成 (Done)**, and **暂停 · 阻塞 (Paused / Blocked)** — with:

- Per-lane phase filter chips, search, type filter (`feature` / `fix`), and active-branch / risk filters;
- Scalable **Done** view with period selector (30d / 90d / All), progressive card pagination, month-grouped table view, and optional cancelled toggle;
- Slide-over detail drawer showing Mandate document SHA-256 fingerprints, TASK progress, branch merge status, latest local assignment, and full event timeline.

## Development & Tests

```bash
pytest tests/test_manage_tickets_skill.py -q
```

Tests are fully offline and cover event folding, cross-worktree visibility, conflict-free parallel merges, `doctor` cleanup guards, `master` branch auto-detection, Antigravity & Codex hooks, board rendering, and `install.sh`.
