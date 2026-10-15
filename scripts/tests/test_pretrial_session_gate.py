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
        ("classify", "--session", ENGINE, "--files", "1", "--changed-lines", "5"),
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

    second = _agentctl(tmp_path, "classify", "--session", ENGINE, "--files", "1", "--changed-lines", "5")

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
        ("classify", *target, "--files", "1", "--changed-lines", "5"),
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
    classified = _agentctl(tmp_path, "classify", "--session", "engine-a", "--files", "1", "--changed-lines", "5")
    assert classified.returncode == 0, f"setup: classify failed: {classified.stdout[-300:]}"
    _state_file(tmp_path, "engine-b").unlink()
    _use_home(monkeypatch, tmp_path)

    assert engine_session_binding.driven_sessions(HARNESS) == ["engine-a", "engine-c"]
    assert engine_session_binding.resolve(HARNESS) == "engine-a"
    assert engine_session_binding.resolve("never-bound") is None


def test_readonly_query_of_other_session_keeps_base_decision(tmp_path):
    _drive_to_executing(tmp_path, ENGINE, harness="harness-other")
    _write_harness_state(tmp_path, "PLAN_READY")

    query = _agentctl(tmp_path, "status", "--session", ENGINE)

    assert query.returncode == 0, f"setup: status failed: {query.stdout[-300:]}"
    assert _is_deny(_edit_from_harness(tmp_path)), (
        "a read-only query of another session must not move the gate off the harness session"
    )
