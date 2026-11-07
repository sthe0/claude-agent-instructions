"""Command line of the telemetry record.

    telemetry.py sync [--session <id>] [--stale] [--budget SECONDS]
    telemetry.py emit-precommit --started <epoch seconds> --exit <code>
    telemetry.py import [--transcripts] [--all] [--max-rss-mib N] [--stats-json FILE]
    telemetry.py rebuild [--transcripts]
    telemetry.py prune [--retention-days N]
    telemetry.py check-profile-unchanged --reference F --premises P --transcripts-snapshot T
    telemetry.py now
    telemetry.py path

`import` reads the history that predates live emission (gate, decision, judge, spawn-cost and
quality ledgers, engine state files, and with `--transcripts` the session transcripts) into the
record (lib/telemetry/importer.py); a second run adds nothing. `--all` replays every spool, not
only those of ended sessions. It prints the stats as one JSON line (also to `--stats-json FILE`)
and exits 1 when the run is incomplete (a batch deferred past its budget, an unreadable source or
a failed batch) or its peak RSS passed `--max-rss-mib`. `rebuild` moves a corrupt record aside and
builds a new one with the same origin id from the spools and the sources; `prune` deletes the sync
and decision rows older than `telemetry-prune-retention-days` (config.md) or `--retention-days`.
`check-profile-unchanged` recomputes the time-profile aggregate from stage 1's frozen inputs and
compares it with the frozen reference (lib/telemetry/profile_check.py: exit 0 unchanged, 1 a figure
differs, 2 a premise or the session set differs).

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


def _import(args) -> int:
    from lib.telemetry import importer
    stats = importer.run_import(args.transcripts, replay_all_spools=args.all)
    over = args.max_rss_mib is not None and stats["peak_rss_mib"] > args.max_rss_mib
    stats["rss_exceeded"] = over
    text = json.dumps(stats, sort_keys=True, default=str)
    if args.stats_json:
        with open(args.stats_json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0 if stats["complete"] and not over else 1


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
    imp = sub.add_parser("import", help="import the history that predates live emission")
    imp.add_argument("--transcripts", action="store_true", help="also import session transcripts")
    imp.add_argument("--all", action="store_true", help="replay every spool, not only those of ended sessions")
    imp.add_argument("--max-rss-mib", type=float, default=None, help="exit 1 when the peak RSS passes this")
    imp.add_argument("--stats-json", default=None, help="also write the stats to this file")
    rebuild_cmd = sub.add_parser("rebuild", help="rebuild a corrupt record from the spools and the sources")
    rebuild_cmd.add_argument("--transcripts", action="store_true")
    prune_cmd = sub.add_parser("prune", help="delete sync and decision rows past the retention window")
    prune_cmd.add_argument("--retention-days", type=int, default=None)
    check = sub.add_parser("check-profile-unchanged", help="compare the time-profile aggregate with the frozen reference")
    check.add_argument("--reference", required=True)
    check.add_argument("--premises", required=True)
    check.add_argument("--transcripts-snapshot", required=True)
    sub.add_parser("now", help="print the epoch time")
    sub.add_parser("path", help="print the record's path")
    args = parser.parse_args(argv)
    if args.command == "import":
        return _import(args)
    if args.command == "rebuild":
        from lib.telemetry import importer
        report = importer.rebuild(args.transcripts)
        print(json.dumps(report, sort_keys=True, default=str))
        return 0 if report["complete"] else 1
    if args.command == "prune":
        from lib.telemetry import importer
        print(json.dumps(importer.prune(args.retention_days), sort_keys=True))
        return 0
    if args.command == "check-profile-unchanged":
        from lib.telemetry import profile_check
        return profile_check.run(args.reference, args.premises, args.transcripts_snapshot)
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
