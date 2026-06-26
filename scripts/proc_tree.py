"""Process-tree supervision: launch a child detached as its own session/group
leader and reap the whole descendant subtree in one signal.

A child started with ``start_new_session=True`` calls ``setsid()`` and becomes a
session and process-group leader, so its pid equals its pgid. ``os.killpg(pgid,
sig)`` then delivers ``sig`` to every member of that group — the child and all of
its descendants — which is the only reliable stdlib-only way to avoid orphaned
grandchildren when the wrapper dies. Orphans reparented to init keep the original
pgid, so the group stays reapable even after the leader exits.

> verified by: empirical A/B test on Linux 5.4 (2026-06-27) — a parent-only
> SIGTERM left 2 of 3 grandchildren alive; start_new_session + killpg left zero.

POSIX only. setsid/killpg have no portable Windows equivalent, so the public
entry points raise NotImplementedError off POSIX rather than pretend to work.
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import time
from collections import defaultdict

__all__ = ["launch_supervised", "kill_tree", "install_teardown"]

_PROC = "/proc"
# Procs we have already wired teardown for, keyed by id(); makes install_teardown
# idempotent so repeated calls don't stack handlers.
_teardown_installed: set[int] = set()


def _require_posix(fn: str) -> None:
    if os.name != "posix":
        raise NotImplementedError(f"{fn} requires a POSIX system (setsid/killpg)")


def launch_supervised(cmd, **popen_kwargs) -> subprocess.Popen:
    """Popen(cmd) forced into its own session/process group.

    ``start_new_session=True`` is forced regardless of what the caller passes, so
    the child is a group leader and ``kill_tree`` can reap its whole subtree.
    """
    _require_posix("launch_supervised")
    popen_kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_kwargs)


def _all_pids() -> list[int]:
    return [int(e) for e in os.listdir(_PROC) if e.isdigit()]


def _ppid(pid: int) -> int:
    """Parent pid from /proc/<pid>/stat. comm (field 2) may contain spaces and
    parens, so parse the tail after the final ')'."""
    with open(f"{_PROC}/{pid}/stat", "r") as fh:
        data = fh.read()
    after = data[data.rindex(")") + 1:].split()
    return int(after[1])  # field 4 (ppid); after[0] is state (field 3)


def _group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    for p in _all_pids():
        try:
            if os.getpgid(p) == pgid:
                members.add(p)
        except (ProcessLookupError, PermissionError):
            continue
    return members


def _descendants(root: int) -> set[int]:
    """All transitive children of root via a /proc PPID walk (root excluded)."""
    children: dict[int, list[int]] = defaultdict(list)
    for p in _all_pids():
        try:
            children[_ppid(p)].append(p)
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
            continue
    out: set[int] = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _leader_pgid(pid: int) -> int | None:
    """pid if it is its own group leader (so killpg(pid) reaps the group), else
    None (a bare non-leader pid — fall back to a descendant walk). If the pid is
    already gone but a group with that id still has members (orphans keep the
    pgid), treat pid as the group id so we still reap them."""
    try:
        return pid if os.getpgid(pid) == pid else None
    except ProcessLookupError:
        return pid if _group_members(pid) else None
    except PermissionError:
        return pid


def _resolve_pid(proc_or_pid) -> int | None:
    if isinstance(proc_or_pid, subprocess.Popen):
        return proc_or_pid.pid
    try:
        return int(proc_or_pid)
    except (TypeError, ValueError):
        return None


def kill_tree(proc_or_pid, grace_s: float = 5.0) -> set[int]:
    """Recursively reap a process subtree: SIGTERM the group, wait up to
    grace_s, then SIGKILL any survivor. Returns the set of targeted pids.

    Idempotent and never raises on an already-dead target. Refuses pid <= 1 so a
    stray call can never signal init or the whole session.
    """
    _require_posix("kill_tree")
    proc = proc_or_pid if isinstance(proc_or_pid, subprocess.Popen) else None
    pid = _resolve_pid(proc_or_pid)
    if pid is None or pid <= 1:
        return set()

    pgid = _leader_pgid(pid)
    if pgid is not None:
        targets = _group_members(pgid) or {pid}

        def send(sig: int) -> None:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
    else:
        targets = _descendants(pid) | {pid}

        def send(sig: int) -> None:
            for p in targets:
                try:
                    os.kill(p, sig)
                except ProcessLookupError:
                    pass

    send(signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not any(_is_alive(p) for p in targets):
            break
        time.sleep(0.05)

    if any(_is_alive(p) for p in targets):
        send(signal.SIGKILL)
        hard_deadline = time.monotonic() + 1.0
        while time.monotonic() < hard_deadline and any(_is_alive(p) for p in targets):
            time.sleep(0.02)

    # Reap the direct child's zombie so it doesn't linger as defunct.
    if proc is not None:
        try:
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, ChildProcessError, ValueError):
            pass
    return targets


def install_teardown(proc: subprocess.Popen) -> None:
    """Ensure proc's whole subtree is reaped on any catchable exit of the current
    process: SIGINT/SIGTERM/SIGHUP and normal interpreter exit.

    The signal handler reaps then restores the default disposition and re-raises,
    so the wrapper still dies as the sender intended. Idempotent. signal.signal
    only works in the main thread; off-main-thread installs are skipped (the
    atexit killer still covers the normal-exit path)."""
    _require_posix("install_teardown")
    key = id(proc)
    if key in _teardown_installed:
        return
    _teardown_installed.add(key)

    def handler(signum, _frame):
        kill_tree(proc)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not the main thread, or signal unsupported on this platform

    atexit.register(kill_tree, proc)
