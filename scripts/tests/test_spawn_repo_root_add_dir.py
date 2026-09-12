"""A `developer` spawn's workspace defaults to the parent process's cwd (no
`cwd=` override anywhere in spawn-specialist.py's child launch), while the
parent SESSION's own trust boundary is repo_root-granular (session_scope).
On a monorepo mount that holds several product subtrees under one
repo_root, a developer stage whose deliverable legitimately lives in a
sibling subtree of the same mount hits a permission wall the parent was
never going to hit itself.

`repo_root_add_dir_args` closes that gap for `developer` only: it detects
the VCS root of the spawn's cwd (git first, then arc, mirroring
hook-scope-track.py::resolve_repo_root_vcs) and grants --add-dir <root> when
that root sits strictly above cwd. Read-only kinds (thinker, code-reviewer)
and planner (writes only its own plan file, see PLANS_WRITE_KINDS) get
nothing here — this is a developer-only grant, layered independently of the
plans_add_dir_args grant already covering plans_directory.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_repo_root_add_dir", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "mount"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_developer_gets_repo_root_when_cwd_is_a_subtree(tmp_path):
    root = _git_repo(tmp_path)
    sub = root / "team-a" / "service"
    sub.mkdir(parents=True)
    assert MOD.repo_root_add_dir_args("developer", str(sub)) == ["--add-dir", str(root)]


def test_developer_gets_nothing_when_cwd_already_is_repo_root(tmp_path):
    root = _git_repo(tmp_path)
    assert MOD.repo_root_add_dir_args("developer", str(root)) == []


def test_non_developer_kinds_get_nothing_even_from_a_subtree(tmp_path):
    root = _git_repo(tmp_path)
    sub = root / "team-a" / "service"
    sub.mkdir(parents=True)
    for kind in ("thinker", "code-reviewer", "planner", "tech-writer"):
        assert MOD.repo_root_add_dir_args(kind, str(sub)) == []


def test_no_grant_outside_any_vcs_repo(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert MOD.repo_root_add_dir_args("developer", str(bare)) == []


def test_vcs_root_prefers_git_over_arc(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] == "arc":
            raise AssertionError("arc should not be probed once git succeeds")
        return real_run(args, **kwargs)

    monkeypatch.setattr(MOD.subprocess, "run", fake_run)
    assert MOD._vcs_root(str(root)) == str(root.resolve())
