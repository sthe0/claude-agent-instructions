#!/usr/bin/env python3
"""cap-run.sh --own-tasks: bound a command's own process tree, not the user's whole count.

Difficulty removed: cap-run.sh's count-based cap is the user's whole task count at start
plus a fixed headroom, so other sessions' later growth consumes the run's headroom and
decides its fate (the R2 landing failure: pytest died with ~1179 "can't start new thread"
while the gate's own tree was nowhere near its headroom). In --own-tasks mode the bound is
the started tree's own task count: every tick the soft RLIMIT_NPROC of each process in
the tree is re-set to (the user's other tasks now) + budget + SLACK, so the kernel keeps
the tree bounded at budget + SLACK while others grow, and the tree is killed if its own
count ever exceeds budget + SLACK.

RLIMIT_NPROC is checked at fork/clone against the real UID's whole task count, using the
forking process's own soft limit (kernel 5.4 counts per real UID; see
memory-global/leaves/fanout-trial-under-cap.md), so raising that soft limit as others grow
keeps the tree's own headroom constant. The supervisor never raises a hard limit, never a
soft limit already in force above it (a nested cap-run's bound is honored through the
supervisor's own inherited soft limit, re-read every tick), and touches only PIDs in the
tree it started.

The soft limit is only re-derived every PERIOD_S: other tasks EXITTING within one tick
transiently widen the tree's bound by that drop until the next tick re-tightens it (the
tree is still killed at that tick if it grew past budget + SLACK). Other tasks growing
never widens it — the kernel refuses the tree's own forks at budget + SLACK as set.

Where the kernel does not offer per-process limit adjustment, cap-run.sh keeps its
count-based cap instead of handing off here (norm: away from Linux, or without
resource.prlimit, the count-based cap stays).
"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import time

PERIOD_S = 0.5  # the tick norm: the supervisor never sleeps longer between re-sets
SLACK = 128     # the documented overshoot over the budget the bound allows
TREE_KILL_EXIT = 137  # 128 + SIGKILL: the tree was killed, not exited
KILL_GRACE_S = 10


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the closing paren of comm, or None when gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
    except OSError:
        return None
    rparen = stat.rfind(b")")
    if rparen < 0:
        return None
    return stat[rparen + 2:].split()


def scan_procs() -> tuple[dict[int, tuple[int, int]], int]:
    """({pid: (ppid, nlwp)} over this user's processes, this user's total task count)."""
    uid = os.getuid()
    procs: dict[int, tuple[int, int]] = {}
    total = 0
    try:
        names = os.listdir("/proc")
    except OSError:
        return procs, total
    for name in names:
        if not name.isdigit():
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            fields = _stat_fields(int(name))
        except OSError:
            continue
        if fields is None or len(fields) < 18:
            continue
        procs[int(name)] = (int(fields[1]), int(fields[17]))  # ppid, num_threads
        total += int(fields[17])
    return procs, total


def tree_pids(root: int, procs: dict[int, tuple[int, int]]) -> set[int]:
    """PIDs of the whole tree rooted at root (root included while it lives)."""
    tree = {root}
    frontier = [root]
    while frontier:
        nxt = []
        for pid in frontier:
            for child, (ppid, _) in procs.items():
                if ppid == pid and child not in tree:
                    tree.add(child)
                    nxt.append(child)
        frontier = nxt
    return tree


def nproc_limits(pid: int) -> tuple[int, int] | None:
    """(soft, hard) RLIMIT_NPROC of pid, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/limits", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Max processes"):
                    parts = line.split()
                    soft, hard = parts[2], parts[3]

                    def parse(value: str) -> int:
                        return resource.RLIM_INFINITY if value == "unlimited" else int(value)

                    return parse(soft), parse(hard)
    except (OSError, ValueError, IndexError):
        return None
    return None


def set_soft(pid: int, target: int) -> None:
    """Set pid's soft RLIMIT_NPROC to target, clamped to its own hard limit; skips gone pids."""
    limits = nproc_limits(pid)
    if limits is None:
        return
    _, hard = limits
    if hard != resource.RLIM_INFINITY:
        target = min(target, hard)
    try:
        resource.prlimit(pid, resource.RLIMIT_NPROC, (target, hard))
    except (OSError, OverflowError):
        pass  # the pid died or is not ours: never a reason to stop supervising


def own_tasks() -> int:
    """This supervisor's own task count (excluded from both the tree and the others)."""
    fields = _stat_fields(os.getpid())
    return int(fields[17]) if fields and len(fields) >= 18 else 1


def _as_exit_code(returncode: int) -> int:
    """A child killed by a signal reports 128 + signal, not a negative number."""
    return 128 - returncode if returncode < 0 else returncode


def supervise(child: subprocess.Popen, budget: int) -> int:
    """Keep the tree bounded while it lives; the child's exit code, or TREE_KILL_EXIT."""
    while True:
        if child.poll() is not None:
            return _as_exit_code(child.returncode)
        procs, total = scan_procs()
        tree = tree_pids(child.pid, procs)
        tree_tasks = sum(procs[pid][1] for pid in tree if pid in procs)
        mine = own_tasks()
        others = max(total - tree_tasks - mine, 0)
        soft, _ = resource.getrlimit(resource.RLIMIT_NPROC)
        # The soft limit already in force (a nested cap-run's bound) is the ceiling.
        target = others + budget + SLACK
        if soft != resource.RLIM_INFINITY:
            target = min(target, soft)
        for pid in tree:
            set_soft(pid, target)
        if tree_tasks > budget + SLACK:
            print(f"cap-run.sh: own-tasks {budget}: the tree reached {tree_tasks} tasks "
                  f"(budget + slack {budget + SLACK}); killing the tree", file=sys.stderr, flush=True)
            for pid in tree:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            child.wait()
            return TREE_KILL_EXIT
        time.sleep(PERIOD_S)


def initial_soft(budget: int) -> int:
    """The soft limit the child starts with: others now + budget + slack, nested-clamped."""
    _, total = scan_procs()
    others = max(total - own_tasks(), 0)
    soft, _ = resource.getrlimit(resource.RLIMIT_NPROC)
    target = others + budget + SLACK
    if soft != resource.RLIM_INFINITY:
        target = min(target, soft)
    return target


def main() -> int:
    args = sys.argv[1:]
    budget = secs = None
    cmd: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--own-tasks" and i + 1 < len(args):
            budget = int(args[i + 1])
            i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            secs = int(args[i + 1])
            i += 2
        elif args[i] == "--":
            cmd = args[i + 1:]
            break
        else:
            print(f"cap_own_tasks: unexpected argument {args[i]!r}", file=sys.stderr)
            return 2
    if not cmd or budget is None or budget <= 0:
        print("usage: cap_own_tasks.py --own-tasks N [--timeout S] -- <command...>", file=sys.stderr)
        return 2
    if secs is None:
        secs = 3600

    target = initial_soft(budget)
    env = dict(os.environ)
    env["CAP_RUN_NPROC"] = str(target)

    def child_limits() -> None:
        # target is already clamped to the supervisor's own soft limit, which cannot
        # exceed the hard limit the child inherits, so no second clamp is needed.
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        resource.setrlimit(resource.RLIMIT_NPROC, (target, hard))

    # The same wall-clock contract as cap-run.sh's timeout leg, and --foreground so a
    # group kill from a supervising runner reaches the whole tree.
    if _have_timeout():
        argv = ["timeout", "--foreground", "-k", str(KILL_GRACE_S), str(secs), *cmd]
    else:
        print("cap_own_tasks: 'timeout' not found; running without a time limit", file=sys.stderr)
        argv = cmd
    child = subprocess.Popen(argv, preexec_fn=child_limits, env=env)
    return supervise(child, budget)


def _have_timeout() -> bool:
    from shutil import which
    return which("timeout") is not None


if __name__ == "__main__":
    sys.exit(main())
