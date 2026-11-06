"""Command line of the telemetry record: `sync` applies a session's spool, `path` prints the record's path.

    telemetry.py sync --session <id> [--budget SECONDS]
    telemetry.py path

`sync` prints the replay result as one JSON line and exits 0 on deferral or store failure
(the spool keeps the events). Without --budget it makes one attempt under the store's
250 ms deadline. With --budget S > 0 it retries while the spool is not drained: waits 1 s,
doubling to at most 30 s, and stops when the next wait would pass the budget.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from lib import config_root
from lib.telemetry import store

BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0


def sync(session_id: str, budget_s: float = 0.0, sleep=time.sleep, clock=time.monotonic) -> dict:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sync_cmd = sub.add_parser("sync", help="apply a session's spool to the record")
    sync_cmd.add_argument("--session", required=True)
    sync_cmd.add_argument("--budget", type=float, default=0.0, help="seconds to keep retrying while deferred")
    sub.add_parser("path", help="print the record's path")
    args = parser.parse_args(argv)
    if args.command == "path":
        print(config_root.telemetry_db())
        return 0
    print(json.dumps(sync(args.session, args.budget), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
