#!/usr/bin/env python3
"""Durable Ticket ledger shared by sessions, worktrees, and branches.

Committed layer (travels with code through branches and merges):
    docs/plans/<ticket-id>/ticket.md
    docs/plans/<ticket-id>/ticket-events/<stamp>-<hex>.json   (one immutable event per file)

Local shared layer (never committed; shared by every worktree of the clone):
    <main-worktree>/.codex/tickets/sessions/<session>.json
    <main-worktree>/.codex/tickets/assignments/<ticket-id>/<assignment>.json
    <main-worktree>/.codex/tickets/checkpoints/*.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix is the supported environment.
    fcntl = None


SCHEMA_VERSION = 1
PLANS_DIR = Path("docs/plans")
EVENTS_DIRNAME = "ticket-events"
TICKET_FILENAME = "ticket.md"
KINDS = ("feature", "fix")
PHASES = (
    "Intent",
    "Spec",
    "Plan",
    "Executing",
    "Verifying",
    "Done",
    "Paused",
    "Blocked",
    "Cancelled",
)
CLOSED_PHASES = ("Done", "Cancelled")
TASK_STATUSES = ("Todo", "InProgress", "Done", "Blocked", "Dropped")
EVENT_TYPES = (
    "created",
    "phase",
    "mandate",
    "branch",
    "task",
    "followup",
    "integrated",
    "handoff",
    "decision",
)
TICKET_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
EVENT_FILE_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6,12}\.json")
SESSION_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SESSION_ENV_VARS = ("TICKETS_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
HOOK_INPUT_LIMIT_BYTES = 1_000_000
CONTEXT_LIMIT_CHARS = 4_500
TEXT_LIMIT = 2_000


class TicketError(RuntimeError):
    """Raised for actionable ticket errors."""


_LAST_SEQ = 0


def next_seq() -> int:
    """Strictly increasing nanosecond sequence to order events written in the same second."""
    global _LAST_SEQ
    _LAST_SEQ = max(time.time_ns(), _LAST_SEQ + 1)
    return _LAST_SEQ


# --------------------------------------------------------------------------- utils


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stamp_of(value: str) -> str:
    return parse_time(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def normalize_time(value: str | None) -> str:
    if not value:
        return utc_now()
    try:
        moment = parse_time(value)
    except ValueError as exc:
        raise TicketError(f"Invalid ISO 8601 timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: str | None, field_name: str, *, required: bool = False) -> str:
    text = (value or "").strip()
    if required and not text:
        raise TicketError(f"{field_name} must not be empty")
    if len(text) > TEXT_LIMIT:
        raise TicketError(f"{field_name} exceeds {TEXT_LIMIT} characters; reference a file instead")
    return text


def clean_list(values: list[str] | None, field_name: str) -> list[str]:
    return [clean_text(value, field_name, required=True) for value in values or []]


def validate_ticket_id(ticket_id: str) -> str:
    if not TICKET_ID_PATTERN.fullmatch(ticket_id or ""):
        raise TicketError(
            f"Invalid ticket id {ticket_id!r}: use 2-80 lowercase letters, digits, or '-'"
        )
    return ticket_id


def git(cwd: Path, *arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise TicketError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result


def git_out(cwd: Path, *arguments: str) -> str | None:
    result = git(cwd, *arguments)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def atomic_write_text(path: Path, text: str, *, private: bool = True) -> None:
    path.parent.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600 if private else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any, *, private: bool = True) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", private=private)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise TicketError(f"Invalid JSON at {path}: {exc}") from exc


def emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- context


@dataclass
class Context:
    root: Path
    state_dir: Path
    is_git: bool
    main_branch: str
    branch: str | None
    session: str | None = None

    @property
    def plans_dir(self) -> Path:
        return self.root / PLANS_DIR


def discover_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise TicketError(f"Project root does not exist: {root}")
        return root
    top = git_out(Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else Path.cwd().resolve()


def discover_state_dir(root: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("TICKETS_STATE_DIR"):
        return Path(os.environ["TICKETS_STATE_DIR"]).expanduser().resolve()
    common = git_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        common_path = Path(common).resolve()
        main_root = common_path.parent if common_path.name == ".git" else common_path
        return main_root / ".codex" / "tickets"
    return root / ".codex" / "tickets"


def detect_main_branch(root: Path, is_git: bool, explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("TICKETS_MAIN_BRANCH"):
        return os.environ["TICKETS_MAIN_BRANCH"]
    if is_git:
        for candidate in ("main", "master"):
            if git_out(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}") is not None:
                return candidate
        origin_head = git_out(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        if origin_head and "/" in origin_head:
            return origin_head.split("/", 1)[1]
    return "main"


def build_context_for_dir(target_dir: Path, args: argparse.Namespace) -> Context:
    top = git_out(target_dir, "rev-parse", "--show-toplevel")
    root = Path(top).resolve() if top else target_dir.resolve()
    is_git = top is not None
    branch = git_out(root, "symbolic-ref", "--quiet", "--short", "HEAD") if is_git else None
    main_branch = detect_main_branch(root, is_git, args.main_branch)
    return Context(
        root=root,
        state_dir=discover_state_dir(root, args.state_dir),
        is_git=is_git,
        main_branch=main_branch,
        branch=branch,
    )


def build_context(args: argparse.Namespace) -> Context:
    root = discover_root(args.project_root)
    is_git = git_out(root, "rev-parse", "--is-inside-work-tree") == "true"
    branch = git_out(root, "symbolic-ref", "--quiet", "--short", "HEAD") if is_git else None
    main_branch = detect_main_branch(root, is_git, args.main_branch)
    return Context(
        root=root,
        state_dir=discover_state_dir(root, args.state_dir),
        is_git=is_git,
        main_branch=main_branch,
        branch=branch,
    )


@contextmanager
def locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / ".lock").open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def session_key(ctx: Context, explicit: str | None) -> str:
    candidate = explicit
    if not candidate:
        for name in SESSION_ENV_VARS:
            if os.environ.get(name):
                candidate = os.environ[name]
                break
    if not candidate:
        digest = hashlib.sha1(str(ctx.root).encode("utf-8")).hexdigest()[:12]
        candidate = f"wt-{digest}"
    if not SESSION_KEY_PATTERN.fullmatch(candidate):
        raise TicketError(f"Invalid session id {candidate!r}")
    return candidate


# --------------------------------------------------------------------------- events


def events_dir(base: Path, ticket_id: str) -> Path:
    return base / PLANS_DIR / ticket_id / EVENTS_DIRNAME


def write_event(
    ctx: Context, ticket_id: str, event_type: str, data: dict[str, Any], at: str | None = None
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise TicketError(f"Unsupported event type {event_type!r}")
    moment = normalize_time(at)
    target_dir = events_dir(ctx.root, ticket_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    while True:
        event_id = f"{stamp_of(moment)}-{secrets.token_hex(3)}"
        path = target_dir / f"{event_id}.json"
        if not path.exists():
            break
    event = {
        "schema_version": SCHEMA_VERSION,
        "id": event_id,
        "ticket": ticket_id,
        "type": event_type,
        "at": moment,
        "seq": next_seq(),
        "branch": ctx.branch,
        "data": data,
    }
    atomic_write_json(path, event, private=False)
    event["_path"] = str(path.relative_to(ctx.root))
    return event


def parse_event(raw: str, origin: str) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
        return None
    if not isinstance(event.get("data"), dict) or not isinstance(event.get("at"), str):
        return None
    if not isinstance(event.get("ticket"), str) or not isinstance(event.get("id"), str):
        return None
    return event


@dataclass
class EventRecord:
    event: dict[str, Any]
    locations: set[str] = field(default_factory=set)
    uncommitted_in: set[str] = field(default_factory=set)


@dataclass
class Ledger:
    events: dict[str, EventRecord]
    on_main: set[str]
    worktrees: list[dict[str, Any]]
    branches: list[str]
    warnings: list[str]


def list_worktrees(ctx: Context) -> list[dict[str, Any]]:
    if not ctx.is_git:
        return [{"path": str(ctx.root), "branch": ctx.branch, "exists": True}]
    output = git_out(ctx.root, "worktree", "list", "--porcelain") or ""
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current.get("path"):
                current["exists"] = Path(current["path"]).is_dir()
                worktrees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current = {"path": line[len("worktree ") :], "branch": None}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
    return [item for item in worktrees if not item.get("bare")]


def event_file_key(relative: str) -> tuple[str, str] | None:
    parts = Path(relative).parts
    if len(parts) != 5 or tuple(parts[:2]) != PLANS_DIR.parts or parts[3] != EVENTS_DIRNAME:
        return None
    if not EVENT_FILE_PATTERN.fullmatch(parts[4]):
        return None
    return parts[2], parts[4]


def uncommitted_event_files(worktree: Path) -> set[str]:
    output = git_out(
        worktree, "status", "--porcelain", "--untracked-files=all", "--", str(PLANS_DIR)
    )
    found: set[str] = set()
    for line in (output or "").splitlines():
        relative = line[3:].strip().strip('"')
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        if event_file_key(relative):
            found.add(relative)
    return found


def collect_ledger(ctx: Context, *, include_branches: bool = True) -> Ledger:
    records: dict[str, EventRecord] = {}
    warnings: list[str] = []

    def add(event: dict[str, Any] | None, location: str, origin: str) -> EventRecord | None:
        if event is None:
            warnings.append(f"Skipped invalid event file {origin}")
            return None
        record = records.get(event["id"])
        if record is None:
            record = records[event["id"]] = EventRecord(event=event)
        record.locations.add(location)
        return record

    worktrees = list_worktrees(ctx)
    for worktree in worktrees:
        if not worktree.get("exists"):
            continue
        path = Path(worktree["path"])
        label = f"worktree:{path}"
        dirty = uncommitted_event_files(path) if ctx.is_git else set()
        plans = path / PLANS_DIR
        if not plans.is_dir():
            continue
        for event_path in sorted(plans.glob(f"*/{EVENTS_DIRNAME}/*.json")):
            relative = str(event_path.relative_to(path))
            if not event_file_key(relative):
                continue
            try:
                raw = event_path.read_text(encoding="utf-8")
            except OSError:
                continue
            record = add(parse_event(raw, relative), label, relative)
            if record and relative in dirty:
                record.uncommitted_in.add(str(path))

    branches: list[str] = []
    on_main: set[str] = set()
    if ctx.is_git and include_branches:
        refs = git_out(ctx.root, "for-each-ref", "refs/heads", "--format=%(refname:short)") or ""
        branches = [ref for ref in refs.splitlines() if ref]
        for branch in branches:
            listing = git_out(ctx.root, "ls-tree", "-r", "--name-only", branch, "--", str(PLANS_DIR))
            missing: list[str] = []
            for relative in (listing or "").splitlines():
                key = event_file_key(relative)
                if not key:
                    continue
                event_id = key[1].removesuffix(".json")
                if branch == ctx.main_branch:
                    on_main.add(event_id)
                if event_id in records:
                    records[event_id].locations.add(f"branch:{branch}")
                else:
                    missing.append(relative)
            for relative in missing:
                raw = git_out(ctx.root, "show", f"{branch}:{relative}")
                if raw is not None:
                    add(parse_event(raw, f"{branch}:{relative}"), f"branch:{branch}", relative)
    return Ledger(records, on_main, worktrees, branches, warnings)


# --------------------------------------------------------------------------- fold


def empty_ticket(ticket_id: str) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "kind": None,
        "title": ticket_id,
        "parent": None,
        "goal": "",
        "phase": None,
        "phase_reason": "",
        "phase_history": [],
        "mandate": None,
        "branches": {},
        "tasks": {},
        "followups": {},
        "decisions": [],
        "summary": "",
        "next_steps": [],
        "blockers": [],
        "created_at": None,
        "updated_at": None,
        "event_count": 0,
        "children": [],
        "timeline": [],
        "not_on_main": 0,
        "uncommitted": [],
        "seen_in": [],
        "problems": [],
    }


def describe_event(event: dict[str, Any]) -> str:
    data = event["data"]
    kind = event["type"]
    if kind == "created":
        return f"Created {data.get('kind', '')} ticket: {data.get('title', '')}"
    if kind == "phase":
        reason = f" — {data['reason']}" if data.get("reason") else ""
        return f"Phase → {data.get('phase')}{reason}"
    if kind == "mandate":
        parts = [", ".join(Path(doc.get("path", "")).name for doc in data.get("docs", []))]
        if data.get("tasks"):
            parts.append(f"tasks {', '.join(data['tasks'])}")
        return "Mandate approved: " + "; ".join(part for part in parts if part)
    if kind == "branch":
        tasks = f" for {', '.join(data['tasks'])}" if data.get("tasks") else ""
        return f"Branch {data.get('branch')} opened from {data.get('base') or '?'}{tasks}"
    if kind == "task":
        note = f" — {data['note']}" if data.get("note") else ""
        return f"{data.get('task')} → {data.get('status')}{note}"
    if kind == "followup":
        return f"{data.get('fu')} {data.get('action')}: {data.get('title') or data.get('note') or ''}"
    if kind == "integrated":
        commit = (data.get("commit") or "")[:8]
        return f"Branch {data.get('branch')} integrated into {data.get('into')} {commit}".rstrip()
    if kind == "handoff":
        return f"Handoff: {data.get('summary', '')}"
    if kind == "decision":
        return f"Decision: {data.get('text', '')}"
    return kind


def fold_ledger(ledger: Ledger) -> dict[str, dict[str, Any]]:
    tickets: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        ledger.events.values(),
        key=lambda record: (
            record.event["at"],
            record.event.get("seq") if isinstance(record.event.get("seq"), int) else 0,
            record.event["id"],
        ),
    )
    for record in ordered:
        event = record.event
        ticket = tickets.setdefault(event["ticket"], empty_ticket(event["ticket"]))
        data = event["data"]
        kind = event["type"]
        ticket["event_count"] += 1
        ticket["updated_at"] = event["at"]
        if event["id"] not in ledger.on_main:
            ticket["not_on_main"] += 1
        ticket["uncommitted"].extend(sorted(record.uncommitted_in))
        ticket["seen_in"].extend(sorted(record.locations))
        ticket["timeline"].append(
            {
                "id": event["id"],
                "at": event["at"],
                "type": kind,
                "branch": event.get("branch"),
                "text": describe_event(event),
                "on_main": event["id"] in ledger.on_main,
            }
        )
        if kind == "created":
            ticket["kind"] = data.get("kind")
            ticket["title"] = data.get("title") or ticket["title"]
            ticket["parent"] = data.get("parent") or None
            ticket["goal"] = data.get("goal") or ""
            ticket["created_at"] = event["at"]
        elif kind == "phase":
            ticket["phase"] = data.get("phase")
            ticket["phase_reason"] = data.get("reason") or ""
            ticket["phase_history"].append({"phase": data.get("phase"), "at": event["at"]})
        elif kind == "mandate":
            ticket["mandate"] = {**data, "at": event["at"]}
            for task in data.get("tasks") or []:
                ticket["tasks"].setdefault(task, {"status": "Todo", "at": event["at"], "note": ""})
        elif kind == "branch":
            entry = ticket["branches"].setdefault(data["branch"], {})
            entry.update(
                {
                    "base": data.get("base"),
                    "tasks": data.get("tasks") or [],
                    "purpose": data.get("purpose") or "",
                    "opened_at": event["at"],
                    "integrated_at": entry.get("integrated_at"),
                }
            )
            for task in data.get("tasks") or []:
                ticket["tasks"].setdefault(task, {"status": "Todo", "at": event["at"], "note": ""})
        elif kind == "task":
            ticket["tasks"][data["task"]] = {
                "status": data.get("status"),
                "at": event["at"],
                "note": data.get("note") or "",
                "commit": data.get("commit") or "",
                "branch": event.get("branch"),
            }
        elif kind == "followup":
            fu = ticket["followups"].setdefault(data["fu"], {"title": "", "status": "Open"})
            if data.get("action") == "open":
                fu.update({"title": data.get("title") or "", "status": "Open", "opened_at": event["at"]})
            else:
                fu.update({"status": "Closed", "closed_at": event["at"], "note": data.get("note") or ""})
        elif kind == "integrated":
            entry = ticket["branches"].setdefault(data["branch"], {"tasks": [], "purpose": ""})
            entry.update(
                {
                    "integrated_at": event["at"],
                    "into": data.get("into"),
                    "commit": data.get("commit"),
                }
            )
        elif kind == "handoff":
            ticket["summary"] = data.get("summary") or ticket["summary"]
            ticket["next_steps"] = data.get("next_steps") or []
            ticket["blockers"] = data.get("blockers") or []
        elif kind == "decision":
            ticket["decisions"].append({"at": event["at"], "text": data.get("text", "")})

    for ticket in tickets.values():
        ticket["uncommitted"] = sorted(set(ticket["uncommitted"]))
        ticket["seen_in"] = sorted(set(ticket["seen_in"]))
        if ticket["kind"] is None:
            ticket["problems"].append("No created event found")
        if ticket["phase"] is None:
            ticket["phase"] = "Intent"
        parent = ticket["parent"]
        if parent:
            if parent in tickets:
                tickets[parent]["children"].append(ticket["id"])
            else:
                ticket["problems"].append(f"Parent ticket {parent} not found")
        total = [task for task in ticket["tasks"].values() if task["status"] != "Dropped"]
        ticket["task_total"] = len(total)
        ticket["task_done"] = sum(1 for task in total if task["status"] == "Done")
        ticket["open_followups"] = sorted(
            fu_id for fu_id, fu in ticket["followups"].items() if fu["status"] == "Open"
        )
        ticket["active_branches"] = sorted(
            name for name, entry in ticket["branches"].items() if not entry.get("integrated_at")
        )
    return tickets


def load_tickets(ctx: Context, *, include_branches: bool = True) -> tuple[dict[str, dict[str, Any]], Ledger]:
    ledger = collect_ledger(ctx, include_branches=include_branches)
    return fold_ledger(ledger), ledger


def require_ticket(tickets: dict[str, dict[str, Any]], ticket_id: str) -> dict[str, Any]:
    validate_ticket_id(ticket_id)
    if ticket_id not in tickets:
        raise TicketError(
            f"Ticket {ticket_id} not found in any worktree or local branch; run `list --all`"
        )
    return tickets[ticket_id]


# --------------------------------------------------------------------------- local shared state


def session_path(ctx: Context, key: str) -> Path:
    return ctx.state_dir / "sessions" / f"{key}.json"


def assignment_dir(ctx: Context, ticket_id: str) -> Path:
    return ctx.state_dir / "assignments" / ticket_id


def load_binding(ctx: Context, key: str) -> dict[str, Any] | None:
    payload = read_json(session_path(ctx, key))
    return payload if isinstance(payload, dict) else None


def load_assignment(ctx: Context, ticket_id: str, assignment_id: str) -> dict[str, Any] | None:
    payload = read_json(assignment_dir(ctx, ticket_id) / f"{assignment_id}.json")
    return payload if isinstance(payload, dict) else None


def list_assignments(ctx: Context, ticket_id: str) -> list[dict[str, Any]]:
    directory = assignment_dir(ctx, ticket_id)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.glob("*.json"):
        try:
            payload = read_json(path)
        except TicketError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)


def latest_assignment(ctx: Context, ticket_id: str) -> dict[str, Any] | None:
    items = list_assignments(ctx, ticket_id)
    return items[0] if items else None


def new_assignment(ctx: Context, ticket_id: str, key: str, first_request: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": f"asg-{stamp_of(now)}-{secrets.token_hex(3)}",
        "ticket": ticket_id,
        "session": key,
        "worktree": str(ctx.root),
        "branch": ctx.branch,
        "first_request": first_request,
        "status": "Open",
        "started_at": now,
        "updated_at": now,
        "summary": "",
        "completed": [],
        "next_steps": [],
        "blockers": [],
        "files": [],
        "verification": [],
        "notes": [],
    }


def bound_assignment(ctx: Context, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = load_binding(ctx, key)
    if not binding:
        raise TicketError(
            f"Session {key} is not bound to a ticket; run `attach <ticket-id> --session {key}` first"
        )
    assignment = load_assignment(ctx, binding["ticket"], binding["assignment"])
    if not assignment:
        raise TicketError(f"Assignment {binding['assignment']} for session {key} is missing")
    return binding, assignment


# --------------------------------------------------------------------------- commands


def ticket_markdown(ticket_id: str, kind: str, title: str, parent: str | None, goal: str, now: str) -> str:
    lines = [
        "---",
        f"id: {ticket_id}",
        f"kind: {kind}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"parent: {parent}" if parent else "parent:",
        f"created: {now}",
        "---",
        "",
        f"# {title}",
        "",
        "## Goal",
        "",
        goal or "(to be refined in intent.md)",
        "",
        "## How to read this ticket",
        "",
        "Live status is derived from the immutable files in `ticket-events/`.",
        f"Use the `manage-tickets` skill (`show {ticket_id}`) or open the board. "
        "Do not edit or delete event files.",
        "",
    ]
    return "\n".join(lines)


READINESS_BY_PHASE = {
    "Intent": ("intent-readiness", "intent.md"),
    "Spec": ("spec-readiness", "spec.md"),
    "Plan": ("plan-readiness", "plan.md"),
}


def phase_guidance(ticket_id: str, kind: str | None, phase: str | None) -> str | None:
    if kind != "feature" or phase not in READINESS_BY_PHASE:
        return None
    skill, doc = READINESS_BY_PHASE[phase]
    return (
        f"Phase gate (feature · {phase}): run `{skill}` to produce "
        f"`docs/plans/{ticket_id}/{doc}` and obtain human approval before coding."
    )


def cmd_new(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    ticket_id = validate_ticket_id(args.ticket_id)
    title = clean_text(args.title, "title", required=True)
    goal = clean_text(args.goal, "goal")
    tickets, _ = load_tickets(ctx)
    if ticket_id in tickets:
        raise TicketError(f"Ticket {ticket_id} already exists ({tickets[ticket_id]['phase']})")
    if args.parent:
        require_ticket(tickets, args.parent)
    phase = args.phase or ("Intent" if args.kind == "feature" else "Executing")
    at = normalize_time(args.at)
    ticket_file = ctx.plans_dir / ticket_id / TICKET_FILENAME
    if not ticket_file.exists():
        atomic_write_text(
            ticket_file,
            ticket_markdown(ticket_id, args.kind, title, args.parent, goal, at),
            private=False,
        )
    created = write_event(
        ctx,
        ticket_id,
        "created",
        {"kind": args.kind, "title": title, "parent": args.parent, "goal": goal},
        at,
    )
    phase_event = write_event(ctx, ticket_id, "phase", {"phase": phase, "reason": "created"}, at)
    result: dict[str, Any] = {
        "ticket": ticket_id,
        "files": [str(ticket_file.relative_to(ctx.root)), created["_path"], phase_event["_path"]],
    }
    guidance = phase_guidance(ticket_id, args.kind, phase)
    if guidance:
        result["next_skill"] = READINESS_BY_PHASE[phase][0]
        result["guidance"] = guidance
    if args.attach:
        result["binding"] = attach(ctx, ticket_id, args.session, args.first_request or "", "new")
    return result


def cmd_phase(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    if args.phase == "Done":
        open_tasks = [
            task_id
            for task_id, task in ticket["tasks"].items()
            if task["status"] not in ("Done", "Dropped")
        ]
        if (open_tasks or ticket["open_followups"]) and not args.force:
            raise TicketError(
                "Cannot mark Done with open tasks "
                f"{open_tasks} or follow-ups {ticket['open_followups']}; finish them or pass --force"
            )
    event = write_event(
        ctx,
        ticket["id"],
        "phase",
        {"phase": args.phase, "reason": clean_text(args.reason, "reason")},
        args.at,
    )
    return {"ticket": ticket["id"], "phase": args.phase, "file": event["_path"]}


def doc_fingerprint(ctx: Context, raw_path: str) -> dict[str, Any]:
    path = (ctx.root / raw_path).resolve()
    try:
        relative = path.relative_to(ctx.root)
    except ValueError as exc:
        raise TicketError(f"Mandate document must live inside the project: {raw_path}") from exc
    if not path.is_file():
        raise TicketError(f"Mandate document not found: {raw_path}")
    content = path.read_bytes()
    status = ""
    for line in content.decode("utf-8", errors="replace").splitlines()[:40]:
        match = re.match(r"^\s*(?:\*\*)?(?:Status|状态)(?:\*\*)?\s*[:：]\s*(.+)$", line)
        if match:
            status = match.group(1).strip().strip("*").strip()
            break
    return {"path": str(relative), "sha256": hashlib.sha256(content).hexdigest(), "status": status}


def cmd_mandate(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    docs = [doc_fingerprint(ctx, path) for path in args.doc or []]
    if not docs and not args.task:
        raise TicketError("Provide at least one --doc or --task")
    base = args.base or (git_out(ctx.root, "rev-parse", "--short", "HEAD") if ctx.is_git else None)
    event = write_event(
        ctx,
        ticket["id"],
        "mandate",
        {
            "docs": docs,
            "tasks": clean_list(args.task, "task"),
            "base": base,
            "note": clean_text(args.note, "note"),
        },
        args.at,
    )
    return {"ticket": ticket["id"], "docs": docs, "file": event["_path"]}


def cmd_branch(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    branch = args.branch or ctx.branch
    if not branch:
        raise TicketError("Cannot detect the current branch; pass --branch")
    base = args.base
    if not base and ctx.is_git:
        base = git_out(ctx.root, "merge-base", branch, ctx.main_branch)
        base = f"{ctx.main_branch}@{base[:8]}" if base else None
    event = write_event(
        ctx,
        ticket["id"],
        "branch",
        {
            "branch": branch,
            "base": base,
            "tasks": clean_list(args.task, "task"),
            "purpose": clean_text(args.purpose, "purpose"),
        },
        args.at,
    )
    return {"ticket": ticket["id"], "branch": branch, "file": event["_path"]}


def cmd_task(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    event = write_event(
        ctx,
        ticket["id"],
        "task",
        {
            "task": clean_text(args.task_id, "task", required=True),
            "status": args.status,
            "commit": clean_text(args.commit, "commit"),
            "note": clean_text(args.note, "note"),
        },
        args.at,
    )
    return {"ticket": ticket["id"], "task": args.task_id, "status": args.status, "file": event["_path"]}


def cmd_followup(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    if args.open:
        numbers = [int(fu[3:]) for fu in ticket["followups"] if re.fullmatch(r"FU-\d+", fu)]
        fu_id = f"FU-{(max(numbers) + 1) if numbers else 1:03d}"
        data = {"fu": fu_id, "action": "open", "title": clean_text(args.open, "title", required=True)}
    else:
        fu_id = args.close
        if fu_id not in ticket["followups"]:
            raise TicketError(f"Follow-up {fu_id} not found on {ticket['id']}")
        data = {"fu": fu_id, "action": "close", "note": clean_text(args.note, "note")}
    event = write_event(ctx, ticket["id"], "followup", data, args.at)
    return {"ticket": ticket["id"], "followup": fu_id, "file": event["_path"]}


def cmd_integrate(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    into = args.into or ctx.main_branch
    warnings: list[str] = []
    commit = args.commit
    if ctx.is_git:
        commit = commit or git_out(ctx.root, "rev-parse", into)
        if git_out(ctx.root, "rev-parse", "--verify", "--quiet", args.branch) is not None:
            merged = git(ctx.root, "merge-base", "--is-ancestor", args.branch, into).returncode == 0
            if not merged and not args.force:
                raise TicketError(
                    f"Branch {args.branch} is not an ancestor of {into}; merge first or pass --force "
                    "(e.g. after a squash merge)"
                )
            if not merged:
                warnings.append("Recorded integration without ancestry proof (--force)")
    event = write_event(
        ctx,
        ticket["id"],
        "integrated",
        {"branch": args.branch, "into": into, "commit": commit},
        args.at,
    )
    return {"ticket": ticket["id"], "file": event["_path"], "warnings": warnings}


def cmd_decision(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    event = write_event(
        ctx, ticket["id"], "decision", {"text": clean_text(args.text, "text", required=True)}, args.at
    )
    return {"ticket": ticket["id"], "file": event["_path"]}


def attach(
    ctx: Context, ticket_id: str, explicit_session: str | None, first_request: str, decision: str
) -> dict[str, Any]:
    key = session_key(ctx, explicit_session)
    with locked(ctx.state_dir):
        binding = load_binding(ctx, key)
        assignment = None
        if binding and binding.get("ticket") == ticket_id:
            assignment = load_assignment(ctx, ticket_id, binding.get("assignment", ""))
        if assignment is None:
            assignment = new_assignment(ctx, ticket_id, key, clean_text(first_request, "first_request"))
        else:
            assignment["status"] = "Open"
            assignment["worktree"] = str(ctx.root)
            assignment["branch"] = ctx.branch
            assignment["updated_at"] = utc_now()
        atomic_write_json(assignment_dir(ctx, ticket_id) / f"{assignment['id']}.json", assignment)
        binding = {
            "schema_version": SCHEMA_VERSION,
            "session": key,
            "ticket": ticket_id,
            "assignment": assignment["id"],
            "decision": decision,
            "worktree": str(ctx.root),
            "branch": ctx.branch,
            "bound_at": utc_now(),
        }
        atomic_write_json(session_path(ctx, key), binding)
    return binding


def cmd_attach(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    binding = attach(ctx, ticket["id"], args.session, args.first_request or "", "attach")
    previous = [
        item for item in list_assignments(ctx, ticket["id"]) if item["id"] != binding["assignment"]
    ]
    result: dict[str, Any] = {
        "binding": binding,
        "ticket": ticket_digest(ticket),
        "previous_assignment": previous[0] if previous else None,
    }
    guidance = phase_guidance(ticket["id"], ticket.get("kind"), ticket.get("phase"))
    if guidance:
        result["next_skill"] = READINESS_BY_PHASE[ticket["phase"]][0]
        result["guidance"] = guidance
    return result


def cmd_detach(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    key = session_key(ctx, args.session)
    with locked(ctx.state_dir):
        binding = load_binding(ctx, key)
        if not binding:
            return {"session": key, "detached": False}
        assignment = load_assignment(ctx, binding["ticket"], binding["assignment"])
        if assignment:
            assignment["status"] = "Closed"
            assignment["updated_at"] = utc_now()
            atomic_write_json(
                assignment_dir(ctx, binding["ticket"]) / f"{assignment['id']}.json", assignment
            )
        session_path(ctx, key).unlink(missing_ok=True)
    return {"session": key, "detached": True, "ticket": binding["ticket"]}


def cmd_handoff(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    key = session_key(ctx, args.session)
    with locked(ctx.state_dir):
        binding, assignment = bound_assignment(ctx, key)
        if args.summary is not None:
            assignment["summary"] = clean_text(args.summary, "summary")
        for field_name, values in (
            ("completed", args.add_completed),
            ("blockers", args.add_blocker),
            ("files", args.add_file),
            ("verification", args.add_verification),
            ("notes", args.add_note),
        ):
            for value in clean_list(values, field_name):
                if value not in assignment[field_name]:
                    assignment[field_name].append(value)
        if args.clear_blockers:
            assignment["blockers"] = []
        if args.set_next_step is not None:
            assignment["next_steps"] = clean_list(args.set_next_step, "next_step")
        if args.clear_next_steps:
            assignment["next_steps"] = []
        assignment["branch"] = ctx.branch
        assignment["worktree"] = str(ctx.root)
        assignment["updated_at"] = utc_now()
        atomic_write_json(assignment_dir(ctx, binding["ticket"]) / f"{assignment['id']}.json", assignment)
    result: dict[str, Any] = {"assignment": assignment}
    if args.milestone:
        if not assignment["summary"]:
            raise TicketError("A milestone handoff requires --summary")
        event = write_event(
            ctx,
            binding["ticket"],
            "handoff",
            {
                "summary": assignment["summary"],
                "next_steps": assignment["next_steps"],
                "blockers": assignment["blockers"],
            },
        )
        result["file"] = event["_path"]
    return result


def ticket_digest(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ticket[key]
        for key in (
            "id",
            "kind",
            "title",
            "parent",
            "phase",
            "task_done",
            "task_total",
            "open_followups",
            "active_branches",
            "summary",
            "next_steps",
            "blockers",
            "updated_at",
            "not_on_main",
            "uncommitted",
        )
    }


def branch_matches(ctx: Context, tickets: dict[str, dict[str, Any]]) -> list[str]:
    if not ctx.branch:
        return []
    return sorted(
        ticket_id
        for ticket_id, ticket in tickets.items()
        if ctx.branch in ticket["active_branches"] and ticket["phase"] not in CLOSED_PHASES
    )


def sort_open(tickets: dict[str, dict[str, Any]], include_closed: bool) -> list[dict[str, Any]]:
    items = [
        ticket
        for ticket in tickets.values()
        if include_closed or ticket["phase"] not in CLOSED_PHASES
    ]
    return sorted(items, key=lambda ticket: ticket.get("updated_at") or "", reverse=True)


def cmd_list(ctx: Context, args: argparse.Namespace) -> Any:
    tickets, ledger = load_tickets(ctx)
    items = sort_open(tickets, args.all)
    if args.phase:
        items = [ticket for ticket in items if ticket["phase"] == args.phase]
    if args.as_json:
        return {
            "tickets": [ticket_digest(ticket) for ticket in items],
            "branch_matches": branch_matches(ctx, tickets),
            "warnings": ledger.warnings,
        }
    if not items:
        print("No tickets found.")
        return None
    for ticket in items:
        progress = f"{ticket['task_done']}/{ticket['task_total']}" if ticket["task_total"] else "-"
        flags = []
        if ticket["active_branches"]:
            flags.append("branch=" + ",".join(ticket["active_branches"]))
        if ticket["open_followups"]:
            flags.append(f"fu={len(ticket['open_followups'])}")
        if ticket["not_on_main"]:
            flags.append("not-on-main")
        print(
            f"{(ticket['updated_at'] or '')[:10]}  {ticket['phase']:<10} {ticket['kind'] or '?':<7} "
            f"{progress:>5}  {ticket['id']}  {ticket['title']}  {' '.join(flags)}".rstrip()
        )
    return None


def cmd_show(ctx: Context, args: argparse.Namespace) -> Any:
    tickets, _ = load_tickets(ctx)
    ticket = require_ticket(tickets, args.ticket_id)
    assignments = list_assignments(ctx, ticket["id"])
    payload = {**ticket, "assignments": assignments[:5]}
    if args.as_json:
        return payload
    sys.stdout.write(markdown_ticket(ticket, assignments[0] if assignments else None))
    return None


def cmd_current(ctx: Context, args: argparse.Namespace) -> Any:
    key = session_key(ctx, args.session)
    binding = load_binding(ctx, key)
    tickets, _ = load_tickets(ctx)
    result: dict[str, Any] = {
        "session": key,
        "worktree": str(ctx.root),
        "branch": ctx.branch,
        "binding": binding,
        "branch_matches": branch_matches(ctx, tickets),
    }
    if binding and binding["ticket"] in tickets:
        result["ticket"] = ticket_digest(tickets[binding["ticket"]])
        result["assignment"] = load_assignment(ctx, binding["ticket"], binding["assignment"])
    return result


def markdown_ticket(ticket: dict[str, Any], assignment: dict[str, Any] | None) -> str:
    lines = [
        f"# {ticket['title']} (`{ticket['id']}`)",
        "",
        f"- Kind: `{ticket['kind']}`  Phase: `{ticket['phase']}`"
        + (f"  Parent: `{ticket['parent']}`" if ticket["parent"] else ""),
        f"- Tasks: {ticket['task_done']}/{ticket['task_total']} done",
        f"- Active branches: {', '.join(ticket['active_branches']) or '(none)'}",
        f"- Updated: {ticket['updated_at']}",
    ]
    if ticket["not_on_main"]:
        lines.append(f"- ⚠ {ticket['not_on_main']} event(s) not yet on main")
    if ticket["uncommitted"]:
        lines.append(f"- ⚠ Uncommitted ticket events in: {', '.join(ticket['uncommitted'])}")
    if ticket["goal"]:
        lines += ["", "## Goal", ticket["goal"]]
    if ticket["mandate"]:
        mandate = ticket["mandate"]
        lines += ["", f"## Mandate (base {mandate.get('base') or '?'})"]
        for doc in mandate.get("docs", []):
            lines.append(f"- `{doc['path']}` sha256:{doc['sha256'][:12]} {doc.get('status', '')}".rstrip())
        if mandate.get("tasks"):
            lines.append(f"- Authorized tasks: {', '.join(mandate['tasks'])}")
    if ticket["tasks"]:
        lines += ["", "## Tasks"]
        for task_id, task in sorted(ticket["tasks"].items()):
            lines.append(f"- {task_id}: {task['status']}" + (f" — {task['note']}" if task["note"] else ""))
    if ticket["followups"]:
        lines += ["", "## Follow-ups"]
        for fu_id, fu in sorted(ticket["followups"].items()):
            lines.append(f"- {fu_id} [{fu['status']}] {fu['title']}")
    if ticket["summary"] or ticket["next_steps"] or ticket["blockers"]:
        lines += ["", "## Latest milestone handoff", ticket["summary"] or "(none)"]
        lines += [f"- Next: {step}" for step in ticket["next_steps"]]
        lines += [f"- Blocker: {blocker}" for blocker in ticket["blockers"]]
    if assignment:
        lines += [
            "",
            f"## Latest assignment `{assignment['id']}` ({assignment['status']}, {assignment['updated_at']})",
            f"- Branch: {assignment.get('branch')}  Worktree: {assignment.get('worktree')}",
            assignment.get("summary") or "(no summary)",
        ]
        lines += [f"- Next: {step}" for step in assignment.get("next_steps", [])]
        lines += [f"- Blocker: {blocker}" for blocker in assignment.get("blockers", [])]
        lines += [f"- Done: {item}" for item in assignment.get("completed", [])[-8:]]
    if ticket["children"]:
        lines += ["", f"## Child tickets: {', '.join(sorted(ticket['children']))}"]
    lines += ["", "## Recent timeline"]
    lines += [f"- {item['at']} {item['text']}" for item in ticket["timeline"][-12:]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- doctor


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    tickets, ledger = load_tickets(ctx)
    errors: list[str] = []
    warnings: list[str] = list(ledger.warnings)
    branches = set(ledger.branches)

    target_path: Path | None = None
    target_branch: str | None = None
    if args.before_cleanup:
        target_path = Path(args.before_cleanup).expanduser().resolve()
        match = [item for item in ledger.worktrees if Path(item["path"]).resolve() == target_path]
        if not match:
            errors.append(f"{target_path} is not a registered worktree")
        else:
            target_branch = match[0].get("branch")
            dirty = uncommitted_event_files(target_path)
            if dirty:
                errors.append(
                    f"Uncommitted ticket events in {target_path}: {sorted(dirty)}; commit them first"
                )
            if target_branch and ctx.is_git:
                merged = git(ctx.root, "merge-base", "--is-ancestor", target_branch, ctx.main_branch)
                unmerged_events = [
                    record.event["id"]
                    for record in ledger.events.values()
                    if f"branch:{target_branch}" in record.locations
                    and record.event["id"] not in ledger.on_main
                ]
                if unmerged_events:
                    message = (
                        f"Branch {target_branch} holds {len(unmerged_events)} ticket event(s) not on "
                        f"{ctx.main_branch}; removing the worktree is safe only if the branch is kept"
                    )
                    (warnings if merged.returncode != 0 else errors).append(message)
            open_sessions = []
            sessions_dir = ctx.state_dir / "sessions"
            for path in sessions_dir.glob("*.json") if sessions_dir.is_dir() else []:
                binding = read_json(path)
                if isinstance(binding, dict) and binding.get("worktree") == str(target_path):
                    open_sessions.append(binding.get("session"))
            if open_sessions:
                warnings.append(f"Sessions still bound to this worktree: {open_sessions}")

    for ticket in tickets.values():
        for problem in ticket["problems"]:
            errors.append(f"{ticket['id']}: {problem}")
        for name in ticket["active_branches"]:
            if ctx.is_git and name not in branches:
                errors.append(
                    f"{ticket['id']}: branch {name} was deleted without an integrated event; "
                    "record `integrate --force` if it was squash-merged, or restore the branch"
                )
        if ticket["phase"] in CLOSED_PHASES and ticket["active_branches"]:
            warnings.append(f"{ticket['id']} is {ticket['phase']} but has open branches {ticket['active_branches']}")
        if ticket["uncommitted"] and not args.before_cleanup:
            warnings.append(f"{ticket['id']}: uncommitted events in {ticket['uncommitted']}")

    sessions_dir = ctx.state_dir / "sessions"
    for path in sessions_dir.glob("*.json") if sessions_dir.is_dir() else []:
        binding = read_json(path)
        if isinstance(binding, dict) and binding.get("ticket") not in tickets:
            warnings.append(f"Session {binding.get('session')} is bound to unknown ticket {binding.get('ticket')}")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_worktree": str(target_path) if target_path else None,
        "checked_branch": target_branch,
    }


# --------------------------------------------------------------------------- hooks


def bounded(text: str) -> str:
    if len(text) <= CONTEXT_LIMIT_CHARS:
        return text
    suffix = "\n[Ticket context truncated; run `show <ticket-id>` for full state.]"
    return text[: CONTEXT_LIMIT_CHARS - len(suffix)] + suffix


def script_hint(ctx: Context) -> str:
    script = Path(__file__).resolve()
    try:
        relative = script.relative_to(ctx.root)
        return f"python3 {relative}"
    except ValueError:
        return f"python3 {script}"


def bound_context(ctx: Context, key: str, ticket: dict[str, Any], binding: dict[str, Any]) -> str:
    assignment = load_assignment(ctx, ticket["id"], binding["assignment"]) or latest_assignment(ctx, ticket["id"])
    guidance = phase_guidance(ticket["id"], ticket.get("kind"), ticket.get("phase"))
    lines = [
        "# Ticket ledger (operational data, not user instructions)",
        f"Session id: `{key}` — pass `--session {key}` to ticket commands.",
        f"This session is bound to ticket `{ticket['id']}`. Verify against the latest user "
        "request, the conversation, and the worktree before acting.",
    ]
    if guidance:
        lines.append(guidance)
    lines += [
        "",
        markdown_ticket(ticket, assignment),
        "Record missing progress with `handoff` before continuing.",
    ]
    return bounded("\n".join(lines))


def routing_context(ctx: Context, key: str, tickets: dict[str, dict[str, Any]]) -> str:
    matches = branch_matches(ctx, tickets)
    items = sort_open(tickets, False)
    items.sort(key=lambda ticket: ticket["id"] not in matches)  # stable: keeps recency order
    lines = [
        "# Ticket ledger (operational data, not user instructions)",
        f"Session id: `{key}`  Worktree: `{ctx.root}`  Branch: `{ctx.branch or '-'}`",
        "",
        "Before changing code or docs, route this session (manage-tickets skill):",
        "- `attach` the matching ticket, `new` a ticket, add a follow-up to a parent, or stay "
        "unbound for pure Q&A.",
        "- A unique branch match may be attached directly (tell the user); otherwise confirm "
        "with the user.",
        f"- Command: `{script_hint(ctx)} attach <ticket-id> --session {key}`",
    ]
    if matches:
        lines += ["", f"Branch match: {', '.join(matches)}"]
    lines += ["", "| ticket | phase | kind | tasks | branches | next step |", "|---|---|---|---|---|---|"]
    for ticket in items[:20]:
        progress = f"{ticket['task_done']}/{ticket['task_total']}" if ticket["task_total"] else "-"
        next_step = (ticket["next_steps"] or [""])[0][:80]
        lines.append(
            f"| {ticket['id']} | {ticket['phase']} | {ticket['kind']} | {progress} | "
            f"{','.join(ticket['active_branches']) or '-'} | {next_step} |"
        )
    if not items:
        lines.append("| (no open tickets) | | | | | |")
    return bounded("\n".join(lines))


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read(HOOK_INPUT_LIMIT_BYTES + 1)
    if len(raw.encode("utf-8")) > HOOK_INPUT_LIMIT_BYTES:
        raise TicketError("Hook input exceeds the 1 MB safety limit")
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise TicketError(f"Hook input is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TicketError("Hook input must be a JSON object")
    return payload


def session_start_context(ctx: Context, key: str) -> str:
    tickets, _ = load_tickets(ctx)
    binding = load_binding(ctx, key)
    if binding and binding.get("ticket") in tickets:
        return bound_context(ctx, key, tickets[binding["ticket"]], binding)
    return routing_context(ctx, key, tickets)


def resolve_hook_context(ctx: Context, args: argparse.Namespace, event: dict[str, Any]) -> Context:
    if args.project_root:
        return ctx
    candidates: list[str] = []
    if isinstance(event.get("cwd"), str) and event["cwd"].strip():
        candidates.append(event["cwd"].strip())
    workspace_paths = event.get("workspacePaths")
    if isinstance(workspace_paths, list):
        candidates.extend(item.strip() for item in workspace_paths if isinstance(item, str) and item.strip())
    for raw in candidates:
        path = Path(raw).expanduser()
        if path.is_dir():
            return build_context_for_dir(path, args)
    return ctx


def cmd_hook(ctx: Context, args: argparse.Namespace, runtime: str = "auto") -> dict[str, Any]:
    event = read_hook_input()
    is_agy = runtime == "antigravity" or (
        runtime == "auto" and ("hookEventName" in event or "conversationId" in event)
    )
    ctx = resolve_hook_context(ctx, args, event)
    if not ctx.is_git and not ctx.plans_dir.is_dir():
        return {} if is_agy else {"continue": True}

    if is_agy:
        name = event.get("hookEventName")
        raw_session = event.get("conversationId")
        key = session_key(ctx, raw_session if isinstance(raw_session, str) and raw_session else None)
        if name == "SessionStart":
            context = session_start_context(ctx, key)
            return {"injectSteps": [{"systemMessage": {"systemMessage": context}}]}
        return {}

    name = event.get("hook_event_name")
    raw_session = event.get("session_id")
    key = session_key(ctx, raw_session if isinstance(raw_session, str) and raw_session else None)
    if name == "PreCompact":
        binding = load_binding(ctx, key)
        if not binding:
            return {"continue": True}
        with locked(ctx.state_dir):
            assignment = load_assignment(ctx, binding["ticket"], binding["assignment"])
            now = utc_now()
            atomic_write_json(
                ctx.state_dir / "checkpoints" / f"{stamp_of(now)}-{key}-{secrets.token_hex(3)}.json",
                {
                    "checkpoint_version": 1,
                    "captured_at": now,
                    "trigger": event.get("trigger"),
                    "binding": binding,
                    "assignment": assignment,
                },
            )
        return {"continue": True}
    if name == "SessionStart":
        context = session_start_context(ctx, key)
        return {
            "continue": True,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
        }
    return {"continue": True}


# --------------------------------------------------------------------------- board


def board_payload(ctx: Context) -> dict[str, Any]:
    tickets, ledger = load_tickets(ctx)
    items = []
    for ticket in tickets.values():
        latest = latest_assignment(ctx, ticket["id"])
        items.append(
            {
                **ticket,
                "timeline": ticket["timeline"][-60:],
                "seen_in": ticket["seen_in"][:12],
                "latest_assignment": (
                    {
                        key: latest.get(key)
                        for key in (
                            "id",
                            "status",
                            "branch",
                            "worktree",
                            "summary",
                            "next_steps",
                            "blockers",
                            "completed",
                            "updated_at",
                        )
                    }
                    if latest
                    else None
                ),
            }
        )
    repo_name = Path(git_out(ctx.root, "rev-parse", "--show-toplevel") or ctx.root).name
    if ctx.is_git:
        common = git_out(ctx.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            repo_name = Path(common).resolve().parent.name
    return {
        "generated_at": utc_now(),
        "repository": repo_name,
        "main_branch": ctx.main_branch,
        "worktrees": [
            {"path": item["path"], "branch": item.get("branch"), "exists": item.get("exists")}
            for item in ledger.worktrees
        ],
        "branches": ledger.branches,
        "warnings": ledger.warnings,
        "tickets": sorted(items, key=lambda item: item.get("updated_at") or "", reverse=True),
    }


def render_board(payload: dict[str, Any]) -> str:
    template_path = Path(__file__).resolve().parent.parent / "assets" / "board.html"
    template = template_path.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("/*__BOARD_DATA__*/null", data)


def cmd_board(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    payload = board_payload(ctx)
    output = Path(args.output).expanduser().resolve() if args.output else ctx.state_dir / "board" / "index.html"
    atomic_write_text(output, render_board(payload), private=False)
    if args.json_output:
        atomic_write_json(Path(args.json_output).expanduser().resolve(), payload, private=False)
    return {"output": str(output), "tickets": len(payload["tickets"])}


def cmd_serve(ctx: Context, args: argparse.Namespace) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.path.split("?")[0] not in ("/", "/index.html", "/data.json"):
                self.send_error(404)
                return
            try:
                payload = {**board_payload(ctx), "live": True}
                if self.path.startswith("/data.json"):
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                else:
                    body = render_board(payload).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
            except Exception as exc:  # pragma: no cover - surfaced to the browser
                self.send_error(500, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *items: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Ticket board: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- CLI


def add_at(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--at", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Ticket ledger across sessions and worktrees.")
    parser.add_argument("--project-root", help="Worktree root; defaults to the current Git root.")
    parser.add_argument("--state-dir", help="Override the shared local state directory.")
    parser.add_argument("--main-branch", help="Integration branch (default: main).")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Create a ticket (ticket.md + created/phase events).")
    new.add_argument("ticket_id")
    new.add_argument("--title", required=True)
    new.add_argument("--kind", choices=KINDS, default="feature")
    new.add_argument("--parent")
    new.add_argument("--goal")
    new.add_argument("--phase", choices=PHASES)
    new.add_argument("--attach", action="store_true", help="Bind the current session to it.")
    new.add_argument("--session")
    new.add_argument("--first-request")
    add_at(new)

    listing = sub.add_parser("list", help="List tickets (open by default).")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--phase", choices=PHASES)
    listing.add_argument("--json", action="store_true", dest="as_json")

    show = sub.add_parser("show", help="Show a ticket with its latest assignment.")
    show.add_argument("ticket_id")
    show.add_argument("--json", action="store_true", dest="as_json")

    current = sub.add_parser("current", help="Show this session's binding and branch matches.")
    current.add_argument("--session")

    attach_parser = sub.add_parser("attach", help="Bind this session to a ticket.")
    attach_parser.add_argument("ticket_id")
    attach_parser.add_argument("--session")
    attach_parser.add_argument("--first-request")

    detach = sub.add_parser("detach", help="Close this session's assignment and unbind it.")
    detach.add_argument("--session")

    handoff = sub.add_parser("handoff", help="Update this session's assignment handoff.")
    handoff.add_argument("--session")
    handoff.add_argument("--summary")
    handoff.add_argument("--add-completed", action="append")
    handoff.add_argument("--set-next-step", action="append")
    handoff.add_argument("--clear-next-steps", action="store_true")
    handoff.add_argument("--add-blocker", action="append")
    handoff.add_argument("--clear-blockers", action="store_true")
    handoff.add_argument("--add-file", action="append")
    handoff.add_argument("--add-verification", action="append")
    handoff.add_argument("--add-note", action="append")
    handoff.add_argument(
        "--milestone", action="store_true", help="Also commit summary/next steps as a handoff event."
    )

    phase = sub.add_parser("phase", help="Record a phase transition.")
    phase.add_argument("ticket_id")
    phase.add_argument("phase", choices=PHASES)
    phase.add_argument("--reason")
    phase.add_argument("--force", action="store_true")
    add_at(phase)

    mandate = sub.add_parser("mandate", help="Record approved docs (sha256) and authorized tasks.")
    mandate.add_argument("ticket_id")
    mandate.add_argument("--doc", action="append")
    mandate.add_argument("--task", action="append")
    mandate.add_argument("--base")
    mandate.add_argument("--note")
    add_at(mandate)

    branch = sub.add_parser("branch", help="Record an implementation branch/worktree lane.")
    branch.add_argument("ticket_id")
    branch.add_argument("--branch")
    branch.add_argument("--base")
    branch.add_argument("--task", action="append")
    branch.add_argument("--purpose")
    add_at(branch)

    task = sub.add_parser("task", help="Record a plan task status.")
    task.add_argument("ticket_id")
    task.add_argument("task_id")
    task.add_argument("--status", choices=TASK_STATUSES, required=True)
    task.add_argument("--commit")
    task.add_argument("--note")
    add_at(task)

    followup = sub.add_parser("followup", help="Open or close a follow-up on a ticket.")
    followup.add_argument("ticket_id")
    group = followup.add_mutually_exclusive_group(required=True)
    group.add_argument("--open", metavar="TITLE")
    group.add_argument("--close", metavar="FU-ID")
    followup.add_argument("--note")
    add_at(followup)

    integrate = sub.add_parser("integrate", help="Record that a branch was merged.")
    integrate.add_argument("ticket_id")
    integrate.add_argument("--branch", required=True)
    integrate.add_argument("--into")
    integrate.add_argument("--commit")
    integrate.add_argument("--force", action="store_true")
    add_at(integrate)

    decision = sub.add_parser("decision", help="Record a durable decision.")
    decision.add_argument("ticket_id")
    decision.add_argument("text")
    add_at(decision)

    doctor = sub.add_parser("doctor", help="Check ledger integrity and worktree cleanup safety.")
    doctor.add_argument("--before-cleanup", metavar="WORKTREE")

    board = sub.add_parser("board", help="Render the self-contained kanban HTML.")
    board.add_argument("--output")
    board.add_argument("--json-output")

    serve = sub.add_parser("serve", help="Serve a live board on 127.0.0.1.")
    serve.add_argument("--port", type=int, default=8765)

    sub.add_parser("path", help="Print the shared local state directory.")
    hook = sub.add_parser("hook", help="Handle a Codex or Antigravity lifecycle Hook event from stdin.")
    hook.add_argument("--runtime", choices=("auto", "codex", "antigravity"), default="auto")
    return parser


COMMANDS = {
    "new": cmd_new,
    "phase": cmd_phase,
    "mandate": cmd_mandate,
    "branch": cmd_branch,
    "task": cmd_task,
    "followup": cmd_followup,
    "integrate": cmd_integrate,
    "decision": cmd_decision,
    "attach": cmd_attach,
    "detach": cmd_detach,
    "handoff": cmd_handoff,
    "current": cmd_current,
    "board": cmd_board,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = build_context(args)
        if args.command == "hook":
            try:
                emit_json(cmd_hook(ctx, args, runtime=args.runtime))
            except Exception as exc:  # Hooks must never break the session.
                print(f"warning: ticket hook failed: {exc}", file=sys.stderr)
                emit_json({} if args.runtime == "antigravity" else {"continue": True})
            return 0
        if args.command == "path":
            print(ctx.state_dir)
        elif args.command == "list":
            result = cmd_list(ctx, args)
            if result is not None:
                emit_json(result)
        elif args.command == "show":
            result = cmd_show(ctx, args)
            if result is not None:
                emit_json(result)
        elif args.command == "doctor":
            result = cmd_doctor(ctx, args)
            emit_json(result)
            return 0 if result["ok"] else 1
        elif args.command == "serve":
            cmd_serve(ctx, args)
        else:
            emit_json(COMMANDS[args.command](ctx, args))
    except TicketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: filesystem operation failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
