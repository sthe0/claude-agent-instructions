"""Hermetic tests for hook-cursor-adapt.py and cursor_hook_contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
ADAPTER = SCRIPTS_DIR / "hook-cursor-adapt.py"
STATE_GATE = SCRIPTS_DIR / "hook-state-gate.py"
MOCK_STOP_BLOCK = Path(__file__).resolve().parent / "fixtures" / "mock_stop_block_hook.py"
MOCK_MALFORMED = Path(__file__).resolve().parent / "fixtures" / "mock_malformed_hook.py"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import hook_registry  # noqa: E402


def _write_state(home: Path, session_id: str, node: str, weight_class: str | None = None) -> None:
    state_dir = home / ".claude" / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if weight_class is not None:
        data["weight_class"] = weight_class
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


def _registry_with_rows(tmp_path: Path, rows: list[dict]) -> Path:
    registry = tmp_path / "desired.json"
    registry.write_text(
        json.dumps({"version": 1, "hooks": rows}, indent=2),
        encoding="utf-8",
    )
    return registry


def _run_adapter(
    payload: dict,
    home: Path,
    registry_path: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CLAUDE_AGENT_HOME": str(home / "agent-home"),
        "HOOK_REGISTRY_PATH": str(registry_path),
    }
    if extra_env:
        env.update(extra_env)
    hook_registry.REGISTRY_PATH = registry_path
    return subprocess.run(
        [sys.executable, str(ADAPTER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _state_gate_row() -> dict:
    return {
        "claude_event": "PreToolUse",
        "claude_matcher": "Edit|Write",
        "command": "hook-state-gate.py",
        "timeout": 5,
        "role": "gate",
        "cursor_event": "preToolUse",
        "cursor_matcher": "Write|StrReplace",
        "cursor_skip_reason": None,
        "cursor_fail_closed": True,
    }


def _mock_stop_row() -> dict:
    rel = MOCK_STOP_BLOCK.relative_to(SCRIPTS_DIR)
    return {
        "claude_event": "Stop",
        "claude_matcher": None,
        "command": str(rel),
        "timeout": 5,
        "role": "gate",
        "cursor_event": "stop",
        "cursor_matcher": None,
        "cursor_skip_reason": None,
        "cursor_fail_closed": False,
    }


def _mock_malformed_row(fail_closed: bool) -> dict:
    rel = MOCK_MALFORMED.relative_to(SCRIPTS_DIR)
    return {
        "claude_event": "PreToolUse",
        "claude_matcher": "Edit|Write",
        "command": str(rel),
        "timeout": 5,
        "role": "gate",
        "cursor_event": "preToolUse",
        "cursor_matcher": "Write|StrReplace",
        "cursor_skip_reason": None,
        "cursor_fail_closed": fail_closed,
    }


def _write_payload(
    tool_name: str,
    session_id: str,
    file_path: str,
    hook_event_name: str = "preToolUse",
) -> dict:
    return {
        "hook_event_name": hook_event_name,
        "tool_name": tool_name,
        "session_id": session_id,
        "tool_input": {"file_path": file_path},
        "cwd": "/work/project",
    }


def test_strreplace_denied_when_not_executing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry_with_rows(tmp_path, [_state_gate_row()])
    _write_state(home, "sess-deny", "PLAN_READY", weight_class="SUBSTANTIVE")
    proc = _run_adapter(
        _write_payload("StrReplace", "sess-deny", "/work/project/module.py"),
        home,
        registry,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["permission"] == "deny"
    assert "PLAN_READY" in out["agent_message"]


def test_write_allowed_when_executing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry_with_rows(tmp_path, [_state_gate_row()])
    _write_state(home, "sess-allow", "EXECUTING")
    proc = _run_adapter(
        _write_payload("Write", "sess-allow", "/work/project/module.py"),
        home,
        registry,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["permission"] == "allow"


def test_stop_block_becomes_followup_message(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry_with_rows(tmp_path, [_mock_stop_row()])
    proc = _run_adapter(
        {
            "hook_event_name": "stop",
            "session_id": "stop-sess",
            "loop_count": 0,
            "transcript_path": "",
            "cwd": "/work/project",
        },
        home,
        registry,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "followup_message" in out
    assert "mock stop obligation unmet" in out["followup_message"]
    assert "permission" not in out


def test_malformed_child_output_fail_open(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry_with_rows(tmp_path, [_mock_malformed_row(fail_closed=False)])
    proc = _run_adapter(
        _write_payload("Write", "sess-mal", "/work/project/module.py"),
        home,
        registry,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["permission"] == "allow"


def test_session_binding_maps_conversation_id(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agent_home = home / "agent-home"
    agent_home.mkdir()
    sessions_dir = agent_home / "state" / "cursor-hook-sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "cursor-conv-1.json").write_text(
        json.dumps({"agentctl_session": "bound-agentctl-session"}),
        encoding="utf-8",
    )
    _write_state(home, "bound-agentctl-session", "EXECUTING")
    registry = _registry_with_rows(tmp_path, [_state_gate_row()])
    proc = _run_adapter(
        {
            "hook_event_name": "preToolUse",
            "conversation_id": "cursor-conv-1",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/project/module.py"},
            "cwd": "/work/project",
        },
        home,
        registry,
        extra_env={"CLAUDE_AGENT_HOME": str(agent_home)},
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["permission"] == "allow"
