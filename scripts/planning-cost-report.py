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

Trial mode (the reachability-first trial: a control arm against a risk-shaped arm). ARMS_DIR holds
control/ and risk/, each with state.json, history.jsonl, ledger.jsonl and plan.toml (the arm's final
plan), and optionally classified.json ([{id, axis}]), judge.json ({R, V}) and skills.json.
  planning-cost-report.py concerns ARMS_DIR
      Blind export of every counted review concern: opaque id and text, no arm, phase, axis or bet.
  planning-cost-report.py judge-view ARMS_DIR ARM
      The arm's plan (plan.toml: for the risk arm the plan after its launch elaborations, an unwritten one
      shown as it is) rendered without bets and phase, with each stage's knowledge appended because the
      plan view omits it.
  planning-cost-report.py trial ARMS_DIR [--out PATH] [--cap N] [--engine-commit SHA] [--baseline FILE]
      One row per arm, the comparison and the decision. `--check FILE [--inputs ARMS_DIR]` of a
      trial document re-derives them and asserts the arms ran under the trial's rules.
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


ARMS = ("control", "risk")
TRIAL_CAP = 137
TRIAL_COLUMNS = [
    "arm", "session_id", "bound", "state_sha256", "history_sha256", "stop_reason", "blockers", "lazy_pending",
    "approve_sandbox", "approve_elaboration", "dispatch_events", "agent_overrides", "diagnosing_entries",
    "replans", "review_acts_to_admissible", "review_acts_deferred", "review_acts_total",
    "thinker_spawn_h_to_admissible", "thinker_spawn_h_deferred", "thinker_spawn_h_total", "minutes",
    "skeleton_returns", "late_r", "judge_r", "judge_v", "skills_planner_sha256", "skills_thinker_sha256"]
STOP_REASONS = ("admissible", "elaborated", "cap", "forced")
TRIAL_KEYS = ("mode", "engine_commit", "baseline_sha256", "cap", "row_columns", "rows", "rows_sha256", "arms",
              "comparison", "decision")
FASTER_RATIO = 0.5
TRIAL_FORMULA = {
    "arms": "control is unbound (never approved) and runs until the plan has no review blockers; risk is bound, "
            "is approved once as a sandbox and then elaborates its at-launch stages until none is pending",
    "stop_reason": "forced = an agent override stands; elaborated = bound, approved, no lazy stage pending; "
                   "admissible = no blocker and not (bound and approved); cap = the run stopped before the "
                   "arm reached its condition (cap is the review-act budget the run was given)",
    "review_acts": "counted acts (lib.review_cycles.is_act) before the first approve event are to_admissible, "
                   "after it deferred; an arm that never approved has no deferred acts",
    "thinker_spawn_h": "spawn-cost ledger rows of kind thinker whose spawner_session (else session_id) is the arm's session, "
                       "duration_ms / 3.6e6, split at the ts of the first approve event",
    "late_r": "concerns the classifier put on axis R that were raised after the first approve event",
    "judge_r_v": "defects of axes R and V the blind judge found in the plan rendered by judge-view",
    "faster": "the risk arm's total review acts AND thinker spawn-hours are each <= 50 percent of control's",
    "quality_held": "the risk arm's judge R+V defects <= control's",
    "verdict": "inconclusive unless control stopped admissible and risk stopped elaborated and both "
               "comparisons are defined; then pass when faster and quality held, else fail",
}


def read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError) as exc:
        raise Malformed(f"{path}: {exc}")


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Malformed(f"{path}: {exc}")


def concern_id(arm: str, event_index: int, position: int, text: str) -> str:
    return hashlib.sha256(f"{arm}\0{event_index}\0{position}\0{text}".encode()).hexdigest()[:12]


def first_approve_index(history: list[dict]) -> int | None:
    return next((i for i, e in enumerate(history) if e.get("event") == "approve"), None)


def arm_concerns(arm: str, history: list[dict]) -> list[dict]:
    """Every concern of a counted review, with its opaque id and whether it was raised after the approve."""
    cut = first_approve_index(history)
    out = []
    for i, event in enumerate(history):
        if not is_act(event):
            continue
        for position, text in enumerate(event.get("concerns") or ()):
            out.append({"id": concern_id(arm, i, position, str(text)), "text": str(text),
                        "deferred": cut is not None and i > cut})
    return out


def arm_dir_plan(arm_dir: Path) -> Path:
    plan = arm_dir / "plan.toml"
    if not plan.is_file():
        raise Malformed(f"{plan} is missing")
    return plan


def gate_facts(state_text: str, plan: Path) -> tuple[bool, list[str], list[int], dict]:
    """bound, plan_review_blockers and the stages whose launch is still owed, from the engine's own gates."""
    import os
    from agentctl import gates
    from agentctl.state import SessionState
    # Review records bind to the plan path the arm wrote; rebound to the copy here, or a recorded pass
    # reads as missing once the arm's own plan file is gone.
    recorded = json.loads(state_text).get("plan_path")
    if recorded:
        state_text = state_text.replace(json.dumps(recorded)[1:-1], json.dumps(str(plan))[1:-1])
    state = SessionState.from_json(state_text)
    state.plan_path = str(plan)
    saved = os.environ.get("AGENTCTL_PLAN_REVIEW")
    os.environ["AGENTCTL_PLAN_REVIEW"] = "1"
    try:
        blockers = gates.plan_review_blockers(state, str(plan))
        doc = gates.load_plan(str(plan))
        pending = [s.index for s in doc.stages if gates.lazy_stage_blockers(state, s.index)]
    finally:
        if saved is None:
            os.environ.pop("AGENTCTL_PLAN_REVIEW", None)
        else:
            os.environ["AGENTCTL_PLAN_REVIEW"] = saved
    return bool(state.risk_planning), list(blockers), pending, state.effort_actuals or {}


def skill_hashes(arm_dir: Path) -> tuple[str, str]:
    given = read_json(arm_dir / "skills.json")
    if given is not None:
        return str(given["planner"]), str(given["thinker"])
    return tuple(file_sha256(REPO_ROOT / "skills" / "specializations" / role / "SKILL.md")
                 for role in ("planner", "thinker"))


def derive_stop_reason(row: dict) -> str:
    approved = row["approve_sandbox"] > 0
    if row["agent_overrides"]:
        return "forced"
    if row["bound"] and approved and not row["lazy_pending"]:
        return "elaborated"
    if not (row["bound"] and approved) and not row["blockers"]:
        return "admissible"
    return "cap"


def arm_row(arm: str, arm_dir: Path) -> list:
    state_path = arm_dir / "state.json"
    if not state_path.is_file():
        raise Malformed(f"{state_path} is missing")
    state_text = state_path.read_text(encoding="utf-8")
    history = read_jsonl(arm_dir / "history.jsonl")
    ledger = read_jsonl(arm_dir / "ledger.jsonl")
    state_json = json.loads(state_text)
    session = str(state_json.get("session_id"))
    bound, blockers, pending, actuals = gate_facts(state_text, arm_dir_plan(arm_dir))
    cut = first_approve_index(history)
    cut_ts = parse_ts(history[cut].get("ts")) if cut is not None else None
    acts = [(i, e) for i, e in enumerate(history) if is_act(e)]
    deferred_acts = sum(1 for i, _ in acts if cut is not None and i > cut)
    hours = [0.0, 0.0]
    for item in ledger:
        # A spawn made without --session outside a linked contour has no spawner_session; its session_id
        # is then the harness session that ran spawn-specialist, the arm's root.
        spawner = item.get("spawner_session") or item.get("session_id")
        if item.get("event") != "spawn" or item.get("kind") != "thinker" or spawner != session:
            continue
        ts = parse_ts(item.get("ts"))
        late = cut_ts is not None and ts is not None and ts > cut_ts
        hours[1 if late else 0] += float(item.get("duration_ms") or 0.0) / 3.6e6
    approves = sum(1 for e in history if e.get("event") == "approve")
    classified = read_json(arm_dir / "classified.json")
    judged = read_json(arm_dir / "judge.json")
    late_r = None
    if classified is not None:
        axes = {c["id"]: c["axis"] for c in classified}
        late_r = sum(1 for c in arm_concerns(arm, history) if c["deferred"] and axes.get(c["id"]) == "R")
    planner_sha, thinker_sha = skill_hashes(arm_dir)
    row = {
        "arm": arm, "session_id": session, "bound": bound,
        "state_sha256": hashlib.sha256(state_text.encode()).hexdigest(),
        "history_sha256": file_sha256(arm_dir / "history.jsonl"),
        "blockers": blockers, "lazy_pending": pending,
        "approve_sandbox": min(approves, 1), "approve_elaboration": max(approves - 1, 0),
        "dispatch_events": sum(1 for e in history if e.get("event") == "dispatch"),
        "agent_overrides": sum(1 for e in history if e.get("event") == "plan_review"
                               and e.get("verdict") == "override" and e.get("reviewer") == "agent"),
        "diagnosing_entries": sum(1 for e in history if e.get("event") == "declare"),
        "replans": sum(1 for e in history if e.get("event") == "replan"),
        "review_acts_to_admissible": len(acts) - deferred_acts, "review_acts_deferred": deferred_acts,
        "review_acts_total": len(acts),
        "thinker_spawn_h_to_admissible": round(hours[0], 4), "thinker_spawn_h_deferred": round(hours[1], 4),
        "thinker_spawn_h_total": round(sum(hours), 4),
        "minutes": round(float(actuals.get("active_minutes") or 0.0), 3),
        "skeleton_returns": sum(1 for e in history if e.get("event") == "skeleton_return"),
        "late_r": late_r,
        "judge_r": None if judged is None else judged["R"], "judge_v": None if judged is None else judged["V"],
        "skills_planner_sha256": planner_sha, "skills_thinker_sha256": thinker_sha,
    }
    row["stop_reason"] = derive_stop_reason(row)
    return [row[c] for c in TRIAL_COLUMNS]


def trial_rows(arms_dir: Path) -> list[list]:
    return [arm_row(arm, arms_dir / arm) for arm in ARMS]


def arms_from_rows(rows: list[list]) -> dict:
    return {r[0]: dict(zip(TRIAL_COLUMNS, r)) for r in rows}


def ratio(part, whole):
    return round(part / whole, 4) if whole else None


def derive_trial(rows: list[list]) -> tuple[dict, dict]:
    arms = arms_from_rows(rows)
    control, risk = arms["control"], arms["risk"]
    acts = ratio(risk["review_acts_total"], control["review_acts_total"])
    spawn = ratio(risk["thinker_spawn_h_total"], control["thinker_spawn_h_total"])
    faster = None if acts is None or spawn is None else acts <= FASTER_RATIO and spawn <= FASTER_RATIO
    judged = [v for a in (control, risk) for v in (a["judge_r"], a["judge_v"])]
    held = None if any(v is None for v in judged) else (
        risk["judge_r"] + risk["judge_v"] <= control["judge_r"] + control["judge_v"])
    completed = control["stop_reason"] == "admissible" and risk["stop_reason"] == "elaborated"
    if not completed or faster is None or held is None:
        verdict = "inconclusive"
    else:
        verdict = "pass" if faster and held else "fail"
    comparison = {"review_acts_ratio": acts, "thinker_spawn_h_ratio": spawn, "faster": faster,
                  "quality_held": held}
    return comparison, {"completed": completed, "verdict": verdict}


def structure_violations(rows: list[list], cap: int) -> list[str]:
    arms = arms_from_rows(rows)
    control, risk = arms["control"], arms["risk"]
    named = {
        "stop_reason": any(derive_stop_reason(a) != a["stop_reason"] or a["stop_reason"] not in STOP_REASONS
                           for a in arms.values()),
        "dispatch": any(a["dispatch_events"] for a in arms.values()),
        "agent_override": any(a["agent_overrides"] for a in arms.values()),
        "approve_in_control": bool(control["approve_sandbox"] or control["approve_elaboration"]),
        "approve_order": risk["approve_sandbox"] != 1,
        "bound": control["bound"] or not risk["bound"],
        "skill_hashes": control["skills_planner_sha256"] != risk["skills_planner_sha256"]
        or control["skills_thinker_sha256"] != risk["skills_thinker_sha256"],
        "cap": not isinstance(cap, int) or cap <= 0,
    }
    return [f"MISMATCH: structure {name}" for name, broken in named.items() if broken]


def assemble_trial(rows: list[list], cap: int, engine_commit: str, baseline_sha256: str | None) -> dict:
    comparison, decision = derive_trial(rows)
    return {
        "mode": "trial", "formula": TRIAL_FORMULA, "engine_commit": engine_commit,
        "baseline_sha256": baseline_sha256, "cap": cap,
        "row_columns": TRIAL_COLUMNS, "rows": rows, "rows_sha256": digest(rows),
        "arms": arms_from_rows(rows), "comparison": comparison, "decision": decision,
    }


def check_trial(doc: dict, arms_dir: Path | None) -> list[str]:
    for key in TRIAL_KEYS:
        if key not in doc:
            raise Malformed(f"missing key {key!r}")
    rows = doc["rows"]
    if doc["row_columns"] != TRIAL_COLUMNS:
        raise Malformed("row_columns differ from the script's")
    if not isinstance(rows, list) or [r[0] for r in rows if isinstance(r, list) and r] != list(ARMS) or any(
            not isinstance(r, list) or len(r) != len(TRIAL_COLUMNS) for r in rows):
        raise Malformed("rows are not one list of row_columns length per arm")
    diffs: list[str] = []
    if doc["rows_sha256"] != digest(rows):
        diffs.append("MISMATCH: rows_sha256 does not match the rows")
    try:
        want = assemble_trial(rows, doc["cap"], doc["engine_commit"], doc["baseline_sha256"])
        diffs.extend(structure_violations(rows, doc["cap"]))
    except (TypeError, KeyError, ValueError) as exc:
        raise Malformed(f"rows do not hold the declared columns: {exc}")
    diffs.extend(f"MISMATCH: {key}" for key in ("arms", "comparison", "decision") if doc[key] != want[key])
    if arms_dir is not None and rows != trial_rows(arms_dir):
        diffs.append("MISMATCH: rows differ from the arms directory")
    return diffs


def trial_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="planning-cost-report.py", description="Trial mode of the report")
    parser.add_argument("command", choices=["concerns", "judge-view", "trial"])
    parser.add_argument("arms_dir", help="directory holding <arm>/{state.json,history.jsonl,ledger.jsonl,plan.toml}")
    parser.add_argument("arm", nargs="?", choices=ARMS, help="judge-view: the arm whose plan to render")
    parser.add_argument("--out")
    parser.add_argument("--cap", type=int, default=TRIAL_CAP)
    parser.add_argument("--engine-commit")
    parser.add_argument("--baseline", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    arms_dir = Path(args.arms_dir)
    try:
        if args.command == "concerns":
            items = [{"id": c["id"], "text": c["text"]} for arm in ARMS
                     for c in arm_concerns(arm, read_jsonl(arms_dir / arm / "history.jsonl"))]
            print(json.dumps(sorted(items, key=lambda c: c["id"]), indent=1))
            return 0
        if args.command == "judge-view":
            if not args.arm:
                parser.error("judge-view needs the arm")
            sys.path.insert(0, str(SCRIPTS_DIR))
            from agentctl.gates import load_plan
            from agentctl.render import render_plan_md
            plan_doc = load_plan(str(arm_dir_plan(arms_dir / args.arm)))
            print(render_plan_md(plan_doc).rstrip())
            for stage in plan_doc.stages:
                if stage.knowledge:
                    print(f"\n- **Knowledge, stage {stage.index}:** {stage.knowledge}")
            return 0
        import subprocess
        commit = args.engine_commit or subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        baseline = Path(args.baseline)
        doc = assemble_trial(trial_rows(arms_dir), args.cap, commit,
                             file_sha256(baseline) if baseline.is_file() else None)
    except (OSError, ValueError, Malformed) as exc:
        print(f"malformed: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(doc, indent=1) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"verdict={doc['decision']['verdict']} control={doc['arms']['control']['stop_reason']} "
          f"risk={doc['arms']['risk']['stop_reason']}")
    return 0


def check(doc, snapshot: Path | None) -> list[str]:
    if not isinstance(doc, dict):
        raise Malformed("not an object")
    if doc.get("mode") == "trial":
        return check_trial(doc, snapshot)
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
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("concerns", "judge-view", "trial"):
        return trial_main(argv)
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
