"""`agentctl.git_surface`: the digest of what git would execute in a repository (hooks, config-named
programs, attributes, hook frameworks) and its comparison against a snapshot taken at spawn."""
import os
import subprocess
from pathlib import Path

import pytest

import test_tool_contracts_segments as seg_tests
from agentctl import git_surface


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture
def repo(tmp_path):
    return seg_tests._init_repo(tmp_path / "repo", "feat")


def timeout():
    return 20


def capture(path):
    return git_surface.capture(str(path), timeout)


def plant(path, body="#!/bin/sh\nexit 0\n", mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


def test_the_digest_is_stable_and_the_same_from_a_subdirectory(repo):
    (repo / "sub").mkdir()
    first = capture(repo)
    assert capture(repo).digest == first.digest
    assert capture(repo / "sub").digest == first.digest


def test_a_directory_that_is_not_a_repository_raises_not_a_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(git_surface.NotARepo):
        capture(tmp_path / "plain")


def test_a_spent_deadline_is_unreadable_not_empty(repo):
    with pytest.raises(git_surface.Unreadable):
        git_surface.capture(str(repo), lambda: 0)


@pytest.mark.parametrize("change", [
    "hook-added", "hook-edited", "hook-chmod", "symlink-target-edited", "symlink-retargeted",
    "config-exec-key", "config-program-edited", "attributes", "info-attributes", "framework-file",
    "framework-dir-file", "nested-attributes", "alias", "remote-url", "remote-pushurl", "url-rewrite",
    "protocol", "gpg-format", "trailer-cmd", "lefthook-local", "overcommit", "package-json", "lefthook-dir",
])
def test_every_surface_kind_moves_the_digest(repo, tmp_path, change):
    hooks = repo / ".git" / "hooks"
    target = plant(tmp_path / "target-hook")
    program = plant(tmp_path / "monitor")
    plant(hooks / "pre-commit")
    (hooks / "pre-push").symlink_to(target)
    seg_tests._git(repo, "config", "core.fsmonitor", str(program))
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitattributes").write_text("*.a text\n")
    plant(repo / ".husky" / "pre-push", body="exit 0\n")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    seg_tests._git(repo, "add", "-f", "sub/.gitattributes", ".husky/pre-push", ".pre-commit-config.yaml")
    seg_tests._git(repo, "commit", "-q", "-m", "tracked surface")
    before = capture(repo)
    {
        "hook-added": lambda: plant(hooks / "post-commit"),
        "hook-edited": lambda: (hooks / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/x\n"),
        "hook-chmod": lambda: (hooks / "pre-commit").chmod(0o644),
        "symlink-target-edited": lambda: target.write_text("#!/bin/sh\ntouch /tmp/x\n"),
        "symlink-retargeted": lambda: ((hooks / "pre-push").unlink(), (hooks / "pre-push").symlink_to(program)),
        "config-exec-key": lambda: seg_tests._git(repo, "config", "diff.external", "/tmp/evil"),
        "config-program-edited": lambda: program.write_text("#!/bin/sh\ntouch /tmp/x\n"),
        "attributes": lambda: (repo / ".gitattributes").write_text("*.txt filter=evil\n"),
        "info-attributes": lambda: (repo / ".git" / "info").mkdir(exist_ok=True) or (repo / ".git" / "info" / "attributes").write_text("* filter=e\n"),
        "framework-file": lambda: (repo / ".pre-commit-config.yaml").write_text("repos: [x]\n"),
        "framework-dir-file": lambda: (repo / ".husky" / "pre-push").write_text("touch /tmp/x\n"),
        "nested-attributes": lambda: (repo / "sub" / ".gitattributes").write_text("*.a filter=evil\n"),
        "alias": lambda: seg_tests._git(repo, "config", "alias.st", "status -s"),
        "remote-url": lambda: seg_tests._git(repo, "config", "remote.origin.url", "ext::sh -c evil"),
        "remote-pushurl": lambda: seg_tests._git(repo, "config", "remote.origin.pushurl", "ext::sh -c evil"),
        "url-rewrite": lambda: seg_tests._git(repo, "config", "url.ext::sh.insteadOf", "https://"),
        "protocol": lambda: seg_tests._git(repo, "config", "protocol.ext.allow", "always"),
        "gpg-format": lambda: seg_tests._git(repo, "config", "gpg.format", "ssh"),
        "trailer-cmd": lambda: seg_tests._git(repo, "config", "trailer.t.cmd", "/tmp/evil"),
        "lefthook-local": lambda: (repo / "lefthook-local.yml").write_text("pre-push: {}\n"),
        "overcommit": lambda: (repo / ".overcommit.yml").write_text("PreCommit: {}\n"),
        "package-json": lambda: (repo / "package.json").write_text("{}\n"),
        "lefthook-dir": lambda: plant(repo / ".lefthook" / "pre-push" / "check.sh"),
    }[change]()
    assert capture(repo).digest != before.digest


@pytest.mark.parametrize("change", ["sample-hook", "plain-config", "untouched-file", "branch-config"])
def test_what_git_never_executes_leaves_the_digest_alone(repo, change):
    before = capture(repo)
    {
        "sample-hook": lambda: plant(repo / ".git" / "hooks" / "pre-push.sample"),
        "plain-config": lambda: seg_tests._git(repo, "config", "user.name", "someone"),
        "untouched-file": lambda: (repo / "a.txt").write_text("x"),
        "branch-config": lambda: seg_tests._git(repo, "config", "branch.feat.remote", "origin"),
    }[change]()
    assert capture(repo).digest == before.digest


def test_capture_never_starts_a_program_the_repository_configures(repo, tmp_path):
    marker = tmp_path / "fsmonitor-ran"
    program = plant(tmp_path / "monitor", body=f"#!/bin/sh\necho ran >> {marker}\nprintf '\\0'\n")
    seg_tests._git(repo, "config", "core.fsmonitor", str(program))
    assert capture(repo).digest
    assert not marker.exists()


def test_every_config_file_git_read_is_a_pinned_path(repo, tmp_path):
    included = tmp_path / "extra.gitconfig"
    included.write_text("[core]\n\tpager = cat\n")
    seg_tests._git(repo, "config", "include.path", str(included))
    assert str(included.resolve()) in capture(repo).paths


def test_runnable_is_true_only_for_something_the_repository_holds_that_git_starts(repo, tmp_path):
    assert capture(repo).runnable is False
    plant(repo / ".git" / "hooks" / "pre-push.sample")
    assert capture(repo).runnable is False
    seg_tests._git(repo, "config", "alias.st", "status -s")
    assert capture(repo).runnable is False
    plant(repo / ".git" / "hooks" / "pre-push")
    assert capture(repo).runnable is True


@pytest.mark.parametrize("change", [
    lambda repo: (repo / "lefthook.yml").write_text("pre-push: {}\n"),
    lambda repo: (repo / "package.json").write_text("{}\n"),
    lambda repo: plant(repo / ".husky" / "pre-push", body="exit 0\n"),
    lambda repo: seg_tests._git(repo, "config", "core.fsmonitor", "/usr/bin/true"),
], ids=["lefthook", "package-json", "husky", "repo-config-program"])
def test_each_runnable_kind_marks_the_surface_runnable(repo, change):
    change(repo)
    assert capture(repo).runnable is True


def test_core_hookspath_is_followed_and_relative_to_the_toplevel(repo):
    seg_tests._git(repo, "config", "core.hooksPath", "githooks")
    plant(repo / "githooks" / "pre-push")
    before = capture(repo)
    plant(repo / "githooks" / "pre-push", body="#!/bin/sh\ntouch /tmp/x\n")
    assert capture(repo).digest != before.digest
    assert capture(repo / "githooks").digest != before.digest


def test_the_hooks_directory_git_does_not_run_from_is_not_part_of_the_surface(repo):
    elsewhere = repo.parent / "elsewhere-hooks"
    seg_tests._git(repo, "config", "core.hooksPath", str(elsewhere))
    before = capture(repo)
    plant(repo / ".git" / "hooks" / "pre-push")
    assert capture(repo).digest == before.digest
    plant(elsewhere / "pre-push")
    assert capture(repo).digest != before.digest


def test_paths_name_every_pinned_location(repo):
    seg_tests._git(repo, "config", "core.hooksPath", "githooks")
    plant(repo / "githooks" / "pre-push")
    surface = capture(repo)
    real = str(repo.resolve())
    assert any(p.startswith(real + "/githooks") for p in surface.paths)
    assert any(p.endswith("/.git/config") for p in surface.paths)
    assert any(p.endswith("/.gitattributes") for p in surface.paths)


def test_the_snapshot_holds_one_surface_per_repository_and_skips_what_is_not_one(repo, tmp_path):
    (tmp_path / "plain").mkdir()
    snapshot = git_surface.snapshot_venues([str(repo), str(repo), str(tmp_path / "plain")], timeout)
    assert snapshot["schema"] == git_surface.SCHEMA
    assert list(snapshot["repos"]) == [str(repo.resolve() / ".git")]


def test_mismatch_is_none_only_for_an_equal_surface(repo):
    snapshot = git_surface.snapshot_venues([str(repo)], timeout)
    assert git_surface.mismatch(snapshot, capture(repo)) is None
    plant(repo / ".git" / "hooks" / "post-index-change")
    assert "hooks" in git_surface.mismatch(snapshot, capture(repo))


@pytest.mark.parametrize("snapshot", [None, {}, {"schema": 1}, {"schema": 1, "repos": []}, {"schema": 99, "repos": {}},
                                      {"schema": 1, "repos": {"/elsewhere": {}}}])
def test_a_missing_or_malformed_snapshot_never_matches(repo, snapshot):
    assert git_surface.mismatch(snapshot, capture(repo))


def test_a_repository_the_snapshot_lacks_never_matches(repo, tmp_path):
    other = seg_tests._init_repo(tmp_path / "other", "x")
    snapshot = git_surface.snapshot_venues([str(repo)], timeout)
    assert "is not in the git-surface snapshot" in git_surface.mismatch(snapshot, capture(other))
