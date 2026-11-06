"""Command line of the telemetry record.

    telemetry.py sync [--session <id>] [--stale] [--budget SECONDS]
    telemetry.py emit-precommit --started <epoch seconds> --exit <code>
    telemetry.py now
    telemetry.py path

`sync` runs one sync (lib/telemetry/sync.py): it replays the named session's spool and every
other spool with unreplayed bytes, files the rows the events imply and prints the result as one
JSON line. `--session` marks a sync a SessionEnd hook started; `--stale` a sweep the command path
started; both may be absent for a manual run. It exits 0 on deferral or store failure (the spools
keep the events). `--budget S` is how long it keeps retrying a deferred spool (default
`sync.BUDGET_S`). `emit-precommit` writes the `precommit` event of one pre-commit run; `now` prints
the epoch time the hook measures from; `path` prints the record's path.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from lib import config_root
from lib.telemetry import live, store, sync as telemetry_sync

BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0


def sync(session_id: str, budget_s: float = 0.0, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Replay one spool, retrying while it is not drained: waits 1 s, doubling to at most 30 s,
    and stops when the next wait would pass the budget."""
    end = clock() + budget_s
    wait = BACKOFF_FIRST_S
    total = {"inserted": 0, "torn": 0, "superseded": 0}
    while True:
        result = store.replay(session_id)
        for key in total:
            total[key] += result[key]
        if result["drained"] or result["failed"] or budget_s <= 0 or clock() + wait > end:
            return {**result, **total}
        sleep(wait)
        wait = min(wait * 2, BACKOFF_MAX_S)


def emit_precommit(started: float, exit_code: int) -> "str | None":
    duration = max(0.0, time.time() - started) if started > 0 else None
    return live.emit_event(live.PRECOMMIT_SESSION, "precommit", "precommit", {
        "duration_s": None if duration is None else round(duration, 3), "exit": exit_code})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sync_cmd = sub.add_parser("sync", help="replay spools into the record and file what the events imply")
    sync_cmd.add_argument("--session", default=None, help="the session whose SessionEnd started this sync")
    sync_cmd.add_argument("--stale", action="store_true", help="a sweep started by the command path")
    sync_cmd.add_argument("--budget", type=float, default=None, help="seconds to keep retrying while deferred")
    pre = sub.add_parser("emit-precommit", help="record one pre-commit run")
    pre.add_argument("--started", type=float, default=0.0)
    pre.add_argument("--exit", dest="exit_code", type=int, default=0)
    sub.add_parser("now", help="print the epoch time")
    sub.add_parser("path", help="print the record's path")
    args = parser.parse_args(argv)
    if args.command == "path":
        print(config_root.telemetry_db())
        return 0
    if args.command == "now":
        print(f"{time.time():.6f}")
        return 0
    if args.command == "emit-precommit":
        emit_precommit(args.started, args.exit_code)
        return 0
    print(json.dumps(telemetry_sync.run(args.session, args.budget), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
