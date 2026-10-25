"""spawn-specialist refuses a child whose start directory is in no project while the parent
session runs in a project that registers hooks (safety net B).

A directory is a project when Claude Code would load a project settings file starting there:
`.claude/settings.json` in it, `.claude/settings.local.json` at the root of its git repository
(the main checkout's root for a linked worktree; the directory itself outside git or when that
root is $HOME) or a legacy `.claude/settings.local.json` in it. User-level and managed files
never count. Everything here runs on temporary directories, a fake HOME and --dry-run or a stub
`claude`; no real child is launched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from argparse import Namespace
from pathlib import Path

import pytest

from test_spawn_runaway_cap import _read_cost_rows, _setup_fake_home, _stub_claude, _write_exec

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_child_project", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()

HOOKS = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x.sh"}]}]}}
PERMISSIONS_ONLY = {"permissions": {"allow": ["Bash(true)"]}}


def _write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    return path


@pytest.fixture(autouse=True)
def _fake_environment(tmp_path, monkeypatch):
    home = _setup_fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(MOD, "COST_LOG", home / ".local" / "log" / "claude-spawn-costs.jsonl")
    return home


@pytest.fixture
def home(tmp_path) -> Path:
    return (tmp_path / "home").resolve()


@pytest.fixture
def world(tmp_path):
    """A project with a hook, a project holding only a local file, and two bare directories."""
    root = tmp_path.resolve()
    project = _repo(root / "proj")
    _write_json(project / ".claude" / "settings.json", HOOKS)
    (project / "sub").mkdir()
    local = _repo(root / "localproj")
    _write_json(local / ".claude" / "settings.local.json", PERMISSIONS_ONLY)
    (local / "sub").mkdir()
    outside = root / "outside"
    outside.mkdir()
    return Namespace(project=project, local=local, outside=outside, root=root)


def _files(directory: Path) -> "tuple[str, ...]":
    return MOD.project_settings_sources(str(directory))[0]


# ── the project rule ────────────────────────────────────────────────────────

def test_start_directory_settings_json_makes_a_project(world):
    assert _files(world.project) == (str(world.project / ".claude" / "settings.json"),)


def test_subdirectory_of_project_with_only_a_start_directory_settings_json_is_no_project(world):
    assert _files(world.project / "sub") == ()


def test_subdirectory_of_a_repository_whose_root_holds_settings_local_json_is_a_project(world):
    assert _files(world.local / "sub") == (str(world.local / ".claude" / "settings.local.json"),)


def test_linked_worktree_reads_settings_local_json_from_the_main_checkout(world):
    main = _repo(world.root / "main")
    (main / "README").write_text("x\n")
    _git("add", "README", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    worktree = world.root / "main-wt"
    _git("worktree", "add", "-q", "-b", "wt", str(worktree), cwd=main)
    assert _files(worktree) == ()
    _write_json(main / ".claude" / "settings.local.json", PERMISSIONS_ONLY)
    assert _files(worktree) == (str(main / ".claude" / "settings.local.json"),)


def test_directory_outside_git_reads_settings_local_json_from_itself(world):
    _write_json(world.outside / ".claude" / "settings.local.json", PERMISSIONS_ONLY)
    assert _files(world.outside) == (str(world.outside / ".claude" / "settings.local.json"),)


def test_repository_rooted_at_home_reads_settings_local_json_from_the_start_directory(world, home):
    _git("init", "-q", cwd=home)
    _write_json(home / ".claude" / "settings.local.json", PERMISSIONS_ONLY)
    start = home / "work"
    start.mkdir()
    assert _files(start) == ()
    _write_json(start / ".claude" / "settings.local.json", PERMISSIONS_ONLY)
    assert _files(start) == (str(start / ".claude" / "settings.local.json"),)


def test_user_level_settings_of_the_harness_root_are_not_a_project(home):
    _write_json(home / ".claude" / "settings.json", HOOKS)
    assert _files(home) == ()


def test_managed_settings_file_is_not_a_project(world, monkeypatch):
    managed = _write_json(world.outside / "managed-settings.json", HOOKS)
    monkeypatch.setattr(MOD.hook_wiring, "managed_settings_path", lambda: managed)
    _write_json(world.outside / ".claude" / "settings.json", HOOKS)
    monkeypatch.setattr(MOD, "harness_config_root", lambda: world.outside / ".claude")
    assert _files(world.outside) == ()


def test_git_timeout_reports_unknown_and_finds_no_local_file(world, tmp_path, monkeypatch):
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_exec(stub / "git", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setattr(MOD, "GIT_PROBE_TIMEOUT_S", 0.3)
    started = time.monotonic()
    files, note = MOD.project_settings_sources(str(world.local / "sub"))
    assert (files, note) == ((), "unknown")
    assert time.monotonic() - started < 4


# ── which settings register a hook ──────────────────────────────────────────

@pytest.mark.parametrize(
    "content, expected",
    [
        (HOOKS, True),
        ({"hooks": {}}, False),
        (PERMISSIONS_ONLY, False),
        ("{not json", True),
        ("[1, 2]", False),
    ],
)
def test_registers_hook(world, content, expected):
    path = _write_json(world.outside / "s.json", content)
    assert MOD.registers_hook([str(path)]) is expected


def test_a_file_that_cannot_be_read_counts_as_registering(world):
    assert MOD.registers_hook([str(world.outside / "missing.json")]) is True


# ── the parent root ─────────────────────────────────────────────────────────

def _parent(monkeypatch, *, project_dir=None, session=None, cwd=None):
    for name in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if project_dir is not None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    if session is not None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    if cwd is not None:
        monkeypatch.chdir(cwd)
    return MOD.parent_project()


def _transcript(home: Path, session: str, directory: Path, cwd: Path, *, junk_first: bool = True) -> Path:
    folder = home / ".claude" / "projects" / MOD._project_dir_name(str(directory))
    folder.mkdir(parents=True, exist_ok=True)
    lines = ['{"type":"summary","summary":"no cwd"}'] if junk_first else []
    lines.append(json.dumps({"type": "user", "cwd": str(cwd)}))
    path = folder / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_parent_root_comes_from_claude_project_dir(world, monkeypatch):
    parent = _parent(monkeypatch, project_dir=world.project, session="s1", cwd=world.outside)
    assert (parent.root, parent.source, parent.hooks) == (str(world.project), "CLAUDE_PROJECT_DIR", True)
    assert parent.files == (str(world.project / ".claude" / "settings.json"),)


def test_parent_root_comes_from_the_transcripts_first_cwd(world, home, monkeypatch):
    _transcript(home, "sess-a", world.project, world.project)
    parent = _parent(monkeypatch, session="sess-a", cwd=world.outside)
    assert (parent.root, parent.source, parent.hooks) == (str(world.project), "transcript", True)


def test_parent_root_walks_up_to_the_ancestor_the_transcript_directory_is_named_after(world, home, monkeypatch):
    _transcript(home, "sess-b", world.project, world.project / "sub")
    parent = _parent(monkeypatch, session="sess-b", cwd=world.outside)
    assert parent.root == str(world.project)


@pytest.mark.parametrize("shape", ["missing", "unreadable", "no-cwd", "no-matching-ancestor"])
def test_parent_root_is_unresolved_and_fails_closed(world, home, monkeypatch, shape):
    if shape == "unreadable":
        folder = home / ".claude" / "projects" / MOD._project_dir_name(str(world.project))
        (folder / "sess-c.jsonl").mkdir(parents=True)
    elif shape == "no-cwd":
        folder = home / ".claude" / "projects" / MOD._project_dir_name(str(world.project))
        folder.mkdir(parents=True)
        (folder / "sess-c.jsonl").write_text('{"type":"summary"}\n')
    elif shape == "no-matching-ancestor":
        _transcript(home, "sess-c", world.project, world.outside)
    parent = _parent(monkeypatch, session="sess-c", cwd=world.outside)
    assert (parent.root, parent.source, parent.session, parent.hooks) == (None, "unresolved", "sess-c", True)


def test_parent_root_is_the_process_cwd_without_a_session(world, monkeypatch):
    parent = _parent(monkeypatch, cwd=world.project)
    assert (parent.root, parent.source, parent.hooks) == (str(world.project), "cwd", True)


def test_parent_in_no_project_registers_no_hook(world, monkeypatch):
    parent = _parent(monkeypatch, project_dir=world.outside)
    assert (parent.files, parent.hooks) == ((), False)


def test_parent_in_a_project_without_hooks_registers_none(world, monkeypatch):
    parent = _parent(monkeypatch, project_dir=world.local)
    assert parent.files and parent.hooks is False


def test_parent_project_file_that_does_not_parse_counts_as_registering(world, monkeypatch):
    _write_json(world.outside / ".claude" / "settings.json", "{broken")
    parent = _parent(monkeypatch, project_dir=world.outside)
    assert parent.hooks is True


def test_parent_with_a_hookless_file_and_a_git_timeout_counts_as_registering(world, tmp_path, monkeypatch):
    _write_json(world.outside / ".claude" / "settings.json", "{}")
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_exec(stub / "git", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setattr(MOD, "GIT_PROBE_TIMEOUT_S", 0.3)
    parent = _parent(monkeypatch, project_dir=world.outside)
    assert parent.files and parent.hooks is True


# ── the decision ────────────────────────────────────────────────────────────

def _state(root="/p", hooks=True):
    return MOD.ParentProject(root, "cwd", None, ("/p/.claude/settings.json",), hooks)


@pytest.mark.parametrize(
    "parent, child_files, verdict",
    [
        (_state(), (), "parent-hooks"),
        (_state(), ("/c/.claude/settings.json",), None),
        (_state(hooks=False), (), None),
        (MOD.ParentProject(None, "unresolved", "s", (), True), (), "parent-unresolved"),
        (MOD.ParentProject(None, "unresolved", "s", (), True), ("/c/.claude/settings.json",), None),
    ],
)
def test_decide_child_project(parent, child_files, verdict):
    assert MOD.decide_child_project(parent, child_files) == verdict


# ── the command line ────────────────────────────────────────────────────────

BASE_ARGV = [
    "--kind", "developer", "--done-criterion", "stub does nothing", "--criterion-type", "measurable",
    "--complexity", "medium", "--effort", "medium", "--budget", "small",
]


def _spawn(tmp_path, home, extra, *, project_dir=None, session=None, dry_run=True, bin_dir=None, cwd=None):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID")}
    env["HOME"] = str(home)
    env["AGENT_RECURSION_DEPTH"] = "0"
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    argv = ["python3", str(SCRIPT), *BASE_ARGV, "--plan", str(plan), *extra]
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run(argv, env=env, cwd=cwd or tmp_path, capture_output=True, text=True, timeout=60)


def test_non_project_child_of_a_project_parent_is_refused(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.outside)], project_dir=world.project)
    assert result.returncode == 2, result.stderr
    assert result.stdout.splitlines()[0] == "Refusal: child-outside-project"
    assert str(world.outside) in result.stderr
    assert str(world.project) in result.stderr
    assert str(world.project / ".claude" / "settings.json") in result.stderr
    assert "--allow-child-outside-project" in result.stderr
    assert "max-recursion-depth" not in result.stderr + result.stdout
    refused = [r for r in _read_cost_rows(home) if r.get("event") == "refused"]
    assert [r["reason"] for r in refused] == ["child-outside-project"]
    assert refused[0]["workdir"] == str(world.outside)


def test_child_in_the_parents_own_project_passes(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.project)], project_dir=world.project)
    assert result.returncode == 0, result.stderr


def test_subdirectory_of_a_project_with_only_a_start_directory_settings_json_is_refused(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.project / "sub")], project_dir=world.project)
    assert result.returncode == 2
    assert result.stdout.splitlines()[0] == "Refusal: child-outside-project"


def test_subdirectory_of_a_repository_with_a_root_settings_local_json_passes(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.local / "sub")], project_dir=world.project)
    assert result.returncode == 0, result.stderr


def test_parent_in_no_project_passes_any_child(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.outside)], project_dir=world.outside)
    assert result.returncode == 0, result.stderr


def test_parent_in_a_project_without_hooks_passes_a_non_project_child(tmp_path, home, world):
    result = _spawn(tmp_path, home, ["--workdir", str(world.outside)], project_dir=world.local)
    assert result.returncode == 0, result.stderr


def test_parent_without_a_session_or_project_dir_is_judged_by_the_process_cwd(tmp_path, home, world):
    refused = _spawn(tmp_path, home, ["--workdir", str(world.outside)], cwd=world.project)
    assert refused.returncode == 2
    passed = _spawn(tmp_path, home, ["--workdir", str(world.outside)], cwd=world.outside)
    assert passed.returncode == 0, passed.stderr


def test_unresolved_parent_session_refuses_a_non_project_child_and_names_the_id(tmp_path, home, world):
    refused = _spawn(tmp_path, home, ["--workdir", str(world.outside)], session="sess-gone", cwd=world.outside)
    assert refused.returncode == 2
    assert "sess-gone" in refused.stderr
    passed = _spawn(tmp_path, home, ["--workdir", str(world.project)], session="sess-gone", cwd=world.outside)
    assert passed.returncode == 0, passed.stderr


def test_parent_root_from_the_transcript_decides_the_cli(tmp_path, home, world):
    _transcript(home, "sess-t", world.project, world.project)
    refused = _spawn(tmp_path, home, ["--workdir", str(world.outside)], session="sess-t", cwd=world.outside)
    assert refused.returncode == 2
    assert str(world.project) in refused.stderr


def test_git_timeout_refuses_a_non_project_child_of_a_project_parent(world, monkeypatch, capsys, tmp_path):
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_exec(stub / "git", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setattr(MOD, "GIT_PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.project))
    stop, override = MOD._child_project_gate(
        Namespace(allow_child_outside_project=None, kind="developer"), str(world.outside)
    )
    assert (stop, override) == (2, None)
    assert capsys.readouterr().out.splitlines()[0] == "Refusal: child-outside-project"


def test_override_passes_with_a_notice_and_no_row_on_dry_run(tmp_path, home, world):
    result = _spawn(
        tmp_path, home, ["--workdir", str(world.outside), "--allow-child-outside-project", "scratch helper"],
        project_dir=world.project,
    )
    assert result.returncode == 0, result.stderr
    assert "scratch helper" in result.stderr
    assert str(world.outside) in result.stderr and str(world.project) in result.stderr
    assert _read_cost_rows(home) == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_blank_override_reason_is_refused(tmp_path, home, world, reason):
    result = _spawn(
        tmp_path, home, ["--workdir", str(world.outside), "--allow-child-outside-project", reason],
        project_dir=world.project,
    )
    assert result.returncode == 2
    assert "non-blank" in result.stderr


def test_override_is_recorded_on_the_spawn_row_only(tmp_path, home, world):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_claude(bin_dir, home, "sess-override", cost_usd=0.01)
    result = _spawn(
        tmp_path, home, ["--workdir", str(world.outside), "--allow-child-outside-project", "scratch helper"],
        project_dir=world.project, dry_run=False, bin_dir=bin_dir,
    )
    assert result.returncode == 0, result.stderr
    rows = _read_cost_rows(home)
    assert [r["event"] for r in rows] == ["spawn"]
    assert rows[0]["child_outside_project_override"] == {
        "reason": "scratch helper", "parent_root": str(world.project), "child_dir": str(world.outside),
    }


def test_a_passing_spawn_carries_no_override_field(tmp_path, home, world):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_claude(bin_dir, home, "sess-plain", cost_usd=0.01)
    result = _spawn(tmp_path, home, ["--workdir", str(world.project)], project_dir=world.project,
                    dry_run=False, bin_dir=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "child_outside_project_override" not in _read_cost_rows(home)[0]


def test_assembled_prompt_is_not_refused(tmp_path, world, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.project))
    monkeypatch.chdir(world.outside)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    prompt = MOD.assembled_prompt([*BASE_ARGV, "--plan", str(plan), "--workdir", str(world.outside)])
    assert prompt
