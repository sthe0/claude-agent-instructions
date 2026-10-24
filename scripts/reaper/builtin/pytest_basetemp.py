"""Reaper `pytest-basetemp`: numbered pytest run directories whose run is over.

Difficulty removed: every pytest run creates `<temp root>/pytest-of-<user>/pytest-N` and
pytest itself keeps only the newest few, but only when a later run reaches its own cleanup;
runs that were killed, or that sit on a machine where many suites run at once, leave
directories that nothing reclaims, and the temp volume fills.

Lock contract this reaper relies on (installed pytest 8.3.5, `_pytest/pathlib.py`): the run
that creates `pytest-N` writes its pid into `pytest-N/.lock` (created with O_EXCL) and
removes the file at exit (not in a forked child). pytest's own cleanup treats a missing
lock, or one older than three days, as deletable.

A directory is proven dead, not guessed. scan() looks only at `pytest-<digits>` direct
children of the current user's pytest root and proposes removal when ALL hold:

* it is a real directory (not a symlink) owned by the current uid;
* the newer of its own and its `.lock`'s mtime is older than SHORT_FLOOR_HOURS;
* it has no `.lock`, or the lock holds a pid that is not alive on this host;
* no process has its cwd at or inside it;
* no live session-scope record owns it.

Everything else examined is kept with a reason; any error while judging one directory
resolves to keep. `pytest-current`, `garbage-*` and any other name get no verdict.

The pytest root is also this reaper's pressure path: a disk-pressure pass (`runner --on-pressure`)
makes it due before its 24-hour throttle, under the same rules.

remove() judges the directory again, with a fresh /proc scan, before deleting it, so a run
that started between scan and removal is kept.
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from reaper.contract import KEEP, REMOVE, ReapContext, Verdict
from session_scope.registry import pid_alive

NAME = "pytest-basetemp"
THROTTLE_HOURS = 24.0
# Backstop only; the lock pid, the cwd scan and scope ownership are the proof.
# pytest's own 3-day lock timeout is not used: it is too long to bound the stock on a busy host.
SHORT_FLOOR_HOURS = 2.0
LOCK_NAME = ".lock"
NUMBERED_DIR = re.compile(r"pytest-[0-9]+")


def _current_uid() -> int:
    return os.getuid()


def pytest_root() -> Path:
    return Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"


def cwd_holders() -> "list[str]":
    """The cwd of every process whose /proc entry is readable; unreadable entries are skipped.

    Raises OSError without /proc.
    """
    held = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            held.append(os.readlink(f"/proc/{entry}/cwd"))
        except OSError:
            continue
    return held


def _inside(container: str, candidate: str) -> bool:
    return candidate == container or candidate.startswith(container + os.sep)


def _keep(path: Path, reason: str) -> Verdict:
    return Verdict(str(path), KEEP, reason)


def judge(path: Path, ctx: ReapContext, holders: "list[str] | None") -> Verdict:
    try:
        return _judge(path, ctx, holders)
    except Exception as exc:
        return _keep(path, f"error: {type(exc).__name__}")


def _judge(path: Path, ctx: ReapContext, holders: "list[str] | None") -> Verdict:
    if not NUMBERED_DIR.fullmatch(path.name):
        return _keep(path, "not a numbered pytest directory")
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return _keep(path, "symlink")
    if not stat.S_ISDIR(st.st_mode):
        return _keep(path, "not a directory")
    if st.st_uid != _current_uid():
        return _keep(path, "foreign owner")
    newest = st.st_mtime
    lock_state = "no lock"
    lock = path / LOCK_NAME
    try:
        lock_st = os.lstat(lock)
    except FileNotFoundError:
        lock_st = None
    if lock_st is not None:
        if not stat.S_ISREG(lock_st.st_mode):
            return _keep(path, "unreadable lock")
        newest = max(newest, lock_st.st_mtime)
    age_hours = (ctx.now - newest) / 3600.0
    if age_hours < SHORT_FLOOR_HOURS:
        return _keep(path, f"fresh ({age_hours:.1f}h < {SHORT_FLOOR_HOURS:.0f}h floor)")
    if lock_st is not None:
        try:
            pid = int(lock.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return _keep(path, "unreadable lock")
        if pid <= 0:
            return _keep(path, "unreadable lock")
        if pid_alive(pid):
            return _keep(path, f"live lock (pid {pid})")
        lock_state = f"dead lock pid {pid}"
    if holders is None:
        return _keep(path, "cwd scan unavailable")
    real = os.path.realpath(path)
    if any(_inside(real, held) for held in holders):
        return _keep(path, "process cwd inside")
    if ctx.owned_path(str(path)):
        return _keep(path, "owned")
    return Verdict(str(path), REMOVE, f"{lock_state}, idle {age_hours:.0f}h")


def _holders_or_none() -> "list[str] | None":
    try:
        return cwd_holders()
    except OSError:
        return None


def pressure_paths(ctx: ReapContext) -> "list[str]":
    return [str(pytest_root())]


def scan(ctx: ReapContext) -> "list[Verdict]":
    root = pytest_root()
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    holders = _holders_or_none()
    return [judge(root / name, ctx, holders) for name in names if NUMBERED_DIR.fullmatch(name)]


def _make_writable(top: str) -> None:
    pending = [top]
    while pending:
        current = pending.pop()
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            continue
        if not stat.S_ISDIR(mode):
            continue
        try:
            os.chmod(current, stat.S_IMODE(mode) | 0o700)
            pending.extend(os.path.join(current, name) for name in os.listdir(current))
        except OSError:
            continue


def _rmtree(path: str) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except PermissionError:
        _make_writable(path)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return


def remove(path: str, ctx: ReapContext) -> bool:
    target = Path(path)
    if not NUMBERED_DIR.fullmatch(target.name):
        return False
    real = os.path.realpath(target)
    if os.path.dirname(real) != os.path.realpath(pytest_root()) or os.path.basename(real) != target.name:
        return False
    if not os.path.lexists(target):
        return True
    if judge(target, ctx, _holders_or_none()).action != REMOVE:
        return False
    _rmtree(str(target))
    return True
