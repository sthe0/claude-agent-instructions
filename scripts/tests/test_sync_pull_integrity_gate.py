"""Tests for the post-pull integrity gate in sync-instructions-repo.sh.

The gate runs after a pull that APPLIED commits (the behind>0 reconcile path):
  - global layer: python3 $REPO/scripts/verify-all.py
    (env seam CLAUDE_VERIFY_ALL_BIN)
  - project layer: python3 $REPO/scripts/verify-leaf-structure.py --root
    <proj>/.claude/agent-memory, when the invocation dir (or an ancestor) holds
    .claude/agent-memory/MEMORY.md and that dir is not under $REPO
    (env seam CLAUDE_VERIFY_LEAF_BIN).

Fail-open: a failing check emits a WARN but the pull still returns 0. The gate
must NOT run on an up-to-date pull (behind==0 early return).

Each test builds a throwaway bare "remote" + a clone that is one commit behind,
then runs the real script with the verify entrypoints stubbed via the env seams.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sync-instructions-repo.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repos(tmp_path: Path) -> Path:
    """Create a bare 'remote' with two commits and a clone that is 1 behind.

    Returns the clone path (the $REPO under test)."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir()
    seed.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c1")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)],
                   check=True, capture_output=True, text=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")

    # advance the remote so the clone is one commit behind
    (seed / "b.txt").write_text("2\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c2")
    _git(seed, "push", "origin", "main")
    return clone


def _stub(path: Path, rc: int) -> Path:
    path.write_text(f"import sys\nsys.exit({rc})\n")
    return path


def _run_pull(clone: Path, cwd: Path, tmp_path: Path, **env_extra: str):
    env = dict(os.environ)
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(clone)
    env["CLAUDE_SYNC_NONINTERACTIVE"] = "1"
    env["HOME"] = str(tmp_path)  # keep the sync log hermetic under tmp
    env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT), "pull"], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


class TestPullIntegrityGate:
    def test_global_check_runs_and_is_fail_open(self, tmp_path):
        clone = _make_repos(tmp_path)
        va = _stub(tmp_path / "va.py", 1)  # global check FAILS
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_VERIFY_ALL_BIN=str(va))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out            # fail-open: pull still succeeds
        assert "pull: done" in out
        assert "WARN global integrity" in out

    def test_global_check_ok_logs_ok(self, tmp_path):
        clone = _make_repos(tmp_path)
        va = _stub(tmp_path / "va.py", 0)
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_VERIFY_ALL_BIN=str(va))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "integrity OK (global" in out

    def test_up_to_date_pull_skips_the_gate(self, tmp_path):
        clone = _make_repos(tmp_path)
        va = _stub(tmp_path / "va.py", 1)
        # first pull applies the commit and runs the gate
        _run_pull(clone, tmp_path, tmp_path, CLAUDE_VERIFY_ALL_BIN=str(va))
        # second pull: behind==0 -> gate must NOT run
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_VERIFY_ALL_BIN=str(va))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "already up to date" in out
        assert "integrity" not in out

    def test_project_check_runs_from_a_subdirectory(self, tmp_path):
        clone = _make_repos(tmp_path)
        va = _stub(tmp_path / "va.py", 0)
        vl = _stub(tmp_path / "vl.py", 1)  # project check FAILS
        proj = tmp_path / "proj"
        (proj / ".claude" / "agent-memory").mkdir(parents=True)
        (proj / ".claude" / "agent-memory" / "MEMORY.md").write_text("# idx\n")
        deep = proj / "src" / "deep"
        deep.mkdir(parents=True)
        # invoked from a subdir -> walk-up must still find the project root
        r = _run_pull(clone, deep, tmp_path,
                      CLAUDE_VERIFY_ALL_BIN=str(va), CLAUDE_VERIFY_LEAF_BIN=str(vl))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out            # fail-open
        assert "WARN project integrity" in out

    def test_project_check_skipped_without_agent_memory(self, tmp_path):
        clone = _make_repos(tmp_path)
        va = _stub(tmp_path / "va.py", 0)
        vl = _stub(tmp_path / "vl.py", 1)
        plain = tmp_path / "plain"
        plain.mkdir()
        r = _run_pull(clone, plain, tmp_path,
                      CLAUDE_VERIFY_ALL_BIN=str(va), CLAUDE_VERIFY_LEAF_BIN=str(vl))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "project integrity" not in out
