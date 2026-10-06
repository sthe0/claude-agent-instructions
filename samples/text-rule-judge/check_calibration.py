#!/usr/bin/env python3
"""Acceptance check for the text-rule judge calibration.

  check_calibration.py calibration.json hook-e2e.json

Recomputes every figure from the per-run records and never trusts a summary field
(a summary that disagrees with its own runs is itself a failure). Exits nonzero on
any failed assertion, printing one line per failure.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

MIN_ITEMS = 16
MAX_LATENCY_S = 184
INCIDENT_IDS = ("incident-2026-09-17", "incident-2026-10-05")
INCIDENT_RULE = "say-13"
MAX_FLIPS = 1
LABELS = ("violation", "clean")


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_run(run) -> str | None:
    if not isinstance(run, dict):
        return "run is not an object"
    for key in ("verdict", "genuine"):
        if not _is_bool(run.get(key)):
            return f"{key} is not a JSON bool"
    if not isinstance(run.get("judged_rules"), list):
        return "judged_rules is not a list"
    if not _is_number(run.get("latency_s")):
        return "latency_s is not a number"
    for attempt in run.get("failed_attempts", []):
        if not (isinstance(attempt, dict) and _is_number(attempt.get("latency_s"))):
            return "failed attempt without numeric latency_s"
    return None


def check(calibration: dict, e2e: dict, labelled: dict[str, str] | None = None) -> list[str]:
    failures: list[str] = []
    items = calibration.get("items")
    if not isinstance(items, list):
        return ["calibration has no items list"]

    runs_per_item = calibration.get("runs_per_item")
    latencies: list[float] = []
    false_denies = 0
    flips = 0
    by_id: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("label") not in LABELS or not isinstance(item.get("runs"), list):
            failures.append(f"malformed item {item.get('id') if isinstance(item, dict) else item!r}")
            continue
        if item["id"] in by_id:
            failures.append(f"{item['id']}: duplicate item id")
        by_id[item["id"]] = item
        if len(item["runs"]) != runs_per_item or len(item["runs"]) < 2:
            failures.append(f"{item['id']}: {len(item['runs'])} runs, expected runs_per_item={runs_per_item} >= 2")
        for run in item["runs"]:
            problem = _valid_run(run)
            if problem:
                failures.append(f"{item['id']}: {problem}")
                continue
            latencies.append(run["latency_s"])
            latencies.extend(a["latency_s"] for a in run.get("failed_attempts", []))
            if item["label"] == "clean" and run["verdict"] and run["genuine"]:
                false_denies += 1
        verdicts = {run["verdict"] for run in item["runs"] if isinstance(run, dict) and _is_bool(run.get("verdict"))}
        if len(verdicts) > 1:
            flips += 1

    if labelled is not None:
        got = {i: it.get("label") for i, it in by_id.items()}
        if got != labelled:
            drift = sorted(k for k in set(got) | set(labelled) if got.get(k) != labelled.get(k))
            failures.append(f"items/labels differ from labelled.jsonl: {drift}")

    n = len(items)
    if n < MIN_ITEMS:
        failures.append(f"n={n} < {MIN_ITEMS} labelled items")
    if not any(item.get("label") == "violation" for item in by_id.values()):
        failures.append("no violation item")
    if not any(item.get("label") == "clean" for item in by_id.values()):
        failures.append("no clean item")
    if false_denies:
        failures.append(f"{false_denies} genuine YES run(s) on clean items (false denies)")
    if flips > MAX_FLIPS:
        failures.append(f"{flips} items flip between their runs (> {MAX_FLIPS}): one pass is not evidence")

    for incident_id in INCIDENT_IDS:
        item = by_id.get(incident_id)
        if item is None:
            failures.append(f"{incident_id}: missing")
            continue
        if item.get("label") != "violation":
            failures.append(f"{incident_id}: not labelled violation")
        for index, run in enumerate(item.get("runs", [])):
            if _valid_run(run):
                continue
            if not (run["verdict"] and run["genuine"] and INCIDENT_RULE in run["judged_rules"]):
                failures.append(
                    f"{incident_id} run {index + 1}: not a genuine YES naming {INCIDENT_RULE} "
                    f"(verdict={run['verdict']} genuine={run['genuine']} rules={run['judged_rules']})"
                )

    if latencies:
        max_s = max(latencies)
        if max_s > MAX_LATENCY_S:
            failures.append(f"max latency {max_s} s > {MAX_LATENCY_S} s")
        for key, recomputed in (("n", n), ("max_s", round(max_s, 2)), ("flips", flips),
                                ("median_s", round(statistics.median(latencies), 2)),
                                ("p90_s", round(sorted(latencies)[math.ceil(0.9 * len(latencies)) - 1], 2))):
            if calibration.get(key) != recomputed:
                failures.append(f"summary {key}={calibration.get(key)!r} != recomputed {recomputed!r}")
    else:
        failures.append("no latencies recorded")

    prefilter = calibration.get("prefilter")
    if not (isinstance(prefilter, dict) and _is_number(prefilter.get("publications")) and prefilter["publications"] > 0
            and _is_number(prefilter.get("fired")) and _is_number(prefilter.get("rate"))):
        failures.append("prefilter trigger rate on historical publications not recorded")

    positive, negative = e2e.get("positive"), e2e.get("negative")
    if not (isinstance(positive, dict) and positive.get("decision") == "deny"
            and INCIDENT_RULE in str(positive.get("reason", ""))):
        failures.append(f"hook e2e positive: expected deny naming {INCIDENT_RULE}")
    if not (isinstance(negative, dict) and negative.get("decision") == "allow"):
        failures.append("hook e2e negative: expected allow")
    if isinstance(positive, dict) and "TEXT_RULE_JUDGE_DENY" not in (positive.get("advisories") or []):
        failures.append("hook e2e positive: no TEXT_RULE_JUDGE_DENY advisory (deny not from a judged YES with a span)")
    if isinstance(negative, dict):
        bad = {"TEXT_RULE_JUDGE_FAIL_OPEN", "TEXT_RULE_JUDGE_BUDGET_EXHAUSTED"} & set(negative.get("advisories") or [])
        if bad:
            failures.append(f"hook e2e negative: allow came from a fail-open path {sorted(bad)}")
    for name, side in (("positive", positive), ("negative", negative)):
        if isinstance(side, dict) and side.get("returncode") != 0:
            failures.append(f"hook e2e {name}: returncode {side.get('returncode')!r}")
        if isinstance(side, dict) and _is_number(side.get("latency_s")) and side["latency_s"] > 190:
            failures.append(f"hook e2e {name}: latency {side['latency_s']} s over the 190 s hook budget")
    return failures


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        calibration = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        e2e = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read inputs: {exc}")
        return 1
    labelled_path = Path(__file__).with_name("labelled.jsonl")
    try:
        rows = [json.loads(line) for line in labelled_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        labelled = {row["id"]: row["label"] for row in rows}
    except (OSError, ValueError, KeyError) as exc:
        print(f"FAIL: cannot read labelled.jsonl: {exc}")
        return 1
    failures = check(calibration, e2e, labelled)
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print("OK: calibration holds")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
