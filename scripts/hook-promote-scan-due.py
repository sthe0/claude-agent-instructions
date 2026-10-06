#!/usr/bin/env python3
"""SessionStart hook: surface promote-scan clusters that crossed the induction threshold.

Difficulty removed: record-experience.py promote-scan already clusters the experience
corpus and flags principle-induction candidates at principle-promotion-threshold — but
it only fires when someone runs it by hand. Every sibling scanner (self-diagnose,
policy-scorecard) has a SessionStart due-hook; promote-scan did not, so a flagged
cluster can sit unnoticed for weeks. This hook runs promote-scan on a 7-day cadence
and prints any flagged clusters to stderr so a live session picks each one up as a
candidate to lift into a principle/v1 leaf.

The hook itself never edits anything; it only surfaces mechanically-decidable
candidates. Strictly fail-open: a missing script, timeout, crash, or unparseable
output degrades to silence — never to a blocked or slowed session start.

Throttled to at most once per 7 days (matching the policy-scorecard cadence), because
principle-induction candidates accrue over weeks, not per-session.

Two manual modes, mirroring hook-self-diagnose-due.py:
  --dry-run    — always runs the scanner and reports; never reads or writes STAMP.
  --force-run  — runs regardless of throttle; never writes STAMP (preserves cadence).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RECORD_EXPERIENCE = SCRIPT_DIR / "record-experience.py"
STAMP = Path.home() / ".local" / "state" / "claude-promote-scan.stamp"
THROTTLE_DAYS = 7
SCAN_TIMEOUT_S = 20
MAX_PRINTED_CLUSTERS = 10
JUDGE_BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"


def last_run() -> "float | None":
    try:
        return float(STAMP.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def record_run(now_ts: float) -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(str(now_ts), encoding="utf-8")
    except OSError:
        pass


def run_scanner() -> "dict | None":
    """Run promote-scan --scope global --json cache-only and return its
    ``{"clusters": [...], "stats": {...}}`` object.

    Returns None on any failure (missing script, timeout, crash, bad JSON) — distinct
    from a clean empty ``clusters`` list, which means no experience leaves exist yet. The two
    must not share an encoding so a failed scan never silently suppresses a real candidate.
    The judge budget is 0: a session start never waits on a model, so pairs the verdict
    cache has not decided are counted as unjudged instead."""
    if not RECORD_EXPERIENCE.is_file():
        return None
    try:
        out = subprocess.run(
            [sys.executable, str(RECORD_EXPERIENCE),
             "promote-scan", "--scope", "global", "--json"],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S,
            env={**os.environ, JUDGE_BUDGET_ENV: "0"},
        )
    except Exception:
        return None
    if out.returncode != 0 or not (out.stdout or "").strip():
        return None
    try:
        scan = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(scan, dict) or not isinstance(scan.get("clusters"), list):
        return None
    return scan


def report(clusters: "list[dict]", unjudged_items: int = 0) -> None:
    if unjudged_items:
        print(
            f"promote-scan: {unjudged_items} experience leaf(s) unjudged (cache-only run) — "
            "clusters may be under-merged; run `scripts/record-experience.py promote-scan` "
            "for a judged pass.",
            file=sys.stderr,
        )
    flagged = [c for c in clusters if c.get("flagged")]
    if not flagged:
        return
    print(
        f"promote-scan: {len(flagged)} principle-induction candidate(s) at or above "
        "principle-promotion-threshold — lift each into a principle/v1 leaf via "
        "`scripts/record-experience.py promote-scan` (config.md § principle-promotion-threshold).",
        file=sys.stderr,
    )
    for cluster in flagged[:MAX_PRINTED_CLUSTERS]:
        members = ", ".join(cluster.get("members", []))
        total = cluster.get("occurrences_total", "?")
        print(f"  [{total} occurrence(s)] {members}", file=sys.stderr)
    remaining = len(flagged) - MAX_PRINTED_CLUSTERS
    if remaining > 0:
        print(
            f"  ... and {remaining} more (run scripts/record-experience.py promote-scan)",
            file=sys.stderr,
        )


def report_scan(scan: "dict | None") -> None:
    scan = scan or {"clusters": []}
    stats = scan.get("stats") or {}
    unjudged = stats.get("unjudged_items")
    report(scan["clusters"], unjudged if isinstance(unjudged, int) else 0)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report only, never reads or writes STAMP",
    )
    parser.add_argument(
        "--force-run", action="store_true",
        help="scan now regardless of throttle, without consuming the cadence",
    )
    args = parser.parse_args(argv)

    now_ts = time.time()

    if args.dry_run:
        report_scan(run_scanner())
        return 0

    if not args.force_run:
        prev = last_run()
        if prev is not None and (now_ts - prev) < THROTTLE_DAYS * 86400.0:
            return 0

    scan = run_scanner()
    if scan is not None:
        report_scan(scan)

    if not args.force_run:
        record_run(now_ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
