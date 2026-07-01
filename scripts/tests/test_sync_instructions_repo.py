"""Regression tests for sync-instructions-repo.sh — branch-aware pull/push.

Guards against the bug where a hardcoded BRANCH=main default made cmd_pull
rebase a feature branch's local commits onto origin/main instead of its own
upstream (origin/<branch>), silently rewriting history and diverging from
origin/<branch>.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "sync-instructions-repo.sh"

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


def make_bare_and_clone(tmp_path: Path):
    """Bare 'origin' repo seeded with one commit on main, plus a clone of it."""
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / "seed"
    git("clone", "--quiet", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=seed)
    git("commit", "--quiet", "-m", "seed: initial content", cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    return origin, clone


def advance_main(tmp_path: Path, origin: Path, subject: str):
    """Push one unrelated commit to origin/main via a throwaway clone."""
    other = tmp_path / f"other-{subject.replace(' ', '_').replace(':', '')}"
    git("clone", "--quiet", "-b", "main", str(origin), str(other), cwd=tmp_path)
    (other / "ADVANCE.md").write_text(f"{subject}\n")
    git("add", "ADVANCE.md", cwd=other)
    git("commit", "--quiet", "-m", subject, cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)


def run_script(repo: Path, cmd: str, home: Path):
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(repo),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), cmd], env=env, capture_output=True, text=True
    )


def branch(repo: Path) -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()


def log_subjects(repo: Path, ref: str = "HEAD") -> list:
    return git("log", "--format=%s", ref, cwd=repo).stdout.splitlines()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_pull_reconciles_own_branch_not_main(tmp_path, home):
    """Feature-branch pull must reconcile against its own upstream
    (origin/<branch>), not a hardcoded origin/main."""
    origin, clone = make_bare_and_clone(tmp_path)

    git("checkout", "--quiet", "-b", "feature", cwd=clone)
    git("push", "--quiet", "-u", "origin", "feature", cwd=clone)

    (clone / "feature.txt").write_text("feature work\n")
    git("add", "feature.txt", cwd=clone)
    git("commit", "--quiet", "-m", "feature: local work", cwd=clone)

    advance_main(tmp_path, origin, "main: unrelated advance")

    result = run_script(clone, "pull", home)
    assert result.returncode == 0, result.stderr

    subjects = log_subjects(clone)
    assert "feature: local work" in subjects
    assert "main: unrelated advance" not in subjects
    assert branch(clone) == "feature"


def test_pull_main_still_fast_forwards(tmp_path, home):
    origin, clone = make_bare_and_clone(tmp_path)
    assert branch(clone) == "main"

    advance_main(tmp_path, origin, "main: advance")

    result = run_script(clone, "pull", home)
    assert result.returncode == 0, result.stderr
    assert "main: advance" in log_subjects(clone)


def test_pull_detached_head_targets_main(tmp_path, home):
    origin, clone = make_bare_and_clone(tmp_path)
    git("checkout", "--quiet", "--detach", "HEAD", cwd=clone)
    assert branch(clone) == "HEAD"

    advance_main(tmp_path, origin, "main: advance while detached")

    result = run_script(clone, "pull", home)
    assert result.returncode == 0, result.stderr
    assert "main: advance while detached" in log_subjects(clone)


def test_push_publishes_new_branch_via_dash_u(tmp_path, home):
    origin, clone = make_bare_and_clone(tmp_path)
    git("checkout", "--quiet", "-b", "new-feature", cwd=clone)
    (clone / "new.txt").write_text("new branch work\n")
    git("add", "new.txt", cwd=clone)
    git("commit", "--quiet", "-m", "new-feature: first commit", cwd=clone)

    before = git("ls-remote", "--heads", str(origin), cwd=clone).stdout
    assert "new-feature" not in before

    result = run_script(clone, "push", home)
    assert result.returncode == 0, result.stderr

    after = git("ls-remote", "--heads", str(origin), cwd=clone).stdout
    assert "refs/heads/new-feature" in after
    local_head = git("rev-parse", "new-feature", cwd=clone).stdout.strip()
    assert local_head in after
