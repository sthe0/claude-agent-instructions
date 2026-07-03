"""hook-plan-delivery-gate.py: deny a plan-approval AskUserQuestion issued in the
same turn the plan was submitted (PLAN_READY node, plan_submitted_ts >=
last_user_prompt_ts) — the plan text cannot have rendered to the user yet.
Driven end-to-end via subprocess with CLAUDE_CONFIG_DIR pointed at a tmp tree so
the hook's real state-file resolution lands under tmp_path. The pure
gate_decision function is also unit-tested directly via an importlib load."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-plan-delivery-gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_plan_delivery_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(payload: dict, config_dir: Path) -> subprocess.CompletedProcess:
    # HOME also pinned to tmp_path: resolve_agentctl_state_file's legacy-root
    # fallback hardcodes Path.home() (not CLAUDE_CONFIG_DIR) — without this an
    # unset HOME falls back to the real user's home and the hook would read
    # ~/.claude/agentctl/state for a session-id collision with a real file.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def write_state(
    config_dir: Path,
    session_id: str,
    node: str,
    plan_submitted_ts: float | None = None,
    last_user_prompt_ts: float | None = None,
) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if plan_submitted_ts is not None:
        data["plan_submitted_ts"] = plan_submitted_ts
    if last_user_prompt_ts is not None:
        data["last_user_prompt_ts"] = last_user_prompt_ts
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


def ask_payload(session_id: str) -> dict:
    return {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "tool_input": {"questions": [{"question": "Approve the plan?", "options": []}]},
    }


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _deny_reason(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# --- end-to-end via subprocess -----------------------------------------------

def test_deny_when_plan_submitted_same_turn(tmp_path):
    write_state(tmp_path, "sess1", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess1"), tmp_path)
    assert proc.returncode == 0  # hook never raises / never exits non-zero
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "final" in reason.lower() and "plan" in reason.lower()


def test_deny_when_plan_submitted_after_prompt(tmp_path):
    # plan_submitted_ts > last_user_prompt_ts (later in the same turn) is still same-turn
    write_state(tmp_path, "sess2", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess2"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_allow_when_plan_submitted_earlier_turn(tmp_path):
    # a later prompt (new turn) advanced last_user_prompt_ts past plan_submitted_ts
    write_state(tmp_path, "sess3", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=105.0)
    proc = run_hook(ask_payload("sess3"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert proc.stdout.strip() == ""


def test_allow_when_node_not_plan_ready(tmp_path):
    write_state(tmp_path, "sess4", "EXECUTING", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess4"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_when_no_live_session(tmp_path):
    # session never ran `agentctl start` (no state file) -> no observable, allow
    proc = run_hook(ask_payload("unknown"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_when_timestamps_missing_legacy_state(tmp_path):
    # legacy state predating schema 10 has neither timestamp -> fail open
    write_state(tmp_path, "sess5", "PLAN_READY")
    proc = run_hook(ask_payload("sess5"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_non_ask_tool_is_ignored(tmp_path):
    write_state(tmp_path, "sess6", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    payload = {"tool_name": "Edit", "session_id": "sess6", "tool_input": {"file_path": "/x.py"}}
    proc = run_hook(payload, tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_corrupt_state_file_allows(tmp_path):
    state_dir = tmp_path / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sess7.json").write_text("{not valid json")
    proc = run_hook(ask_payload("sess7"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_malformed_stdin_allows(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="this is not json",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_session_id_allows(tmp_path):
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    proc = run_hook(payload, tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


# --- pure gate_decision, unit-tested directly --------------------------------

def test_gate_decision_pure_function():
    mod = _load_module()
    assert mod.gate_decision("PLAN_READY", 100.0, 100.0)[0] == "deny"
    assert mod.gate_decision("PLAN_READY", 105.0, 100.0)[0] == "deny"
    assert mod.gate_decision("PLAN_READY", 100.0, 105.0)[0] == "allow"
    assert mod.gate_decision("EXECUTING", 100.0, 100.0)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", None, 100.0)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", 100.0, None)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", None, None)[0] == "allow"


def test_load_gate_fields_missing_node_returns_none(tmp_path):
    mod = _load_module()
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"plan_submitted_ts": 1.0}))
    assert mod.load_gate_fields(p) is None


def test_load_gate_fields_unreadable_returns_none(tmp_path):
    mod = _load_module()
    p = tmp_path / "missing.json"
    assert mod.load_gate_fields(p) is None
