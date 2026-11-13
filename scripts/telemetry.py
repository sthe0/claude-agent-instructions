"""Command line of the telemetry record.

    telemetry.py sync [--session <id>] [--stale] [--budget SECONDS]
    telemetry.py emit-precommit --started <epoch seconds> --exit <code>
    telemetry.py import [--transcripts] [--all] [--max-rss-mib N] [--stats-json FILE] [--sources DIR]
    telemetry.py snapshot-sources --out DIR
    telemetry.py rebuild [--transcripts]
    telemetry.py prune [--retention-days N]
    telemetry.py check-profile-unchanged --reference F --premises P --transcripts-snapshot T
    telemetry.py align-causes [--inputs DIR] [--draft | --text-out FILE | --report]
    telemetry.py check-causes --imported --expect N
    telemetry.py check-tokens (--task <id> [--require-spawn] [--require-compaction] | --imported-coverage)
    telemetry.py check-events --task <id> [--unrecorded-window F]
        [--known-losses K (--known-losses-until TS | --known-losses-until-landing F2)]
    telemetry.py check-latency --max-seconds S --since-landing F
        [--sync-outage-from TS (--sync-outage-until-landing F2 | --sync-outage-until TS2)] [--pre-fix-late P]
    telemetry.py export --out F [--with-text]
    telemetry.py merge F
    telemetry.py check-roundtrip --workdir D
    telemetry.py now
    telemetry.py path

`export` writes the record's structured rows with their origin under a header (origin id,
taken_at, the version of every enumeration table) into the new file F, created 0600; text artifacts
only with `--with-text`. `merge F` merges such an export into the record by each table's merge key
(lib/telemetry/portability.py): a re-merge adds nothing, an export replaces only its own origin's
rows and only when taken after that origin's last merged export, and an unknown table version or
another fixture revision refuses the whole merge (exit 2). `check-roundtrip` copies the record,
opened read-only, into D, exports the copy, merges it twice into a new record under D and exits 1
when the row counts differ, the second merge adds a row or the export holds text; it writes only
into D.

`import` reads the history that predates live emission (gate, decision, judge, spawn-cost and
quality ledgers, engine state files, and with `--transcripts` the session transcripts) into the
record (lib/telemetry/importer.py); a second run adds nothing. `--all` replays every spool, not
only those of ended sessions. It prints the stats as one JSON line (also to `--stats-json FILE`)
and exits 1 when the run is incomplete (a batch deferred past its budget, an unreadable source or
a failed batch) or its peak RSS passed `--max-rss-mib`. `snapshot-sources --out DIR` copies every
import source once (append-only files cut at their size, after a free-space check) with a manifest
of the live path of each copy, and exits 1 when a source could not be read or copied (listed under
`skipped` in the manifest); `import --sources DIR` then reads only those copies, naming rows by
the live paths (lib/telemetry/snapshot.py), so two imports read one fixed input. `rebuild` moves a corrupt record aside and
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
An act named by one entry {session, act, ts, cause} of `--known-losses K` (same session, same act
key, ts within 2 s; one act per entry) is the counted loss known_loss:<cause>. K needs a bound,
`--known-losses-until TS` or `--known-losses-until-landing F` (its landed_at), and the run exits 2
on a missing file, an entry without a cause, an entry at or after the bound, or an entry that
names no act of the task.
`check-latency` classifies each non-synthetic session that ended after F's `landed_at` by the
latency rule (sync.latency_marks): on_time, late, waiting, in_flight or sync_deferred; it exits 1
on a late session or when no session of that population has reached the record. A session leaves
that population only when its rows prove it synthetic; the excluded ones are listed and counted.
A late session is a counted named loss instead when its T1 falls in the sync outage window
[--sync-outage-from, end) (sync_outage; the end is F2's landed_at, bound to the record's land:landed
event of F2's head, or TS2), when P names it with a pre_fix_cause_* cause (pre_fix_late:<cause>, or
import_only:<cause> for a session no live row or spool ever carried), or when it is such an
import-only session whose earliest timed row precedes F's landed_at (import_only:pre_emitter). An
unreadable F2 or P, a landed_at off its landing event, or an entry naming a session that is not
late, not real and not import-only, or late outside [landed_at, TS), exits 2. Both are
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
    stats = importer.run_import(args.transcripts, replay_all_spools=args.all, sources=args.sources)
    over = args.max_rss_mib is not None and stats["peak_rss_mib"] > args.max_rss_mib
    stats["rss_exceeded"] = over
    text = json.dumps(stats, sort_keys=True, default=str)
    if args.stats_json:
        with open(args.stats_json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0 if stats["complete"] and not over else 1


def _snapshot_sources(args) -> int:
    from lib.telemetry import snapshot
    try:
        result = snapshot.take(args.out)
    except (OSError, snapshot.SnapshotError) as err:
        print(f"snapshot-sources: {err}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if result["skipped"] else 0


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


def _read_json_file(path: str, key: "str | None"):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if key is None else data[key]


def _known_losses(args) -> "list | None":
    from lib.telemetry import phases, report
    bound = args.known_losses_until
    if args.known_losses_until_landing:
        bound = _read_json_file(args.known_losses_until_landing, "landed_at")
    if args.known_losses is None:
        if bound is not None:
            raise ValueError("--known-losses-until* given without --known-losses")
        return None
    return report.read_known_losses(_read_json_file(args.known_losses, "entries"), phases._ms(bound))


def _check_record(args) -> int:
    """check-events / check-latency over the record (lib/telemetry/report.py)."""
    from datetime import datetime, timezone

    from lib.telemetry import report
    latency = args.command == "check-latency"
    try:
        window = _read_json_file(args.unrecorded_window, "unrecorded_window") \
            if getattr(args, "unrecorded_window", None) else None
        landed_at = _read_json_file(args.since_landing, "landed_at") if latency else None
        landing = _read_json_file(args.sync_outage_until_landing, None) \
            if latency and args.sync_outage_until_landing else None
        entries = _read_json_file(args.pre_fix_late, "entries") if latency and args.pre_fix_late else None
        outage_from = telemetry_sync.parse_time(args.sync_outage_from) if latency else None
        outage_until = telemetry_sync.parse_time(args.sync_outage_until) if latency else None
        known_losses = None if latency else _known_losses(args)
    except (OSError, ValueError, KeyError, TypeError) as err:
        report.emit_usage(args.command)
        print(f"{args.command}: {err}", file=sys.stderr)
        return 2
    conn = store.open_readonly()
    if conn is None:
        report.emit_usage(args.command)
        print(f"{args.command}: the record is unavailable", file=sys.stderr)
        return 2
    try:
        report.emit_usage(args.command, conn, getattr(args, "task", None))
        if args.command == "check-events":
            result = report.check_events(conn, args.task, window, known_losses=known_losses)
        else:
            if outage_from is not None and landing is not None:
                outage_until = report.outage_end_from_landing(conn, landing if isinstance(landing, dict) else {})
            outage = (outage_from, outage_until) if outage_from is not None else None
            result = report.check_latency(conn, args.max_seconds, landed_at, datetime.now(timezone.utc),
                                          outage, entries if isinstance(entries, list) else None)
    except (report.LatencyInputError, report.KnownLossError) as err:
        print(f"{args.command}: {err}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


def _portability(args) -> int:
    from lib.telemetry import portability
    try:
        if args.command == "export":
            result = portability.export(config_root.telemetry_db(), args.out, with_text=args.with_text)
        elif args.command == "merge":
            conn = store.open_for_merge()
            try:
                result = portability.merge(conn, args.file)
            finally:
                conn.close()
        else:
            result = portability.check_roundtrip(args.workdir, config_root.telemetry_db())
    except (portability.Refused, OSError, ValueError) as err:
        print(f"{args.command}: {err}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok", True) else 1


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
    imp.add_argument("--sources", default=None, help="read the sources of this snapshot-sources directory only")
    snap = sub.add_parser("snapshot-sources", help="copy every import source once, for an import on a fixed input")
    snap.add_argument("--out", required=True, help="the directory to create")
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
    evt.add_argument("--known-losses", default=None,
                     help="JSON file with entries [{session, act, ts, cause}] of acts lost before the bound")
    bound = evt.add_mutually_exclusive_group()
    bound.add_argument("--known-losses-until", default=None, help="ISO time every known loss precedes")
    bound.add_argument("--known-losses-until-landing", default=None,
                       help="landing record (JSON with landed_at) every known loss precedes")
    lat = sub.add_parser("check-latency", help="check that ended sessions reach the record within the bound")
    lat.add_argument("--max-seconds", type=float, required=True)
    lat.add_argument("--since-landing", required=True, help="landing record (JSON with landed_at)")
    lat.add_argument("--sync-outage-from", default=None, help="start (ISO time) of a sync outage window")
    until = lat.add_mutually_exclusive_group()
    until.add_argument("--sync-outage-until-landing", default=None,
                       help="landing record (JSON with head, landed_at) whose landing ends the outage")
    until.add_argument("--sync-outage-until", default=None, help="end (ISO time) of the outage window")
    lat.add_argument("--pre-fix-late", default=None,
                     help='JSON {"entries": [{"session", "cause"}]} of sessions late for a cause this change fixes')
    exp = sub.add_parser("export", help="export the record's rows for a merge on another machine")
    exp.add_argument("--out", required=True, help="the export file to create (must not exist)")
    exp.add_argument("--with-text", action="store_true", help="also export the text artifacts")
    mrg = sub.add_parser("merge", help="merge an export into the record")
    mrg.add_argument("file")
    rt = sub.add_parser("check-roundtrip", help="export a copy of the record and merge it into a scratch record")
    rt.add_argument("--workdir", required=True, help="directory receiving every file the check writes")
    sub.add_parser("now", help="print the epoch time")
    sub.add_parser("path", help="print the record's path")
    args = parser.parse_args(argv)
    if args.command == "check-latency" and (args.sync_outage_from is None) != (
            args.sync_outage_until_landing is None and args.sync_outage_until is None):
        lat.error("--sync-outage-from needs one of --sync-outage-until-landing / --sync-outage-until, and they need it")
    if args.command in ("export", "merge", "check-roundtrip"):
        return _portability(args)
    if args.command == "import":
        return _import(args)
    if args.command == "snapshot-sources":
        return _snapshot_sources(args)
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
