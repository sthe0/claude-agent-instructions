"""Issue #268: a spawned child (AGENT_RECURSION_DEPTH >= 1) has no TTY to see an
interactive `ask` dialog -- the harness silently treats an unanswered `ask` as a
deny with the reason DROPPED, so the child never learns why. This pins that
`decide_detailed` in `hook-guard-permission-surface.py` turns a G-branch
`"ask"` into an explicit `"deny"` carrying a `PERMISSION-REQUEST:` hint once
`AGENT_RECURSION_DEPTH >= 1`, while depth 0 stays byte-identical to the
G-branch's own `"ask"` verdict.

conftest.py's `_no_ambient_recursion_depth` autouse fixture drops
AGENT_RECURSION_DEPTH for the whole suite by default, so every test below that
does not itself set the var runs at the depth-0 baseline."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

GUARD_PATH = Path(__file__).resolve().parent.parent / "hook-guard-permission-surface.py"

_spec = importlib.util.spec_from_file_location("hook_guard_permission_surface", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# A concrete G1-state fire: a Bash write target under agentctl's own state
# directory. Any G-branch fire would do for this test's purpose (the
# depth-gating wrapper is branch-agnostic); this one is cheap to construct.
# `bash_write_targets` never expands a `$VAR` reference, so the state dir must
# be a literal resolved path -- `_isolate_agent_home` points CLAUDE_AGENT_HOME/
# CLAUDE_CONFIG_DIR at it, mirroring test_hook_guard_permission_surface.py's
# own `_isolate_agent_home` helper (config_root.agent_home() checks
# CLAUDE_CONFIG_DIR before CLAUDE_AGENT_HOME -- the live harness always sets
# the former, so isolating agent_home() must override both).
_FIRING_TOOL_NAME = "Bash"


def _isolate_agent_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))


def _firing_tool_input(tmp_path) -> dict:
    return {"command": f"echo hi > {tmp_path}/agentctl/state/some-session.json"}


def _run_hook(payload: dict, env_extra: dict | None = None) -> dict | None:
    env = {**__import__("os").environ, **(env_extra or {})}
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def test_depth_one_denies_with_permission_request_hint(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    decision, branch, message = guard.decide_detailed(
        _FIRING_TOOL_NAME, _firing_tool_input(tmp_path), str(tmp_path), "default", None,
    )
    assert decision == "deny"
    assert branch == "G1-state"
    assert "PERMISSION-REQUEST:" in message


def test_depth_zero_still_asks(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    decision, branch, message = guard.decide_detailed(
        _FIRING_TOOL_NAME, _firing_tool_input(tmp_path), str(tmp_path), "default", None,
    )
    assert decision == "ask"
    assert branch == "G1-state"
    assert "PERMISSION-REQUEST:" not in message


def test_depth_zero_byte_identical_to_decide_core(tmp_path, monkeypatch):
    """At depth 0, decide_detailed must return EXACTLY what _decide_core
    returns -- no wrapping, no message mutation -- for every input, not just
    the one firing case above."""
    _isolate_agent_home(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    cases = [
        ("Bash", {"command": "ls -la"}, str(tmp_path)),
        ("Bash", _firing_tool_input(tmp_path), str(tmp_path)),
        ("Edit", {"file_path": str(tmp_path / "foo.txt"), "old_string": "a", "new_string": "b"}, str(tmp_path)),
    ]
    for tool_name, tool_input, cwd in cases:
        core = guard._decide_core(tool_name, tool_input, cwd, "default", None)
        detailed = guard.decide_detailed(tool_name, tool_input, cwd, "default", None)
        assert core == detailed


def test_main_logs_actual_decision_deny_at_depth_one(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    log_path = tmp_path / "guard.jsonl"
    payload = {
        "session_id": "sess-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "tool_use_id": "tu-1",
        "tool_name": _FIRING_TOOL_NAME,
        "tool_input": _firing_tool_input(tmp_path),
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    out = _run_hook(payload, {
        "AGENT_RECURSION_DEPTH": "1",
        "CLAUDE_PERMISSION_GUARD_LOG": str(log_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "CLAUDE_AGENT_HOME": str(tmp_path),
    })
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PERMISSION-REQUEST:" in out["hookSpecificOutput"]["permissionDecisionReason"]
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["decision"] == "deny"


def test_main_logs_actual_decision_ask_at_depth_zero(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    log_path = tmp_path / "guard.jsonl"
    payload = {
        "session_id": "sess-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "tool_use_id": "tu-1",
        "tool_name": _FIRING_TOOL_NAME,
        "tool_input": _firing_tool_input(tmp_path),
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    out = _run_hook(payload, {
        "AGENT_RECURSION_DEPTH": "0",
        "CLAUDE_PERMISSION_GUARD_LOG": str(log_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "CLAUDE_AGENT_HOME": str(tmp_path),
    })
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["decision"] == "ask"
