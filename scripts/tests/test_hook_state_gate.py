"""hook-state-gate.py: weight-aware deny of production Edit/Write unless the agentctl
session has passed the gate appropriate to its weight class. Driven end-to-end via
subprocess with HOME pointed at a tmp tree so the hook's real STATE_ROOT
(~/.claude/agentctl/state) resolves under tmp_path. The pure gate_decision is also
unit-tested directly via an importlib load of the hook module."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hook-state-gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_state_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def write_state(home: Path, session_id: str, node: str, weight_class: str | None = None) -> None:
    state_dir = home / ".claude" / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if weight_class is not None:
        data["weight_class"] = weight_class
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


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


def _deny_reason(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_deny_on_production_edit_when_not_executing(tmp_path):
    write_state(tmp_path, "sess1", "PLAN_READY", weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload("sess1", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0  # hook never raises / never exits non-zero
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "PLAN_READY" in reason and "approve" in reason.lower()


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
    # a non-production extension (.txt) is never gated regardless of node
    write_state(tmp_path, "sess3", "PLAN_READY")
    proc = run_hook(edit_payload("sess3", "/work/project/NOTES.txt"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_deny_instructions_repo_path_when_not_executing(tmp_path):
    # the agent's own config/instructions now flow through the spine — no longer exempt
    write_state(tmp_path, "sess4", "PLAN_READY", weight_class="SUBSTANTIVE")
    proc = run_hook(
        edit_payload("sess4", "/home/u/claude-agent-instructions/scripts/x.py"), tmp_path
    )
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_deny_claude_md_when_not_executing(tmp_path):
    # CLAUDE.md is .md and gated — the headline behavior of uniform gating
    write_state(tmp_path, "sess4b", "PLAN_READY", weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload("sess4b", "/home/u/.claude/CLAUDE.md"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_allow_memory_edit_even_when_not_executing(tmp_path):
    # memory (all three scopes) is the only state-changing write left exempt
    write_state(tmp_path, "sess4c", "PLAN_READY", weight_class="SUBSTANTIVE")
    for p in (
        "/home/u/.claude/memory-global/leaves/foo.md",
        "/home/u/proj/.claude/agent-memory/MEMORY.md",
        "/home/u/.claude/projects/abc/memory/leaves/bar.md",
    ):
        proc = run_hook(edit_payload("sess4c", p), tmp_path)
        assert proc.returncode == 0, p
        assert not _is_deny(proc), p


def test_allow_plan_artifact_at_planning_position(tmp_path):
    # a plan is the result-image of active planning: writable at planning-position
    # nodes (here PLANNING) even though SUBSTANTIVE prod edits are still gated
    write_state(tmp_path, "sess4d", "PLANNING", weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload("sess4d", "/home/u/.claude/plans/task.md"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


@pytest.mark.parametrize("node", ["CLASSIFIED", "ROUTED", "PLANNING", "PLAN_READY"])
def test_allow_plan_at_every_planning_position_node(tmp_path, node):
    write_state(tmp_path, f"planok-{node}", node, weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload(f"planok-{node}", "/home/u/.claude/plans/task.md"), tmp_path)
    assert proc.returncode == 0, node
    assert not _is_deny(proc), node


@pytest.mark.parametrize("node", ["APPROVED", "DECOMPOSED", "EXECUTING", "VERIFYING", "RESOLUTION", "RESOLVED"])
def test_deny_plan_outside_planning_position(tmp_path, node):
    # changing a plan once past the planning position is a difficulty to overcome
    # reflexively (replan / overcome-difficulty), not an in-place edit — even at
    # EXECUTING (which is in ALLOW_NODES for ordinary prod files)
    write_state(tmp_path, f"planno-{node}", node, weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload(f"planno-{node}", "/home/u/.claude/plans/task.md"), tmp_path)
    assert proc.returncode == 0, node
    assert _is_deny(proc), node
    assert "replan" in _deny_reason(proc).lower()


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


# --- gate_decision pure-function table ------------------------------------

@pytest.mark.parametrize(
    "weight_class, node, expected, needle",
    [
        (None, "CLASSIFIED", "deny", "classify"),
        ("CHAT", "ROUTED", "allow", ""),
        ("SMALL_CHANGE", "ROUTED", "deny", "next-stage"),
        ("SMALL_CHANGE", "EXECUTING", "allow", ""),
        ("SUBSTANTIVE", "PLAN_READY", "deny", "approv"),
        ("SUBSTANTIVE", "APPROVED", "deny", "approv"),
        ("SUBSTANTIVE", "EXECUTING", "allow", ""),
        ("SUBSTANTIVE", "RESOLVED", "deny", "reset"),
        (None, "RESOLVED", "deny", "reset"),
    ],
)
def test_gate_decision_rows(weight_class, node, expected, needle):
    mod = _load_module()
    decision, reason = mod.gate_decision(weight_class, node)
    assert decision == expected
    if needle:
        assert needle in reason.lower()


# --- gate_decision with is_plan=True: the node-aware plan rule overrides ----

@pytest.mark.parametrize(
    "node, expected",
    [
        ("CLASSIFIED", "allow"),
        ("ROUTED", "allow"),
        ("PLANNING", "allow"),
        ("PLAN_READY", "allow"),
        ("APPROVED", "deny"),
        ("DECOMPOSED", "deny"),
        ("EXECUTING", "deny"),   # in ALLOW_NODES for prod files, but denied for plans
        ("VERIFYING", "deny"),
        ("RESOLUTION", "deny"),
        ("RESOLVED", "deny"),
        ("BLOCKED", "deny"),
    ],
)
def test_gate_decision_plan_rows(node, expected):
    mod = _load_module()
    decision, reason = mod.gate_decision("SUBSTANTIVE", node, is_plan=True)
    assert decision == expected, node
    if expected == "deny":
        assert "replan" in reason.lower()


# --- a few end-to-end rows through stdin + state file ----------------------

def test_e2e_unclassified_denies_with_classify_hint(tmp_path):
    write_state(tmp_path, "u1", "CLASSIFIED", weight_class=None)
    proc = run_hook(edit_payload("u1", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    assert "classify" in _deny_reason(proc).lower()


def test_e2e_small_change_routed_denies_with_next_stage(tmp_path):
    write_state(tmp_path, "u2", "ROUTED", weight_class="SMALL_CHANGE")
    proc = run_hook(edit_payload("u2", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    assert "next-stage" in _deny_reason(proc).lower()


def test_e2e_small_change_executing_allows(tmp_path):
    write_state(tmp_path, "u3", "EXECUTING", weight_class="SMALL_CHANGE")
    proc = run_hook(edit_payload("u3", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_e2e_chat_routed_allows(tmp_path):
    write_state(tmp_path, "u4", "ROUTED", weight_class="CHAT")
    proc = run_hook(edit_payload("u4", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_e2e_resolved_denies_with_reset(tmp_path):
    write_state(tmp_path, "u5", "RESOLVED", weight_class="SUBSTANTIVE")
    proc = run_hook(edit_payload("u5", "/work/project/module.py"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    assert "reset" in _deny_reason(proc).lower()
