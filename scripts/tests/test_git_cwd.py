"""Unit coverage for lib.git_cwd.effective_git_cwd — the shared resolver for the
tree a git/arc commit command actually targets (extracted from
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


def test_arc_commit_with_cd_redirect():
    # the leading-cd branch is command-agnostic, so it resolves for arc too
    assert git_cwd.effective_git_cwd("cd /repo/b && arc commit -m x", "/repo/a") == "/repo/b"


def test_dash_c_before_non_commit_verb_does_not_redirect():
    # the -C scan requires tokens[i+3] == "commit"; `status` must not redirect
    assert git_cwd.effective_git_cwd("git -C /repo/b status", "/repo/a") == "/repo/a"


def test_unparseable_command_falls_back_to_payload_cwd():
    # an unbalanced quote makes shlex.split raise -> fallback, never a wilder guess
    assert git_cwd.effective_git_cwd('git commit -m "unterminated', "/repo/a") == "/repo/a"
