"""Baseline of where planning time went, every committed number re-derivable from hashed rows.

Difficulty removed: a change to how plans are made (the reachability-first trial) can only
be judged against a fixed reference. A reference recalled from a dossier or re-counted by
hand drifts; this one is a committed table of opaque rows whose aggregates `--check`
re-derives, and whose rows `--check --inputs` regenerates from the private snapshot.

Three row tables, no free text:
  rows       one per session with a review record in the window: counted review acts, the
             admissible cut, plan versions, pair-review events, active minutes
  role_rows  one per spawn role: spawns, list-price spend, spawn-hours
  axis_rows  one per (session, plan version): concern counts by axis, and how many of those
             concerns change what the plan builds (the concern text stays in the snapshot)

Usage:
  planning-cost-report.py SNAPSHOT_DIR [--out PATH]
      SNAPSHOT_DIR holds state/, transcripts/, claude-spawn-costs.jsonl and dossier/
      (classified-<session>.json). Read, never written.
  planning-cost-report.py --check FILE
      Re-derives totals, axes, counterfactual and dossier_comparison from the embedded
      rows alone. Exits 1 with `MISMATCH: <key>` lines on any difference, 2 if malformed.
  planning-cost-report.py --check FILE --inputs SNAPSHOT_DIR
      Also regenerates the rows from the snapshot and compares them (and the inputs digest).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.review_cycles import (  # noqa: E402
    REVIEW_RECORD_EVENTS, file_sha256, is_act, load_sessions, parse_ts, transcript_prompts)

REPO_ROOT = SCRIPTS_DIR.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "operations" / "planning-cost-baseline.json"

WINDOW = ["2026-10-08", "2026-10-23"]  # [start, end) by UTC day of the event
AXES = "RVDWS"
ROW_COLUMNS = ["session", "first_review_day", "plan_versions", "review_acts", "approved", "acts_to_cut",
               "acts_after_cut", "pair_review_events", "active_minutes", "cycle_minutes_to_cut"]
ROLE_COLUMNS = ["role", "spawns", "cost_usd", "hours"]
AXIS_COLUMNS = ["session", "plan_version", "R", "V", "D", "W", "S", "builds"]
COUNTED_SESSIONS_NOTE = "sessions never approved have no admissible cut: acts_to_cut and acts_after_cut are null"

FORMULA = {
    "window": "a session is in the window when its first review record (plan_review, plan_pair_review, "
              "plan_review_compose, review_carried) falls in [window[0], window[1]) by UTC day",
    "counted_act": "a plan_review or plan_pair_review history event for which agentctl.cli._is_counted_review "
                   "holds (lib.review_cycles.is_act); carried, override and composition records are not acts",
    "review_acts": "counted acts over the whole history",
    "admissible_cut": "acts_to_cut = counted acts before the first approve event; acts_after_cut = review_acts - "
                      "acts_to_cut; a session never approved has no cut (null) and is counted in "
                      "totals.sessions_never_approved",
    "plan_versions": "distinct plan_sha256 over submit_plan and replan events",
    "active_minutes": "state effort_actuals.active_minutes, the booked quantity",
    "cycle_minutes_to_cut": "sum of the rebuilt minutes of the review cycles of review-cost-calibrate.py "
                            "(lib.review_cycles cycle cutting, scaled to active_minutes); null without a transcript",
    "spend": "claude-spawn-costs.jsonl rows with event spawn and ts in the window, grouped by kind: cost_usd is "
             "list price (telemetry under flat billing, not money), hours = sum(duration_ms) / 3.6e6",
    "axes": "R reachability, V controls, D format accounting, W wording, S safety and rollback; the label of one "
            "concern is taken from the dossier's classified-<session>.json as given (comp.py's rule: the first "
            "letter of the label; builds = label ends with * or is a bare R)",
    "builds": "concerns whose label changes what the plan builds",
    "counterfactual": "if only concerns that change what is built blocked approval, acts_to_cut shrinks by the "
                      "share of such concerns: acts_to_cut * builds_share over the labelled sessions, an "
                      "illustration under the labelling error, not a prediction",
    "hash": "sha256 of json.dumps(table, sort_keys=True, separators=(',', ':')) over the table's rows",
    "labelling_error": "labels are one reader's judgement, error about +-5 percentage points",
}

DOSSIER = {
    "role_spend_usd": {"thinker": 4055.0, "developer": 983.0, "planner": 964.0},
    "role_hours": {"thinker": 174.0, "developer": 110.0, "planner": 44.0},
    "sessions_with_review": 30,
    "axis_share_2283939a": {"R": 12, "V": 48, "D": 24, "W": 13, "S": 3},
    "builds_share_range_pct": [15, 23],
}
SPEND_TOLERANCE = 0.05
SHARE_TOLERANCE_PP = 5.0
HEAVY_SESSION = "2283939a"
SPAN_EXPLANATION = ("the dossier was written during the last day of its span; its figures lie between the same "
                    "sums over a window ending before that day and over the full window, so the difference is "
                    "the part of that day the dossier had not yet seen")


class Malformed(Exception):
    pass


def digest(table: list) -> str:
    return hashlib.sha256(json.dumps(table, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def in_window(day: str | None) -> bool:
    return day is not None and WINDOW[0] <= day < WINDOW[1]


def load_calibrate():
    spec = importlib.util.spec_from_file_location("review_cost_calibrate", SCRIPTS_DIR / "review-cost-calibrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def day_of(ts) -> str | None:
    seconds = parse_ts(ts)
    return None if seconds is None else dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%d")


def session_row(session_id: str, state: dict, cal, prompts) -> list | None:
    history = state["history"]
    review_days = sorted(d for d in (day_of(e.get("ts")) for e in history
                                     if e.get("event") in REVIEW_RECORD_EVENTS) if d)
    if not review_days or not in_window(review_days[0]):
        return None
    acts_total = 0
    acts_to_cut = 0
    approved = False
    versions = set()
    for event in history:
        kind = event.get("event")
        if kind in ("submit_plan", "replan") and event.get("plan_sha256"):
            versions.add(event["plan_sha256"])
        if kind == "approve":
            approved = True
        if is_act(event):
            acts_total += 1
            if not approved:
                acts_to_cut += 1
    cal_rows, _ = cal.session_rows(session_id, state, prompts)
    minutes_idx = cal.ROW_COLUMNS.index("minutes")
    minutes = [r[minutes_idx] for r in cal_rows if r[minutes_idx] is not None]
    return [session_id[:8], review_days[0], len(versions), acts_total, approved,
            acts_to_cut if approved else None, acts_total - acts_to_cut if approved else None,
            sum(1 for e in history if e.get("event") == "plan_pair_review"),
            round(float((state.get("effort_actuals") or {}).get("active_minutes") or 0.0), 3),
            round(sum(minutes), 3) if minutes else None]


def session_rows_and_manifest(snapshot: Path) -> tuple[list[list], list[str]]:
    cal = load_calibrate()
    rows, manifest = [], []
    for session_id, state, path in load_sessions(snapshot):
        transcript = snapshot / "transcripts" / f"{session_id}.jsonl"
        prompts = transcript_prompts(transcript) if transcript.is_file() else None
        row = session_row(session_id, state, cal, prompts)
        if row is None:
            continue
        rows.append(row)
        manifest.append(f"state/{path.name} {file_sha256(path)}")
        if prompts is not None:
            manifest.append(f"transcripts/{transcript.name} {file_sha256(transcript)}")
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows, manifest


def role_rows_and_manifest(snapshot: Path) -> tuple[list[list], list[str]]:
    path = snapshot / "claude-spawn-costs.jsonl"
    totals: dict[str, list] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") != "spawn" or not in_window((row.get("ts") or "")[:10]):
            continue
        entry = totals.setdefault(str(row.get("kind")), [0, 0.0, 0.0])
        entry[0] += 1
        entry[1] += float(row.get("cost_usd") or 0.0)
        entry[2] += float(row.get("duration_ms") or 0.0) / 3.6e6
    rows = [[role, n, round(cost, 4), round(hours, 4)] for role, (n, cost, hours) in sorted(totals.items())]
    return rows, [f"claude-spawn-costs.jsonl {file_sha256(path)}"]


def axis_rows_and_manifest(snapshot: Path) -> tuple[list[list], list[str]]:
    rows, manifest = [], []
    for path in sorted((snapshot / "dossier").glob("classified-*.json")):
        session = path.stem.split("-", 1)[1]
        counts: dict[int, dict[str, int]] = {}
        for item in json.loads(path.read_text(encoding="utf-8")):
            cell = counts.setdefault(int(item["plan_version"]), {a: 0 for a in AXES + "B"})
            cell[item["category"]] += 1
            cell["B"] += 1 if item["changes_what_is_built"] else 0
        rows.extend([session, version] + [cell[a] for a in AXES] + [cell["B"]]
                    for version, cell in sorted(counts.items()))
        manifest.append(f"dossier/{path.name} {file_sha256(path)}")
    return rows, manifest


def build_tables(snapshot: Path) -> tuple[list, list, list, str]:
    rows, m1 = session_rows_and_manifest(snapshot)
    roles, m2 = role_rows_and_manifest(snapshot)
    axis, m3 = axis_rows_and_manifest(snapshot)
    return rows, roles, axis, hashlib.sha256("\n".join(sorted(m1 + m2 + m3)).encode()).hexdigest()


def column(rows: list[list], columns: list[str], name: str) -> list:
    idx = columns.index(name)
    return [r[idx] for r in rows]


def derive_totals(rows: list[list], roles: list[list]) -> dict:
    col = lambda name: column(rows, ROW_COLUMNS, name)  # noqa: E731
    cut = [v for v in col("acts_to_cut") if v is not None]
    after = [v for v in col("acts_after_cut") if v is not None]
    minutes = [v for v in col("cycle_minutes_to_cut") if v is not None]
    versions = col("plan_versions")
    return {
        "sessions": len(rows),
        "sessions_with_acts": sum(1 for v in col("review_acts") if v > 0),
        "sessions_approved": len(cut),
        "sessions_never_approved": len(rows) - len(cut),
        "review_acts": sum(col("review_acts")),
        "acts_to_cut": sum(cut),
        "acts_after_cut": sum(after),
        "pair_review_events": sum(col("pair_review_events")),
        "plan_versions": sum(versions),
        "max_plan_versions": sorted(versions, reverse=True)[:2],
        "active_minutes": round(sum(col("active_minutes")), 3),
        "cycle_minutes_to_cut": round(sum(minutes), 3),
        "sessions_with_cycle_minutes": len(minutes),
        "spend_usd_by_role": {r[0]: r[2] for r in roles},
        "hours_by_role": {r[0]: r[3] for r in roles},
        "spawns_by_role": {r[0]: r[1] for r in roles},
        "spend_usd_total": round(sum(r[2] for r in roles), 4),
        "hours_total": round(sum(r[3] for r in roles), 4),
    }


def share(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def derive_axes(axis_rows: list[list]) -> dict:
    out: dict = {}
    for session in sorted({r[0] for r in axis_rows}):
        mine = [r for r in axis_rows if r[0] == session]
        counts = {a: sum(r[AXIS_COLUMNS.index(a)] for r in mine) for a in AXES}
        n = sum(counts.values())
        builds = sum(r[AXIS_COLUMNS.index("builds")] for r in mine)
        out[session] = {"concerns": n, "versions": len(mine), "counts": counts,
                        "share_pct": {a: share(counts[a], n) for a in AXES},
                        "builds": builds, "builds_pct": share(builds, n)}
    n = sum(v["concerns"] for v in out.values())
    builds = sum(v["builds"] for v in out.values())
    return {"sessions": out, "concerns": n, "builds": builds, "builds_pct": share(builds, n)}


def derive_counterfactual(totals: dict, axes: dict) -> dict:
    pct = axes["builds_pct"]
    return {
        "builds_pct": pct,
        "acts_to_cut": totals["acts_to_cut"],
        "acts_if_only_building_concerns_block": round(totals["acts_to_cut"] * pct / 100.0, 1),
        "thinker_spend_usd": totals["spend_usd_by_role"].get("thinker"),
        "thinker_spend_usd_if_only_building_concerns_block": round(
            (totals["spend_usd_by_role"].get("thinker") or 0.0) * pct / 100.0, 2),
        "basis": FORMULA["counterfactual"],
    }


def compare(name: str, derived, dossier, tolerance: float, unit: str, explanation: str = "") -> dict:
    if derived is None or not dossier:
        return {"claim": name, "dossier": dossier, "derived": derived, "within": None, "explanation": explanation}
    if unit == "pct":
        delta = round(derived - dossier, 2)
        within = abs(delta) <= SHARE_TOLERANCE_PP
    else:
        delta = round((derived - dossier) / dossier, 4)
        within = abs(delta) <= tolerance
    return {"claim": name, "dossier": dossier, "derived": derived, "delta": delta, "unit": unit,
            "within": within, "explanation": "" if within else explanation}


def derive_comparison(totals: dict, axes: dict) -> list[dict]:
    out = []
    for role in ("thinker", "developer", "planner"):
        out.append(compare(f"{role} spend usd", totals["spend_usd_by_role"].get(role),
                           DOSSIER["role_spend_usd"][role], SPEND_TOLERANCE, "rel",
                           SPAN_EXPLANATION))
        out.append(compare(f"{role} hours", totals["hours_by_role"].get(role),
                           DOSSIER["role_hours"][role], SPEND_TOLERANCE, "rel",
                           SPAN_EXPLANATION))
    out.append(compare("sessions with review", totals["sessions"], DOSSIER["sessions_with_review"],
                       SPEND_TOLERANCE, "rel", "the dossier does not record its session criterion; this report counts every session with a review "
                       "record in the window, including sessions with pair reviews only"))
    heavy = axes["sessions"].get(HEAVY_SESSION)
    for axis in AXES:
        out.append(compare(f"axis {axis} share in {HEAVY_SESSION} pct",
                           heavy["share_pct"][axis] if heavy else None,
                           DOSSIER["axis_share_2283939a"][axis], SHARE_TOLERANCE_PP, "pct"))
    lo, hi = DOSSIER["builds_share_range_pct"]
    values = [v["builds_pct"] for v in axes["sessions"].values()]
    out.append({"claim": "share of concerns that change what the plan builds, range over labelled sessions pct",
                "dossier": [lo, hi], "derived": [min(values), max(values)] if values else None,
                "within": bool(values) and lo - SHARE_TOLERANCE_PP <= min(values) and max(values) <= hi + SHARE_TOLERANCE_PP,
                "explanation": ""})
    out.append({"claim": "93 percent of thinker spend went to superseded plan versions", "dossier": 93,
                "derived": None, "within": None,
                "explanation": "not re-derivable: spawn rows carry neither session nor plan version; the dossier "
                               "figure is an upper bound (the engine carries some verdicts to a new version)"})
    return out


def assemble(rows: list, roles: list, axis_rows: list, inputs_sha256: str | None) -> dict:
    totals = derive_totals(rows, roles)
    axes = derive_axes(axis_rows)
    return {
        "formula": FORMULA,
        "window": WINDOW,
        "inputs_sha256": inputs_sha256,
        "row_columns": ROW_COLUMNS,
        "rows": rows,
        "rows_sha256": digest(rows),
        "role_columns": ROLE_COLUMNS,
        "role_rows": roles,
        "role_rows_sha256": digest(roles),
        "axis_columns": AXIS_COLUMNS,
        "axis_rows": axis_rows,
        "axis_rows_sha256": digest(axis_rows),
        "totals": totals,
        "axes": axes,
        "counterfactual": derive_counterfactual(totals, axes),
        "dossier_comparison": derive_comparison(totals, axes),
        "notes": [
            "Concern labels are one reader's judgement, with an error of about 5 percentage points; they are "
            "used as the dossier gave them and were not re-labelled.",
            COUNTED_SESSIONS_NOTE,
            "spend is list price, telemetry under flat billing, not money.",
            "The counterfactual is an illustration from the building-concern share, not a measured saving.",
        ],
    }


def check(doc, snapshot: Path | None) -> list[str]:
    if not isinstance(doc, dict):
        raise Malformed("not an object")
    for key in ("row_columns", "rows", "role_columns", "role_rows", "axis_columns", "axis_rows", "rows_sha256",
                "role_rows_sha256", "axis_rows_sha256", "totals", "axes", "counterfactual", "dossier_comparison"):
        if key not in doc:
            raise Malformed(f"missing key {key!r}")
    for cols, rows_key, want in (("row_columns", "rows", ROW_COLUMNS), ("role_columns", "role_rows", ROLE_COLUMNS),
                                 ("axis_columns", "axis_rows", AXIS_COLUMNS)):
        if doc[cols] != want:
            raise Malformed(f"{cols} differ from the script's")
        if not isinstance(doc[rows_key], list) or any(
                not isinstance(r, list) or len(r) != len(want) for r in doc[rows_key]):
            raise Malformed(f"{rows_key} are not lists of {cols} length")
    rows, roles, axis_rows = doc["rows"], doc["role_rows"], doc["axis_rows"]
    diffs: list[str] = []
    for name, table in (("rows", rows), ("role_rows", roles), ("axis_rows", axis_rows)):
        if doc[f"{name}_sha256"] != digest(table):
            diffs.append(f"MISMATCH: {name}_sha256 does not match the {name}")
    try:
        want = assemble(rows, roles, axis_rows, doc.get("inputs_sha256"))
    except (TypeError, AttributeError, ValueError, IndexError, KeyError) as exc:
        raise Malformed(f"rows do not hold the declared columns: {exc}")
    for key in ("totals", "axes", "counterfactual", "dossier_comparison"):
        if doc[key] != want[key]:
            diffs.append(f"MISMATCH: {key}")
    diffs.extend(f"MISMATCH: dossier_comparison unexplained: {c['claim']}"
                 for c in want["dossier_comparison"] if c["within"] is False and not c["explanation"])
    if snapshot is not None:
        fresh_rows, fresh_roles, fresh_axis, inputs_sha = build_tables(snapshot)
        for name, got, now in (("rows", rows, fresh_rows), ("role_rows", roles, fresh_roles),
                               ("axis_rows", axis_rows, fresh_axis)):
            if got != now:
                diffs.append(f"MISMATCH: {name} differ from the snapshot")
        if doc.get("inputs_sha256") != inputs_sha:
            diffs.append("MISMATCH: inputs_sha256 differs from the snapshot")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", nargs="?", help="snapshot directory")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", metavar="FILE")
    parser.add_argument("--inputs", metavar="SNAPSHOT_DIR")
    parser.add_argument("--report", action="store_true", help="print the totals and write nothing")
    args = parser.parse_args(argv)
    if args.check:
        try:
            doc = json.loads(Path(args.check).read_text(encoding="utf-8"))
            diffs = check(doc, Path(args.inputs) if args.inputs else None)
        except (OSError, ValueError, Malformed) as exc:
            print(f"malformed: {exc}", file=sys.stderr)
            return 2
        for diff in diffs:
            print(diff)
        if diffs:
            return 1
        print("ok")
        return 0
    if not args.snapshot:
        parser.error("a snapshot directory or --check FILE is required")
    rows, roles, axis_rows, inputs_sha = build_tables(Path(args.snapshot))
    doc = assemble(rows, roles, axis_rows, inputs_sha)
    if args.report:
        print(json.dumps({k: doc[k] for k in ("totals", "axes", "counterfactual", "dossier_comparison")}, indent=1))
        return 0
    Path(args.out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"sessions={doc['totals']['sessions']} review_acts={doc['totals']['review_acts']} "
          f"acts_to_cut={doc['totals']['acts_to_cut']} axis_rows={len(axis_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
