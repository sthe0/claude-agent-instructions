"""Unit coverage for lib.git_cwd.effective_git_cwd — the shared resolver for the
tree a VCS commit command actually targets (extracted from
hook-guard-canon-readonly.py, #44). Both redirect branches (`git -C <dir>` and a
leading `cd <dir>`) plus the fallback-to-payload_cwd path are covered so the
extraction stays behavior-identical to the original."""
from __future__ import annotations

import os

from lib import git_cwd


def test_cd_redirect_absolute():
    assert git_cwd.effective_git_cwd("cd /repo/b && git commit -m x", "/repo/a") == "/repo/b"


def test_cd_redirect_relative_resolved_against_payload_cwd():
    assert git_cwd.effective_git_cwd("cd sub && git commit -m x", "/repo/a") == os.path.join("/repo/a", "sub")


def test_dash_c_absolute():
    assert git_cwd.effective_git_cwd("git -C /repo/b commit -m x", "/repo/a") == "/repo/b"


def test_dash_c_relative_resolved_against_payload_cwd():
    assert git_cwd.effective_git_cwd("git -C sub commit -m x", "/repo/a") == os.path.join("/repo/a", "sub")


def test_bare_commit_no_redirect_returns_payload_cwd():
    assert git_cwd.effective_git_cwd("git commit -m x", "/repo/a") == "/repo/a"


def test_non_git_vcs_commit_with_cd_redirect():
    # the leading-cd branch is command-agnostic, so it resolves for any VCS
    assert git_cwd.effective_git_cwd("cd /repo/b && othervcs commit -m x", "/repo/a") == "/repo/b"


def test_dash_c_before_non_commit_verb_does_not_redirect():
    # the -C scan requires tokens[i+3] == "commit"; `status` must not redirect
    assert git_cwd.effective_git_cwd("git -C /repo/b status", "/repo/a") == "/repo/a"


def test_unparseable_command_falls_back_to_payload_cwd():
    # an unbalanced quote makes shlex.split raise -> fallback, never a wilder guess
    assert git_cwd.effective_git_cwd('git commit -m "unterminated', "/repo/a") == "/repo/a"


def test_leading_cd_missing_dir_semicolon_does_not_relocate(tmp_path):
    # the leading `cd` fails at runtime; `;` runs the next segment unconditionally
    # in payload_cwd anyway, so the redirect must NOT be honored
    missing = tmp_path / "does-not-exist"
    payload_cwd = str(tmp_path)
    assert git_cwd.effective_git_cwd(f"cd {missing} ; echo b > s2", payload_cwd) == payload_cwd


def test_leading_cd_to_existing_dir_still_relocates(tmp_path):
    # the `cd` succeeds, so the redirect is honored exactly as for `&&` — the
    # safe-shape check only ever narrows the `;` case, never the `&&` one
    target = tmp_path / "exists"
    target.mkdir()
    assert git_cwd.effective_git_cwd(f"cd {target} ; echo b > s2", str(tmp_path)) == str(target)


def test_git_C_wins_over_failed_leading_cd(tmp_path):
    # precedence: the `-C <dir> commit` scan must win even when a leading `cd`
    # to a missing dir would otherwise be recognized as the safe no-relocate shape
    missing = tmp_path / "does-not-exist"
    worktree = tmp_path / "worktree"
    assert git_cwd.effective_git_cwd(
        f"cd {missing} ; git -C {worktree} commit -m x", str(tmp_path)
    ) == str(worktree)
