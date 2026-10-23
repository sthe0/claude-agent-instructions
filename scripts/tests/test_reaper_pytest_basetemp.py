"""Tests for the `pytest-basetemp` reaper.

Hermetic: the temp root, HOME and the config root are redirected under tmp_path, so the
pytest root the reaper resolves is a throw-away one; no test reads or writes the real
temp volume. The runner is driven through execute_pass with only this reaper.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import registry, runner  # noqa: E402
from reaper.builtin import pytest_basetemp as bt  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext  # noqa: E402
from session_scope.registry import ScopeRecord  # noqa: E402

NOW = 2_000_000_000.0
HOUR = 3600.0
OLD = NOW - 5 * HOUR
FRESH = NOW - 0.5 * HOUR
DEAD_PID = 2**22 + 12345


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    base = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("TMPDIR", str(base / "tmp"))
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    directory = bt.pytest_root()
    assert directory.parent == base / "tmp"
    directory.mkdir(parents=True)
    return directory


def make_dir(root: Path, name: str, *, mtime: float = OLD, lock: "str | None" = None,
             lock_mtime: "float | None" = None) -> Path:
    path = root / name
    path.mkdir()
    (path / "test_x0").mkdir()
    (path / "test_x0" / "data.txt").write_text("residue", encoding="utf-8")
    if lock is not None:
        (path / ".lock").write_text(lock, encoding="ascii")
        stamp = mtime if lock_mtime is None else lock_mtime
        os.utime(path / ".lock", (stamp, stamp))
    os.utime(path, (mtime, mtime))
    return path


def make_ctx(root: Path, records: "list[ScopeRecord] | None" = None, *, dry_run: bool = False) -> ReapContext:
    return ReapContext(
        now=NOW, dry_run=dry_run, project_dir=root, deadletter_dir=root / "deadletter",
        scope_records=records or [],
    )


def verdicts_by_name(root: Path, ctx: "ReapContext | None" = None) -> dict:
    return {Path(v.path).name: v for v in bt.scan(ctx or make_ctx(root))}


def hold_cwd(path: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=path)


def test_dir_with_a_live_lock_pid_is_kept(root):
    make_dir(root, "pytest-1", lock=str(os.getpid()))
    verdict = verdicts_by_name(root)["pytest-1"]
    assert (verdict.action, verdict.reason) == (KEEP, f"live lock (pid {os.getpid()})")


def test_old_dir_with_a_dead_pid_lock_is_removed(root):
    make_dir(root, "pytest-2", lock=str(DEAD_PID))
    verdict = verdicts_by_name(root)["pytest-2"]
    assert verdict.action == REMOVE
    assert f"dead lock pid {DEAD_PID}" in verdict.reason


def test_dir_with_a_dead_pid_lock_younger_than_the_floor_is_kept(root):
    make_dir(root, "pytest-3", mtime=FRESH, lock=str(DEAD_PID))
    verdict = verdicts_by_name(root)["pytest-3"]
    assert verdict.action == KEEP and verdict.reason.startswith("fresh")


def test_a_fresh_lock_keeps_an_old_directory(root):
    make_dir(root, "pytest-4", mtime=OLD, lock=str(DEAD_PID), lock_mtime=FRESH)
    verdict = verdicts_by_name(root)["pytest-4"]
    assert verdict.action == KEEP and verdict.reason.startswith("fresh")


@pytest.mark.parametrize("body", ["", "not-a-pid", "0", "-5", "12 34"])
def test_a_garbled_lock_is_kept(root, body):
    make_dir(root, "pytest-5", lock=body)
    verdict = verdicts_by_name(root)["pytest-5"]
    assert (verdict.action, verdict.reason) == (KEEP, "unreadable lock")


def test_old_lockless_dir_is_removed_and_fresh_lockless_dir_is_kept(root):
    make_dir(root, "pytest-6")
    make_dir(root, "pytest-7", mtime=FRESH)
    found = verdicts_by_name(root)
    assert found["pytest-6"].action == REMOVE
    assert found["pytest-7"].action == KEEP and found["pytest-7"].reason.startswith("fresh")


def test_a_process_with_its_cwd_inside_the_dir_keeps_it(root):
    make_dir(root, "pytest-8", lock=str(DEAD_PID))
    nested = root / "pytest-8" / "test_x0"
    holder = hold_cwd(nested)
    try:
        verdict = verdicts_by_name(root)["pytest-8"]
    finally:
        holder.kill()
        holder.wait()
    assert (verdict.action, verdict.reason) == (KEEP, "process cwd inside")


def test_a_dir_owned_by_a_live_scope_record_is_kept(root):
    path = make_dir(root, "pytest-9", lock=str(DEAD_PID))
    record = ScopeRecord(session_id="s", heartbeat_ts=NOW - 60.0, pid=DEAD_PID, cwd=str(path / "test_x0"))
    verdict = verdicts_by_name(root, make_ctx(root, [record]))["pytest-9"]
    assert (verdict.action, verdict.reason) == (KEEP, "owned")


def test_names_other_than_numbered_pytest_dirs_get_no_verdict(root):
    make_dir(root, "pytest-10")
    for name in ("pytest-current", "garbage-1", "pytest-", "pytest-1a", "other", "pytest-12\n"):
        make_dir(root, name)
    assert set(verdicts_by_name(root)) == {"pytest-10"}


def test_a_symlink_named_like_a_numbered_dir_is_kept(root, tmp_path):
    target = make_dir(tmp_path.resolve(), "elsewhere")
    (root / "pytest-11").symlink_to(target)
    verdict = verdicts_by_name(root)["pytest-11"]
    assert (verdict.action, verdict.reason) == (KEEP, "symlink")
    assert bt.remove(str(root / "pytest-11"), make_ctx(root)) is False
    assert (root / "pytest-11").is_symlink() and (target / "test_x0" / "data.txt").exists()


def test_a_dir_of_another_uid_is_kept(root, monkeypatch):
    make_dir(root, "pytest-12", lock=str(DEAD_PID))
    monkeypatch.setattr(bt, "_current_uid", lambda: os.getuid() + 1)
    verdict = verdicts_by_name(root)["pytest-12"]
    assert (verdict.action, verdict.reason) == (KEEP, "foreign owner")


def test_an_error_while_judging_one_dir_keeps_only_that_dir(root, monkeypatch):
    make_dir(root, "pytest-13", lock=str(DEAD_PID))
    make_dir(root, "pytest-14")

    def broken(pid: int) -> bool:
        raise OSError("boom")

    monkeypatch.setattr(bt, "pid_alive", broken)
    found = verdicts_by_name(root)
    assert found["pytest-13"].action == KEEP and found["pytest-13"].reason == "error: OSError"
    assert found["pytest-14"].action == REMOVE
    assert bt.remove(str(root / "pytest-13"), make_ctx(root)) is False
    assert (root / "pytest-13").exists()


def test_an_unavailable_cwd_scan_keeps_every_dir(root, monkeypatch):
    make_dir(root, "pytest-15", lock=str(DEAD_PID))

    def no_proc() -> "list[str]":
        raise OSError("no /proc")

    monkeypatch.setattr(bt, "cwd_holders", no_proc)
    verdict = verdicts_by_name(root)["pytest-15"]
    assert (verdict.action, verdict.reason) == (KEEP, "cwd scan unavailable")
    assert bt.remove(str(root / "pytest-15"), make_ctx(root)) is False
    assert (root / "pytest-15").exists()


def test_remove_deletes_a_tree_holding_a_read_only_subdir(root):
    path = make_dir(root, "pytest-15")
    locked = path / "locked"
    locked.mkdir()
    (locked / "inner.txt").write_text("x", encoding="utf-8")
    locked.chmod(0o500)
    assert bt.remove(str(path), make_ctx(root)) is True
    assert not path.exists()


def test_remove_keeps_a_dir_whose_lock_became_live_after_the_scan(root):
    path = make_dir(root, "pytest-16")
    ctx = make_ctx(root)
    assert verdicts_by_name(root, ctx)["pytest-16"].action == REMOVE
    (path / ".lock").write_text(str(os.getpid()), encoding="ascii")
    os.utime(path / ".lock", (OLD, OLD))
    os.utime(path, (OLD, OLD))
    assert bt.remove(str(path), ctx) is False
    assert (path / "test_x0" / "data.txt").exists()


def test_remove_keeps_a_dir_a_process_entered_after_the_scan(root):
    path = make_dir(root, "pytest-17")
    ctx = make_ctx(root)
    assert verdicts_by_name(root, ctx)["pytest-17"].action == REMOVE
    holder = hold_cwd(path)
    try:
        assert bt.remove(str(path), ctx) is False
    finally:
        holder.kill()
        holder.wait()
    assert (path / "test_x0" / "data.txt").exists()


def test_remove_succeeds_twice_on_the_same_path(root):
    path = make_dir(root, "pytest-18")
    ctx = make_ctx(root)
    assert bt.remove(str(path), ctx) is True
    assert bt.remove(str(path), ctx) is True
    assert not path.exists()


def test_remove_refuses_a_path_that_is_not_a_direct_child_of_the_root(root, tmp_path):
    outside = make_dir(tmp_path.resolve(), "pytest-19")
    (root / "wrapper").mkdir()
    nested = make_dir(root / "wrapper", "pytest-21")
    garbage = make_dir(root, "garbage-1")
    current = make_dir(root, "pytest-current")
    for candidate in (outside, nested, garbage, current):
        assert bt.remove(str(candidate), make_ctx(root)) is False
        assert candidate.exists()


def test_missing_root_yields_no_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "nowhere"))
    assert bt.scan(make_ctx(tmp_path)) == []


def test_pass_removes_only_the_dead_dirs_and_dry_run_removes_nothing(root):
    make_dir(root, "pytest-22", lock=str(DEAD_PID))
    make_dir(root, "pytest-23", lock=str(os.getpid()))
    reaper = registry.Reaper(bt.NAME, "builtin", bt.__file__, bt)
    stamps, log = root.parent / "stamps", root.parent / "removed.jsonl"

    def run(*, dry_run: bool) -> str:
        out = io.StringIO()
        runner.execute_pass(
            [reaper], make_ctx(root, dry_run=dry_run), dry_run=dry_run, force_run=not dry_run, only=None,
            stamps=stamps, log_path=log, out=out, err=io.StringIO(),
        )
        return out.getvalue()

    assert f"{bt.NAME} REMOVE {root / 'pytest-22'} " in run(dry_run=True)
    assert (root / "pytest-22").exists() and not log.exists()
    run(dry_run=False)
    assert not (root / "pytest-22").exists() and (root / "pytest-23").exists()


def test_production_discovery_finds_pytest_basetemp_as_builtin(tmp_path):
    found = registry.discover(tmp_path)
    assert ("pytest-basetemp", "builtin") in [(r.name, r.layer) for r in found]
