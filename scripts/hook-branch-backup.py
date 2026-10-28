#!/usr/bin/env python3
"""Stop + SessionStart hook: start the branch-backup upkeep and the disk-pressure check
detached and return at once.

Difficulty removed: a worktree branch is the only copy of its commits until someone
pushes it, and a session that ends (or a disk that dies) before landing takes them along.
The push itself is the `git-worktrees` reaper's `upkeep` (docs/operations/reapers.md);
this hook only gives it a trigger at every turn end and session start. A push can wedge
on the network, so it never runs in the hook's own process: the upkeep is spawned in its
own session with output appended to ~/.local/state/claude-reaper/upkeep.log, and the hook
returns without waiting. `--no-wait` makes a second trigger exit at once while a run holds
the lock.

The same two events also start `hook-reaper.py --on-pressure` (log: pressure.log beside
upkeep.log): the 24-hour SessionStart throttle lets residue fill a disk long before the next
pass, so a free-space check at every turn end runs the reapers whose paths are short of
space. Each spawn is independent; the check decides cheaply and exits 0 when nothing is due.

Fail-open and silent: it prints nothing (a Stop hook's stdout would decorate the turn)
and exits 0 on every path, including a spawn failure and malformed hook stdin. It acts on
the repository this file lives in, whatever the session's cwd.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn_detached(log_name: str, *flags: str) -> None:
    log_path = Path.home() / ".local" / "state" / "claude-reaper" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            ["python3", str(REPO_ROOT / "scripts" / "hook-reaper.py"), *flags],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=str(REPO_ROOT),
        )


def spawn_upkeep() -> None:
    _spawn_detached("upkeep.log", "--upkeep-only", "--no-wait")


def spawn_pressure() -> None:
    _spawn_detached("pressure.log", "--on-pressure")


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        spawn_upkeep()
    except Exception:
        pass
    try:
        spawn_pressure()
    except Exception:
        pass
    return 0


from lib import hook_telemetry  # noqa: E402


if __name__ == "__main__":
    sys.exit(hook_telemetry.run(main, mechanism="hook-branch-backup"))
