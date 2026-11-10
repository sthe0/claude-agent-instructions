#!/usr/bin/env python3
"""Compare the phase shares the telemetry record gives with the shares the transcripts give.

  telemetry-compare.py --historical --premises P --transcripts-snapshot T [--min-historical-tasks N]
  telemetry-compare.py --live [--min-tasks N] [--synthesize] [--max-synthetic N]

Both sides cover one task set and are cut at the same boundaries: each task contributes, per
session, only the span of its own event rows there, so a session that served two tasks gives each
its own part. The record side reads the session's points through phases.py (live command events,
else the imported reconstruction); the transcript side reads the agentctl Bash calls of the
session's transcript through the same phases.py functions.

--historical: the tasks whose last act is at or before premises.json's analysis_cutoff and that
hold imported rows; the transcripts come from the snapshot (streamed member by member, never the
frozen reference aggregate). A task is left out of both sides, and counted, when a root,
spawned-specialist or Agent-tool contour has no transcript in the snapshot, has one the record
never imported, or a session of it was only partly recorded; a judge contour needs no transcript.

--live: non-synthetic, ended tasks whose every session is live from its first event (no
transcript-derived event row other than spawn-cost and judge rows), with a transcript for every
non-judge contour, no partly recorded session and every spool committed; each exclusion is
counted under its own key before --synthesize tops the set up to --min-tasks with synthetic tasks.

A phase agrees when its shares differ by at most max(2 pp, half the transcript side's
reconstruction interval); the sampling SE is reported, never added. The interval is spanned by
placing each transition at its Bash call's tool_use or at its tool_result time.

Exit 0 when every phase agrees; 1 when one differs or too few tasks join; 2 when the snapshot is
not the one premises.json names or the record is absent.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import tarfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root, time_profile, time_profile_uncertainty  # noqa: E402
from lib.telemetry import importer, phases, profile_check, report, store, sync  # noqa: E402

TWO_POINTS = 0.02
EXIT_OK, EXIT_DIFFER, EXIT_PREMISE = 0, 1, 2
JUDGE_ROLE = "judge"
RECORDED = ("imported", "transcript", "live")
TRANSCRIPT_DERIVED = ("imported", "transcript")
LIVE_EXEMPT_KINDS = ("spawn_cost", "judge")
PARTIAL_LOSS = "partial_session_before_first_live_event"
ENDED = ("resolved", "abandoned")
DRIVER = Path(__file__).resolve().parent / "telemetry-synthetic-task.py"
LIVE_KEYS = ("live_tasks_synthetic", "live_tasks_imported_only", "live_tasks_mixed_provenance",
             "live_tasks_without_transcript", "live_tasks_partially_recorded", "live_tasks_open",
             "live_tasks_spool_uncommitted")
HISTORICAL_KEYS = ("historical_tasks_without_transcript", "historical_tasks_transcript_not_imported",
                   "historical_tasks_partially_recorded")


# --- phase ms over a window ---------------------------------------------------------------

def phase_ms(points, landing, window) -> dict:
    """Per-phase ms of `points` inside `window` (lo_ms, hi_ms); landing intervals take their
    overlap from whatever phase the points name."""
    lo, hi = window
    out: dict = {}
    for start, end, phase in phases.phase_intervals(points):
        a, b = max(start, lo), min(hi if end is None else end, hi)
        if b <= a:
            continue
        inside = sum(max(0, min(b, i["end"]) - max(a, i["start"])) for i in landing
                     if i["start"] is not None and i["end"] is not None)
        out[phase] = out.get(phase, 0) + (b - a) - inside
        if inside:
            out[phases.LANDING] = out.get(phases.LANDING, 0) + inside
    return out


def record_phase_ms(conn, session: str, window) -> dict:
    points, landing = phases.session_points(conn, session)
    return phase_ms(points, landing, window)


def transcript_calls(lines) -> list:
    """(tool_use stamp, tool_result stamp, fields) of each Bash call in a transcript's lines."""
    pending, calls = {}, []
    for obj in lines:
        message = obj.get("message") if isinstance(obj, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                pending[block.get("id")] = (obj.get("timestamp"), (block.get("input") or {}).get("command"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                used, command = pending.pop(block["tool_use_id"])
                fields = importer._bash_fields(command)
                if fields is not None:
                    fields["exit"] = importer._tool_exit(block)
                    calls.append((used, obj.get("timestamp"), fields))
    return calls


def transcript_phase_ms(calls, window, at_result: bool = False) -> dict:
    rows = [(c[1] if at_result else c[0], c[2]) for c in calls]
    rows = [r for r in rows if r[0] is not None]
    landing = phases.call_landing_intervals(
        [(t, f.get("exit")) for t, f in rows if f.get("program") == phases.LAND_PROGRAM])
    return phase_ms(phases.bash_points(rows), landing, window)


# --- shares and agreement -----------------------------------------------------------------

def _add(total: dict, part: dict) -> None:
    for phase, ms in part.items():
        total[phase] = total.get(phase, 0) + ms


def side_shares(per_task: list) -> dict:
    """{phase: {share, se}} over tasks, each a {phase: ms} dict: the share pools the ms, the SE
    is that of the per-task shares."""
    pooled: dict = {}
    for part in per_task:
        _add(pooled, part)
    total = sum(pooled.values())
    out = {}
    for phase in sorted(pooled):
        shares = [p.get(phase, 0) / sum(p.values()) for p in per_task if sum(p.values())]
        mean = sum(shares) / len(shares) if shares else 0.0
        se = (math.sqrt(sum((s - mean) ** 2 for s in shares) / (len(shares) - 1) / len(shares))
              if len(shares) > 1 else 0.0)
        out[phase] = {"share": pooled[phase] / total if total else 0.0, "se": se}
    return out


def tolerance(stat: dict) -> float:
    return max(TWO_POINTS, time_profile_uncertainty.reconstruction_half_width(stat["lo"], stat["hi"]))


def agreement(record: list, early: list, late: list) -> dict:
    """Per phase: the record share, the transcript share with its (lo, hi) interval and SE, the
    tolerance and whether the difference is within it."""
    rec, at_use, at_result = side_shares(record), side_shares(early), side_shares(late)
    out, ok = {}, True
    for phase in sorted(set(rec) | set(at_use) | set(at_result)):
        a = at_use.get(phase, {"share": 0.0, "se": 0.0})
        b = at_result.get(phase, {"share": 0.0, "se": 0.0})
        stat = {"record": rec.get(phase, {"share": 0.0})["share"], "transcript": a["share"],
                "lo": min(a["share"], b["share"]), "hi": max(a["share"], b["share"]), "se": a["se"]}
        stat["tolerance"] = tolerance(stat)
        stat["difference"] = abs(stat["record"] - stat["transcript"])
        stat["ok"] = stat["difference"] <= stat["tolerance"]
        ok = ok and stat["ok"]
        out[phase] = stat
    return {"ok": ok, "phases": out}


# --- the record's task sets ---------------------------------------------------------------

def _task_rows(conn) -> list:
    return conn.execute("SELECT origin, task_id, epoch, status, synthetic FROM tasks "
                        "ORDER BY opened_at, task_id").fetchall()


def _windows(events, provenance=None) -> dict:
    """session -> (lo_ms, hi_ms) of the task's event rows there (of `provenance` when given)."""
    out: dict = {}
    for _, session, _, _, prov, moment, _ in events:
        ms = time_profile.epoch_or_iso_ms(moment)
        if ms is None or (provenance is not None and prov not in provenance) or session.startswith("telemetry:"):
            continue
        lo, hi = out.get(session, (ms, ms))
        out[session] = (min(lo, ms), max(hi, ms))
    return out


def _roles(conn, sessions) -> dict:
    return {s: (conn.execute("SELECT role FROM sessions WHERE session_id = ?", (s,)).fetchone() or [None])[0]
            for s in sessions}


def _has_rows(conn, session: str, provenance) -> bool:
    marks = ",".join("?" * len(provenance))
    return conn.execute(f"SELECT 1 FROM events WHERE session_id = ? AND provenance IN ({marks}) LIMIT 1",
                        (session, *provenance)).fetchone() is not None


def _transcript_derived(conn, session: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM events WHERE session_id = ? AND provenance IN (?, ?) AND kind NOT IN (?, ?) LIMIT 1",
        (session, *TRANSCRIPT_DERIVED, *LIVE_EXEMPT_KINDS)).fetchone() is not None


def _partially_recorded(conn, sessions) -> bool:
    return any(conn.execute("SELECT 1 FROM losses WHERE session_id = ? AND loss_mode = ? AND count > 0 LIMIT 1",
                            (s, PARTIAL_LOSS)).fetchone() for s in sessions)


def _contours(conn, key, windows) -> list:
    return sorted(set(report.task_session_ids(conn, key)) | set(windows))


def live_set(conn, transcripts: dict, chosen_synthetic=frozenset()) -> "tuple[list, dict]":
    """([(key, {session: window})], exclusion counts). `chosen_synthetic` are the synthetic task
    keys a --synthesize top-up added; they pass the synthetic test and every other one."""
    counts = dict.fromkeys(LIVE_KEYS, 0)
    chosen = []
    for origin, task_id, epoch, status, synthetic in _task_rows(conn):
        key = (origin, task_id, epoch)
        if synthetic and key not in chosen_synthetic:
            counts["live_tasks_synthetic"] += 1
            continue
        windows = _windows(report.task_event_rows(conn, key), ("live",))
        sessions = _contours(conn, key, windows)
        roles = _roles(conn, sessions)
        needs = [s for s in sessions if roles.get(s) != JUDGE_ROLE]
        if not windows:
            reason = "live_tasks_imported_only"
        elif any(_transcript_derived(conn, s) for s in sessions):
            reason = "live_tasks_mixed_provenance"
        elif any(s not in transcripts for s in needs):
            reason = "live_tasks_without_transcript"
        elif _partially_recorded(conn, sessions):
            reason = "live_tasks_partially_recorded"
        elif status not in ENDED:
            reason = "live_tasks_open"
        elif any(sync.unreplayed_bytes(conn, s) for s in sessions):
            reason = "live_tasks_spool_uncommitted"
        else:
            chosen.append((key, windows))
            continue
        counts[reason] += 1
    return chosen, counts


def historical_set(conn, cutoff_ms: int, stems: set) -> "tuple[list, dict]":
    """([(key, {session: window})], exclusion counts) of the tasks ended by the cutoff."""
    counts = dict.fromkeys(HISTORICAL_KEYS, 0)
    chosen = []
    for origin, task_id, epoch, _, synthetic in _task_rows(conn):
        key = (origin, task_id, epoch)
        events = report.task_event_rows(conn, key)
        last = max((time_profile.epoch_or_iso_ms(e[5]) for e in events if time_profile.epoch_or_iso_ms(e[5]) is not None), default=None)
        windows = _windows(events, ("imported",))
        if synthetic or not windows or last is None or last > cutoff_ms:
            continue
        contours = _contours(conn, key, windows)
        roles = _roles(conn, contours)
        required = [s for s in contours if roles.get(s) != JUDGE_ROLE]
        if any(s not in stems for s in required):
            counts["historical_tasks_without_transcript"] += 1
        elif any(not _has_rows(conn, s, RECORDED) for s in required):
            counts["historical_tasks_transcript_not_imported"] += 1
        elif _partially_recorded(conn, windows):
            counts["historical_tasks_partially_recorded"] += 1
        else:
            chosen.append((key, windows))
    return chosen, counts


# --- the two sides ------------------------------------------------------------------------

def sides(conn, chosen: list, lines_of) -> "tuple[list, list, list]":
    """Per task: record ms, transcript ms at tool_use and at tool_result, each cut per session."""
    record, early, late = [], [], []
    for _, windows in chosen:
        rec, use, result = {}, {}, {}
        for session, window in windows.items():
            calls = transcript_calls(lines_of(session))
            _add(rec, record_phase_ms(conn, session, window))
            _add(use, transcript_phase_ms(calls, window))
            _add(result, transcript_phase_ms(calls, window, at_result=True))
        record.append(rec)
        early.append(use)
        late.append(result)
    return record, early, late


def _parse(raw: bytes):
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def snapshot_stems(snapshot: Path) -> set:
    return {PurePosixPath(n).stem for n in profile_check._member_names(snapshot) if n.endswith(".jsonl")}


def snapshot_lines(snapshot: Path, wanted: set) -> dict:
    """stem -> parsed lines of the snapshot members of `wanted`, streamed member by member."""
    out: dict = {}
    with tarfile.open(snapshot, "r|*") as tar:
        for member in tar:
            stem = PurePosixPath(member.name).stem
            if not member.isreg() or not member.name.endswith(".jsonl") or stem not in wanted:
                continue
            out.setdefault(stem, []).extend(o for o in map(_parse, tar.extractfile(member)) if o is not None)
    return out


def local_transcripts() -> dict:
    """stem -> path of every transcript under the harness projects roots."""
    out = {}
    for root in config_root.projects_roots():
        for path in Path(root).rglob("*.jsonl"):
            out.setdefault(path.stem, path)
    return out


def _file_lines(path: Path) -> list:
    with open(path, "rb") as handle:
        return [o for o in map(_parse, handle) if o is not None]


def historical(conn, premises: dict, snapshot: Path, min_tasks: int) -> "tuple[int, dict]":
    expected = premises.get("transcript_snapshot_sha256")
    actual = profile_check.sha256_of(snapshot)
    if actual != expected:
        return EXIT_PREMISE, {"error": f"snapshot sha256 {actual} is not the premises' {expected}"}
    cutoff = time_profile.epoch_or_iso_ms(premises["analysis_cutoff"])
    chosen, counts = historical_set(conn, cutoff, snapshot_stems(snapshot))
    lines = snapshot_lines(snapshot, {s for _, windows in chosen for s in windows})
    result = agreement(*sides(conn, chosen, lambda s: lines.get(s, [])))
    payload = {"side": "historical", "tasks": len(chosen), **counts, **result}
    if len(chosen) < min_tasks:
        payload["ok"] = False
        payload["error"] = f"{len(chosen)} task(s) join the snapshot, fewer than {min_tasks}"
    return (EXIT_OK if payload["ok"] else EXIT_DIFFER), payload


def load_driver():
    spec = importlib.util.spec_from_file_location("telemetry_synthetic_task", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_keys(conn) -> set:
    return {(r[0], r[1], r[2]) for r in _task_rows(conn) if r[4]}


def live(min_tasks: int, synthesize: bool, max_synthetic: int) -> "tuple[int, dict]":
    conn = store.open_readonly()
    if conn is None:
        return EXIT_PREMISE, {"error": "no telemetry record"}
    transcripts = local_transcripts()
    chosen, counts = live_set(conn, transcripts)
    added = set()
    wanted = min(max(min_tasks - len(chosen), 0), max_synthetic)
    if synthesize and wanted:
        before = _synthetic_keys(conn)
        conn.close()
        load_driver().run(wanted)
        conn = store.open_readonly()
        added = _synthetic_keys(conn) - before
        transcripts = local_transcripts()
        chosen, _ = live_set(conn, transcripts, frozenset(added))
    try:
        result = agreement(*sides(conn, chosen, lambda s: _file_lines(transcripts[s]) if s in transcripts else []))
    finally:
        conn.close()
    payload = {"side": "live", "tasks": len(chosen), "synthesized": len(added), **counts, **result}
    if len(chosen) < min_tasks:
        payload["ok"] = False
        payload["error"] = f"{len(chosen)} live task(s), fewer than {min_tasks}"
    return (EXIT_OK if payload["ok"] else EXIT_DIFFER), payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--historical", action="store_true")
    side.add_argument("--live", action="store_true")
    parser.add_argument("--premises", type=Path)
    parser.add_argument("--transcripts-snapshot", type=Path)
    parser.add_argument("--min-historical-tasks", type=int, default=1)
    parser.add_argument("--min-tasks", type=int, default=1)
    parser.add_argument("--synthesize", action="store_true")
    parser.add_argument("--max-synthetic", type=int, default=10)
    args = parser.parse_args(argv)
    report.emit_usage("compare")
    if args.historical:
        if args.premises is None or args.transcripts_snapshot is None:
            parser.error("--historical needs --premises and --transcripts-snapshot")
        conn = store.open_readonly()
        if conn is None:
            status, payload = EXIT_PREMISE, {"error": "no telemetry record"}
        else:
            try:
                premises = json.loads(args.premises.read_text(encoding="utf-8"))
                status, payload = historical(conn, premises, args.transcripts_snapshot, args.min_historical_tasks)
            finally:
                conn.close()
    else:
        status, payload = live(args.min_tasks, args.synthesize, args.max_synthetic)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
