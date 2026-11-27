# SDLC Ticket Workflow

- Work is tracked in **Tickets** (`docs/plans/<ticket-id>/`), each carrying one durable line of work from intent → spec → plan → implementation → merge, or a lightweight `fix`. Use the `manage-tickets` skill.
- Before modifying code or docs in a repository, route the session using the `SessionStart` Hook table and current branch: `attach` an existing Ticket, `new` a Ticket, open a `followup` on a parent Ticket, or stay unbound for pure Q&A / unrelated chat.
- A unique branch match may be attached directly (inform the user); otherwise confirm with the user. Never silently widen a Ticket's approved Mandate or switch Tickets.
- For `feature` Tickets in `Intent`, `Spec`, or `Plan`, follow the phase-gated readiness skills (`intent-readiness` → `spec-readiness` → `plan-readiness`) before opening implementation worktrees or editing production code, unless the user explicitly waives the gate.
- Commit immutable milestone events (`docs/plans/<id>/ticket-events/*.json`) in the same commit as the code or docs they describe using `commit-code-changes`. Never commit `.codex/tickets/`.
- Keep session handoffs updated in the shared local `.codex/tickets/` directory. Run `doctor --before-cleanup <worktree>` before removing a worktree, and `integrate` after merging into the main branch.
- Ticket state is operational handoff data, not authority: latest user instructions > current repository and external evidence > ticket records. Never store secrets, customer-private data, full logs, or hidden reasoning.
