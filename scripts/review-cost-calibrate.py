#!/usr/bin/env python3
"""Derive the review-cycle thresholds from every recorded pre-approval review cycle.

Difficulty removed: a review loop cannot see its own accumulated cost, so a bound on
it (review acts and active minutes in one review cycle) guessed from intuition lands
either inside ordinary cycles or far past them. Both bounds are taken from what the
recorded cycles actually spent, over the whole population, with the p90 rule and the
min_n rule of docs/operations/review-backstop-arming.json.

  review-cycle-acts            p90 of the review acts in one cycle
  review-cycle-active-minutes  p90 of the booked active minutes in one cycle

A review cycle runs from a PLAN_READY arrival of a new plan sha256 (a `submit_plan`
or a `replan` event) to the next such arrival or to `approve`. An act is a fresh
`plan_review` / `plan_pair_review` event for which the engine's own
cli._is_counted_review holds; carried records, overrides and composition records are
not acts. Minutes are the booked `active_minutes` quantity, rebuilt from the user
prompts of the session's transcript with hook-engine-start.py's booking rule.

Usage:
  review-cost-calibrate.py SNAPSHOT_DIR [--out PATH] [--decision KEY=N ...] [--report]
      SNAPSHOT_DIR holds state/ (copy of the agentctl state directory) and
      transcripts/ (<session_id>.jsonl). Both are read, never written.
  review-cost-calibrate.py --check FILE
      Re-derives the file from its embedded rows alone (the snapshot is never read);
      exits 1 on any difference, 2 on a malformed file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import effort  # noqa: E402
from agentctl.cli import _is_counted_review  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "operations" / "review-cost-calibration.json"

MIN_N = 20
CAP_MINUTES = 30.0  # substantive-wall-clock-min, the cap hook-engine-start.py books a prompt gap at
DEVIATION_LIMIT = 0.10
FORMULA = "p90 = sorted(v)[min(n-1, round(0.9*(n-1)))]; candidate = ceil(p90); n >= min_n else user_decision"
ROW_COLUMNS = ["session", "cycle", "plan_sha12", "opened_by", "closed_by", "open", "acts", "minutes", "minutes_bound"]
KEYS = {"review-cycle-acts": "review_cycle_acts", "review-cycle-active-minutes": "review_cycle_active_minutes"}
MOTIVATING_PREFIXES = ("2283939a", "7dd14604")
REVIEW_EVENTS = ("plan_review", "plan_pair_review")
REVIEW_RECORD_EVENTS = REVIEW_EVENTS + ("plan_review_compose", "review_carried")

BOUND_EXACT, BOUND_SCALED, BOUND_NONE = "exact", "scaled", "unavailable"


def parse_ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_act(event: dict) -> bool:
    return event.get("event") in REVIEW_EVENTS and _is_counted_review(
        str(event.get("verdict")), str(event.get("reviewer")))


def cut_cycles(history: list[dict]) -> tuple[list[dict], int]:
    """Review cycles of one session in order, and the acts that fall outside any cycle.

    A cycle opens on the first `submit_plan` (verified) or `replan` event whose
    plan_sha256 differs from the previous arrival's, and closes at the next such
    arrival or at `approve`. A same-sha event opens nothing, and so does a `replan` after
    the session's approval: that review loop is post-approval and not part of the population."""
    cycles: list[dict] = []
    current: dict | None = None
    previous_sha: str | None = None
    approved = False
    outside = 0

    def close(by: str, ts) -> None:
        nonlocal current
        if current is not None:
            current["closed_by"], current["t_close"], current["open"] = by, ts, False
            current = None

    for event in history:
        kind = event.get("event")
        sha = event.get("plan_sha256")
        if kind in ("submit_plan", "replan") and sha and sha != previous_sha and (
                kind == "replan" or event.get("verified")) and not (kind == "replan" and approved):
            approved = False
            ts = parse_ts(event.get("ts"))
            close("submit" if kind == "submit_plan" else "replan", ts)
            current = {"plan_sha": sha, "opened_by": "submit" if kind == "submit_plan" else "replan",
                       "closed_by": None, "open": True, "t_open": ts, "t_close": None, "acts": 0}
            cycles.append(current)
            previous_sha = sha
        elif kind == "approve":
            approved = True
            close("approve", parse_ts(event.get("ts")))
        elif is_act(event):
            if current is None:
                outside += 1
            else:
                current["acts"] += 1
    return cycles, outside


def blocked_intervals(history: list[dict]) -> tuple[list[tuple[float, float]], bool]:
    """BLOCKED intervals the history places, and whether any BLOCKED stretch is unplaced.

    `block` and `unblock` events place an interval; an `unblock` with no `block` before
    it (the engine parks a node without logging) or a `block` with no `unblock` leaves a
    stretch whose bounds are unknown."""
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    unplaced = False
    for event in history:
        kind, ts = event.get("event"), parse_ts(event.get("ts"))
        if kind == "block":
            if start is not None or ts is None:
                unplaced = True
            start = ts
        elif kind == "unblock":
            if start is None or ts is None:
                unplaced = True
            else:
                intervals.append((start, ts))
            start = None
    if start is not None:
        unplaced = True
    return intervals, unplaced


def book_prompts(prompts: list[float], child_runs, blocked: list[tuple[float, float]],
                 cap: float = CAP_MINUTES) -> list[tuple[float, float]]:
    """`(prompt ts, minutes booked at that prompt)` by hook-engine-start.py's rule: the
    first prompt books nothing; each later one books the gap since the previous prompt less
    the part a child run covers, capped, and nothing when the prompt lands while BLOCKED."""
    intervals = effort.child_run_intervals(child_runs, None)
    booked: list[tuple[float, float]] = []
    previous: float | None = None
    for ts in sorted(prompts):
        minutes = 0.0
        if previous is not None and ts > previous and not any(lo <= ts < hi for lo, hi in blocked):
            covered = effort.covered_seconds(intervals, previous, ts)
            minutes = min(max(0.0, (ts - previous) - covered) / 60.0, cap)
        booked.append((ts, minutes))
        previous = ts
    return booked


def cycle_minutes(booked: list[tuple[float, float]], cycle: dict) -> float | None:
    if cycle["t_open"] is None or (not cycle["open"] and cycle["t_close"] is None):
        return None
    hi = float("inf") if cycle["open"] else cycle["t_close"]
    return sum(m for ts, m in booked if cycle["t_open"] < ts <= hi)


def transcript_prompts(path: Path) -> list[float]:
    """Timestamps of the user-prompt turns of a Claude Code transcript: user-role lines that
    are neither tool results, meta lines, nor sidechain lines. A task-notification turn is
    kept: the UserPromptSubmit hook books it like a typed prompt."""
    out: list[float] = []
    with path.open("rb") as fh:
        for raw in fh:
            if b'"type":"user"' not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
                continue
            content = (line.get("message") or {}).get("content")
            if isinstance(content, list) and all(
                    isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            ts = parse_ts(line.get("timestamp"))
            if ts is not None:
                out.append(ts)
    return out


def has_review_records(state: dict) -> bool:
    if any(e.get("event") in REVIEW_RECORD_EVENTS for e in state.get("history") or ()):
        return True
    return bool(state.get("plan_review") or state.get("plan_pair_reviews") or state.get("plan_stage_reviews"))


def load_sessions(snapshot: Path) -> list[tuple[str, dict, Path]]:
    sessions = []
    for path in sorted((snapshot / "state").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id") == path.stem and isinstance(data.get("history"), list):
            if has_review_records(data):
                sessions.append((path.stem, data, path))
    return sessions


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def session_rows(session_id: str, state: dict, prompts: list[float] | None) -> tuple[list[list], dict]:
    history = state["history"]
    cycles, outside = cut_cycles(history)
    blocked, unplaced_block = blocked_intervals(history)
    runs = state.get("effort_child_runs") or []
    open_run = any(isinstance(r, dict) and not isinstance(r.get("ended"), (int, float)) for r in runs)
    recorded = float((state.get("effort_actuals") or {}).get("active_minutes") or 0.0)
    counted = int(state.get("user_prompt_count") or 0)
    window_start: float | None = None
    if prompts is not None and counted > len(prompts):
        prompts = None
    elif prompts is not None and 0 < counted < len(prompts):
        prompts = sorted(prompts)[-counted:]
        window_start = prompts[0]
    elif prompts is not None and counted == 0:
        prompts = None
    booked = book_prompts(prompts, runs, blocked) if prompts is not None else None
    rebuilt = sum(m for _, m in booked) if booked is not None else None
    deviation = None
    scale = None
    if rebuilt is not None:
        deviation = abs(rebuilt - recorded) / recorded if recorded > 0 else (0.0 if rebuilt == 0 else 1.0)
        if rebuilt > 0 and recorded > 0:
            scale = recorded / rebuilt
        elif rebuilt == 0 and recorded == 0:
            scale = 1.0
    exact = deviation is not None and deviation <= DEVIATION_LIMIT and not unplaced_block and not open_run
    rows = []
    for number, cycle in enumerate(cycles, 1):
        minutes = None
        if booked is not None and scale is not None and (
                window_start is None or (cycle["t_open"] is not None and cycle["t_open"] >= window_start)):
            raw = cycle_minutes(booked, cycle)
            minutes = None if raw is None else raw * scale
        bound = BOUND_NONE if minutes is None else (BOUND_EXACT if exact else BOUND_SCALED)
        rows.append([session_id, number, cycle["plan_sha"][:12], cycle["opened_by"], cycle["closed_by"],
                     cycle["open"], cycle["acts"], None if minutes is None else round(minutes, 3), bound])
    info = {
        "session": session_id,
        "cycles": len(cycles),
        "acts_outside_cycles": outside,
        "plan_pair_review_events": sum(1 for e in history if e.get("event") == "plan_pair_review"),
        "transcript": prompts is not None,
        "prompts": None if prompts is None else len(prompts),
        "recorded_prompts": int(state.get("user_prompt_count") or 0),
        "recorded_active_minutes": round(recorded, 3),
        "reconstructed_active_minutes": None if rebuilt is None else round(rebuilt, 3),
        "deviation": None if deviation is None else round(deviation, 4),
        "unplaced_block": unplaced_block,
        "open_child_run": open_run,
    }
    return rows, info


def p90(values: list[float]):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]


def population(rows: list[list], column: str) -> dict:
    idx = ROW_COLUMNS.index(column)
    taken = [r for r in rows if r[idx] is not None]
    per_session: dict[str, int] = {}
    for r in taken:
        per_session[r[0]] = per_session.get(r[0], 0) + 1
    pop = {
        "n": len(taken),
        "sessions": len(per_session),
        "max_session_share": round(max(per_session.values()) / len(taken), 4) if taken else 0.0,
        "open_cycles": sum(1 for r in taken if r[ROW_COLUMNS.index("open")]),
    }
    if column == "minutes":
        bounds = {r[ROW_COLUMNS.index("minutes_bound")] for r in taken}
        pop["minutes_bound"] = BOUND_SCALED if BOUND_SCALED in bounds else BOUND_EXACT
        pop["exact_rows"] = sum(1 for r in taken if r[ROW_COLUMNS.index("minutes_bound")] == BOUND_EXACT)
        pop["cycles_without_minutes"] = len(rows) - len(taken)
    return pop


def candidate_of(values: list[float]) -> tuple[float | None, int | None]:
    if not values:
        return None, None
    value = p90(values)
    return value, math.ceil(round(value, 6))


def derive(rows: list[list], decisions: dict[str, int]) -> tuple[dict, dict]:
    columns = {"review-cycle-acts": "acts", "review-cycle-active-minutes": "minutes"}
    pops, derived = {}, {}
    for key, column in columns.items():
        idx = ROW_COLUMNS.index(column)
        values = [r[idx] for r in rows if r[idx] is not None]
        pops[KEYS[key]] = population(rows, column)
        value, candidate = candidate_of(values)
        entry = {"n": len(values), "p90": value, "candidate": candidate}
        if len(values) >= MIN_N:
            entry.update(rule="formula", value=candidate)
        else:
            entry.update(rule="user_decision", decision=decisions.get(key), value=decisions.get(key))
        derived[key] = entry
    return pops, derived


def motivating(rows: list[list], derived: dict) -> dict:
    threshold = derived["review-cycle-acts"]["p90"]
    out = {}
    for prefix in MOTIVATING_PREFIXES:
        mine = [r for r in rows if r[0].startswith(prefix)]
        if not mine:
            out[prefix] = {"session": None, "cycles": 0, "max_acts": None, "at_or_above_p90": None}
            continue
        top = max(r[ROW_COLUMNS.index("acts")] for r in mine)
        out[prefix] = {"session": mine[0][0], "cycles": len(mine), "max_acts": top,
                       "at_or_above_p90": None if threshold is None else top >= threshold}
    return out


def rows_digest(rows: list[list]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(snapshot: Path, decisions: dict[str, int]) -> dict:
    rows: list[list] = []
    inventory = []
    manifest = []
    for session_id, state, path in load_sessions(snapshot):
        manifest.append(f"state/{path.name} {file_sha256(path)}")
        transcript = snapshot / "transcripts" / f"{session_id}.jsonl"
        prompts = None
        if transcript.is_file():
            manifest.append(f"transcripts/{transcript.name} {file_sha256(transcript)}")
            prompts = transcript_prompts(transcript)
        session_rows_, info = session_rows(session_id, state, prompts)
        rows.extend(session_rows_)
        inventory.append(info)
    return assemble(rows, inventory, hashlib.sha256("\n".join(sorted(manifest)).encode()).hexdigest(), decisions)


def assemble(rows: list[list], inventory: list[dict], inputs_sha256: str, decisions: dict[str, int]) -> dict:
    pops, derived = derive(rows, decisions)
    return {
        "formula": FORMULA,
        "min_n": MIN_N,
        "inputs_sha256": inputs_sha256,
        "inventory": inventory,
        "row_columns": ROW_COLUMNS,
        "rows": rows,
        "rows_sha256": rows_digest(rows),
        "populations": pops,
        "derived": derived,
        "motivating": motivating(rows, derived),
    }


class Malformed(Exception):
    pass


def check(path: Path) -> list[str]:
    """Differences between the file and a re-derivation from its own rows; raises Malformed."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Malformed(f"unreadable: {exc}")
    if not isinstance(doc, dict):
        raise Malformed("not an object")
    for key in ("min_n", "row_columns", "rows", "rows_sha256", "populations", "derived", "motivating"):
        if key not in doc:
            raise Malformed(f"missing key {key!r}")
    rows, derived_in = doc["rows"], doc["derived"]
    if doc["row_columns"] != ROW_COLUMNS or doc["min_n"] != MIN_N:
        raise Malformed("row_columns or min_n differ from the script's")
    if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != len(ROW_COLUMNS) for r in rows):
        raise Malformed("rows are not lists of row_columns length")
    if not isinstance(derived_in, dict) or set(derived_in) != set(KEYS):
        raise Malformed("derived keys differ")
    if any(not isinstance(v, dict) for v in derived_in.values()):
        raise Malformed("derived entries are not objects")
    diffs: list[str] = []
    if doc["rows_sha256"] != rows_digest(rows):
        diffs.append("rows_sha256 does not match the rows")
    decisions = {k: v.get("decision") for k, v in derived_in.items()
                 if v.get("rule") == "user_decision" and v.get("decision") is not None}
    try:
        pops, derived = derive(rows, decisions)
        want_motivating = motivating(rows, derived)
    except (TypeError, AttributeError, ValueError, IndexError) as exc:
        raise Malformed(f"rows do not hold the declared columns: {exc}")
    if doc["populations"] != pops:
        diffs.append("populations differ from the rows")
    for key, want in derived.items():
        got = derived_in[key]
        for field in ("n", "p90", "candidate", "rule", "value"):
            if got.get(field) != want.get(field):
                diffs.append(f"derived[{key}].{field}: file {got.get(field)!r}, rows give {want.get(field)!r}")
        if want["rule"] == "user_decision" and got.get("decision") != want.get("decision"):
            diffs.append(f"derived[{key}].decision: file {got.get('decision')!r}, expected {want.get('decision')!r}")
    if doc["motivating"] != want_motivating:
        diffs.append("motivating differs from the rows")
    return diffs


def parse_decisions(items: list[str]) -> dict[str, int]:
    out = {}
    for item in items:
        key, _, value = item.partition("=")
        if key not in KEYS or not value.isdigit():
            raise SystemExit(f"--decision wants KEY=N with KEY in {sorted(KEYS)}: {item!r}")
        out[key] = int(value)
    return out


def report(doc: dict) -> str:
    inv = doc["inventory"]
    with_tr = [i for i in inv if i["transcript"]]
    lines = [
        f"sessions with review records: {len(inv)}; with transcript: {len(with_tr)}; cycles: {len(doc['rows'])}",
    ]
    for key, entry in doc["derived"].items():
        pop = doc["populations"][KEYS[key]]
        lines.append(f"{key}: n={entry['n']} sessions={pop['sessions']} p90={entry['p90']} "
                     f"candidate={entry['candidate']} rule={entry['rule']}")
    devs = sorted(i["deviation"] for i in with_tr if i["deviation"] is not None)
    if devs:
        within = sum(1 for d in devs if d <= DEVIATION_LIMIT)
        lines.append(f"reconstruction within {DEVIATION_LIMIT:.0%} of recorded: {within}/{len(devs)} sessions")
    lines.append(f"motivating: {json.dumps(doc['motivating'], sort_keys=True)}")
    for i in with_tr:
        lines.append(f"  {i['session'][:8]} prompts={i['prompts']}/{i['recorded_prompts']} recorded={i['recorded_active_minutes']} "
                     f"rebuilt={i['reconstructed_active_minutes']} dev={i['deviation']} "
                     f"blk={int(i['unplaced_block'])} run={int(i['open_child_run'])} cycles={i['cycles']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", nargs="?", help="snapshot directory holding state/ and transcripts/")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--decision", action="append", default=[], metavar="KEY=N")
    parser.add_argument("--report", action="store_true", help="print the digest and write nothing")
    parser.add_argument("--check", metavar="FILE")
    args = parser.parse_args(argv)
    if args.check:
        try:
            diffs = check(Path(args.check))
        except Malformed as exc:
            print(f"malformed: {exc}", file=sys.stderr)
            return 2
        for diff in diffs:
            print(diff, file=sys.stderr)
        if diffs:
            return 1
        pending = [k for k, v in json.loads(Path(args.check).read_text())["derived"].items() if v.get("value") is None]
        print("ok" + (f" (decision pending: {', '.join(pending)})" if pending else ""))
        return 0
    if not args.snapshot:
        parser.error("a snapshot directory or --check FILE is required")
    doc = build(Path(args.snapshot), parse_decisions(args.decision))
    if args.report:
        print(report(doc))
        return 0
    Path(args.out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print("\n".join(report(doc).splitlines()[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
