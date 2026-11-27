---
name: manage-tickets
description: Route every session to a durable Ticket and keep Ticket progress continuous across sessions, worktrees, branches, merges, and context compaction; also renders the R&D kanban board. Use at the start of every session (before changing code or docs), after a compact continuation, when the user asks to continue or resume earlier work, when an intent/spec/plan is approved, when an implementation worktree or branch is opened, after completing a plan TASK or follow-up, before merging or removing a worktree, before ending a session with unfinished work, and when the user asks for the ticket board / 看板 / which tickets are planned, in progress, or done. Never store secrets, customer-private data, full logs, or hidden reasoning.
---

# Manage Tickets

A **Ticket** is one durable line of work — normally one `docs/plans/<ticket-id>/` folder carrying intent → spec → plan → implementation, or a lightweight `fix`. Sessions come and go; the Ticket persists. Ticket state is operational data, not authority: the user's latest request, the current repository, and approved documents win.

## Concepts

| Concept | Meaning | Where |
|---|---|---|
| Ticket | Durable work item; id = folder slug; `feature` or `fix` | `docs/plans/<id>/ticket.md` (Git) |
| Event | Immutable fact, one file each: `created`, `phase`, `mandate`, `branch`, `task`, `followup`, `integrated`, `handoff`, `decision` | `docs/plans/<id>/ticket-events/*.json` (Git) |
| Mandate | Approved docs (path + sha256 + status), authorized TASKs, base revision | `mandate` event |
| Follow-up | Small fix attached to a Ticket (`FU-001`), no new folder | `followup` event |
| Assignment | One session's working handoff on a Ticket | `.codex/tickets/assignments/` (local, shared) |
| Binding | Which Ticket this session works on (`attach` / `new` / none) | `.codex/tickets/sessions/` (local, shared) |

Phases: `Intent → Spec → Plan → Executing → Verifying → Done`, plus `Paused`, `Blocked`, `Cancelled`. A `fix` starts at `Executing`.

**Continuity guarantees.** Live state is the fold of all event files found in every worktree (including uncommitted files) and every local branch tree, de-duplicated by filename. One file per event means parallel lanes never conflict on merge, events travel into `main` with the code, and progress stays visible from any checkout before merge. The local layer lives in the **main worktree's** `.codex/tickets/` (resolved through `git rev-parse --git-common-dir`), so removing a worktree never loses assignments. A Ticket can only be lost if a branch holding unmerged events is deleted — `doctor` guards that.

## Command

Run `scripts/tickets.py` bundled with this skill from any worktree of the target repository (uses only the Python standard library):

```bash
python3 .agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py <command> ...
```

If this plugin is installed in another location, use the script path shown by the `SessionStart` Hook. Pass `--session <id>` on session-scoped commands (`attach`, `detach`, `handoff`, `current`); the SessionStart Hook prints the id. Without it the script uses `TICKETS_SESSION_ID` / `CODEX_THREAD_ID` / `CODEX_SESSION_ID`, then a per-worktree key. Never edit or delete event or state files by hand.

## 1. Route the session first

Before changing code or docs:

1. Read the Hook context; if absent run `list` (add `--all` for closed) and `current --session <id>`.
2. Decide the binding, strongest evidence first: the current branch matches a Ticket's active branch → the user names a ticket id, TASK id, plan folder, or file → semantic match.
   - Unique branch match: attach directly and tell the user.
   - Otherwise propose 1–3 candidates and let the user choose: **attach**, **new ticket**, **follow-up on a parent**, or **no ticket** (pure Q&A, read-only analysis).
3. `attach <id> --session <sid> --first-request "<short paraphrase>"`, then read `show <id>`, the Mandate documents, the previous assignment, and verify with `git status`/diffs. Explain drift before continuing.
4. New work: `new <id> --title "..." [--kind fix] [--parent <id>] --goal "..." --attach --session <sid>`. Small fixes that belong to an existing Ticket: `followup <id> --open "..."` instead.

**Stay inside the Mandate.** When the work drifts beyond the bound Ticket's approved scope (another bug, unapproved behaviour, a different plan), stop and ask the user: follow-up, new Ticket, or revise spec/plan. Never silently widen scope or switch Tickets.

## 2. Phase-driven SDLC workflow

`manage-tickets` and the three readiness skills form one unified workflow: the Ticket is the lifecycle spine, and each pre-execution phase invokes its matching readiness skill before moving forward.

| Ticket kind & phase | Next action / skill | Gate to advance |
|---|---|---|
| `feature` · `Intent` | Invoke [intent-readiness](../intent-readiness/SKILL.md) → `docs/plans/<id>/intent.md` | Human approves intent → `phase <id> Spec` + `mandate <id> --doc docs/plans/<id>/intent.md` |
| `feature` · `Spec` | Invoke [spec-readiness](../spec-readiness/SKILL.md) → `docs/plans/<id>/spec.md` | Human approves spec → `phase <id> Plan` + `mandate <id> --doc ...intent.md --doc ...spec.md` |
| `feature` · `Plan` | Invoke [plan-readiness](../plan-readiness/SKILL.md) → `docs/plans/<id>/plan.md` | Human approves plan → `phase <id> Executing` + `mandate <id> --doc ...spec.md --doc ...plan.md --task ...` |
| `feature` · `Executing` | Open worktree/branch (`branch`), execute authorized TASKs within Mandate | Commit `task` events with code → `phase <id> Verifying` |
| `fix` (starts `Executing`) or open `followup` | Skip readiness skills; implement and verify directly | Close `followup` or `integrate` + `phase <id> Done` |
| `Verifying` → `Done` | Acceptance / UAT, merge into `main`, run `integrate` | `phase <id> Done` (rejects open tasks/follow-ups unless `--force`) |

**Phase gate rule.** For a `feature` Ticket still in `Intent`, `Spec`, or `Plan`, **do not create implementation branches/worktrees or edit production code**. Immediately enter the phase's readiness skill and stop at its human approval gate. Only skip a readiness stage when the user explicitly waives it — record the waiver with `decision` (or create the Ticket as `--kind fix`) before advancing to `Executing`.

## 3. Record milestones as events (committed with the work)

| Moment | Command |
|---|---|
| intent / spec / plan approved | `phase <id> Spec\|Plan\|Executing` and `mandate <id> --doc <path> ... [--task TASK-001 ...]` |
| implementation worktree/branch opened | `branch <id> [--branch feat/x] --task TASK-00N ... --purpose "..."` (run inside the new worktree) |
| TASK finished / blocked | `task <id> TASK-00N --status Done\|Blocked\|InProgress\|Dropped [--commit sha] [--note ...]` |
| follow-up opened / closed | `followup <id> --open "..."` / `followup <id> --close FU-00N [--note ...]` |
| durable decision | `decision <id> "..."` |
| branch merged | `integrate <id> --branch feat/x [--into main]` on the target branch (`--force` after squash merges) |
| acceptance / release | `phase <id> Verifying`, then `phase <id> Done` (rejects open tasks/follow-ups unless `--force`) |

Event files are new files under `docs/plans/<id>/ticket-events/`; **commit them in the same commit as the code or docs they describe** (see `commit-code-changes`). Don't batch them into unrelated commits.

## 4. Keep the session handoff current (local)

```bash
python3 .agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py handoff --session <sid> \
  --summary "What is true now" --add-completed "Verified outcome" \
  --set-next-step "Highest-priority next action" --add-file path --add-verification "cmd → result"
```

Run it after meaningful progress, before risky work, immediately before manual `/compact`, when blocked, and before ending an unfinished session. Add `--milestone` at real milestones (end of a TASK, end of a session that another worktree/session will continue) to also commit the summary/next steps as a `handoff` event visible on every branch. `detach --session <sid>` closes the assignment when the session is done.

Record outcomes, not narration; label assumptions; reference repository files instead of copying content.

## 5. Worktree lifecycle

1. Open: create the worktree, then in it run `branch <id> ...` and `attach <id> --session <sid>`.
2. Work: commit `task` events with the code.
3. Merge: on `main`, merge the branch, run `integrate <id> --branch <branch>`, and commit the event.
4. Clean up: run `doctor --before-cleanup <worktree-path>`; it fails on uncommitted ticket events and on unmerged events whose branch would be lost. Remove the worktree only when it passes; keep the branch if it still holds unmerged events.
5. Periodically run `doctor` to catch deleted branches without `integrate`, bindings to unknown tickets, and closed tickets with open branches.

## 6. Kanban board

For the usual "show me the board" request, invoke the [board](../board/SKILL.md) skill, which renders and opens it in one step (`board --open`).

- `board [--output path] [--open]` writes a self-contained HTML board (default `.codex/tickets/board/index.html`) with four stacked, collapsible lanes — 计划中 (Intent/Spec/Plan), 进行中 (Executing/Verifying), 已完成, 暂停·阻塞 — each a wrapping card grid with phase filter chips, task progress, active branches, open follow-ups, and not-on-main badges. The 已完成 lane scales to many tickets: period filter (30/90 days/all), 12 cards then "show more", a month-grouped list view, and an include-cancelled toggle. A detail drawer shows mandate, tasks, branches, timeline, and the latest assignment. `--open` opens the result and reports `opened` in the JSON reply; `TICKETS_OPENER` overrides the viewer command.
- `serve [--port 8765]` serves a live board on `127.0.0.1` that rebuilds on every request; run it as a background process.
- To publish: when the runtime offers an artifact/page publishing tool (for example Antigravity's `publish_artifact`), publish the generated HTML and reuse the same artifact id on later updates (store it in `.codex/tickets/board/artifact.json`). Otherwise open the file or use `serve`.

## 7. Hooks

Both Antigravity (plugin root `hooks.json`) and Codex (`.codex/hooks.json`) run the `hook` command:

- **Antigravity** (`SessionStart` via `--runtime antigravity`): resolves the target project from stdin `cwd` / `workspacePaths`, reads `conversationId`, and injects the bound Ticket context or open-ticket routing table via `injectSteps` (no-ops silently in non-Git folders without `docs/plans/`).
- **Codex** (`SessionStart` on `startup|resume|compact`, `PreCompact` on `manual|auto`): bound sessions get their Ticket, Mandate, and latest assignment; unbound sessions get the session id, branch matches, and an open-ticket table for routing; `PreCompact` checkpoints the bound assignment to `.codex/tickets/checkpoints/`.

The Hook never fails the session and never writes Ticket events. After compaction, reconcile the restored context with the conversation and run `handoff` for any missing progress. New or changed Codex Hooks must be trusted via `/hooks`; never claim Hook coverage when untrusted.

## Conflict rules

- Current user instructions > repository and external evidence > ticket state.
- Do not attach a session to a Ticket the user did not intend; ask when ambiguous.
- Do not mark approvals yourself — record `phase`/`mandate` only after the human approval recorded by the readiness skills.
- Do not commit `.codex/tickets/`; do commit `docs/plans/<id>/ticket.md` and `ticket-events/`.
- Legacy `.codex/task-state/` snapshots are read-only history; consult them only when the user points to old work not yet migrated.
