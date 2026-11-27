from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/manage-tickets/scripts/tickets.py"
PLUGIN_MANIFEST = ROOT / "plugin.json"
PLUGIN_HOOKS = ROOT / "hooks.json"
CODEX_TEMPLATE = ROOT / "codex/hooks.json.template"
INSTALL_SCRIPT = ROOT / "scripts/install.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def env() -> dict[str, str]:
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in ("TICKETS_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "TICKETS_STATE_DIR")
    }
    return {**base, **GIT_ENV}


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=env(), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def run(cwd: Path, *arguments: str, event: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(cwd), *arguments],
        input=json.dumps(event) if event is not None else None,
        cwd=cwd,
        env=env(),
        text=True,
        capture_output=True,
        check=False,
    )


def ok(cwd: Path, *arguments: str, event: dict | None = None):
    result = run(cwd, *arguments, event=event)
    assert result.returncode == 0, result.stderr + result.stdout
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def commit_all(cwd: Path, message: str) -> None:
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "repo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text(".codex/*\n", encoding="utf-8")
    (main / "README.md").write_text("demo\n", encoding="utf-8")
    commit_all(main, "init")
    return main


def add_worktree(repo: Path, name: str) -> Path:
    path = repo.parent / f"wt-{name}"
    git(repo, "worktree", "add", "-q", str(path), "-b", name)
    return path


def tickets(cwd: Path, *extra: str) -> dict[str, dict]:
    payload = ok(cwd, "list", "--all", "--json", *extra)
    return {item["id"]: item for item in payload["tickets"]}


def test_new_ticket_writes_committed_files_and_events(repo: Path) -> None:
    result = ok(repo, "new", "follow-up-v2", "--title", "Follow up v2", "--goal", "Better nudges")
    ticket_dir = repo / "docs/plans/follow-up-v2"
    assert (ticket_dir / "ticket.md").read_text(encoding="utf-8").startswith("---\nid: follow-up-v2")
    events = sorted(
        (json.loads(path.read_text()) for path in (ticket_dir / "ticket-events").glob("*.json")),
        key=lambda event: (event["at"], event["seq"]),
    )
    assert [event["type"] for event in events] == ["created", "phase"]
    assert len(result["files"]) == 3
    state = tickets(repo)["follow-up-v2"]
    assert state["phase"] == "Intent" and state["kind"] == "feature"
    assert state["uncommitted"] == [str(repo)]
    duplicate = run(repo, "new", "follow-up-v2", "--title", "again")
    assert duplicate.returncode == 2 and "already exists" in duplicate.stderr


def test_fix_ticket_defaults_to_executing_and_links_parent(repo: Path) -> None:
    ok(repo, "new", "parent-feature", "--title", "Parent")
    ok(repo, "new", "fix-send-bug", "--title", "Fix send", "--kind", "fix", "--parent", "parent-feature")
    state = tickets(repo)
    assert state["fix-send-bug"]["phase"] == "Executing"
    shown = ok(repo, "show", "parent-feature", "--json")
    assert shown["children"] == ["fix-send-bug"]
    missing = run(repo, "new", "orphan", "--title", "x", "--parent", "nope")
    assert missing.returncode == 2


def test_mandate_fingerprints_docs_and_seeds_tasks(repo: Path) -> None:
    ok(repo, "new", "feat-a", "--title", "A")
    spec = repo / "docs/plans/feat-a/spec.md"
    spec.write_text("# Spec\n\nStatus: APPROVED — READY FOR PLAN\n", encoding="utf-8")
    ok(repo, "phase", "feat-a", "Plan")
    result = ok(repo, "mandate", "feat-a", "--doc", "docs/plans/feat-a/spec.md", "--task", "TASK-001", "--task", "TASK-002")
    assert result["docs"][0]["status"] == "APPROVED — READY FOR PLAN"
    assert len(result["docs"][0]["sha256"]) == 64
    shown = ok(repo, "show", "feat-a", "--json")
    assert shown["task_total"] == 2 and shown["task_done"] == 0
    outside = run(repo, "mandate", "feat-a", "--doc", "../outside.md")
    assert outside.returncode == 2


def test_worktree_progress_is_visible_from_main_before_merge_and_survives_cleanup(repo: Path) -> None:
    ok(repo, "new", "feat-b", "--title", "B")
    ok(repo, "mandate", "feat-b", "--task", "TASK-001", "--task", "TASK-002")
    commit_all(repo, "ticket")
    worktree = add_worktree(repo, "feat/b")
    ok(worktree, "branch", "feat-b", "--task", "TASK-001", "--task", "TASK-002")
    ok(worktree, "phase", "feat-b", "Executing")

    # Uncommitted worktree events are already visible from the main checkout.
    seen = tickets(repo)["feat-b"]
    assert seen["phase"] == "Executing"
    assert seen["active_branches"] == ["feat/b"]
    assert seen["uncommitted"] == [str(worktree)]

    blocked = run(repo, "doctor", "--before-cleanup", str(worktree))
    assert blocked.returncode == 1 and "Uncommitted ticket events" in blocked.stdout

    (worktree / "code.py").write_text("x = 1\n", encoding="utf-8")
    ok(worktree, "task", "feat-b", "TASK-001", "--status", "Done")
    commit_all(worktree, "TASK-001 with ticket event")

    # Worktree removed but branch kept: progress is still read from the branch tree.
    cleanup = run(repo, "doctor", "--before-cleanup", str(worktree))
    assert cleanup.returncode == 0, cleanup.stdout
    assert "not on main" in cleanup.stdout
    git(repo, "worktree", "remove", str(worktree))
    after = tickets(repo)["feat-b"]
    assert after["task_done"] == 1 and after["not_on_main"] > 0

    git(repo, "merge", "-q", "--no-ff", "feat/b", "-m", "merge feat/b")
    ok(repo, "integrate", "feat-b", "--branch", "feat/b")
    commit_all(repo, "record integration")
    git(repo, "branch", "-q", "-d", "feat/b")
    merged = tickets(repo)["feat-b"]
    assert merged["active_branches"] == [] and merged["not_on_main"] == 0
    assert merged["task_done"] == 1
    assert ok(repo, "doctor")["ok"] is True


def test_parallel_lanes_merge_without_conflicts(repo: Path) -> None:
    ok(repo, "new", "feat-c", "--title", "C")
    ok(repo, "mandate", "feat-c", "--task", "TASK-001", "--task", "TASK-002")
    commit_all(repo, "ticket")
    lane_one = add_worktree(repo, "lane-1")
    lane_two = add_worktree(repo, "lane-2")
    ok(lane_one, "task", "feat-c", "TASK-001", "--status", "Done")
    commit_all(lane_one, "lane 1")
    ok(lane_two, "task", "feat-c", "TASK-002", "--status", "Done")
    commit_all(lane_two, "lane 2")
    git(repo, "merge", "-q", "--no-ff", "lane-1", "-m", "m1")
    git(repo, "merge", "-q", "--no-ff", "lane-2", "-m", "m2")
    state = tickets(repo)["feat-c"]
    assert state["task_done"] == 2 and state["not_on_main"] == 0


def test_deleted_unmerged_branch_is_reported(repo: Path) -> None:
    ok(repo, "new", "feat-d", "--title", "D")
    commit_all(repo, "ticket")
    git(repo, "checkout", "-q", "-b", "feat/d")
    ok(repo, "branch", "feat-d")
    commit_all(repo, "branch event")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "feat/d", "-m", "merge")
    git(repo, "branch", "-q", "-d", "feat/d")
    report = run(repo, "doctor")
    assert report.returncode == 1 and "deleted without an integrated event" in report.stdout


def test_session_binding_handoff_and_shared_state_across_worktrees(repo: Path) -> None:
    ok(repo, "new", "feat-e", "--title", "E")
    commit_all(repo, "ticket")
    worktree = add_worktree(repo, "feat/e")
    binding = ok(worktree, "attach", "feat-e", "--session", "thr_1", "--first-request", "implement E")
    assert binding["binding"]["ticket"] == "feat-e"
    # Shared state lives in the main worktree, whichever worktree runs the command.
    assert ok(worktree, "path").strip() == str(repo / ".codex/tickets")
    ok(worktree, "handoff", "--session", "thr_1", "--summary", "Half done", "--set-next-step", "Write tests", "--add-file", "code.py")
    current = ok(repo, "current", "--session", "thr_1")
    assert current["assignment"]["next_steps"] == ["Write tests"]
    milestone = ok(worktree, "handoff", "--session", "thr_1", "--milestone")
    assert milestone["file"].endswith(".json")
    assert tickets(repo)["feat-e"]["next_steps"] == ["Write tests"]
    # Re-attaching the same session reuses its assignment; a new session gets a new one.
    again = ok(worktree, "attach", "feat-e", "--session", "thr_1")
    assert again["binding"]["assignment"] == binding["binding"]["assignment"]
    other = ok(repo, "attach", "feat-e", "--session", "thr_2")
    assert other["previous_assignment"]["summary"] == "Half done"
    assert ok(repo, "detach", "--session", "thr_2")["detached"] is True
    unbound = run(repo, "handoff", "--session", "thr_2", "--summary", "x")
    assert unbound.returncode == 2 and "not bound" in unbound.stderr


def test_followups_and_done_guard(repo: Path) -> None:
    ok(repo, "new", "feat-f", "--title", "F")
    ok(repo, "mandate", "feat-f", "--task", "TASK-001")
    assert ok(repo, "followup", "feat-f", "--open", "Edge case")["followup"] == "FU-001"
    assert ok(repo, "followup", "feat-f", "--open", "Another")["followup"] == "FU-002"
    guarded = run(repo, "phase", "feat-f", "Done")
    assert guarded.returncode == 2 and "open tasks" in guarded.stderr
    ok(repo, "task", "feat-f", "TASK-001", "--status", "Done")
    ok(repo, "followup", "feat-f", "--close", "FU-001")
    ok(repo, "followup", "feat-f", "--close", "FU-002", "--note", "won't do")
    ok(repo, "phase", "feat-f", "Done")
    assert tickets(repo)["feat-f"]["phase"] == "Done"
    assert "feat-f" not in {item["id"] for item in ok(repo, "list", "--json")["tickets"]}


def test_session_start_hook_routes_or_restores(repo: Path) -> None:
    ok(repo, "new", "feat-g", "--title", "G")
    commit_all(repo, "ticket")
    worktree = add_worktree(repo, "feat/g")
    ok(worktree, "branch", "feat-g")
    startup = ok(worktree, "hook", event={"hook_event_name": "SessionStart", "source": "startup", "session_id": "thr_9"})
    context = startup["hookSpecificOutput"]["additionalContext"]
    assert "Branch match: feat-g" in context and "thr_9" in context

    ok(worktree, "attach", "feat-g", "--session", "thr_9")
    ok(worktree, "handoff", "--session", "thr_9", "--summary", "Wired the API", "--set-next-step", "Add UI")
    compact = ok(worktree, "hook", event={"hook_event_name": "SessionStart", "source": "compact", "session_id": "thr_9"})
    restored = compact["hookSpecificOutput"]["additionalContext"]
    assert "bound to ticket `feat-g`" in restored and "Add UI" in restored

    precompact = ok(worktree, "hook", event={"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "thr_9"})
    assert precompact == {"continue": True}
    assert list((repo / ".codex/tickets/checkpoints").glob("*thr_9*.json"))

    broken = run(worktree, "hook", event=None)
    assert broken.returncode == 0 and json.loads(broken.stdout) == {"continue": True}


def test_board_renders_self_contained_html(repo: Path, tmp_path: Path) -> None:
    ok(repo, "new", "feat-h", "--title", "Board <script>alert(1)</script>")
    output = tmp_path / "board.html"
    data = tmp_path / "board.json"
    result = ok(repo, "board", "--output", str(output), "--json-output", str(data))
    assert result["tickets"] == 1
    assert result["opened"] is None
    html = output.read_text(encoding="utf-8")
    assert "/*__BOARD_DATA__*/" not in html and "feat-h" in html
    assert "</script>alert" not in html  # embedded data cannot close the script tag
    assert json.loads(data.read_text())["tickets"][0]["id"] == "feat-h"


def test_board_opens_rendered_html(repo: Path, tmp_path: Path) -> None:
    recorder = tmp_path / "opener.sh"
    log = tmp_path / "opened.txt"
    recorder.write_text(
        "#!/bin/sh\nprintf '%s' \"$1\" > " + str(log) + "\n", encoding="utf-8"
    )
    recorder.chmod(0o755)
    output = tmp_path / "board.html"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(repo), "board", "--output", str(output), "--open"],
        cwd=repo,
        env={**env(), "TICKETS_OPENER": str(recorder)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["opened"] is True
    assert log.read_text(encoding="utf-8") == str(output)
    assert output.is_file()


def test_invalid_ids_and_event_files_are_rejected(repo: Path) -> None:
    assert run(repo, "new", "Bad_ID", "--title", "x").returncode == 2
    ok(repo, "new", "feat-i", "--title", "I")
    broken = repo / "docs/plans/feat-i/ticket-events/20260101T000000Z-abcdef.json"
    broken.write_text("{not json", encoding="utf-8")
    payload = ok(repo, "list", "--json")
    assert any("invalid event" in warning for warning in payload["warnings"])
    assert "feat-i" in {item["id"] for item in payload["tickets"]}


def test_antigravity_session_start_hook_routes_and_restores(repo: Path) -> None:
    ok(repo, "new", "feat-agy", "--title", "AGY")
    commit_all(repo, "ticket")
    worktree = add_worktree(repo, "feat/agy")
    ok(worktree, "branch", "feat-agy")
    startup = ok(
        worktree,
        "hook",
        "--runtime",
        "antigravity",
        event={
            "hookEventName": "SessionStart",
            "conversationId": "conv-1234",
            "workspacePaths": [str(worktree)],
            "cwd": str(worktree),
        },
    )
    msg = startup["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Branch match: feat-agy" in msg and "conv-1234" in msg

    ok(worktree, "attach", "feat-agy", "--session", "conv-1234")
    ok(worktree, "handoff", "--session", "conv-1234", "--summary", "Ready to test", "--set-next-step", "Run pytest")
    bound = ok(
        worktree,
        "hook",
        "--runtime",
        "antigravity",
        event={"hookEventName": "SessionStart", "conversationId": "conv-1234"},
    )
    restored = bound["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "bound to ticket `feat-agy`" in restored and "Run pytest" in restored

    broken = run(worktree, "hook", "--runtime", "antigravity", event=None)
    assert broken.returncode == 0 and json.loads(broken.stdout) == {}


def test_feature_phase_guidance_maps_to_readiness_skills(repo: Path) -> None:
    created = ok(repo, "new", "feat-sdlc", "--title", "SDLC")
    assert created["next_skill"] == "intent-readiness"
    assert "intent.md" in created["guidance"]

    ok(repo, "phase", "feat-sdlc", "Spec")
    attached = ok(repo, "attach", "feat-sdlc", "--session", "s-sdlc")
    assert attached["next_skill"] == "spec-readiness"

    hook = ok(
        repo,
        "hook",
        "--runtime",
        "antigravity",
        event={"hookEventName": "SessionStart", "conversationId": "s-sdlc"},
    )
    msg = hook["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "spec-readiness" in msg and "docs/plans/feat-sdlc/spec.md" in msg

    ok(repo, "phase", "feat-sdlc", "Executing")
    attached_exec = ok(repo, "attach", "feat-sdlc", "--session", "s-sdlc")
    assert "next_skill" not in attached_exec

    fix = ok(repo, "new", "fix-quick", "--title", "Quick fix", "--kind", "fix")
    assert "next_skill" not in fix


def test_plugin_hook_resolves_target_workspace_and_ignores_non_git(repo: Path, tmp_path: Path) -> None:
    ok(repo, "new", "feat-cross", "--title", "Cross repo")
    commit_all(repo, "ticket")
    sub = repo / "src"
    sub.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "hook", "--runtime", "antigravity"],
        input=json.dumps(
            {
                "hookEventName": "SessionStart",
                "conversationId": "conv-plugin-test",
                "cwd": str(sub),
                "workspacePaths": [str(repo)],
            }
        ),
        cwd=ROOT,  # simulate running from plugin directory, not target repo
        env=env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    msg = payload["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "feat-cross" in msg and str(repo) in msg

    plain = tmp_path / "plain-dir"
    plain.mkdir()
    noop = subprocess.run(
        [sys.executable, str(SCRIPT), "hook", "--runtime", "antigravity"],
        input=json.dumps(
            {
                "hookEventName": "SessionStart",
                "conversationId": "conv-plain",
                "cwd": str(plain),
                "workspacePaths": [str(plain)],
            }
        ),
        cwd=ROOT,
        env=env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert noop.returncode == 0 and json.loads(noop.stdout) == {}


def test_master_branch_auto_detection(tmp_path: Path) -> None:
    master_repo = tmp_path / "master-repo"
    master_repo.mkdir()
    git(master_repo, "init", "-q", "-b", "master")
    (master_repo / "README.md").write_text("demo\n", encoding="utf-8")
    commit_all(master_repo, "init")
    ok(master_repo, "new", "feat-master", "--title", "Master default")
    commit_all(master_repo, "ticket")
    state = tickets(master_repo)["feat-master"]
    assert state["not_on_main"] == 0


def test_plugin_bundle_and_project_installer(repo: Path) -> None:
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "sdlc-tickets"
    for skill in (
        "manage-tickets",
        "intent-readiness",
        "spec-readiness",
        "plan-readiness",
        "commit-code-changes",
        "board",
    ):
        assert (ROOT / "skills" / skill / "SKILL.md").is_file()
    assert (ROOT / "rules/AGENTS.md").is_file()

    agy_config = json.loads(PLUGIN_HOOKS.read_text(encoding="utf-8"))
    handlers = agy_config["sdlc-tickets"]["SessionStart"]
    assert len(handlers) == 1
    assert handlers[0]["command"].endswith(" hook --runtime antigravity")

    installed = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--project", str(repo), "--with-codex-hooks"],
        cwd=ROOT,
        env=env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr + installed.stdout
    plugin_dir = repo / ".agents/plugins/sdlc-tickets"
    assert (plugin_dir / "plugin.json").is_file()
    assert (plugin_dir / "hooks.json").is_file()
    assert (plugin_dir / "skills/manage-tickets/scripts/tickets.py").is_file()

    codex_hooks = json.loads((repo / ".codex/hooks.json").read_text(encoding="utf-8"))
    for hook_name in ("PreCompact", "SessionStart"):
        command = codex_hooks["hooks"][hook_name][0]["hooks"][0]["command"]
        assert ".agents/plugins/sdlc-tickets/skills/manage-tickets/scripts/tickets.py" in command
        assert command.endswith(" hook")


def test_claude_code_hook_contract(repo: Path) -> None:
    startup = ok(
        repo,
        "hook",
        "--runtime",
        "claude",
        event={"hook_event_name": "SessionStart", "source": "startup", "session_id": "cc-1"},
    )
    context = startup["hookSpecificOutput"]["additionalContext"]
    assert startup["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Project workflow rules" in context and "commit-code-changes" in context

    codex = ok(repo, "hook", event={"hook_event_name": "SessionStart", "source": "startup", "session_id": "cc-1"})
    assert "Project workflow rules" not in codex["hookSpecificOutput"]["additionalContext"]

    ok(repo, "new", "feat-cc", "--title", "Claude", "--attach", "--session", "cc-1")
    bound = ok(
        repo,
        "hook",
        "--runtime",
        "claude",
        event={"hook_event_name": "SessionStart", "source": "resume", "session_id": "cc-1"},
    )
    bound_context = bound["hookSpecificOutput"]["additionalContext"]
    assert "bound to ticket `feat-cc`" in bound_context
    assert "Project workflow rules" in bound_context

    precompact = run(
        repo,
        "hook",
        "--runtime",
        "claude",
        event={"hook_event_name": "PreCompact", "trigger": "manual", "session_id": "cc-1"},
    )
    assert precompact.returncode ==0
    assert precompact.stdout == "" # stdout would become compaction instructions
    assert list((repo / ".codex/tickets/checkpoints").glob("*cc-1*.json"))

def test_claude_code_plugin_bundle() -> None:
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "sdlc-tickets"
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["name"] == "sdlc-tickets"
    assert marketplace["plugins"][0]["source"] == "./"

    hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact"}
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact|fork"
    for name in ("SessionStart", "PreCompact"):
        command = hooks[name][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}" in command
        assert command.endswith("hook --runtime claude")
