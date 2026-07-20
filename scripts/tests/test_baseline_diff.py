"""Tests for scripts/lib/baseline_diff.py — the reusable HEAD-baseline vs
working-tree violation-set diff.

Hermetic: every repo is a local `git init` in tmp_path. A tiny synthetic
finder scans a text file for lines containing a marker string, returning
(rel, lineno, line) tuples — the same shape as
verify-config-root-refs.py:find_unallowed — so the tests exercise the
primitive the way stage 4 will use it, without importing that verifier.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lib import baseline_diff

MARKER = "LEGACY"
TARGET = "notes.txt"

GIT_ENV_ARGS = (
    ("user.email", "test@example.com"),
    ("user.name", "Test"),
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, val in GIT_ENV_ARGS:
        _git(repo, "config", key, val)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _finder(repo_root: Path):
    """(rel, lineno, line) for every line containing MARKER under repo_root."""
    out = []
    target = Path(repo_root) / TARGET
    if not target.exists():
        return out
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if MARKER in line:
            out.append((TARGET, lineno, line))
    return out


def _key(item):
    rel, _lineno, line = item
    return (rel, line.strip())


# ── new_violations: pre-existing tolerated, new one reported ────────────────

def test_pre_existing_violation_tolerated(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, f"line one\n{MARKER} old one\n")
    _commit(repo, "seed with a pre-existing violation")

    # Unrelated clean change staged — no new violation introduced.
    _write(repo, "unrelated.txt", "hello\n")

    result = baseline_diff.new_violations(_finder, repo, key=_key)
    assert result == []


def test_new_violation_reported(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, f"line one\n{MARKER} old one\n")
    _commit(repo, "seed with a pre-existing violation")

    _write(repo, TARGET, f"line one\n{MARKER} old one\n{MARKER} brand new\n")

    result = baseline_diff.new_violations(_finder, repo, key=_key)
    assert len(result) == 1
    assert result[0] == (TARGET, 3, f"{MARKER} brand new")


def test_violation_in_untouched_file_tolerated(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, f"{MARKER} untouched\n")
    _write(repo, "other.txt", "clean\n")
    _commit(repo, "seed")

    # Working-tree change touches a DIFFERENT file only.
    _write(repo, "other.txt", "clean, modified\n")

    result = baseline_diff.new_violations(_finder, repo, key=_key)
    assert result == []


def test_line_number_drift_does_not_produce_false_new(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, f"{MARKER} stable violation\n")
    _commit(repo, "seed")

    # Insert blank lines above the unchanged violation, shifting its line number.
    _write(repo, TARGET, f"\n\n\n{MARKER} stable violation\n")

    result = baseline_diff.new_violations(_finder, repo, key=_key)
    assert result == []


# ── BaselineUnavailable degrade path ─────────────────────────────────────────

def test_unborn_head_degrades_to_full_current_list(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)  # no commit yet — unborn HEAD
    _write(repo, TARGET, f"{MARKER} only entry\n")

    notes = []
    result = baseline_diff.new_violations(_finder, repo, key=_key, on_degraded=notes.append)
    assert result == [(TARGET, 1, f"{MARKER} only entry")]
    assert len(notes) == 1


def test_not_a_git_worktree_degrades_to_full_current_list(tmp_path):
    repo = tmp_path / "plain_dir"
    repo.mkdir()
    _write(repo, TARGET, f"{MARKER} only entry\n")

    result = baseline_diff.new_violations(_finder, repo, key=_key)
    assert result == [(TARGET, 1, f"{MARKER} only entry")]


def test_baseline_worktree_raises_on_unborn_head(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(baseline_diff.BaselineUnavailable):
        with baseline_diff.baseline_worktree(repo):
            pass


# ── baseline_worktree: no mutation, no leftover worktree ────────────────────

def test_baseline_worktree_does_not_mutate_caller_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, "seed\n")
    _commit(repo, "seed")

    _write(repo, TARGET, "seed\nworking tree addition\n")

    with baseline_diff.baseline_worktree(repo) as baseline_root:
        assert (baseline_root / TARGET).read_text() == "seed\n"

    # Caller's working tree is untouched by the baseline checkout.
    assert (repo / TARGET).read_text() == "seed\nworking tree addition\n"


def test_baseline_worktree_cleaned_up(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, "seed\n")
    _commit(repo, "seed")

    with baseline_diff.baseline_worktree(repo) as baseline_root:
        assert baseline_root.exists()

    assert not baseline_root.exists()
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(baseline_root) not in listing


# ── GIT_INDEX_FILE leak: a hook context must not reset the caller's index ────

def _git_touching_finder(repo_root: Path):
    """A finder that shells out to git reading the INDEX (as
    verify-config-root-refs.py's `git ls-files --cached` does), then applies
    the same MARKER scan. Exercises BOTH the finder's git calls and
    baseline_worktree's `git worktree add` under the leaked env."""
    subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--cached"],
        capture_output=True,
        text=True,
    )
    return _finder(repo_root)


def test_git_index_file_leak_does_not_reset_caller_index(tmp_path, monkeypatch):
    """Reproduces the empty-commit defect: during a real `git commit`, git
    exports GIT_INDEX_FILE pointing at the very index being committed. Without
    scrubbing, new_violations' nested `git worktree add` inherits it and resets
    THAT index to HEAD — the caller's staged change vanishes. Assert the staged
    index is UNCHANGED and os.environ is restored afterward."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, TARGET, f"line one\n{MARKER} old one\n")
    _commit(repo, "seed with a pre-existing violation")

    # Stage a new change — this is what a real commit is about to record.
    _write(repo, "staged.txt", "staged content\n")
    _git(repo, "add", "staged.txt")
    staged_before = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "staged.txt" in staged_before

    # Emulate exactly what `git commit` exports to the pre-commit hook.
    index_path = str((repo / ".git" / "index").resolve())
    monkeypatch.setenv("GIT_INDEX_FILE", index_path)

    result = baseline_diff.new_violations(_git_touching_finder, repo, key=_key)

    # (a) The staged index is untouched — the staged file is still staged.
    staged_after = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "staged.txt" in staged_after, "new_violations reset the caller's staged index"
    # (b) os.environ is restored: the caller's GIT_INDEX_FILE survives the call.
    assert os.environ.get("GIT_INDEX_FILE") == index_path
    # And the primitive still works: no NEW MARKER violation was introduced.
    assert result == []


def test_clean_git_env_restores_all_vars(monkeypatch):
    """The scrub pops every leaking var on entry and restores each on exit,
    leaving os.environ byte-identical afterward."""
    monkeypatch.setenv("GIT_INDEX_FILE", "/some/index")
    monkeypatch.setenv("GIT_DIR", "/some/gitdir")
    monkeypatch.delenv("GIT_NAMESPACE", raising=False)
    with baseline_diff._clean_git_env():
        assert "GIT_INDEX_FILE" not in os.environ
        assert "GIT_DIR" not in os.environ
    assert os.environ["GIT_INDEX_FILE"] == "/some/index"
    assert os.environ["GIT_DIR"] == "/some/gitdir"
    assert "GIT_NAMESPACE" not in os.environ
