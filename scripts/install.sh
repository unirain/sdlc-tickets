#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_NAME="sdlc-tickets"

usage() {
  cat <<'EOF'
Install the sdlc-tickets plugin into a single project (default) or globally.

Usage:
  scripts/install.sh --project <repo-path> [--with-codex-hooks]
  scripts/install.sh --global

Options:
  --project <path>     Install into <path>/.agents/plugins/sdlc-tickets (workspace-scoped).
  --with-codex-hooks   Also write <path>/.codex/hooks.json pointing to the installed plugin script.
  --global             Install into ~/.gemini/config/plugins/sdlc-tickets (applies to all projects).
  -h, --help           Show this message.
EOF
}

PROJECT_DIR=""
WITH_CODEX_HOOKS=0
GLOBAL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_DIR="${2:-}"
      shift 2
      ;;
    --with-codex-hooks)
      WITH_CODEX_HOOKS=1
      shift
      ;;
    --global)
      GLOBAL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$GLOBAL" -eq 1 && -n "$PROJECT_DIR" ]]; then
  echo "error: pass either --project <path> or --global, not both" >&2
  exit 2
fi

if [[ "$GLOBAL" -eq 0 && -z "$PROJECT_DIR" ]]; then
  echo "error: specify --project <repo-path> (or --global)" >&2
  usage >&2
  exit 2
fi

copy_plugin() {
  local dest="$1"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp "$SOURCE_DIR/plugin.json" "$dest/plugin.json"
  cp "$SOURCE_DIR/hooks.json" "$dest/hooks.json"
 cp -R "$SOURCE_DIR/.claude-plugin" "$dest/.claude-plugin"
 cp -R "$SOURCE_DIR/hooks" "$dest/hooks"
  cp -R "$SOURCE_DIR/rules" "$dest/rules"
  cp -R "$SOURCE_DIR/skills" "$dest/skills"
  find "$dest" -name "__pycache__" -type d -exec rm -rf {} +
  find "$dest" -name "*.pyc" -delete
}

if [[ "$GLOBAL" -eq 1 ]]; then
  TARGET="$HOME/.gemini/config/plugins/$PLUGIN_NAME"
  copy_plugin "$TARGET"
  echo "Installed global Antigravity plugin: $TARGET"
  exit 0
fi

PROJECT_ABS="$(cd "$PROJECT_DIR" && pwd)"
TARGET="$PROJECT_ABS/.agents/plugins/$PLUGIN_NAME"
copy_plugin "$TARGET"
echo "Installed workspace plugin: $TARGET"

if [[ "$WITH_CODEX_HOOKS" -eq 1 ]]; then
  mkdir -p "$PROJECT_ABS/.codex"
  SCRIPT_EXPR='$(git rev-parse --show-toplevel)/.agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py'
  python3 - "$SOURCE_DIR/codex/hooks.json.template" "$PROJECT_ABS/.codex/hooks.json" "$SCRIPT_EXPR" <<'PY'
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
Path(sys.argv[2]).write_text(template.replace("__TICKETS_SCRIPT__", sys.argv[3]), encoding="utf-8")
PY
  echo "Updated Codex hooks: $PROJECT_ABS/.codex/hooks.json"
fi
