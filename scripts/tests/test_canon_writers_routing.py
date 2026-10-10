"""Routing tests for the four canon writers that go through worktree_route.

Difficulty removed: stamp-memory-dates.py --apply, gen_crutch_registry.py,
permissions-cli.py grant/revoke and verify-readme.py --fix left uncommitted
edits in the read-only canonical checkout. Each now routes through
`worktree_route.route_script` under exactly one write condition; these tests pin
both directions of each condition and prove two routes end to end.

Hermetic: a temporary main checkout with a local bare origin carries copies of
the real scripts. The routes / not-routed tests substitute `route_script`; the
two end-to-end tests run the real route. Nothing touches the real instructions
checkout. This module inherits WORKTREE_ROUTE_DISABLE from the environment on
purpose — setting it is the negative control that turns the routing tests red.
Run serially (never xdist / -n).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import worktree_route  # noqa: E402

LEAF_REL = "memory-global/leaves/seed-leaf.md"
SEED_LEAF = "---\nname: seed-leaf\ndescription: seed\ntype: reference\n---\nbody\n"
PERMISSIONS_REL = "permissions/global.json"
COPIED_SCRIPTS = ("permissions-cli.py", "verify-readme.py", "stamp-memory-dates.py",
                  "land-branch.py")
ROUTING_ANNOUNCE = "[worktree-route] routing"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
SCRIPTS_REGION = (
    "# Scripts\n\n<!-- inventory:scripts:begin -->\n| Script | Purpose |\n|---|---|\n"
    "<!-- inventory:scripts:end -->\n"
)
SKILLS_REGIONS = (
    "# Skills\n\n<!-- inventory:skills:begin -->\n| name | Triggers (summary) | File |\n"
    "|---|---|---|\n<!-- inventory:skills:end -->\n\n"
    "<!-- inventory:specializations:begin -->\n"
    "| name | Spawns when a plan step calls for | File |\n|---|---|---|\n"
    "<!-- inventory:specializations:end -->\n"
)


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout


def _load(name: str, path: Path):
    """Load a script file as a module, leaving sys.path as found (a script may
    prepend its own directory, which for a copy would shadow the real `lib`)."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for key, val in GIT_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv(worktree_route.GUARD_ENV, raising=False)


class Canon:
    """A temporary main checkout (`repo`) cloned from a bare `origin`."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        git("init", "--quiet", "--bare", "-b", "main", str(self.origin), cwd=tmp)
        seed = tmp / "seed"
        git("clone", "--quiet", str(self.origin), str(seed), cwd=tmp)
        scripts = seed / "scripts"
        (scripts / "lib").mkdir(parents=True)
        for name in COPIED_SCRIPTS:
            shutil.copy(SCRIPTS_DIR / name, scripts)
        shutil.copy(SCRIPTS_DIR / "lib" / "worktree_route.py", scripts / "lib")
        (scripts / "lib" / "__init__.py").write_text("")
        (scripts / "README.md").write_text(SCRIPTS_REGION)
        (seed / "docs" / "components").mkdir(parents=True)
        (seed / "docs" / "components" / "skills.md").write_text(SKILLS_REGIONS)
        (seed / PERMISSIONS_REL).parent.mkdir()
        (seed / PERMISSIONS_REL).write_text(json.dumps({"permissions": []}) + "\n")
        (seed / LEAF_REL).parent.mkdir(parents=True)
        (seed / LEAF_REL).write_text(SEED_LEAF)
        (seed / ".gitignore").write_text("__pycache__/\n")
        git("add", "-A", cwd=seed)
        git("commit", "--quiet", "-m", "seed", cwd=seed)
        git("push", "--quiet", "origin", "main", cwd=seed)
        self.repo = tmp / "canon"
        git("clone", "--quiet", str(self.origin), str(self.repo), cwd=tmp)

    def script(self, name: str, root: Path | None = None) -> Path:
        return (root or self.repo) / "scripts" / name

    def run(self, script: Path, *args, cwd: Path | None = None):
        env = {k: v for k, v in os.environ.items() if k != worktree_route.GUARD_ENV}
        return subprocess.run(
            [sys.executable, str(script), *args], cwd=str(cwd or self.repo),
            env=env, capture_output=True, text=True,
        )

    def origin_file(self, rel: str) -> str:
        return git("--git-dir", str(self.origin), "show", f"main:{rel}", cwd=self.tmp)

    def origin_head(self) -> str:
        return git("--git-dir", str(self.origin), "rev-parse", "main", cwd=self.tmp)

    def worktrees(self) -> list[str]:
        out = git("worktree", "list", "--porcelain", cwd=self.repo)
        return [ln for ln in out.splitlines() if ln.startswith("worktree ")]

    def snapshot(self) -> tuple:
        return (git("rev-parse", "HEAD", cwd=self.repo),
                git("status", "--porcelain", cwd=self.repo), self.origin_head())

    def assert_landed_and_clean(self, rel: str) -> None:
        assert (self.repo / rel).read_text() == self.origin_file(rel)
        assert git("rev-parse", "HEAD", cwd=self.repo) == self.origin_head()
        assert git("status", "--porcelain", cwd=self.repo) == ""
        assert len(self.worktrees()) == 1
        assert git("branch", "--format=%(refname:short)", cwd=self.repo).split() == ["main"]


@pytest.fixture
def canon(tmp_path):
    return Canon(tmp_path)


@pytest.fixture
def routed(monkeypatch):
    """Replace route_script with a recorder returning 7; yields the call list."""
    calls: list[dict] = []

    def fake(repo, script_rel, argv, message, *, path_options=(), land_script=None):
        calls.append({"repo": Path(repo), "script": script_rel, "argv": list(argv),
                      "message": message, "path_options": tuple(path_options)})
        return 7

    monkeypatch.setattr(worktree_route, "route_script", fake)
    return calls


def _stamp(canon, monkeypatch):
    mod = _load("stamp_memory_dates_routing", canon.script("stamp-memory-dates.py"))
    monkeypatch.setattr(mod, "project_memory_dirs", lambda: [])
    return mod


# --- stamp-memory-dates.py (R2) ----------------------------------------------

def test_stamp_apply_global_routes(canon, routed, monkeypatch):
    stamp = _stamp(canon, monkeypatch)
    assert stamp.main(["--apply"]) == 7
    assert routed == [{
        "repo": canon.repo, "script": "scripts/stamp-memory-dates.py",
        "argv": ["--apply"], "message": "memory: stamp-memory-dates --apply",
        "path_options": ("--project-dir",),
    }]
    assert (canon.repo / LEAF_REL).read_text() == SEED_LEAF


def test_stamp_apply_all_scope_routes(canon, routed, monkeypatch):
    stamp = _stamp(canon, monkeypatch)
    assert stamp.main(["--apply", "--scope", "all"]) == 7
    assert [c["argv"] for c in routed] == [["--apply", "--scope", "all"]]
    assert (canon.repo / LEAF_REL).read_text() == SEED_LEAF


def test_stamp_dry_run_not_routed(canon, routed, monkeypatch, capsys):
    stamp = _stamp(canon, monkeypatch)
    assert stamp.main([]) == 0
    assert stamp.main(["--scope", "global"]) == 0
    assert routed == []
    assert "would stamp 1" in capsys.readouterr().out
    assert (canon.repo / LEAF_REL).read_text() == SEED_LEAF


def test_stamp_project_scope_not_routed(canon, routed, monkeypatch, tmp_path):
    stamp = _stamp(canon, monkeypatch)
    project = tmp_path / "project"
    leaf = project / ".claude" / "agent-memory" / "note.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("---\nname: note\ndescription: n\ntype: reference\n---\nbody\n")
    assert stamp.main(["--apply", "--scope", "project", "--project-dir", str(project)]) == 0
    assert routed == []
    assert "created:" in leaf.read_text()
    assert (canon.repo / LEAF_REL).read_text() == SEED_LEAF


# --- gen_crutch_registry.py (R3) ---------------------------------------------

def _crutch(monkeypatch, name="gen_crutch_registry_routing"):
    monkeypatch.setattr(sys, "argv", ["gen_crutch_registry.py"])
    return _load(name, SCRIPTS_DIR / "gen_crutch_registry.py")


def test_crutch_registry_routes_from_main_checkout(canon, routed, monkeypatch, tmp_path):
    gen = _crutch(monkeypatch)
    monkeypatch.setattr(gen, "REPO_ROOT", canon.repo)
    monkeypatch.setattr(gen, "REGISTRY_PATH", tmp_path / "crutch_registry.toml")
    assert gen.main() == 7
    assert routed == [{
        "repo": canon.repo, "script": "scripts/gen_crutch_registry.py", "argv": [],
        "message": "crutch-registry: regenerate", "path_options": (),
    }]
    assert not (tmp_path / "crutch_registry.toml").exists()


def test_crutch_registry_import_has_no_route_side_effect(routed, monkeypatch):
    run_routed_calls: list = []
    monkeypatch.setattr(worktree_route, "should_route", lambda repo: True)
    monkeypatch.setattr(worktree_route, "run_routed",
                        lambda *a, **k: run_routed_calls.append((a, k)) or 0)
    _crutch(monkeypatch, "gen_crutch_registry_import_only")
    assert routed == []
    assert run_routed_calls == []


# --- permissions-cli.py (R4) -------------------------------------------------

def _perm(canon):
    return _load("permissions_cli_routing", canon.script("permissions-cli.py"))


def test_permissions_grant_routes(canon, routed):
    perm = _perm(canon)
    argv = ["grant", "Bash(x:*)", "--context", "why", "--date", "2026-01-01"]
    assert perm.main(argv) == 7
    assert routed == [{
        "repo": canon.repo, "script": "scripts/permissions-cli.py", "argv": argv,
        "message": "permissions: grant Bash(x:*)", "path_options": ("--file",),
    }]
    assert json.loads((canon.repo / PERMISSIONS_REL).read_text()) == {"permissions": []}


def test_permissions_revoke_routes(canon, routed):
    perm = _perm(canon)
    argv = ["--file", str(canon.repo / PERMISSIONS_REL), "revoke", "Bash(x:*)"]
    assert perm.main(argv) == 7
    assert [(c["argv"], c["message"], c["path_options"]) for c in routed] == [
        (argv, "permissions: revoke Bash(x:*)", ("--file",))]


def test_permissions_read_commands_not_routed(canon, routed, capsys):
    perm = _perm(canon)
    before = (canon.repo / PERMISSIONS_REL).read_text()
    assert perm.main(["list"]) == 0
    perm.main(["check", "anything"])
    assert perm.main(["digest"]) == 0
    capsys.readouterr()
    assert routed == []
    assert (canon.repo / PERMISSIONS_REL).read_text() == before


def test_permissions_file_outside_repo_not_routed(canon, routed, tmp_path):
    perm = _perm(canon)
    outside = tmp_path / "project" / ".claude" / "agent-memory" / "permissions.json"
    assert perm.main(["--file", str(outside), "grant", "Bash(y:*)", "--context", "c",
                      "--date", "2026-01-01"]) == 0
    assert routed == []
    assert [p["pattern"] for p in json.loads(outside.read_text())["permissions"]] == ["Bash(y:*)"]
    assert json.loads((canon.repo / PERMISSIONS_REL).read_text()) == {"permissions": []}


# --- verify-readme.py (R5) ---------------------------------------------------

def _readme(canon):
    return _load("verify_readme_routing", canon.script("verify-readme.py"))


def test_readme_fix_routes(canon, routed):
    readme = _readme(canon)
    before = (canon.repo / "scripts" / "README.md").read_text()
    assert readme.main(["--fix"]) == 7
    assert readme.main(["--fix", "--root", str(canon.repo)]) == 7
    assert [(c["repo"], c["script"], c["argv"], c["message"], c["path_options"])
            for c in routed] == [
        (canon.repo, "scripts/verify-readme.py", argv, "readme: verify-readme --fix", ("--root",))
        for argv in (["--fix"], ["--fix", "--root", str(canon.repo)])]
    assert (canon.repo / "scripts" / "README.md").read_text() == before


def test_readme_check_not_routed(canon, routed, capsys):
    readme = _readme(canon)
    before = (canon.repo / "scripts" / "README.md").read_text()
    assert readme.main([]) == 1
    assert readme.main(["--root", str(canon.repo), "--staged"]) == 1
    capsys.readouterr()
    assert routed == []
    assert (canon.repo / "scripts" / "README.md").read_text() == before


# --- end to end: the real route ----------------------------------------------

def test_permissions_grant_end_to_end_leaves_canon_clean(canon):
    proc = canon.run(canon.script("permissions-cli.py"), "grant", "Bash(e2e:*)",
                     "--context", "end to end", "--date", "2026-01-01")
    assert proc.returncode == 0, proc.stderr
    assert ROUTING_ANNOUNCE in proc.stderr
    assert "Bash(e2e:*)" in canon.origin_file(PERMISSIONS_REL)
    canon.assert_landed_and_clean(PERMISSIONS_REL)
    assert git("log", "-1", "--format=%s", cwd=canon.repo).strip() == "permissions: grant Bash(e2e:*)"


def test_readme_fix_end_to_end_leaves_canon_clean(canon):
    assert canon.run(canon.script("verify-readme.py")).returncode == 1
    proc = canon.run(canon.script("verify-readme.py"), "--fix")
    assert proc.returncode == 0, proc.stderr
    assert ROUTING_ANNOUNCE in proc.stderr
    assert "permissions-cli.py" in canon.origin_file("scripts/README.md")
    canon.assert_landed_and_clean("scripts/README.md")
    assert git("log", "-1", "--format=%s", cwd=canon.repo).strip() == "readme: verify-readme --fix"
    assert canon.run(canon.script("verify-readme.py")).returncode == 0


def test_linked_worktree_writes_in_place(canon, tmp_path):
    linked = tmp_path / "linked"
    git("worktree", "add", "-b", "work", str(linked), cwd=canon.repo)
    before = canon.snapshot()
    grant = canon.run(canon.script("permissions-cli.py", linked), "grant", "Bash(w:*)",
                      "--context", "in place", "--date", "2026-01-01", cwd=linked)
    fix = canon.run(canon.script("verify-readme.py", linked), "--fix", cwd=linked)
    assert (grant.returncode, fix.returncode) == (0, 0), (grant.stderr, fix.stderr)
    assert ROUTING_ANNOUNCE not in grant.stderr + fix.stderr
    assert "Bash(w:*)" in (linked / PERMISSIONS_REL).read_text()
    assert "permissions-cli.py" in (linked / "scripts" / "README.md").read_text()
    assert canon.snapshot() == before
    assert len(canon.worktrees()) == 2
