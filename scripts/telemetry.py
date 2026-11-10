"""Command line of the telemetry record.

    telemetry.py sync [--session <id>] [--stale] [--budget SECONDS]
    telemetry.py emit-precommit --started <epoch seconds> --exit <code>
    telemetry.py import [--transcripts] [--all] [--max-rss-mib N] [--stats-json FILE]
    telemetry.py rebuild [--transcripts]
    telemetry.py prune [--retention-days N]
    telemetry.py check-profile-unchanged --reference F --premises P --transcripts-snapshot T
    telemetry.py align-causes [--inputs DIR] [--draft | --text-out FILE | --report]
    telemetry.py check-causes --imported --expect N
    telemetry.py check-tokens (--task <id> [--require-spawn] [--require-compaction] | --imported-coverage)
    telemetry.py check-events --task <id> [--unrecorded-window F]
    telemetry.py check-latency --max-seconds S --since-landing F
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

`check-tokens --task <id>` checks one task's token conservation (lib/telemetry/tokens.py): the
usage of the message ids whose winning line falls in the task, read up to each transcript's tokens
offset, against its token rows by role and phase, plus its judge usage; it lists bytes appended
since the last read, winning lines with no open epoch and copies another task owns as gaps that
leave the exit unchanged. It exits 1 on a mismatch, an expected judge call (a judge event of the
task) with no ledger row, or a missing context birth `--require-spawn` / `--require-compaction`
asks for. `--imported-coverage` exits 1 unless every transcript with usage lines has a last
collector read that committed up to its last complete line, with a token row per message id read.
Both exit 2 on a usage error.

`check-events --task <id>` matches every engine-history command and gate evaluation of the task's
sessions to its live event row and exits 1 on an unmatched act; an act inside the
`unrecorded_window {from, to}` of `--unrecorded-window F` is listed as an excused loss instead.
`check-latency` classifies each non-synthetic session that ended after F's `landed_at` by the
latency rule (sync.latency_marks): on_time, late, waiting, in_flight or sync_deferred; it exits 1
on a late session or when no session of that population has reached the record. Both are
lib/telemetry/report.py, as is `agent-report.py core-version`.

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
from pathlib import Path

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


def _align_causes(args) -> int:
    from lib.telemetry import align
    try:
        if args.report:
            report = align.write_report()
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.text_out:
            print(json.dumps({"texts": align.show_texts(args.inputs, args.text_out)}))
            return 0
        if args.draft:
            rows = align.write_draft(args.inputs)
            print(json.dumps({"draft_rows": len(rows), "judged": sum(1 for r in rows if r["aligned_category"])},
                             sort_keys=True))
            return 0
        print(json.dumps(align.run_align(args.inputs), sort_keys=True))
        return 0
    except (align.AlignmentError, OSError, ValueError, KeyError) as err:
        print(f"align-causes: {err}", file=sys.stderr)
        return 1


def _check_causes(args) -> int:
    from lib.telemetry import importer
    result = importer.check_fixture_causes(args.expect)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def _check_tokens(args) -> int:
    from lib.telemetry import tokens
    if not args.task and not args.imported_coverage:
        print("check-tokens: give --task <id> or --imported-coverage", file=sys.stderr)
        return 2
    conn = store.open_readonly()
    if conn is None:
        print("check-tokens: the record is unavailable", file=sys.stderr)
        return 2
    try:
        if args.imported_coverage:
            result = tokens.check_imported_coverage(conn)
        else:
            result = tokens.check_task(conn, args.task, args.require_spawn, args.require_compaction)
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    if "usage_error" in result:
        return 2
    return 0 if result["ok"] else 1


def _read_json_file(path: str, key: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)[key]


def _check_record(args) -> int:
    """check-events / check-latency over the record (lib/telemetry/report.py)."""
    from datetime import datetime, timezone

    from lib.telemetry import report
    report.emit_usage(args.command)
    try:
        window = _read_json_file(args.unrecorded_window, "unrecorded_window") \
            if getattr(args, "unrecorded_window", None) else None
        landed_at = _read_json_file(args.since_landing, "landed_at") if args.command == "check-latency" else None
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"{args.command}: {err}", file=sys.stderr)
        return 2
    conn = store.open_readonly()
    if conn is None:
        print(f"{args.command}: the record is unavailable", file=sys.stderr)
        return 2
    try:
        if args.command == "check-events":
            result = report.check_events(conn, args.task, window)
        else:
            result = report.check_latency(conn, args.max_seconds, landed_at, datetime.now(timezone.utc))
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


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
    align_cmd = sub.add_parser("align-causes", help="one-off: align the hand-classified replan causes to engine history")
    align_cmd.add_argument("--inputs", default=str(Path(__file__).resolve().parents[1].parent
                                                    / (Path(__file__).resolve().parents[1].name + ".inputs")))
    align_cmd.add_argument("--draft", action="store_true", help="write the draft alignment (no hand categories)")
    align_cmd.add_argument("--text-out", default=None, help="write the critique texts to this uncommitted file")
    align_cmd.add_argument("--report", action="store_true", help="write the alignment report from the fixture rows")
    chk = sub.add_parser("check-causes", help="count the imported cause rows and validate each")
    chk.add_argument("--imported", action="store_true", required=True)
    chk.add_argument("--expect", type=int, required=True)
    tok = sub.add_parser("check-tokens", help="check a task's token conservation or the imported coverage")
    tok.add_argument("--task", default=None)
    tok.add_argument("--require-spawn", action="store_true", help="exit 1 without a spawn context birth")
    tok.add_argument("--require-compaction", action="store_true", help="exit 1 without a compaction")
    tok.add_argument("--imported-coverage", action="store_true")
    evt = sub.add_parser("check-events", help="check that every engine act of a task has its event row")
    evt.add_argument("--task", required=True)
    evt.add_argument("--unrecorded-window", default=None, help="JSON file with unrecorded_window {from, to}")
    lat = sub.add_parser("check-latency", help="check that ended sessions reach the record within the bound")
    lat.add_argument("--max-seconds", type=float, required=True)
    lat.add_argument("--since-landing", required=True, help="landing record (JSON with landed_at)")
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
    if args.command == "align-causes":
        return _align_causes(args)
    if args.command == "check-causes":
        return _check_causes(args)
    if args.command == "check-tokens":
        return _check_tokens(args)
    if args.command in ("check-events", "check-latency"):
        return _check_record(args)
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
