"""Pre-trial defect #427: the production-edit gate authorizes by the engine session the
coordinator drives, not by the harness conversation id.

A harness session H (the id in every hook payload and in CLAUDE_CODE_SESSION_ID) may drive a
different engine session E through `agentctl --session E`. The gate reads the state of the
session named in the payload, so an Edit from H is judged by H's own auto-started state and not
by E, which is the one at an execution node; and every command prints a warning that says so.

Driven end to end through subprocesses over an isolated HOME: the agentctl CLI and the
PreToolUse hook, as the harness runs them. A test asserts the repaired behaviour with a message
that starts `BEHAVIOUR:`; every setup assertion lacks that marker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLI = SCRIPTS_DIR / "agentctl-cli.py"
HOOK = SCRIPTS_DIR / "hook-state-gate.py"

HARNESS = "harness-h"
ENGINE = "engine-e"
PRODUCTION_FILE = "/work/project/module.py"
WARNING_PREFIX = "agentctl: warning:"


def _env(home: Path, harness: str | None = HARNESS) -> dict:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    if harness is not None:
        env["CLAUDE_CODE_SESSION_ID"] = harness
    return env


def _agentctl(home: Path, *argv: str, harness: str | None = HARNESS) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, env=_env(home, harness), timeout=120,
    )


def _state_file(home: Path, session: str) -> Path:
    return home / ".claude" / "agentctl" / "state" / f"{session}.json"


def _write_harness_state(home: Path, node: str) -> None:
    path = _state_file(home, HARNESS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"node": node, "weight_class": "SUBSTANTIVE"}))


def _edit_from_harness(home: Path) -> subprocess.CompletedProcess:
    payload = {
        "tool_name": "Edit",
        "session_id": HARNESS,
        "tool_input": {"file_path": PRODUCTION_FILE},
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"}, timeout=60,
    )


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _drive_engine_to_executing(home: Path) -> None:
    """Engine session E reaches EXECUTING through verbs run by harness session H."""
    for argv in (
        ("start", "--session", ENGINE, "--task", "driven", "--goal", "goal"),
        ("classify", "--session", ENGINE, "--changed-lines", "5"),
        ("next-stage", "--session", ENGINE),
    ):
        proc = _agentctl(home, *argv)
        assert proc.returncode == 0, f"setup: {argv[0]} failed: {proc.stdout[-300:]}"
    node = json.loads(_state_file(home, ENGINE).read_text())["node"]
    assert node == "EXECUTING", f"setup: engine session is at {node}"


def test_i427_edit_authorized_by_driven_engine_session(tmp_path):
    _write_harness_state(tmp_path, "PLAN_READY")
    _drive_engine_to_executing(tmp_path)

    proc = _edit_from_harness(tmp_path)

    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert not _is_deny(proc), (
        "BEHAVIOUR: an Edit from the harness session is denied although the engine session "
        f"it drives is at EXECUTING: {proc.stdout[:300]}"
    )


def _session_mismatch_warnings(stderr: str) -> list[str]:
    return [
        line for line in stderr.splitlines()
        if line.startswith(WARNING_PREFIX) and "CLAUDE_CODE_SESSION_ID" in line
    ]


def test_i427_no_session_mismatch_warning_for_bound_session(tmp_path):
    _write_harness_state(tmp_path, "PLAN_READY")
    first = _agentctl(tmp_path, "start", "--session", ENGINE, "--task", "driven", "--goal", "goal")
    assert first.returncode == 0, f"setup: start failed: {first.stdout[-300:]}"

    second = _agentctl(tmp_path, "classify", "--session", ENGINE, "--changed-lines", "5")

    assert second.returncode == 0, f"setup: classify failed: {second.stdout[-300:]}"
    warnings = _session_mismatch_warnings(second.stderr)
    assert not warnings, (
        "BEHAVIOUR: the second state-changing command with --session of an already driven "
        f"engine session still warns about the harness id: {warnings}"
    )


def test_unbound_harness_session_keeps_base_decision(tmp_path):
    _write_harness_state(tmp_path, "PLAN_READY")
    assert _is_deny(_edit_from_harness(tmp_path)), "an unbound harness session at PLAN_READY is denied"

    _write_harness_state(tmp_path, "EXECUTING")
    proc = _edit_from_harness(tmp_path)
    assert proc.returncode == 0 and not _is_deny(proc), "an unbound harness session at EXECUTING is allowed"


def _drive_to_executing(home: Path, session: str | None, harness: str | None = HARNESS) -> None:
    """Drive `session` (the harness session itself when None) to EXECUTING."""
    target = ("--session", session) if session else ()
    for argv in (
        ("start", *target, "--task", "driven", "--goal", "goal"),
        ("classify", *target, "--changed-lines", "5"),
        ("next-stage", *target),
    ):
        proc = _agentctl(home, *argv, harness=harness)
        assert proc.returncode == 0, f"setup: {argv[0]} failed: {proc.stdout[-300:]}"
    state_session = session or harness
    node = json.loads(_state_file(home, state_session).read_text())["node"]
    assert node == "EXECUTING", f"setup: session {state_session} is at {node}"


def _use_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)


def test_binding_follows_most_recent_driven_session(tmp_path):
    _drive_to_executing(tmp_path, "engine-first")
    assert not _is_deny(_edit_from_harness(tmp_path)), "setup: the first driven session is at EXECUTING"

    started = _agentctl(tmp_path, "start", "--session", "engine-second", "--task", "t", "--goal", "g")
    assert started.returncode == 0, f"setup: start failed: {started.stdout[-300:]}"

    assert _is_deny(_edit_from_harness(tmp_path)), (
        "BEHAVIOUR: the gate did not move to the engine session driven last, which is not at EXECUTING"
    )


def test_driving_own_session_after_other_rebinds_to_own(tmp_path):
    _drive_to_executing(tmp_path, ENGINE)
    state_file = _state_file(tmp_path, ENGINE)
    state = json.loads(state_file.read_text())
    state["node"] = "RESOLVED"
    state_file.write_text(json.dumps(state))
    assert _is_deny(_edit_from_harness(tmp_path)), "setup: the driven session is resolved"

    _drive_to_executing(tmp_path, None)

    proc = _edit_from_harness(tmp_path)
    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert not _is_deny(proc), (
        "BEHAVIOUR: after the harness session drove itself to EXECUTING, the gate still follows "
        f"the resolved engine session: {proc.stdout[:300]}"
    )


def test_driven_sessions_lists_every_bound_session_most_recent_first(tmp_path, monkeypatch):
    from lib import engine_session_binding

    for session in ("engine-a", "engine-b", "engine-c"):
        started = _agentctl(tmp_path, "start", "--session", session, "--task", "t", "--goal", "g")
        assert started.returncode == 0, f"setup: start failed: {started.stdout[-300:]}"
    classified = _agentctl(tmp_path, "classify", "--session", "engine-a", "--changed-lines", "5")
    assert classified.returncode == 0, f"setup: classify failed: {classified.stdout[-300:]}"
    _state_file(tmp_path, "engine-b").unlink()
    _use_home(monkeypatch, tmp_path)

    assert engine_session_binding.driven_sessions(HARNESS) == ["engine-a", "engine-c"]
    assert engine_session_binding.resolve(HARNESS) == "engine-a"
    assert engine_session_binding.resolve("never-bound") is None


def test_pending_substantive_session_blocks_release_via_side_session(tmp_path):
    started = _agentctl(tmp_path, "start", "--session", "engine-plan", "--task", "t", "--goal", "g")
    assert started.returncode == 0, f"setup: start failed: {started.stdout[-300:]}"
    classified = _agentctl(tmp_path, "classify", "--session", "engine-plan",
                           "--changed-lines", "900")
    assert classified.returncode == 0, f"setup: classify failed: {classified.stdout[-300:]}"
    plan_file = _state_file(tmp_path, "engine-plan")
    plan_state = json.loads(plan_file.read_text())
    assert plan_state["weight_class"] == "SUBSTANTIVE", "setup: the plan session is substantive"
    plan_state["node"] = "PLAN_READY"
    plan_file.write_text(json.dumps(plan_state))

    _drive_to_executing(tmp_path, "engine-side")

    proc = _edit_from_harness(tmp_path)
    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert _is_deny(proc), (
        "BEHAVIOUR: driving a small side session to EXECUTING released production edits while "
        "the substantive session driven earlier still awaits plan approval"
    )
    assert "engine-plan" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"], (
        "the denial must name the session awaiting approval"
    )

    plan_state["node"] = "RESOLVED"
    plan_file.write_text(json.dumps(plan_state))

    proc = _edit_from_harness(tmp_path)
    assert proc.returncode == 0 and not _is_deny(proc), (
        f"a resolved substantive session must not block the side session: {proc.stdout[:300]}"
    )


def test_own_substantive_session_blocks_release_via_side_session(tmp_path):
    # test_i427's hand-written H state is unbound by construction, which is why it is the
    # exception: here H is bound to itself by the verbs it runs through the CLI.
    for argv in (
        ("start", "--task", "t", "--goal", "g"),
        ("classify", "--changed-lines", "900"),
    ):
        proc = _agentctl(tmp_path, *argv)
        assert proc.returncode == 0, f"setup: {argv[0]} failed: {proc.stdout[-300:]}"
    own_file = _state_file(tmp_path, HARNESS)
    own_state = json.loads(own_file.read_text())
    assert own_state["weight_class"] == "SUBSTANTIVE", "setup: the harness session is substantive"
    own_state["node"] = "PLAN_READY"
    own_file.write_text(json.dumps(own_state))

    _drive_to_executing(tmp_path, "engine-side")

    proc = _edit_from_harness(tmp_path)
    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert _is_deny(proc), (
        "BEHAVIOUR: driving a side session to EXECUTING released production edits while the "
        "harness session's own substantive plan still awaits approval"
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert HARNESS in reason, "the denial must name the harness session awaiting approval"


def _pending_session_then_side_executing(home: Path, pending_node: str) -> Path:
    """Harness H drives substantive `engine-plan` (left at `pending_node`), then a small
    `engine-side` session at EXECUTING. Returns the pending session's state file."""
    for argv in (
        ("start", "--session", "engine-plan", "--task", "t", "--goal", "g"),
        ("classify", "--session", "engine-plan", "--changed-lines", "900"),
    ):
        proc = _agentctl(home, *argv)
        assert proc.returncode == 0, f"setup: {argv[0]} failed: {proc.stdout[-300:]}"
    plan_file = _state_file(home, "engine-plan")
    plan_state = json.loads(plan_file.read_text())
    plan_state["node"] = pending_node
    plan_file.write_text(json.dumps(plan_state))
    _drive_to_executing(home, "engine-side")
    return plan_file


def test_pending_session_guard_does_not_apply_to_a_plan_file(tmp_path):
    _pending_session_then_side_executing(tmp_path, "PLAN_READY")
    assert _is_deny(_edit_from_harness(tmp_path)), "setup: the guard blocks a production path"

    payload = {
        "tool_name": "Edit",
        "session_id": HARNESS,
        # Outside /tmp/: a tmp_path plan file is engine-exempt and never reaches the guard.
        "tool_input": {"file_path": "/work/project/.claude/plans/task.toml"},
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"}, timeout=60,
    )

    assert proc.returncode == 0 and not _is_deny(proc), (
        f"a plan file is not a production path for the pending-session guard: {proc.stdout[:300]}"
    )


def test_pending_session_guard_fails_open_on_an_unreadable_state_file(tmp_path):
    plan_file = _pending_session_then_side_executing(tmp_path, "PLAN_READY")
    plan_file.write_text("{not json")

    proc = _edit_from_harness(tmp_path)

    assert proc.returncode == 0 and not _is_deny(proc), (
        f"an unreadable bound state file must be skipped, not block the edit: {proc.stdout[:300]}"
    )


def test_pending_session_guard_blocks_at_diagnosing(tmp_path):
    _pending_session_then_side_executing(tmp_path, "DIAGNOSING")

    proc = _edit_from_harness(tmp_path)

    assert _is_deny(proc), "a substantive session at DIAGNOSING still holds the gate"
    assert "engine-plan" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_readonly_query_of_other_session_keeps_base_decision(tmp_path):
    _drive_to_executing(tmp_path, ENGINE, harness="harness-other")
    _write_harness_state(tmp_path, "PLAN_READY")

    query = _agentctl(tmp_path, "status", "--session", ENGINE)

    assert query.returncode == 0, f"setup: status failed: {query.stdout[-300:]}"
    assert _is_deny(_edit_from_harness(tmp_path)), (
        "a read-only query of another session must not move the gate off the harness session"
    )


def test_status_of_unbound_session_still_warns_about_the_harness_id(tmp_path):
    started = _agentctl(tmp_path, "start", "--session", ENGINE, "--task", "t", "--goal", "g",
                        harness="harness-other")
    assert started.returncode == 0, f"setup: start failed: {started.stdout[-300:]}"

    query = _agentctl(tmp_path, "status", "--session", ENGINE)

    assert query.returncode == 0, f"status failed: {query.stdout[-300:]}"
    assert _session_mismatch_warnings(query.stderr), (
        "a query of a session the harness never drove must still say the gate does not follow it"
    )


def _edit_from_linked_child(home: Path, child: str) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Edit", "session_id": child, "tool_input": {"file_path": PRODUCTION_FILE}}
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin",
        "AGENTCTL_OWN_SESSION": child, "AGENTCTL_NORM_HANDOFF": "handoff", "AGENT_RECURSION_DEPTH": "1",
    }
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_linked_child_bound_to_another_linked_session_is_not_redirected(tmp_path):
    child, other = "child-c", "child-c2"

    def write_linked_state(session: str, node: str) -> None:
        path = _state_file(tmp_path, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"node": node, "weight_class": "SUBSTANTIVE", "parent_link": {}}))

    write_linked_state(child, "CLASSIFIED")
    started = _agentctl(tmp_path, "start", "--session", other, "--task", "t", "--goal", "g", harness=child)
    assert started.returncode == 0, f"setup: start failed: {started.stdout[-300:]}"
    write_linked_state(other, "EXECUTING")
    assert not _is_deny(_edit_from_linked_child(tmp_path, other)), "setup: the other linked session may edit"

    proc = _edit_from_linked_child(tmp_path, child)

    assert _is_deny(proc), (
        "BEHAVIOUR: a linked child that drove another linked session is decided by that session's "
        f"node instead of its own contour: {proc.stdout[:300]}"
    )
