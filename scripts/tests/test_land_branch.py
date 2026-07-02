"""Tests for land-branch.py — the guarded, ref-only branch-landing primitive.

Exercises both the pure landability logic (assess()) and the CLI end to end
against throwaway local repos (a bare "origin" + a clone), the same fixture
shape used by test_sync_instructions_repo.py and test-land-on-main.sh. No
real network: "origin" is a bare repo on local disk.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "land-branch.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _load():
    spec = importlib.util.spec_from_file_location("land_branch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass() needs the module registered
    spec.loader.exec_module(mod)
    return mod


land_branch = _load()


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def run_cli(*args, cwd):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
    )


def snapshot(repo: Path) -> str:
    return "branch={} head={} status={}".format(
        git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip(),
        git("rev-parse", "HEAD", cwd=repo).stdout.strip(),
        git("status", "--porcelain", cwd=repo).stdout.strip(),
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


def make_feature_branch(clone: Path, name: str = "feature") -> str:
    """Branches off main and adds one commit; returns the branch's tip sha."""
    git("checkout", "--quiet", "-b", name, cwd=clone)
    (clone / "feature.txt").write_text(f"{name} work\n")
    git("add", "feature.txt", cwd=clone)
    git("commit", "--quiet", "-m", f"{name}: work", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone).stdout.strip()


def diverge_main(clone: Path) -> None:
    """Adds a commit on main (in the clone) unrelated to the feature branch,
    so main is no longer an ancestor of the feature branch tip."""
    cur = git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).stdout.strip()
    git("checkout", "--quiet", "main", cwd=clone)
    (clone / "main-only.txt").write_text("main-only work\n")
    git("add", "main-only.txt", cwd=clone)
    git("commit", "--quiet", "-m", "main: diverged work", cwd=clone)
    git("checkout", "--quiet", cur, cwd=clone)


# ── assess() — pure landability logic ─────────────────────────────────────

def test_assess_clean_ff_is_landable(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    sha = make_feature_branch(clone)

    a = land_branch.assess(clone, "feature", "main", "origin")

    assert a.ok, a.reason
    assert a.branch_sha == sha
    assert a.commands() == [
        "git push origin feature:main",
        "git push origin --delete feature",
        f"git branch -f main {sha}",
    ]


def test_assess_non_ff_refuses(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    make_feature_branch(clone)
    diverge_main(clone)

    a = land_branch.assess(clone, "feature", "main", "origin")

    assert not a.ok
    assert "non-fast-forward" in a.reason
    assert a.commands() == []


def test_assess_branch_equals_trunk_refuses(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)

    a = land_branch.assess(clone, "main", "main", "origin")

    assert not a.ok
    assert "equals trunk" in a.reason


def test_assess_detached_head_refuses(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    git("checkout", "--quiet", "--detach", "HEAD", cwd=clone)

    a = land_branch.assess(clone, None, "main", "origin")

    assert not a.ok
    assert "detached" in a.reason


def test_assess_already_up_to_date_refuses(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    git("branch", "--quiet", "feature", "main", cwd=clone)

    a = land_branch.assess(clone, "feature", "main", "origin")

    assert not a.ok
    assert "nothing to land" in a.reason


# ── CLI: --check (zero side effects) ──────────────────────────────────────

def test_check_reports_landable_and_commands(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    sha = make_feature_branch(clone)
    before = snapshot(clone)

    result = run_cli("--check", "--branch", "feature", "--trunk", "main", cwd=clone)

    assert result.returncode == 0, result.stderr
    assert "LANDABLE" in result.stdout
    assert f"git branch -f main {sha}" in result.stdout
    assert snapshot(clone) == before


def test_check_reports_not_landable_on_non_ff(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    make_feature_branch(clone)
    diverge_main(clone)
    before = snapshot(clone)

    result = run_cli("--check", "--branch", "feature", "--trunk", "main", cwd=clone)

    assert result.returncode == 2
    assert "NOT-LANDABLE" in result.stdout
    assert "non-fast-forward" in result.stdout
    assert snapshot(clone) == before


def test_check_detached_head_not_landable(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)
    make_feature_branch(clone)
    git("checkout", "--quiet", "--detach", "HEAD", cwd=clone)
    before = snapshot(clone)

    result = run_cli("--check", "--trunk", "main", cwd=clone)

    assert result.returncode == 2
    assert "detached" in result.stdout
    assert snapshot(clone) == before


# ── CLI: land mode ─────────────────────────────────────────────────────────

def test_land_advances_trunk_and_cleans_up(tmp_path):
    origin, clone = make_bare_and_clone(tmp_path)
    sha = make_feature_branch(clone)
    git("push", "--quiet", "-u", "origin", "feature", cwd=clone)
    before = snapshot(clone)

    result = run_cli("--branch", "feature", "--trunk", "main", cwd=clone)

    assert result.returncode == 0, result.stderr

    # Remote trunk advanced to the branch tip.
    assert git("rev-parse", "refs/heads/main", cwd=origin).stdout.strip() == sha
    # Remote branch is gone.
    remote_branches = git("branch", "-a", cwd=origin).stdout
    assert "feature" not in remote_branches
    # Local trunk fast-forwarded.
    assert git("rev-parse", "main", cwd=clone).stdout.strip() == sha

    # Caller's checked-out branch, HEAD, and working tree are untouched
    # except for the local trunk ref, which is not what's checked out here.
    after = snapshot(clone)
    assert after == before


def test_land_refuses_non_ff_without_side_effects(tmp_path):
    origin, clone = make_bare_and_clone(tmp_path)
    make_feature_branch(clone)
    diverge_main(clone)
    git("push", "--quiet", "-u", "origin", "feature", cwd=clone)
    origin_main_before = git("rev-parse", "refs/heads/main", cwd=origin).stdout.strip()
    before = snapshot(clone)

    result = run_cli("--branch", "feature", "--trunk", "main", cwd=clone)

    assert result.returncode == 2
    assert git("rev-parse", "refs/heads/main", cwd=origin).stdout.strip() == origin_main_before
    remote_branches = git("branch", "-a", cwd=origin).stdout
    assert "feature" in remote_branches
    assert snapshot(clone) == before


def test_land_refuses_branch_equals_trunk(tmp_path):
    _, clone = make_bare_and_clone(tmp_path)

    result = run_cli("--branch", "main", "--trunk", "main", cwd=clone)

    assert result.returncode == 2
    assert "NOT-LANDABLE" in result.stderr


def test_land_never_checks_out_or_resets(tmp_path):
    """Landing from the feature branch itself must not switch HEAD or touch
    the working tree — only refs move."""
    origin, clone = make_bare_and_clone(tmp_path)
    sha = make_feature_branch(clone)
    git("push", "--quiet", "-u", "origin", "feature", cwd=clone)
    (clone / "untracked.txt").write_text("scratch\n")

    result = run_cli("--branch", "feature", "--trunk", "main", cwd=clone)

    assert result.returncode == 0, result.stderr
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).stdout.strip() == "feature"
    assert git("rev-parse", "HEAD", cwd=clone).stdout.strip() == sha
    assert (clone / "untracked.txt").exists()
