---
name: board
description: "Render and open the Ticket kanban board for this repository. Use when the user asks for the ticket board, kanban, 看板, a visual overview of planned or in-progress work, or which tickets are done."
---

# Ticket Board

Render the R&D kanban board and open it for the user.

## Render and open

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/manage-tickets/scripts/tickets.py" board --open
```

Outside Claude Code the plugin lives elsewhere; use the script path printed by the `SessionStart` Hook:

```bash
python3 .agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py board --open
```

The command writes a self-contained HTML file (default `.codex/tickets/board/index.html`) and opens it in the default viewer. The JSON result carries `output`, `tickets`, and `opened`; when `opened` is `false` (headless host or no viewer available), report the `output` path instead of retrying.

Useful options:

- `--output <path>` renders somewhere other than the default.
- `--json-output <path>` also writes the board payload for inspection.

## Live board

`serve` rebuilds the page on every request, so it reflects new events without re-rendering:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/manage-tickets/scripts/tickets.py" serve --port 8765
```

It blocks, so run it as a background process and tell the user the address (`http://127.0.0.1:8765/`). Stop it when the user is done.

## Publishing

When the runtime offers an artifact or page-publishing tool (for example `publish_artifact`), publish the generated HTML and reuse the same artifact id on later updates, storing it in `.codex/tickets/board/artifact.json`. Otherwise open the file or use `serve`.

## Boundaries

The board is read-only: never modify Ticket events or `.codex/tickets/` state to change what it shows. Fix the data with `manage-tickets` commands instead.
