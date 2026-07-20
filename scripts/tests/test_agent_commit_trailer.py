"""agent_commit_trailer.py: Agent-Session/Agent-Task commit trailer helper.

Covers the pure `trailers()` logic (no session id, no state file, task-field
precedence, the no-goal-leak rule) and a LIVE end-to-end path: a throwaway
git repo carrying its own copy of the real githooks/commit-msg hook, a fake
agentctl state file, and CLAUDE_CODE_SESSION_ID set — proving the hook
injects the trailer into a real commit, that amend does not duplicate it,
and that a human commit (no session env) gets no trailer at all.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
MODULE_PATH = SCRIPTS_DIR / "agent_commit_trailer.py"

_SPEC = importlib.util.spec_from_file_location("agent_commit_trailer", str(MODULE_PATH))
trailer_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = trailer_mod
_SPEC.loader.exec_module(trailer_mod)


def _write_state(config_dir: Path, session_id: str, data: dict) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# trailers() unit behavior
# ---------------------------------------------------------------------------

def test_no_session_id_yields_no_trailers(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert trailer_mod.trailers(session_id="") == []
    assert trailer_mod.trailers() == []  # falls back to the (unset) env var


def test_no_state_file_yields_no_trailers(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert trailer_mod.trailers(session_id="ghost-session") == []


def test_goal_only_state_yields_no_agent_task(monkeypatch, tmp_path):
    # No-goal-leak rule: Agent-Task must NEVER be derived from `goal` — it is
    # a free-text prompt that can carry private detail, and this trailer
    # lands in the public Core repo.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_state(tmp_path, "sess-goal-only", {"goal": "investigate a private incident"})
    assert trailer_mod.trailers(session_id="sess-goal-only") == ["Agent-Session: sess-goal-only"]


def test_tracker_key_preferred_over_task_id(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_state(tmp_path, "sess-both", {"task_id": "task-abc", "tracker_key": "ABC-123"})
    assert trailer_mod.trailers(session_id="sess-both") == [
        "Agent-Session: sess-both",
        "Agent-Task: ABC-123",
    ]


def test_task_id_used_when_no_tracker_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_state(tmp_path, "sess-taskid", {"task_id": "task-abc", "tracker_key": None})
    assert trailer_mod.trailers(session_id="sess-taskid") == [
        "Agent-Session: sess-taskid",
        "Agent-Task: task-abc",
    ]


def test_malformed_state_file_yields_no_trailers(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    state_dir = tmp_path / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "sess-bad.json").write_text("{not json", encoding="utf-8")
    assert trailer_mod.trailers(session_id="sess-bad") == []


# ---------------------------------------------------------------------------
# LIVE: real commit-msg hook injects the trailer into a real commit
# ---------------------------------------------------------------------------

def _install_throwaway_repo(tmp_path: Path) -> Path:
    """A self-contained git repo carrying its OWN copy of the real
    commit-msg hook plus the scripts it calls, so the hook's `git rev-parse
    --show-toplevel`-based path resolution stays entirely within tmp_path —
    this test never reads or writes the real Core repo's git state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    (repo / "githooks").mkdir()
    shutil.copy2(REPO_ROOT / "githooks" / "commit-msg", repo / "githooks" / "commit-msg")
    (repo / "githooks" / "commit-msg").chmod(0o755)

    (repo / "scripts").mkdir()
    for name in ("agent_commit_trailer.py", "verify-tests-accompany-code.py", "verify-self-improvement-edit.py"):
        shutil.copy2(SCRIPTS_DIR / name, repo / "scripts" / name)
    shutil.copytree(SCRIPTS_DIR / "lib", repo / "scripts" / "lib", ignore=shutil.ignore_patterns("__pycache__"))

    subprocess.run(["git", "config", "core.hooksPath", "githooks"], cwd=repo, check=True)
    return repo


def _commit(repo: Path, env: dict, amend: bool = False) -> "subprocess.CompletedProcess[str]":
    target = repo / "file.txt"
    target.write_text((target.read_text(encoding="utf-8") if target.exists() else "") + "x", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, env=env)
    args = ["git", "commit"] + (["--amend", "--no-edit"] if amend else ["-m", "test commit"])
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, env=env, timeout=15)


def _log_body(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_live_git_commit_gets_trailer_and_amend_is_idempotent(tmp_path):
    config_dir = tmp_path / "config"
    _write_state(config_dir, "LIVESESS", {"tracker_key": "LIVE-1", "task_id": "task-x"})
    repo = _install_throwaway_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_SESSION_ID": "LIVESESS",
        "HOME": str(home),
    }

    result = _commit(repo, env)
    assert result.returncode == 0, result.stderr
    log = _log_body(repo)
    assert "Agent-Session: LIVESESS" in log
    assert "Agent-Task: LIVE-1" in log

    amend_result = _commit(repo, env, amend=True)
    assert amend_result.returncode == 0, amend_result.stderr
    amended_log = _log_body(repo)
    assert amended_log.count("Agent-Session:") == 1
    assert amended_log.count("Agent-Task:") == 1


def test_live_git_commit_human_no_session_env_gets_no_trailer(tmp_path):
    repo = _install_throwaway_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)

    result = _commit(repo, env)
    assert result.returncode == 0, result.stderr
    assert "Agent-Session" not in _log_body(repo)
