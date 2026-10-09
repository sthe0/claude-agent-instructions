"""The orphan-worktree sweep is now the `git-worktrees` reaper; this pins the compatibility shim.

The worktree rules are tested in test_reaper_git_worktrees.py and the shared runner rules in
test_reaper_runner.py. What stays here is the one contract the shim owes machine-local
settings that still name `hook-orphan-worktree-sweep.py`: it runs the whole reaper runner
with the same flags.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SHIM = SCRIPTS / "hook-orphan-worktree-sweep.py"


def _run(args, tmp_path):
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path), CLAUDE_CONFIG_DIR=str(tmp_path / "config"),
        CLAUDE_REAPER_PLUGIN_DIR=str(tmp_path / "plugins"), CLAUDE_PROJECT_DIR=str(tmp_path / "project"),
    )
    return subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, env=env)


def test_shim_accepts_the_old_flags_and_runs_the_runner(tmp_path):
    out = _run(["--list"], tmp_path)
    assert out.returncode == 0
    assert any(line.startswith("git-worktrees builtin ") for line in out.stdout.splitlines())


def test_shim_dry_run_prints_only_verdict_lines_and_exits_zero(tmp_path):
    out = _run(["--dry-run"], tmp_path)
    assert out.returncode == 0
    assert all(line.startswith(("git-worktrees REMOVE ", "git-worktrees KEEP ")) for line in out.stdout.splitlines())
