"""Tests for hook-guard-serving-checkout-offmain.py — deny edits/commits in the
serving/PRIMARY Core checkout while it is off the default branch.

Hermetic: every repo is a local `git init` in tmp_path; the hook is invoked as a
subprocess with a JSON payload on stdin (mirrors test_hook_instructions_refresh.py).
CLAUDE_INSTRUCTIONS_REPO points the hook at the fixture "core" repo.

Covers: primary off-main deny (Edit + git commit), and the fail-open ALLOW cases —
on-main, linked worktree, path outside the Core repo, non-git path, detached HEAD,
memory-global/ write, and an unrelated Bash command that merely mentions 'git commit'.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-serving-checkout-offmain.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def make_core(tmp_path: Path) -> Path:
    """A fresh git repo on `main` with a scripts/ dir and a memory-global/ leaf."""
    core = tmp_path / "core"
    core.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=core)
    (core / "README.md").write_text("seed\n")
    (core / "scripts").mkdir()
    (core / "scripts" / "existing.py").write_text("x = 1\n")
    leaves = core / "memory-global" / "leaves"
    leaves.mkdir(parents=True)
    (leaves / "note.md").write_text("leaf\n")
    git("add", "-A", cwd=core)
    git("commit", "--quiet", "-m", "seed", cwd=core)
    return core


def off_main(core: Path, name: str = "feat/x") -> None:
    git("switch", "--quiet", "-c", name, cwd=core)


def run_hook(core: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, "CLAUDE_INSTRUCTIONS_REPO": str(core)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )


def _denied(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout


def _allowed(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and proc.stdout.strip() == ""


# (a) primary off-main + Edit under it -> DENY
def test_primary_offmain_edit_denies(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(core / "scripts" / "existing.py")},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout
    assert "git worktree add" in proc.stdout
    assert "feat/x" in proc.stdout


# (b) primary on-main + Edit -> ALLOW
def test_primary_on_main_edit_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(core / "scripts" / "new_file.py")},
        "cwd": str(core),
    })
    assert _allowed(proc), proc.stdout


# (c) linked worktree off-main + Edit -> ALLOW
def test_linked_worktree_offmain_edit_allows(tmp_path):
    core = make_core(tmp_path)
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "wt-branch", str(wt), "main", cwd=core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(wt / "scripts" / "existing.py")},
        "cwd": str(wt),
    })
    assert _allowed(proc), proc.stdout


# (d) file outside the Core repo -> ALLOW
def test_file_outside_core_repo_allows(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    other = tmp_path / "other"
    other.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=other)
    (other / "f.py").write_text("y\n")
    git("add", "-A", cwd=other)
    git("commit", "--quiet", "-m", "s", cwd=other)
    off_main(other, "feat/y")
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(other / "f.py")},
        "cwd": str(other),
    })
    assert _allowed(proc), proc.stdout


# (e) non-git path -> ALLOW (fail-open)
def test_non_git_path_allows(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(plain / "z.txt")},
        "cwd": str(plain),
    })
    assert _allowed(proc), proc.stdout


# (f) Bash `git commit` in primary off-main -> DENY
def test_bash_git_commit_offmain_denies(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    proc = run_hook(core, {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'wip'"},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


# (g) unrelated Bash mentioning 'git commit' -> ALLOW
def test_bash_unrelated_mentioning_git_commit_allows(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    for cmd in ('echo "run git commit later"', 'git log --grep="git commit"'):
        proc = run_hook(core, {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(core),
        })
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


# (h) detached HEAD in primary + Edit -> ALLOW (fail-open)
def test_detached_head_allows(tmp_path):
    core = make_core(tmp_path)
    git("checkout", "--quiet", "--detach", "HEAD", cwd=core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(core / "scripts" / "existing.py")},
        "cwd": str(core),
    })
    assert _allowed(proc), proc.stdout


# (i) Edit under memory-global/ while primary off-main -> ALLOW (memory-exempt)
def test_memory_global_write_offmain_allows(tmp_path):
    core = make_core(tmp_path)
    off_main(core)
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(core / "memory-global" / "leaves" / "new-leaf.md")},
        "cwd": str(core),
    })
    assert _allowed(proc), proc.stdout
