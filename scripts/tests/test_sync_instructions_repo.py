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


# ── Auto-migration to the isolated root on pull ───────────────────────────────

def _make_stub(path: Path, marker: Path) -> Path:
    """A stub launcher that records it was invoked by touching `marker`."""
    path.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\nexit 0\n')
    path.chmod(0o755)
    return path


def _seed_config_root_lib(repo: Path) -> None:
    """Copy the real config-root.sh into a test clone so the sourced detector
    (agent_legacy_inplace_layout) is defined — clones from make_bare_and_clone
    have no scripts/ tree of their own."""
    lib = repo / "scripts" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "config-root.sh").write_text(
        (SCRIPT.parent / "lib" / "config-root.sh").read_text()
    )


def _legacy_home(home: Path, repo: Path) -> None:
    """Fake the old in-place layout: ~/.claude/CLAUDE.md symlinked into the repo."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "CLAUDE.md").symlink_to(repo / "CLAUDE.md")


def _run_pull_with_stubs(repo, home, tmp_path, extra_env):
    _seed_config_root_lib(repo)
    migrate_marker = tmp_path / "migrate.called"
    setup_marker = tmp_path / "setup.called"
    migrate = _make_stub(tmp_path / "stub-migrate.sh", migrate_marker)
    setup = _make_stub(tmp_path / "stub-setup.sh", setup_marker)
    env = {
        **os.environ, **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(repo),
        "CLAUDE_MIGRATE_BIN": str(migrate),
        "SETUP_SYMLINKS_BIN": str(setup),
        **extra_env,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "pull"], env=env, capture_output=True, text=True
    )
    return result, migrate_marker, setup_marker


def test_pull_auto_migrates_when_interactive(tmp_path, home):
    """Interactive pull on a legacy layout auto-runs migrate + setup stubs."""
    origin, clone = make_bare_and_clone(tmp_path)
    _legacy_home(home, clone)

    result, migrate_marker, setup_marker = _run_pull_with_stubs(
        clone, home, tmp_path, {"CLAUDE_SYNC_FORCE_INTERACTIVE": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert migrate_marker.exists(), "migrate stub should run in interactive mode"
    assert setup_marker.exists(), "setup stub should run in interactive mode"
    assert "migrating to" in (result.stdout + result.stderr)


def test_pull_notifies_not_migrates_when_noninteractive(tmp_path, home):
    """Cron/headless pull on a legacy layout notifies but moves NOTHING."""
    origin, clone = make_bare_and_clone(tmp_path)
    _legacy_home(home, clone)

    result, migrate_marker, setup_marker = _run_pull_with_stubs(
        clone, home, tmp_path, {"CLAUDE_SYNC_NONINTERACTIVE": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert not migrate_marker.exists(), "migrate must NOT run unattended"
    assert not setup_marker.exists(), "setup must NOT run unattended"
    assert "ACTION NEEDED" in (result.stdout + result.stderr)


def test_pull_no_migration_when_no_legacy_layout(tmp_path, home):
    """Fresh isolated machine (no ~/.claude symlinks) → migration is a no-op."""
    origin, clone = make_bare_and_clone(tmp_path)
    # no _legacy_home: ~/.claude does not exist

    result, migrate_marker, setup_marker = _run_pull_with_stubs(
        clone, home, tmp_path, {"CLAUDE_SYNC_FORCE_INTERACTIVE": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert not migrate_marker.exists()
    assert not setup_marker.exists()
    _out = result.stdout + result.stderr
    assert "ACTION NEEDED" not in _out
    assert "migrating to" not in _out


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
