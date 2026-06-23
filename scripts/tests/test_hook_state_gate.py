"""hook-state-gate.py: deny production Edit/Write unless the agentctl session is
in EXECUTING. Driven end-to-end via subprocess with HOME pointed at a tmp tree so
the hook's real STATE_ROOT (~/.claude/agentctl/state) resolves under tmp_path."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-state-gate.py"


def run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def write_state(home: Path, session_id: str, node: str) -> None:
    state_dir = home / ".claude" / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps({"node": node}))


def edit_payload(session_id: str, file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": session_id,
        "tool_input": {"file_path": file_path},
    }


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_deny_on_production_edit_when_not_executing(tmp_path):
    write_state(tmp_path, "sess1", "PLAN_READY")
    proc = run_hook(edit_payload("sess1", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0  # hook never raises / never exits non-zero
    assert _is_deny(proc)
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "PLAN_READY" in reason and "EXECUTING" in reason


def test_allow_when_executing(tmp_path):
    write_state(tmp_path, "sess2", "EXECUTING")
    proc = run_hook(edit_payload("sess2", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert proc.stdout.strip() == ""


def test_allow_when_no_state_file(tmp_path):
    # session never ran `agentctl start` -> fall back to prose, do not block
    proc = run_hook(edit_payload("unknown", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_non_production_path_even_when_not_executing(tmp_path):
    write_state(tmp_path, "sess3", "PLAN_READY")
    proc = run_hook(edit_payload("sess3", "/work/project/NOTES.md"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_instructions_repo_path_even_when_not_executing(tmp_path):
    # the skip set (claude-agent-instructions / tmp / .claude / memory) is meta-work
    write_state(tmp_path, "sess4", "PLAN_READY")
    proc = run_hook(
        edit_payload("sess4", "/home/u/claude-agent-instructions/scripts/x.py"), tmp_path
    )
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_non_edit_tool_is_ignored(tmp_path):
    write_state(tmp_path, "sess5", "PLAN_READY")
    payload = {"tool_name": "Bash", "session_id": "sess5",
               "tool_input": {"command": "rm -rf /"}}
    proc = run_hook(payload, tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_corrupt_state_file_allows(tmp_path):
    state_dir = tmp_path / ".claude" / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sess6.json").write_text("{not valid json")
    proc = run_hook(edit_payload("sess6", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_malformed_stdin_allows(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="this is not json",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
