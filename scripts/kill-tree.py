#!/usr/bin/env python3
"""Standalone reaper CLI: kill a process and its whole descendant subtree.

Thin wrapper over ``proc_tree.kill_tree`` for tearing down a spawned ``claude -p``
specialist (or any supervised tree) by pid from the shell. A bare ``kill <pid>``
signals only the wrapper and orphans its children; this reaps the group.

  python3 scripts/kill-tree.py <pid> [--grace SECONDS] [--signal TERM|KILL]

``--signal KILL`` skips the SIGTERM grace period (``proc_tree.kill_tree`` always
escalates TERM -> grace -> KILL; KILL here just sets the grace to zero).
"""
from __future__ import annotations

import argparse
import os
import sys

import proc_tree  # sibling module in scripts/; run-dir is on sys.path[0]


def _refuse(msg: str) -> int:
    print(f"kill-tree: refusing: {msg}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap a process and its whole descendant subtree."
    )
    parser.add_argument("pid", type=int, help="pid of the subtree root to reap")
    parser.add_argument(
        "--grace",
        type=float,
        default=5.0,
        help="seconds to wait after SIGTERM before SIGKILL (default 5)",
    )
    parser.add_argument(
        "--signal",
        choices=("TERM", "KILL"),
        default="TERM",
        help="TERM (default): graceful TERM->grace->KILL; KILL: skip the grace",
    )
    args = parser.parse_args(argv)

    if args.pid <= 1:
        return _refuse(f"pid {args.pid} <= 1 (init/self-kill guard)")
    if args.pid == os.getpid():
        return _refuse("target is this process")
    try:
        target_pgid = os.getpgid(args.pid)
    except ProcessLookupError:
        target_pgid = None
    except PermissionError:
        return _refuse(f"no permission to inspect pid {args.pid}")
    if target_pgid is not None and target_pgid == os.getpgrp():
        return _refuse("target shares the caller's process group")

    grace_s = 0.0 if args.signal == "KILL" else args.grace
    reaped = proc_tree.kill_tree(args.pid, grace_s=grace_s)
    if reaped:
        print("reaped: " + " ".join(str(p) for p in sorted(reaped)))
    else:
        print("reaped: <none>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
