---
name: commit-code-changes
description: "Review and commit completed code changes in a Git repository after implementation and verification. Use automatically at the end of every coding task in this project when task-owned changes are ready: inspect the worktree, exclude unrelated or sensitive files, split changes into logical atomic groups, and create concise local commits without pushing."
---

# Commit Code Changes

Create clean local commits after finishing and verifying a coding task. Preserve all pre-existing or unrelated worktree changes.

## Workflow

1. Inspect the repository.
   - Run `git status --short`, `git diff`, and `git diff --cached`.
   - Identify only the files and hunks produced by the current task.
   - If no task-owned changes remain, do not create an empty commit.
   - If unrelated staged changes cannot be safely separated, stop and ask the user before changing the index.

2. Verify the implementation and project artifacts.
   - If the repository maintains a code map (e.g., `docs/code-map/README.md` or a `maintain-code-map` skill), check whether indexed paths/symbols, entry points, responsibilities, workflow branches, or data authority changed; update the map and run its checker (e.g., `make check-code-map`) before staging, keeping map updates in the same logical commit.
   - Include the task's new ticket event files (`docs/plans/<ticket-id>/ticket-events/*.json`, written by [manage-tickets](../manage-tickets/SKILL.md)) in the same commit as the code or docs they describe; never edit or delete existing event files. Never commit `.codex/tickets/`.
   - Run the relevant tests, linters, or build checks before committing.
   - Do not commit when a relevant check fails unless the user explicitly accepts the failure.
   - Recheck generated files and migrations when they are part of the task.

3. Plan logical groups.
   - Make each commit represent one coherent concern that can be reviewed or reverted independently.
   - Keep implementation and its directly related tests together.
   - Separate unrelated documentation, refactors, or tooling changes when they stand alone.
   - Do not split mechanically by file when the files implement the same concern.

4. Stage one group at a time.
   - Stage explicit paths or selected hunks; never use `git add .` or `git add -A` in a dirty worktree.
   - Run `git diff --cached --check`, inspect `git diff --cached --stat`, then read the full staged diff.
   - Confirm the staged set contains no secrets, credentials, local workspace data, generated noise, or unrelated user changes.

5. Commit the group.
   - Inspect recent history with `git log -5 --oneline` and match the repository's message style.
   - Use a concise, imperative, single-line message, normally no more than 72 characters.
   - Use a conventional prefix such as `feat:`, `fix:`, `test:`, `docs:`, or `chore:` only when the repository already follows that convention.
   - Never amend, bypass hooks, rewrite history, or create an empty commit unless the user explicitly requests it.
   - If a hook modifies files, inspect those changes and rerun the relevant verification before committing again.

6. Finish the handoff.
   - Repeat staging and committing for each planned group.
   - Run `git status --short` after the final commit.
   - Report each commit hash and message, verification performed, and any intentionally uncommitted files.
   - Do not push, tag, open a pull request, or change branches unless the user explicitly asks.

## Safety Rules

- Preserve user-owned edits and existing staged changes.
- Do not use destructive Git commands to clean the worktree.
- Do not commit unresolved conflicts, credential files, or ignored local artifacts.
- Treat commit hooks as mandatory; never use `--no-verify` without explicit permission.
