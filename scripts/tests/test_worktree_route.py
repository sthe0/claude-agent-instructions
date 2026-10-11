"""Tests for lib/worktree_route.py and the routing glue in record-experience.py.

Hermetic: a bare local "origin" plus a clone playing the canonical checkout.
The clone carries the real record-experience.py, worktree_route.py and
land-branch.py with stub agentctl/semantic_join modules, so the routed child
is the real script. Nothing touches the real instructions checkout.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
LEAF_REL = "memory-global/leaves/experience/2026-01-01-seed.md"
SEED_LEAF = (
    "---\nname: seed\ndescription: seed leaf\ncreated: 2026-01-01\n"
    "last_verified: 2026-01-01\n---\n## Difficulty\nx\n## Contexts\n### initial\nbody\n"
)
ROUTING_ANNOUNCE = "[worktree-route] routing"
PROBE = """\
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if sys.argv[1] == "write":
    (ROOT / "probe-out.txt").write_text("\\n".join(sys.argv[2:]) + "\\n")
print("ROOT", ROOT)
print("GUARD", os.environ.get("WORKTREE_ROUTED", ""))
sys.stderr.write(f"child-err {ROOT}\\n")
"""
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS_DIR))
from lib import worktree_route  # noqa: E402

re_mod = _load("record_experience_route_under_test", SCRIPTS_DIR / "record-experience.py")


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
        capture_output=True, text=True, check=True,
    ).stdout


def snapshot(repo: Path) -> tuple:
    return (git("rev-parse", "HEAD", cwd=repo), git("status", "--porcelain", cwd=repo),
            (repo / LEAF_REL).read_text())


class Canon:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        git("init", "--quiet", "--bare", "-b", "main", str(self.origin), cwd=tmp)
        seed = tmp / "seed"
        git("clone", "--quiet", str(self.origin), str(seed), cwd=tmp)
        scripts = seed / "scripts"
        (scripts / "lib").mkdir(parents=True)
        (scripts / "agentctl").mkdir()
        shutil.copy(SCRIPTS_DIR / "record-experience.py", scripts)
        shutil.copy(SCRIPTS_DIR / "land-branch.py", scripts)
        shutil.copy(SCRIPTS_DIR / "lib" / "worktree_route.py", scripts / "lib")
        (scripts / "lib" / "__init__.py").write_text("")
        (scripts / "lib" / "semantic_join.py").write_text(
            "tokenize = term_score = None\n")
        (scripts / "agentctl" / "__init__.py").write_text("")
        (scripts / "agentctl" / "edit_ledger.py").write_text("def stamp(*a, **k):\n    pass\n")
        (scripts / "probe.py").write_text(PROBE)
        (seed / ".gitignore").write_text("__pycache__/\n")
        (seed / LEAF_REL).parent.mkdir(parents=True)
        (seed / LEAF_REL).write_text(SEED_LEAF)
        git("add", "-A", cwd=seed)
        git("commit", "--quiet", "-m", "seed", cwd=seed)
        git("push", "--quiet", "origin", "main", cwd=seed)
        self.repo = tmp / "canon"
        git("clone", "--quiet", str(self.origin), str(self.repo), cwd=tmp)
        self.leaf = self.repo / LEAF_REL

    def run(self, *args, cwd=None, script=None):
        cwd = cwd or self.repo
        env = {k: v for k, v in os.environ.items() if k != re_mod.ROUTED_ENV}
        return subprocess.run(
            [sys.executable, str(script or self.repo / "scripts" / "record-experience.py"), *args],
            cwd=str(cwd), env={**env, **GIT_ENV}, capture_output=True, text=True,
        )

    def set_last_verified(self, date="2026-02-02", **kw):
        return self.run("set-last-verified", "--leaf", str(self.leaf), "--date", date, **kw)

    def origin_leaf(self) -> str:
        return git("--git-dir", str(self.origin), "show", f"main:{LEAF_REL}", cwd=self.tmp)

    def worktrees(self) -> list[str]:
        out = git("worktree", "list", "--porcelain", cwd=self.repo)
        return [ln for ln in out.splitlines() if ln.startswith("worktree ")]

    def branches(self) -> list[str]:
        return git("branch", "--format=%(refname:short)", cwd=self.repo).split()


@pytest.fixture
def canon(tmp_path):
    return Canon(tmp_path)


def test_routes_from_main_checkout(canon):
    proc = canon.set_last_verified()
    assert proc.returncode == 0, proc.stderr
    assert "last_verified: 2026-02-02" in canon.origin_leaf()
    assert canon.leaf.read_text() == canon.origin_leaf()
    assert git("status", "--porcelain", cwd=canon.repo) == ""
    assert git("rev-parse", "HEAD", cwd=canon.repo) == git(
        "--git-dir", str(canon.origin), "rev-parse", "main", cwd=canon.tmp)
    assert len(canon.worktrees()) == 1
    assert canon.branches() == ["main"]
    assert "Co-Authored-By" in git("log", "-1", "--format=%B", cwd=canon.repo)


def test_linked_worktree_runs_in_place(canon):
    linked = canon.tmp / "linked"
    git("worktree", "add", "-b", "work", str(linked), cwd=canon.repo)
    before_canon, before_origin = snapshot(canon.repo), canon.origin_leaf()
    proc = canon.run("set-last-verified", "--leaf", str(linked / LEAF_REL),
                     "--date", "2026-03-03", cwd=linked,
                     script=linked / "scripts" / "record-experience.py")
    assert proc.returncode == 0, proc.stderr
    assert "last_verified: 2026-03-03" in (linked / LEAF_REL).read_text()
    assert snapshot(canon.repo) == before_canon
    assert canon.origin_leaf() == before_origin
    assert len(canon.worktrees()) == 2


def test_leaf_path_remapped(tmp_path):
    repo = re_mod.REPO_ROOT
    wt = tmp_path / "wt"
    inside = str(repo / LEAF_REL)
    remap = worktree_route.remap_path_options
    assert remap(["extend", "--leaf", inside, "--plan", "p"], ("--leaf",), repo, wt) == [
        "extend", "--leaf", str(wt / LEAF_REL), "--plan", "p"]
    assert remap([f"--leaf={inside}"], ("--leaf",), repo, wt) == [f"--leaf={wt / LEAF_REL}"]
    outside = str(tmp_path / "elsewhere.md")
    assert remap(["--leaf", outside], ("--leaf",), repo, wt) == ["--leaf", outside]


def test_output_paths_mapped_back(canon):
    proc = canon.set_last_verified()
    assert proc.returncode == 0, proc.stderr
    assert f"on {canon.leaf}" in proc.stdout
    child_output = proc.stdout + "".join(
        ln for ln in proc.stderr.splitlines(keepends=True)
        if not ln.startswith(ROUTING_ANNOUNCE))
    assert "-mem-" not in child_output


def test_fetch_failure_refuses(canon):
    git("remote", "set-url", "origin", str(canon.tmp / "no-such-remote.git"), cwd=canon.repo)
    before = snapshot(canon.repo)
    proc = canon.set_last_verified()
    assert proc.returncode != 0
    assert "git fetch" in proc.stderr
    assert snapshot(canon.repo) == before
    assert len(canon.worktrees()) == 1


def test_unpushed_commits_refuse(canon):
    (canon.repo / "extra.txt").write_text("x\n")
    git("add", "-A", cwd=canon.repo)
    git("commit", "--quiet", "-m", "local only", cwd=canon.repo)
    before = snapshot(canon.repo)
    proc = canon.set_last_verified()
    assert proc.returncode != 0
    assert "not on origin/main" in proc.stderr
    assert snapshot(canon.repo) == before
    assert len(canon.worktrees()) == 1


def test_dirty_tree_refuses(canon):
    (canon.repo / "scratch.txt").write_text("wip\n")
    before = snapshot(canon.repo)
    proc = canon.set_last_verified()
    assert proc.returncode != 0
    assert "uncommitted changes" in proc.stderr
    assert snapshot(canon.repo) == before
    assert len(canon.worktrees()) == 1


def test_failure_at_child_step(canon):
    before = snapshot(canon.repo)
    missing = canon.repo / "memory-global/leaves/experience/missing.md"
    proc = canon.run("set-last-verified", "--leaf", str(missing), "--date", "2026-02-02")
    assert proc.returncode != 0
    assert "step 'child'" in proc.stderr
    assert "-mem-" in proc.stderr
    assert snapshot(canon.repo) == before
    assert canon.origin_leaf() == SEED_LEAF
    # a child that wrote nothing leaves no worktree behind (named departure from "always keep")
    assert len(canon.worktrees()) == 1


def test_failure_at_commit_step(canon):
    hook = canon.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = snapshot(canon.repo)
    proc = canon.set_last_verified()
    assert proc.returncode != 0
    assert "step 'commit'" in proc.stderr
    assert "-mem-" in proc.stderr
    assert snapshot(canon.repo) == before
    assert canon.origin_leaf() == SEED_LEAF
    assert len(canon.worktrees()) == 2


def test_failure_at_land_step(canon):
    hook = canon.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = snapshot(canon.repo)
    proc = canon.set_last_verified()
    assert proc.returncode != 0
    assert "step 'land'" in proc.stderr
    assert "-mem-" in proc.stderr
    assert snapshot(canon.repo) == before
    assert canon.origin_leaf() == SEED_LEAF
    assert len(canon.worktrees()) == 2


def test_failure_at_pull_step(canon, monkeypatch, capsys):
    real_git = worktree_route._git

    def failing_pull(args, cwd):
        if args and args[0] == "pull":
            return subprocess.CompletedProcess(["git", *args], 1, "", "pull refused")
        return real_git(args, cwd)

    def child(worktree):
        target = worktree / LEAF_REL
        target.write_text(target.read_text().replace("2026-01-01", "2026-04-04"))
        return 0, ""

    monkeypatch.setattr(worktree_route, "_git", failing_pull)
    before = snapshot(canon.repo)
    code = worktree_route.run_routed(canon.repo, child, "experience: test")
    err = capsys.readouterr().err
    assert code != 0
    assert "step 'pull'" in err
    assert "-mem-" in err
    assert snapshot(canon.repo) == before
    assert len(canon.worktrees()) == 2


def _parse(*argv):
    return re_mod.build_parser().parse_args(list(argv))


def test_search_not_routed():
    assert not re_mod.needs_worktree(_parse("search", "some keywords"))


def test_promote_scan_not_routed():
    assert not re_mod.needs_worktree(_parse("promote-scan"))


def test_project_scope_not_routed(tmp_path):
    new_args = ["--slug", "s", "--title", "t", "--description", "d", "--confirmed-by", "u",
                "--difficulty", "x", "--order", "o", "--criterion", "c",
                "--context-where", "w", "--plan", "p"]
    assert not re_mod.needs_worktree(
        _parse("new", "--scope", "project", "--project-dir", str(tmp_path), *new_args))
    assert re_mod.needs_worktree(_parse("new", *new_args))
    assert not re_mod.needs_worktree(_parse(
        "extend", "--leaf", str(tmp_path / "leaf.md"), "--context-label", "l",
        "--context-where", "w", "--plan", "p"))


def _route(canon, monkeypatch, *argv, path_options=()):
    for key, val in GIT_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv(worktree_route.GUARD_ENV, raising=False)
    return worktree_route.route_script(
        canon.repo, "scripts/probe.py", list(argv), "probe: write",
        path_options=path_options)


def _origin_file(canon, rel):
    return git("--git-dir", str(canon.origin), "show", f"main:{rel}", cwd=canon.tmp)


def test_route_script_runs_worktree_copy(canon, monkeypatch):
    assert _route(canon, monkeypatch, "write", "payload") == 0
    assert _origin_file(canon, "probe-out.txt") == "payload\n"
    assert (canon.repo / "probe-out.txt").read_text() == "payload\n"
    assert git("status", "--porcelain", cwd=canon.repo) == ""
    assert len(canon.worktrees()) == 1


def test_route_script_remaps_path_options(canon, monkeypatch, tmp_path):
    inside = canon.repo / "a.txt"
    outside = tmp_path / "elsewhere.txt"
    code = _route(canon, monkeypatch, "write", "--target", str(inside),
                  f"--target={inside}", "--other", str(outside),
                  path_options=("--target",))
    assert code == 0
    lines = _origin_file(canon, "probe-out.txt").splitlines()
    assert lines[1].startswith(f"{canon.repo}-mem-") and lines[1].endswith("/a.txt")
    assert lines[2] == "--target=" + lines[1]
    assert lines[4] == str(outside)


def test_route_script_maps_output_back(canon, monkeypatch, capsys):
    assert _route(canon, monkeypatch, "read") == 0
    captured = capsys.readouterr()
    assert f"ROOT {canon.repo}" in captured.out
    assert f"child-err {canon.repo}" in captured.err
    assert "-mem-" not in captured.out
    assert all("-mem-" not in ln for ln in captured.err.splitlines()
               if not ln.startswith(ROUTING_ANNOUNCE))


def test_should_route_false_when_disabled(canon, monkeypatch):
    monkeypatch.setenv(worktree_route.DISABLE_ENV, "1")
    assert worktree_route.should_route(canon.repo) is False


def test_should_route_false_in_linked_worktree(canon):
    linked = canon.tmp / "linked"
    git("worktree", "add", "-b", "work", str(linked), cwd=canon.repo)
    assert worktree_route.should_route(canon.repo) is True
    assert worktree_route.should_route(linked) is False


def test_should_route_false_outside_git(canon, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree_route.should_route(canon.repo) is True
    assert worktree_route.should_route(plain) is False


def test_route_script_announces_route(canon, monkeypatch, capsys):
    assert _route(canon, monkeypatch, "read") == 0
    lines = [ln for ln in capsys.readouterr().err.splitlines()
             if ln.startswith(ROUTING_ANNOUNCE)]
    assert len(lines) == 1
    assert f"{canon.repo}-mem-" in lines[0]


def test_route_script_sets_recursion_guard(canon, monkeypatch, capsys):
    assert _route(canon, monkeypatch, "read") == 0
    assert "GUARD 1" in capsys.readouterr().out
