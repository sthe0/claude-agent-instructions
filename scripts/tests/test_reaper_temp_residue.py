"""Tests for the `temp-residue` reaper (scripts/reaper/builtin/temp_residue.py).

Hermetic: the three roots (the /tmp-like sandbox root, the session temp root, the agent home's
jobs) and the agent home are built under tmp_path and handed to the module through its own
seams (SANDBOX_ROOT, tempfile.tempdir, CLAUDE_CONFIG_DIR); no test reads the real /tmp
candidates, the real session temp root or the real agent home. Time is ``ctx.now``; the
/proc sweep is replaced by an empty one except where a test starts a real holder.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import config as agentctl_config  # noqa: E402
from reaper import liveness, registry, runner  # noqa: E402
from reaper.builtin import temp_residue  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext  # noqa: E402
from session_scope import registry as scope_registry  # noqa: E402

DAY = 86400.0
SLUG = re.compile(r"[a-z]+(-[a-z]+)*")


def git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    }
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return done.stdout


class World:
    canonical: "Path | None" = None

    def __init__(self, root: Path):
        self.root = root
        self.sandbox = root / "sandbox"
        self.session = root / "session-tmp"
        self.home = root / "home"
        self.jobs = self.home / "jobs"
        for path in (self.sandbox, self.session, self.jobs):
            path.mkdir(parents=True)

    def candidate(self, parent: Path, name: str, payload: int = 3000) -> Path:
        path = parent / name
        (path / "inner").mkdir(parents=True)
        (path / "inner" / "blob").write_bytes(b"x" * payload)
        return path

    def sandbox_dir(self, suffix: str = "a1") -> Path:
        return self.candidate(self.sandbox, f"instruction-sandbox.{suffix}")

    def session_dir(self, suffix: str = "b2") -> Path:
        return self.candidate(self.session, f"tmp.{suffix}")

    def job_dir(self, job: str = "job1", suffix: str = "c3") -> Path:
        tmp = self.jobs / job / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return self.candidate(tmp, f"sbx.{suffix}")

    def canonical_repo(self) -> Path:
        """A repository with one commit and a second branch, standing in for the canonical checkout."""
        repo = self.root / "canonical"
        git(repo.parent, "init", "-q", "-b", "main", str(repo))
        (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.env\nsecrets/\n", encoding="utf-8")
        git(repo, "add", "tracked.txt", ".gitignore")
        git(repo, "commit", "-q", "-m", "one")
        git(repo, "branch", "side")
        return repo

    def shared_sandbox(self, parent: "Path | None" = None, suffix: str = "s1") -> Path:
        """The layout scripts/instruction-sandbox.sh builds: a shared, detached, origin-less clone at
        ``core`` and ``home/claude-agent-instructions`` linked to it."""
        repo = getattr(self, "canonical", None) or self.canonical_repo()
        self.canonical = repo
        path = (parent or self.sandbox) / f"instruction-sandbox.{suffix}"
        path.mkdir()
        core = path / "core"
        git(path, "clone", "-q", "--shared", "--no-checkout", str(repo), str(core))
        git(core, "checkout", "-q", "--detach")
        git(core, "remote", "remove", "origin")
        (path / "home").mkdir()
        (path / "home" / "claude-agent-instructions").symlink_to(core)
        return path

    def opt_in(self, value: str = "true") -> None:
        path = self.home / "reaper" / "opt-in.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{temp_residue.NAME} = {value}\n", encoding="utf-8")

    def ctx(self, *, dry_run: bool = False, records=(), **kw) -> ReapContext:
        return ReapContext(
            now=kw.pop("now", time.time() + 5 * DAY), dry_run=dry_run, project_dir=self.root / "project",
            deadletter_dir=self.root / "deadletter", scope_records=list(records), **kw,
        )


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    built = World(tmp_path.resolve())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(built.home))
    monkeypatch.setenv("HOME", str(built.root / "userhome"))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(temp_residue, "SANDBOX_ROOT", str(built.sandbox))
    monkeypatch.setattr(tempfile, "tempdir", str(built.session))
    monkeypatch.setattr(liveness, "sweep_holders", lambda *a, **k: liveness.HolderSweep())
    return built


def by_path(verdicts) -> dict:
    return {v.path: v for v in verdicts}


def reason_of(verdict) -> str:
    return dict(tag.split("=", 1) for tag in verdict.tags if "=" in tag).get("reason", "")


# ── which directories are candidates ──────────────────────────────────────

def test_each_kind_is_found_at_its_own_root_and_only_by_its_name_pattern(world):
    expected = {world.sandbox_dir(), world.session_dir(), world.job_dir()}
    world.candidate(world.sandbox, "tmp.not-here")
    world.candidate(world.session, "sbx.not-here")
    world.candidate(world.session, "other.dir")
    world.candidate(world.session, "tmp.bad name")
    (world.session / "tmp.plainfile").write_text("x", encoding="utf-8")
    world.candidate(world.session / "tmp.b2", "tmp.nested")
    world.candidate(world.jobs / "job1", "sbx.not-under-tmp")
    verdicts = temp_residue.scan(world.ctx())
    assert {Path(v.path) for v in verdicts} == expected | {world.session / "tmp.plainfile"}
    kinds = {Path(v.path).name: next(t for t in v.tags if t.startswith("kind=")) for v in verdicts}
    assert kinds == {
        "instruction-sandbox.a1": "kind=instruction-sandbox", "tmp.b2": "kind=tmp",
        "sbx.c3": "kind=sbx", "tmp.plainfile": "kind=tmp",
    }


def test_the_instruction_sandbox_pattern_is_found_under_the_session_root_too(world):
    path = world.candidate(world.session, "instruction-sandbox.zz")
    assert str(path) in by_path(temp_residue.scan(world.ctx()))


def test_a_symlink_named_like_a_candidate_is_kept_and_its_target_untouched(world):
    target = world.candidate(world.root, "precious")
    link = world.session / "tmp.link"
    link.symlink_to(target)
    world.opt_in()
    verdict = by_path(temp_residue.scan(world.ctx())).get(str(link))
    assert verdict is None or (verdict.action, reason_of(verdict)) == (KEEP, liveness.NOT_A_DIRECTORY)
    assert temp_residue.remove(str(link), world.ctx()) is False
    assert target.exists() and link.is_symlink()


def test_with_no_roots_present_the_scan_is_empty_and_the_summary_still_renders(world):
    for path in (world.sandbox, world.session, world.jobs):
        os.rmdir(path)
    assert temp_residue.scan(world.ctx()) == []
    assert temp_residue.summary([]).count("kind=") == 3


# ── roots are resolved once ───────────────────────────────────────────────

def test_a_symlinked_session_root_is_scanned_once_at_its_resolved_path(world, monkeypatch):
    link = world.root / "link-to-session"
    link.symlink_to(world.session)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    candidate = world.session_dir()
    verdicts = temp_residue.scan(world.ctx())
    assert [v.path for v in verdicts] == [str(candidate)]
    assert str(world.session) in temp_residue.pressure_paths(world.ctx())
    assert str(link) not in temp_residue.pressure_paths(world.ctx())


def test_a_session_root_equal_to_the_sandbox_root_scans_each_candidate_once(world, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(world.sandbox))
    sandbox_candidate = world.sandbox_dir()
    tmp_candidate = world.candidate(world.sandbox, "tmp.same")
    verdicts = temp_residue.scan(world.ctx())
    assert sorted(v.path for v in verdicts) == sorted([str(sandbox_candidate), str(tmp_candidate)])
    assert temp_residue.pressure_paths(world.ctx()).count(str(world.sandbox)) == 1


def test_a_session_root_that_is_a_symlink_to_the_sandbox_root_is_one_root(world, monkeypatch):
    link = world.root / "link-to-sandbox"
    link.symlink_to(world.sandbox)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    world.sandbox_dir()
    verdicts = temp_residue.scan(world.ctx())
    assert len(verdicts) == 1


# ── the proof of death as the reaper applies it ───────────────────────────

def test_a_dead_old_candidate_of_each_kind_is_removed_in_a_dry_run_and_a_live_pass_with_opt_in(world):
    paths = {world.sandbox_dir(), world.session_dir(), world.job_dir()}
    world.opt_in()
    for dry_run in (True, False):
        verdicts = temp_residue.scan(world.ctx(dry_run=dry_run))
        assert {Path(v.path) for v in verdicts if v.action == REMOVE} == paths
        assert all(v.size >= 3000 for v in verdicts)


def test_a_candidate_inside_the_idle_floor_is_kept_with_the_idle_floor_slug(world):
    path = world.session_dir()
    [verdict] = temp_residue.scan(world.ctx(now=time.time() + 60))
    assert (verdict.path, verdict.action, reason_of(verdict)) == (str(path), KEEP, liveness.IDLE_FLOOR)


def test_the_floor_is_three_days_and_moves_only_with_the_module_constant_or_the_clock(world, monkeypatch):
    assert temp_residue.IDLE_FLOOR_SECONDS == 3 * 86400
    top = world.session_dir()
    newest = max(os.lstat(p).st_ctime for p in (top, top / "inner", top / "inner" / "blob"))
    for key in ("REAPER_IDLE_FLOOR_SECONDS", "TEMP_RESIDUE_IDLE_FLOOR", "IDLE_FLOOR_SECONDS"):
        monkeypatch.setenv(key, "0")
    world.opt_in()
    with (world.home / "reaper" / "opt-in.toml").open("a", encoding="utf-8") as handle:
        handle.write("idle-floor = 0\n")
    inside = temp_residue.scan(world.ctx(now=newest + 3 * DAY - 30))
    outside = temp_residue.scan(world.ctx(now=newest + 3 * DAY + 30))
    assert [v.action for v in inside] == [KEEP] and [v.action for v in outside] == [REMOVE]
    monkeypatch.setattr(temp_residue, "IDLE_FLOOR_SECONDS", 10 * DAY)
    assert [v.action for v in temp_residue.scan(world.ctx(now=newest + 3 * DAY + 30))] == [KEEP]


def test_a_candidate_of_a_live_session_is_kept_session_owned(world):
    path = world.session_dir()
    record = scope_registry.ScopeRecord(session_id="s1", heartbeat_ts=time.time() + 5 * DAY, cwd=str(path / "inner"))
    [verdict] = temp_residue.scan(world.ctx(records=[record]))
    assert reason_of(verdict) == liveness.SESSION_OWNED


def test_a_candidate_a_process_holds_is_kept_held_by_the_real_sweep(world, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.home))
    monkeypatch.setattr(temp_residue, "SANDBOX_ROOT", str(world.sandbox))
    monkeypatch.setattr(tempfile, "tempdir", str(world.session))
    path = world.session_dir()
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        cwd=path / "inner", stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        [verdict] = temp_residue.scan(world.ctx())
        assert (verdict.action, reason_of(verdict)) == (KEEP, liveness.HELD)
    finally:
        holder.kill()
        holder.wait()


def test_an_incomplete_sweep_keeps_every_candidate(world, monkeypatch):
    world.session_dir()
    world.sandbox_dir()
    monkeypatch.setattr(liveness, "sweep_holders", lambda *a, **k: liveness.HolderSweep(incomplete="/proc/1/cwd: denied"))
    assert {reason_of(v) for v in temp_residue.scan(world.ctx())} == {liveness.HOLDER_SWEEP_INCOMPLETE}


# ── the disposable shared clone of an instruction sandbox ─────────────────

def verdict_of(world, path: Path):
    return by_path(temp_residue.scan(world.ctx(dry_run=True)))[str(path)]


def assert_kept_as_nested_git(world, path: Path) -> None:
    verdict = verdict_of(world, path)
    assert (verdict.action, reason_of(verdict)) == (KEEP, liveness.NESTED_GIT)


def test_a_clean_shared_clone_sandbox_is_removable_and_its_canonical_repository_survives(world):
    path = world.shared_sandbox()
    world.opt_in()
    assert verdict_of(world, path).action == REMOVE
    assert temp_residue.remove(str(path), world.ctx()) is True
    assert not path.exists() and (world.canonical / ".git" / "objects").is_dir()
    assert git(world.canonical, "rev-parse", "--verify", "HEAD").strip()


def test_judging_a_shared_clone_writes_nothing_into_it(world):
    path = world.shared_sandbox()

    def snapshot():
        return sorted(
            (os.path.join(d, n), os.lstat(os.path.join(d, n)).st_mtime_ns)
            for d, dirs, files in os.walk(path) for n in dirs + files
        )

    before = snapshot()
    assert verdict_of(world, path).action == REMOVE
    assert snapshot() == before


def test_a_local_commit_that_the_alternate_does_not_hold_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    git(path / "core", "commit", "-q", "--allow-empty", "-m", "local work")
    assert_kept_as_nested_git(world, path)


def test_a_branch_with_a_commit_the_alternate_does_not_hold_keeps_the_sandbox_though_head_is_held(world):
    path = world.shared_sandbox()
    git(path / "core", "switch", "-q", "-c", "work")
    git(path / "core", "commit", "-q", "--allow-empty", "-m", "local work")
    git(path / "core", "checkout", "-q", "--detach", "HEAD~1")
    assert_kept_as_nested_git(world, path)


def commit_in(repo: Path, message: str = "own work") -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_a_commit_only_the_reflog_reaches_keeps_the_sandbox_after_a_reset(world):
    path = world.shared_sandbox()
    commit_in(path / "core")
    git(path / "core", "reset", "-q", "--hard", "HEAD~")
    assert_kept_as_nested_git(world, path)


def test_a_detached_commit_left_by_checking_back_out_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    held = git(path / "core", "rev-parse", "HEAD").strip()
    commit_in(path / "core")
    git(path / "core", "checkout", "-q", "--detach", held)
    assert_kept_as_nested_git(world, path)


def test_a_deleted_branch_that_had_a_commit_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    held = git(path / "core", "rev-parse", "HEAD").strip()
    git(path / "core", "switch", "-q", "-c", "work")
    commit_in(path / "core")
    git(path / "core", "checkout", "-q", "--detach", held)
    git(path / "core", "branch", "-q", "-D", "work")
    assert "refs/heads/work" not in git(path / "core", "for-each-ref")
    assert_kept_as_nested_git(world, path)


def test_a_dropped_stash_entry_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    (path / "core" / "tracked.txt").write_text("stashed\n", encoding="utf-8")
    git(path / "core", "stash", "push", "-q")
    git(path / "core", "stash", "drop", "-q")
    assert_kept_as_nested_git(world, path)


def test_an_own_commit_that_was_packed_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    commit_in(path / "core")
    git(path / "core", "reset", "-q", "--hard", "HEAD~")
    git(path / "core", "repack", "-q", "-d")
    counts = git(path / "core", "count-objects", "-v")
    assert "count: 0" in counts and "in-pack: 0" not in counts
    assert_kept_as_nested_git(world, path)


def test_a_linked_worktree_with_a_commit_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    linked = world.root / "linked-worktree"
    git(path / "core", "worktree", "add", "-q", "--detach", str(linked))
    commit_in(linked)
    assert_kept_as_nested_git(world, path)


def test_a_linked_worktree_that_holds_no_commit_still_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    git(path / "core", "worktree", "add", "-q", "--detach", str(world.root / "linked-worktree"))
    assert "count: 0" in git(path / "core", "count-objects", "-v")
    assert_kept_as_nested_git(world, path)


def test_a_repository_with_no_alternates_and_no_objects_keeps_the_sandbox(world):
    path = world.sandbox / "instruction-sandbox.empty"
    (path / "core").mkdir(parents=True)
    git(path / "core", "init", "-q")
    assert_kept_as_nested_git(world, path)


def test_an_ignored_file_keeps_the_sandbox(world):
    for number, name in enumerate((".env", "secrets/token")):
        path = world.shared_sandbox(suffix=f"ig{number}")
        (path / "core" / name).parent.mkdir(exist_ok=True)
        (path / "core" / name).write_text("mine\n", encoding="utf-8")
        assert_kept_as_nested_git(world, path)


def test_a_file_excluded_through_the_clones_own_exclude_file_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    (path / "core" / ".git" / "info" / "exclude").write_text("notes.txt\n", encoding="utf-8")
    (path / "core" / "notes.txt").write_text("mine\n", encoding="utf-8")
    assert_kept_as_nested_git(world, path)


def test_an_untracked_file_is_seen_though_the_clones_config_hides_untracked_files(world):
    path = world.shared_sandbox()
    git(path / "core", "config", "status.showUntrackedFiles", "no")
    (path / "core" / "new.txt").write_text("new\n", encoding="utf-8")
    assert git(path / "core", "status", "--porcelain") == ""
    assert_kept_as_nested_git(world, path)


def test_an_assume_unchanged_edit_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    git(path / "core", "update-index", "--assume-unchanged", "tracked.txt")
    (path / "core" / "tracked.txt").write_text("edited\n", encoding="utf-8")
    assert git(path / "core", "status", "--porcelain") == ""
    assert_kept_as_nested_git(world, path)


def test_a_skip_worktree_edit_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    git(path / "core", "update-index", "--skip-worktree", "tracked.txt")
    (path / "core" / "tracked.txt").write_text("edited\n", encoding="utf-8")
    assert git(path / "core", "status", "--porcelain") == ""
    assert_kept_as_nested_git(world, path)


def test_ignored_bytecode_alone_leaves_the_sandbox_removable(world):
    path = world.shared_sandbox()
    (path / "core" / "stray.pyc").write_bytes(b"\0")
    for directory in ("__pycache__", "pkg/__pycache__/deeper"):
        (path / "core" / directory).mkdir(parents=True)
        (path / "core" / directory / "m.cpython-312.pyc").write_bytes(b"\0")
    assert verdict_of(world, path).action == REMOVE


def test_a_pycache_directory_holding_anything_but_bytecode_keeps_the_sandbox(world):
    for number, setup in enumerate(("notes", "link")):
        path = world.shared_sandbox(suffix=f"pc{number}")
        cache = path / "core" / "__pycache__"
        cache.mkdir()
        if setup == "notes":
            (cache / "notes.txt").write_text("mine\n", encoding="utf-8")
        else:
            (cache / "m.pyc").symlink_to(path / "core" / "tracked.txt")
        assert_kept_as_nested_git(world, path)


def test_a_modified_tracked_file_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    (path / "core" / "tracked.txt").write_text("edited\n", encoding="utf-8")
    assert_kept_as_nested_git(world, path)


def test_an_untracked_file_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    (path / "core" / "new.txt").write_text("new\n", encoding="utf-8")
    assert_kept_as_nested_git(world, path)


def test_a_git_that_exits_non_zero_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    (path / "core" / ".git" / "index").write_bytes(b"not an index")
    assert_kept_as_nested_git(world, path)


def test_a_full_clone_without_alternates_keeps_the_sandbox(world):
    repo = world.canonical_repo()
    path = world.sandbox / "instruction-sandbox.full"
    path.mkdir()
    git(path, "clone", "-q", str(repo), str(path / "core"))
    assert not (path / "core" / ".git" / "objects" / "info" / "alternates").exists()
    assert_kept_as_nested_git(world, path)


def test_an_alternate_that_names_a_missing_objects_directory_keeps_the_sandbox(world):
    path = world.shared_sandbox()
    alternates = path / "core" / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(world.root / "gone" / ".git" / "objects") + "\n", encoding="utf-8")
    assert_kept_as_nested_git(world, path)


def test_an_alternate_repository_inside_the_candidate_keeps_the_sandbox(world):
    path = world.sandbox / "instruction-sandbox.self"
    path.mkdir()
    inner = path / "canonical.git"
    git(path, "clone", "-q", "--bare", str(world.canonical_repo()), str(inner))
    git(path, "clone", "-q", "--shared", str(inner), str(path / "core"))
    assert_kept_as_nested_git(world, path)


def test_a_second_git_entry_elsewhere_in_the_candidate_keeps_the_sandbox(world):
    for number, where in enumerate(("home/other/.git", "core/vendor/.git", "extra/.git")):
        path = world.shared_sandbox(suffix=f"two{number}")
        (path / where).mkdir(parents=True)
        assert_kept_as_nested_git(world, path)


def test_a_failing_or_timing_out_git_keeps_the_sandbox(world, monkeypatch):
    path = world.shared_sandbox()
    monkeypatch.setattr(temp_residue, "_git", lambda *a, **k: None)
    assert_kept_as_nested_git(world, path)


def test_git_that_times_out_or_cannot_start_yields_no_answer(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)

    def missing(*a, **k):
        raise FileNotFoundError(2, "git")

    for failure in (timeout, missing):
        monkeypatch.setattr(subprocess, "run", failure)
        assert temp_residue._git(["status"]) is None


def test_git_runs_with_a_scrubbed_environment_and_a_timeout(world, monkeypatch):
    path = world.shared_sandbox()
    seen = []
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setenv("GIT_INDEX_FILE", str(world.root / "absent-index"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    monkeypatch.setattr(subprocess, "run", spy)
    assert verdict_of(world, path).action == REMOVE
    assert seen and all(k["timeout"] == temp_residue.GIT_TIMEOUT_S for k in seen)
    assert all("GIT_INDEX_FILE" not in k["env"] and "GIT_SSH_COMMAND" not in k["env"] for k in seen)
    assert all(k["env"]["GIT_OPTIONAL_LOCKS"] == "0" for k in seen)


def test_a_program_the_clones_own_config_names_is_not_started_by_judging(world):
    path = world.shared_sandbox()
    marker = world.root / "started"
    hook = world.root / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    git(path / "core", "config", "core.fsmonitor", str(hook))
    assert verdict_of(world, path).action == REMOVE
    assert not marker.exists()


def test_the_exemption_is_for_the_instruction_sandbox_kind_only(world):
    world.shared_sandbox()
    clone_as_tmp = world.session / "tmp.clone"
    shutil.copytree(world.sandbox / "instruction-sandbox.s1", clone_as_tmp, symlinks=True)
    assert_kept_as_nested_git(world, clone_as_tmp)


# ── opt-in ────────────────────────────────────────────────────────────────

def test_without_opt_in_a_live_pass_keeps_what_it_would_remove_and_says_why(world):
    path = world.session_dir()
    [verdict] = temp_residue.scan(world.ctx())
    assert (verdict.path, verdict.action, reason_of(verdict)) == (str(path), KEEP, temp_residue.OPT_IN_OFF)
    assert verdict.size >= 3000 and "opt-in" in verdict.reason


def test_without_opt_in_a_dry_run_still_shows_remove(world):
    world.session_dir()
    [verdict] = temp_residue.scan(world.ctx(dry_run=True))
    assert verdict.action == REMOVE


@pytest.mark.parametrize("content", ["temp-residue = false\n", 'temp-residue = "true"\n', "temp-residue = 1\n", "other = true\n", "= broken ["])
def test_only_the_boolean_true_turns_the_reaper_on(world, content):
    path = world.session_dir()
    (world.home / "reaper").mkdir()
    (world.home / "reaper" / "opt-in.toml").write_text(content, encoding="utf-8")
    [verdict] = temp_residue.scan(world.ctx())
    assert reason_of(verdict) == temp_residue.OPT_IN_OFF
    assert temp_residue.remove(str(path), world.ctx()) is False and path.exists()


def test_remove_refuses_without_opt_in_even_for_a_dead_candidate(world):
    path = world.session_dir()
    assert temp_residue.remove(str(path), world.ctx()) is False
    assert path.exists()


# ── remove() ──────────────────────────────────────────────────────────────

def test_remove_deletes_a_dead_candidate_of_each_kind(world):
    paths = [world.sandbox_dir(), world.session_dir(), world.job_dir()]
    world.opt_in()
    for path in paths:
        assert temp_residue.remove(str(path), world.ctx()) is True
        assert not path.exists()


def test_remove_judges_again_and_refuses_what_became_held_or_fresh(world):
    path = world.session_dir()
    world.opt_in()
    assert temp_residue.remove(str(path), world.ctx(now=time.time())) is False
    held = liveness.HolderSweep(held={"cwd": [str(path)], "exe": [], "fd": [], "maps": []})
    original = liveness.sweep_holders
    liveness.sweep_holders = lambda *a, **k: held
    try:
        assert temp_residue.remove(str(path), world.ctx()) is False
    finally:
        liveness.sweep_holders = original
    assert path.exists()


def test_remove_refuses_a_path_that_is_not_a_current_candidate(world, tmp_path):
    world.opt_in()
    stranger = world.candidate(world.root, "tmp.elsewhere")
    inside = world.session_dir() / "inner"
    assert temp_residue.remove(str(stranger), world.ctx()) is False
    assert temp_residue.remove(str(inside), world.ctx()) is False
    assert stranger.exists() and inside.exists()


def test_remove_through_a_symlinked_spelling_of_the_root_removes_the_candidate_once(world, monkeypatch):
    path = world.session_dir()
    world.opt_in()
    link = world.root / "alias"
    link.symlink_to(world.session)
    assert temp_residue.remove(str(link / "tmp.b2"), world.ctx()) is True
    assert not path.exists()


# ── the summary ───────────────────────────────────────────────────────────

def test_the_summary_has_root_lines_every_kind_line_and_slug_only_reason_lines(world):
    world.session_dir()
    world.candidate(world.session, "tmp.fresh")
    world.opt_in()
    now = time.time() + 5 * DAY
    os.utime(world.session / "tmp.fresh", (now + DAY, now + DAY))
    verdicts = temp_residue.scan(world.ctx(now=now))
    lines = temp_residue.summary(verdicts).splitlines()
    roots = [line for line in lines if line.startswith("temp-residue root=")]
    kinds = [line for line in lines if " kind=" in line]
    reasons = [line for line in lines if " reason=" in line]
    assert {line.split("root=")[1] for line in roots} == {str(world.sandbox), str(world.session), str(world.jobs)}
    assert [re.search(r"kind=(\S+)", line).group(1) for line in kinds] == list(temp_residue.KINDS)
    assert all(re.fullmatch(r"temp-residue kind=\S+ removable=\d+ removable_bytes=\d+ kept=\d+ kept_bytes=\d+", line) for line in kinds)
    assert "removable=1" in kinds[1] and "kept=1" in kinds[1]
    assert "removable=0 removable_bytes=0 kept=0 kept_bytes=0" in kinds[0]
    assert reasons and all(
        re.fullmatch(r"temp-residue reason=([a-z-]+) count=\d+ bytes=\d+", line) for line in reasons
    )
    assert {re.search(r"reason=(\S+)", line).group(1) for line in reasons} <= set(temp_residue.REASONS)


def test_an_os_error_text_never_reaches_the_summary_but_stays_in_the_verdict(world, monkeypatch):
    world.session_dir()
    monkeypatch.setattr(temp_residue, "_mounts", lambda: None)
    [verdict] = temp_residue.scan(world.ctx())
    assert reason_of(verdict) == liveness.OS_ERROR
    text = temp_residue.summary([verdict])
    assert "reason=os-error count=1" in text
    for line in text.splitlines():
        if " reason=" in line:
            assert SLUG.fullmatch(line.split(" reason=")[1].split()[0])
    assert "mountinfo" not in text and verdict.reason != liveness.OS_ERROR


def test_every_keep_slug_the_liveness_module_can_emit_is_a_summary_reason():
    emitted = {
        liveness.IDLE_FLOOR, liveness.HELD, liveness.SESSION_OWNED, liveness.FOREIGN_OWNER,
        liveness.NOT_A_DIRECTORY, liveness.MOUNT_INSIDE, liveness.NESTED_GIT,
        liveness.HOLDER_SWEEP_INCOMPLETE, liveness.OS_ERROR, temp_residue.OPT_IN_OFF,
    }
    assert emitted == set(temp_residue.REASONS) and all(SLUG.fullmatch(s) for s in emitted)


# ── through the runner ────────────────────────────────────────────────────

def only_temp_residue(project, warn):
    return [r for r in registry.discover(project, warn=warn) if r.name == temp_residue.NAME]


def test_the_dry_run_prints_remove_lines_and_the_summary_and_writes_nothing(world, capsys, monkeypatch):
    world.session_dir()
    world.candidate(world.session, "tmp.fresh")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root / "project"))
    monkeypatch.setenv("CLAUDE_REAPER_PLUGIN_DIR", str(world.root / "plugins"))
    os.utime(world.session / "tmp.fresh", (time.time() + 9 * DAY, time.time() + 9 * DAY))
    before = sorted(p for p in world.root.rglob("*") if p.is_file() or p.is_dir())
    original_now = time.time
    monkeypatch.setattr(time, "time", lambda: original_now() + 5 * DAY)
    assert runner.main(["--dry-run", "--only", temp_residue.NAME], discover=only_temp_residue) == 0
    out = capsys.readouterr().out
    assert f"{temp_residue.NAME} REMOVE {world.session / 'tmp.b2'}" in out
    assert f"{temp_residue.NAME} KEEP {world.session / 'tmp.fresh'}" in out
    assert f"temp-residue root={world.session}" in out
    assert [line for line in out.splitlines() if " kind=" in line].__len__() == 3
    assert all(
        re.fullmatch(r"temp-residue reason=[a-z-]+ count=\d+ bytes=\d+", line)
        for line in out.splitlines() if " reason=" in line and line.startswith("temp-residue reason=")
    )
    after = sorted(p for p in world.root.rglob("*") if p.is_file() or p.is_dir())
    assert after == before


def test_a_live_pass_removes_with_opt_in_logs_the_row_and_writes_the_kept_snapshot(world, monkeypatch):
    dead = world.session_dir()
    fresh = world.candidate(world.session, "tmp.fresh")
    world.opt_in()
    future = time.time() + 9 * DAY
    os.utime(fresh, (future, future))
    original_now = time.time
    monkeypatch.setattr(time, "time", lambda: original_now() + 5 * DAY)
    assert runner.main(["--force-run", "--only", temp_residue.NAME], discover=only_temp_residue) == 0
    assert not dead.exists() and fresh.exists()
    rows = [json.loads(line) for line in runner.removal_log_path().read_text(encoding="utf-8").splitlines()]
    assert [(r["path"], r["trigger"]) for r in rows] == [(str(dead), "manual")]
    snapshot = json.loads((runner.kept_snapshot_dir() / "temp-residue.json").read_text(encoding="utf-8"))
    assert [(k["path"], k["reason"].split(":")[0]) for k in snapshot["kept"]] == [(str(fresh), liveness.IDLE_FLOOR)]
    assert snapshot["kept"][0]["bytes"] > 0


def test_the_snapshot_lists_every_keep_with_a_reason_when_the_machine_has_not_opted_in(world, monkeypatch):
    dead = world.session_dir()
    world.sandbox_dir()
    original_now = time.time
    monkeypatch.setattr(time, "time", lambda: original_now() + 5 * DAY)
    runner.main(["--force-run", "--only", temp_residue.NAME], discover=only_temp_residue)
    snapshot = json.loads((runner.kept_snapshot_dir() / "temp-residue.json").read_text(encoding="utf-8"))
    assert dead.exists()
    assert len(snapshot["kept"]) == 2
    assert all(k["reason"].startswith(temp_residue.OPT_IN_OFF) and k["bytes"] > 0 for k in snapshot["kept"])
    assert not runner.removal_log_path().exists()


def test_a_dry_run_through_the_runner_writes_no_stamp_snapshot_or_removal_row(world, monkeypatch):
    world.session_dir()
    world.opt_in()
    runner.main(["--dry-run", "--only", temp_residue.NAME], discover=only_temp_residue)
    assert not (world.home / "reaper" / "kept").exists() and not runner.removal_log_path().exists()
    assert not runner.stamp_dir().exists() or not any(runner.stamp_dir().iterdir())


# ── disk pressure ─────────────────────────────────────────────────────────

def test_the_pressure_paths_are_the_resolved_existing_roots_without_duplicates(world):
    assert temp_residue.pressure_paths(world.ctx()) == [str(world.sandbox), str(world.session), str(world.jobs)]
    os.rmdir(world.jobs)
    assert str(world.jobs) not in temp_residue.pressure_paths(world.ctx())


def test_a_pressure_pass_through_the_runner_removes_dead_candidates_and_never_a_held_one(world, monkeypatch):
    dead = [world.sandbox_dir(), world.session_dir(), world.job_dir()]
    held = world.candidate(world.session, "tmp.held")
    world.opt_in()
    monkeypatch.setattr(
        liveness, "sweep_holders",
        lambda *a, **k: liveness.HolderSweep(held={"cwd": [str(held / "inner")], "exe": [], "fd": [], "maps": []}),
    )
    config = world.root / "config.md"
    config.write_text("| Key | Value |\n|---|---|\n| `reaper-pressure-free-percent` | `10` | basis |\n", encoding="utf-8")
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", config)
    monkeypatch.setattr(runner, "free_percent", lambda path: 2.0)
    original_now = time.time
    monkeypatch.setattr(time, "time", lambda: original_now() + 5 * DAY)
    assert runner.main(["--on-pressure"], discover=only_temp_residue) == 0
    assert not any(path.exists() for path in dead) and held.exists()
    [record] = [json.loads(line) for line in runner.pressure_log_path().read_text(encoding="utf-8").splitlines()]
    assert record["removed"] == 3 and record["due"] == [temp_residue.NAME]
    rows = [json.loads(line) for line in runner.removal_log_path().read_text(encoding="utf-8").splitlines()]
    assert {r["trigger"] for r in rows} == {"pressure"}
    snapshot = json.loads((runner.kept_snapshot_dir() / "temp-residue.json").read_text(encoding="utf-8"))
    assert [(k["path"], k["reason"].split(":")[0]) for k in snapshot["kept"]] == [(str(held), liveness.HELD)]


def test_a_pressure_pass_without_opt_in_removes_nothing(world, monkeypatch):
    dead = world.session_dir()
    config = world.root / "config.md"
    config.write_text("| Key | Value |\n|---|---|\n| `reaper-pressure-free-percent` | `10` | basis |\n", encoding="utf-8")
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", config)
    monkeypatch.setattr(runner, "free_percent", lambda path: 2.0)
    original_now = time.time
    monkeypatch.setattr(time, "time", lambda: original_now() + 5 * DAY)
    runner.main(["--on-pressure"], discover=only_temp_residue)
    assert dead.exists()
