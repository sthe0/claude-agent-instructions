"""`lib.project_root` repository-marker rules: what counts as a real `.git` or `.arc` root."""
from __future__ import annotations

import os

import pytest

from lib import project_root


def test_dot_git_directory_with_head_is_a_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert project_root.git_root_marker(str(tmp_path)) is True


def test_empty_dot_git_directory_is_no_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    assert project_root.git_root_marker(str(tmp_path)) is False


def test_gitlink_file_with_relative_gitdir_is_a_marker(tmp_path):
    gitdir = tmp_path / "meta" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ../meta/wt\n", encoding="utf-8")
    assert project_root.git_root_marker(str(checkout)) is True


def test_gitlink_file_with_dangling_gitdir_is_no_marker(tmp_path):
    (tmp_path / ".git").write_text("gitdir: ../nowhere\n", encoding="utf-8")
    assert project_root.git_root_marker(str(tmp_path)) is False


def test_dot_git_file_without_gitdir_line_is_no_marker(tmp_path):
    (tmp_path / ".git").write_text("not a gitlink\n", encoding="utf-8")
    assert project_root.git_root_marker(str(tmp_path)) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_dot_git_file_is_unknown(tmp_path):
    git = tmp_path / ".git"
    git.write_text("gitdir: x\n", encoding="utf-8")
    git.chmod(0)
    try:
        assert project_root.git_root_marker(str(tmp_path)) is None
        assert project_root.vcs_root_marker(str(tmp_path)) is None
    finally:
        git.chmod(0o600)


def test_vcs_root_marker_accepts_an_arc_directory(tmp_path):
    (tmp_path / ".arc").mkdir()
    assert project_root.vcs_root_marker(str(tmp_path)) is True


def test_vcs_root_marker_rejects_a_bare_directory(tmp_path):
    assert project_root.vcs_root_marker(str(tmp_path)) is False
