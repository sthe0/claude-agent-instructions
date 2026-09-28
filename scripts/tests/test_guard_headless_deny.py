"""A spawned child (AGENT_RECURSION_DEPTH >= 1) has no TTY to see an
interactive `ask` dialog -- the harness silently treats an unanswered `ask` as
a deny with the reason DROPPED, so the child never learns why. This pins that
`main()` in `hook-guard-permission-surface.py` turns a G-branch `"ask"` into
an explicit `"deny"` carrying a `PERMISSION-REQUEST:` hint once
`AGENT_RECURSION_DEPTH >= 1`, while `decide()`/`decide_detailed()` themselves
stay pure (no env read at all) and always report the G-branch's own `"ask"` --
the depth-based rule lives only in `main()`, applied right before the fire is
logged and printed.

conftest.py's `_no_ambient_recursion_depth` autouse fixture drops
AGENT_RECURSION_DEPTH for the whole suite by default, so every test below that
does not itself set the var runs at the depth-0 baseline."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).resolve().parent.parent / "hook-guard-permission-surface.py"
REPLAY_PATH = Path(__file__).resolve().parent.parent / "replay-permission-guard.py"

_spec = importlib.util.spec_from_file_location("hook_guard_permission_surface", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# A concrete G1-state fire: a Bash write target under agentctl's own state
# directory. Any G-branch fire would do for this test's purpose (the
# depth-gating rule in main() is branch-agnostic); this one is cheap to
# construct. `bash_write_targets` never expands a `$VAR` reference, so the
# state dir must be a literal resolved path -- `_isolate_agent_home` points
# CLAUDE_AGENT_HOME/CLAUDE_CONFIG_DIR at it, mirroring
# test_hook_guard_permission_surface.py's own `_isolate_agent_home` helper
# (config_root.agent_home() checks CLAUDE_CONFIG_DIR before
# CLAUDE_AGENT_HOME -- the live harness always sets the former, so isolating
# agent_home() must override both).
_FIRING_TOOL_NAME = "Bash"


def _isolate_agent_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))


def _firing_tool_input(tmp_path) -> dict:
    return {"command": f"echo hi > {tmp_path}/agentctl/state/some-session.json"}


def _run_hook(payload: dict, env_extra: dict | None = None) -> dict | None:
    env = {**os.environ, **(env_extra or {})}
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


def test_decide_detailed_never_returns_deny_regardless_of_depth(tmp_path, monkeypatch):
    """decide()/decide_detailed() are pure G-branch logic -- the depth-based
    ask->deny rule lives only in main(), so decide_detailed must keep
    returning "ask" (never "deny") for a firing input even when
    AGENT_RECURSION_DEPTH is set to a spawned depth."""
    _isolate_agent_home(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    decision, branch, message = guard.decide_detailed(
        _FIRING_TOOL_NAME, _firing_tool_input(tmp_path), str(tmp_path), "default", None,
    )
    assert decision == "ask"
    assert branch == "G1-state"
    assert "PERMISSION-REQUEST:" not in message


def test_depth_one_denies_with_permission_request_hint(tmp_path, monkeypatch):
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


def test_main_asks_at_depth_zero(tmp_path, monkeypatch):
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
    assert "PERMISSION-REQUEST:" not in out["hookSpecificOutput"]["permissionDecisionReason"]
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["decision"] == "ask"


@pytest.mark.parametrize("raw_depth", ["abc", "", "1.0", "-1", " 0 "])
def test_main_asks_on_unparsable_or_non_positive_depth(tmp_path, monkeypatch, raw_depth):
    """A malformed or non-positive AGENT_RECURSION_DEPTH must never escalate
    to deny -- `_recursion_depth()`'s own int() parse failure falls back to 0,
    and a negative or zero depth is never >= 1."""
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
        "AGENT_RECURSION_DEPTH": raw_depth,
        "CLAUDE_PERMISSION_GUARD_LOG": str(log_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "CLAUDE_AGENT_HOME": str(tmp_path),
    })
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_main_denies_on_a_depth_string_with_surrounding_whitespace(tmp_path, monkeypatch):
    """`int(" 2 ")` succeeds in Python (surrounding whitespace is stripped),
    so this must still escalate to deny -- the negative twin of the
    non-positive/unparsable cases above."""
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
        "AGENT_RECURSION_DEPTH": " 2 ",
        "CLAUDE_PERMISSION_GUARD_LOG": str(log_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "CLAUDE_AGENT_HOME": str(tmp_path),
    })
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_replay_still_reports_the_fire_when_run_at_a_spawned_depth(tmp_path, monkeypatch):
    """replay-permission-guard.py imports decide_detailed directly, never
    main() -- it must keep reporting a would-fire ("ask") even when
    AGENT_RECURSION_DEPTH happens to be set to a spawned depth in its own
    environment, since decide_detailed no longer consults that variable at
    all. This is the regression the pre-fix code (which read the env var
    inside decide_detailed) would have silently produced: a replay run
    launched from inside a spawned child's own shell would have seen "deny"
    instead of "ask" and been filtered out of the report entirely."""
    _isolate_agent_home(monkeypatch, tmp_path)
    _spec = importlib.util.spec_from_file_location("replay_permission_guard", REPLAY_PATH)
    replay = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(replay)

    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    decision, branch, _message = replay._guard.decide_detailed(
        _FIRING_TOOL_NAME, _firing_tool_input(tmp_path), str(tmp_path), "default", None,
    )
    assert decision == "ask"
    assert branch == "G1-state"
