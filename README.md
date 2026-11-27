# SDLC Tickets Plugin (`sdlc-tickets`)

Reusable end-to-end SDLC workflow plugin for **Antigravity** and **Codex**:

- **`manage-tickets`**: durable Ticket ledger (`docs/plans/<ticket-id>/ticket.md` + immutable one-file-per-event `ticket-events/*.json`), worktree/branch continuity, shared local session handoffs (`.codex/tickets/`), `doctor` safety checks, and an interactive R&D kanban board (`board` / `serve`).
- **`intent-readiness`**: interview, structure, and gate `intent.md`.
- **`spec-readiness`**: turn approved intent into `spec.md`.
- **`plan-readiness`**: turn approved spec into an implementation-ready `plan.md`.
- **`commit-code-changes`**: clean, atomic local Git commits that keep Ticket event files together with the code/docs they describe.
- **`hooks.json` & `rules/AGENTS.md`**: automatic `SessionStart` Ticket routing and SDLC rules.

## Install into a project (recommended, workspace-scoped)

```bash
./scripts/install.sh --project /path/to/your-repo --with-codex-hooks
```

This installs the plugin into `<repo>/.agents/plugins/sdlc-tickets/` (discovered automatically by Antigravity only for that project) and writes `<repo>/.codex/hooks.json` for Codex.

## Optional: install globally (Antigravity)

```bash
./scripts/install.sh --global
```

## Run tests

```bash
pytest tests/test_manage_tickets_skill.py -q
```
