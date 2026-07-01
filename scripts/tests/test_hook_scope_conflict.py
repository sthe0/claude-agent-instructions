"""hook-scope-conflict.py: PreToolUse deny/warn on a live cross-session scope
overlap. Driven end-to-end via subprocess with HOME pointed at a tmp tree so
DEFAULT_SCOPES_DIR (~/.claude/agentctl/scopes) resolves under tmp_path; other
sessions' scope records are seeded directly through registry.save with a fresh
heartbeat. No git/arc stubs are needed — the conflict hook never resolves a VCS,
it only reads the scope registry and reasons over paths.

pytest tmp_path resolves under /place/vartmp on this machine (not /tmp), so a
seeded .py path is NOT engine-exempt and is_gated_path treats it as a gated
(block-eligible) path — which is what the block-vs-warn split hinges on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from session_scope import registry

HOOK = Path(__file__).resolve().parent.parent / "hook-scope-conflict.py"
INSTALLER = Path(__file__).resolve().parent.parent / "install-reminder-hooks.sh"


def _scopes_dir(home: Path) -> Path:
    return home / ".claude" / "agentctl" / "scopes"


def _seed(home: Path, session_id: str, touched: "list[str]", heartbeat_ts: float,
          cwd: str = "/somewhere", repo_root: str = "/somewhere", vcs: str = "git") -> None:
    rec = registry.ScopeRecord(
        session_id=session_id,
        heartbeat_ts=heartbeat_ts,
        cwd=cwd,
        repo_root=repo_root,
        vcs=vcs,
        touched_paths=[os.path.realpath(p) for p in touched],
    )
    registry.save(_scopes_dir(home), rec)


def _payload(session_id: str, file_path: str, tool: str = "Edit") -> dict:
    return {
        "tool_name": tool,
        "session_id": session_id,
        "cwd": str(Path(file_path).parent),
        "tool_input": {"file_path": file_path},
    }


def run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _decision(stdout: str) -> "dict | None":
    try:
        return json.loads(stdout)["hookSpecificOutput"]
    except Exception:
        return None


def test_block_on_gated_path_held_by_other_live_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared.py"           # .py under /place/vartmp -> gated
    shared.write_text("x")

    _seed(home, "other", [str(shared)], heartbeat_ts=time.time())

    proc = run_hook(_payload("me", str(shared)), home)
    assert proc.returncode == 0
    out = _decision(proc.stdout)
    assert out is not None
    assert out["permissionDecision"] == "deny"
    assert "other" in out["permissionDecisionReason"]
    assert "session-isolate" in out["permissionDecisionReason"]


def test_warn_on_non_gated_path_held_by_other_live_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    notes = repo / "notes.txt"            # .txt -> not a production/gated file
    notes.write_text("x")

    _seed(home, "other", [str(notes)], heartbeat_ts=time.time())

    proc = run_hook(_payload("me", str(notes)), home)
    assert proc.returncode == 0
    assert _decision(proc.stdout) is None          # NOT a deny
    assert "[scope-conflict]" in proc.stdout
    assert "other" in proc.stdout


def test_silent_allow_single_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared.py"
    shared.write_text("x")
    # no other session seeded

    proc = run_hook(_payload("me", str(shared)), home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_silent_allow_distinct_worktrees(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_a = tmp_path / "repoA"
    repo_a.mkdir()
    repo_b = tmp_path / "repoB"
    repo_b.mkdir()
    (repo_a / "shared.py").write_text("x")
    mine = repo_b / "shared.py"
    mine.write_text("x")

    # other session holds repoA/shared.py; I write repoB/shared.py — disjoint roots
    _seed(home, "other", [str(repo_a / "shared.py")], heartbeat_ts=time.time())

    proc = run_hook(_payload("me", str(mine)), home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_stale_other_session_ignored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared.py"
    shared.write_text("x")

    _seed(home, "other", [str(shared)], heartbeat_ts=0.0)   # long-dead heartbeat

    proc = run_hook(_payload("me", str(shared)), home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_own_session_is_never_a_conflict(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared.py"
    shared.write_text("x")

    _seed(home, "me", [str(shared)], heartbeat_ts=time.time())   # my OWN record

    proc = run_hook(_payload("me", str(shared)), home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_ancestor_directory_overlap_blocks(tmp_path):
    # other holds the directory root; I write a descendant file under it.
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    child = repo / "pkg" / "mod.py"
    child.parent.mkdir(parents=True)
    child.write_text("x")

    _seed(home, "other", [str(repo)], heartbeat_ts=time.time())

    proc = run_hook(_payload("me", str(child)), home)
    assert proc.returncode == 0
    out = _decision(proc.stdout)
    assert out is not None and out["permissionDecision"] == "deny"


def test_non_edit_write_tool_is_ignored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared.py"
    shared.write_text("x")
    _seed(home, "other", [str(shared)], heartbeat_ts=time.time())

    payload = {
        "tool_name": "Bash",
        "session_id": "me",
        "tool_input": {"command": f"echo {shared}"},
    }
    proc = run_hook(payload, home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_malformed_stdin_allows(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_installer_registers_conflict_hook_after_state_gate():
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"PreToolUse",       "Edit|Write", "hook-scope-conflict.py"' in text
    # Ordering: the conflict hook must be registered AFTER the plan-approval gate.
    assert text.index("hook-state-gate.py") < text.index("hook-scope-conflict.py")
