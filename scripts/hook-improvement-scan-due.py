#!/usr/bin/env python3
"""SessionStart hook: nudge toward a due improvement scan — never runs one.

Difficulty removed: `backlog-triage-practice.md` gap #2 (no standing
cross-source priority digest) is now closed by the improvement-scan module's
two producers, but running them is a full session task — a live Core+Org
backlog pull, a live telemetry read, model-supplied classification, a rendered
report — the same shape as every other periodic scanner in this fleet
(self-diagnose, policy-scorecard, promote-scan), each of which already has a
SessionStart due-hook that nudges a live session to run it. Improvement-scan
had none, so its own already-stored findings could sit unread indefinitely.

THIS HOOK NEVER RUNS THE SCAN. Running the two producers, supplying
classifications against their closed vocabularies, and rendering the report is
model perception work that belongs to a live session invoking the
`improvement-scan` skill (skills/improvement-scan/SKILL.md) end to end — not to
a SessionStart hook. This hook only reads the durable findings store
(self_diagnose_store.py) for rows already stamped under the improvement-scan
source and, if the cadence is due, prints their count plus an instruction to
invoke that skill.

Strictly fail-open, mirroring every other due-hook: a missing or unreadable
store, or a missing store module, never blocks or slows session start — the
worst case is silence.

Two manual modes, mirroring hook-self-diagnose-due.py:
  --dry-run    — never checks or writes STAMP; always reports.
  --force-run  — never checks the throttle window and never writes STAMP;
                 reports regardless of when it last ran, without perturbing the
                 SessionStart cadence.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import self_diagnose_store as store
except Exception:  # pragma: no cover - fail-open if the store is unavailable
    store = None

STAMP = Path.home() / ".local" / "state" / "claude-improvement-scan.stamp"
# 7 days: matches every other weekly-scope due-hook (policy-scorecard,
# budget-calibration, instruction-grooming, promote-scan, phase3) — the
# improvement scan's findings accrue across sessions rather than per-session,
# and the scan itself does live external reads (a backlog channel, a
# published board artifact) too heavy to prompt for on every session start.
THROTTLE_DAYS = 7.0


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


def open_findings() -> "list[dict] | None":
    """Open, improvement-scan-sourced rows already in the durable store.

    Returns `None` only when the store module itself could not be imported —
    distinct from a clean empty list (a readable store with no open
    improvement-scan rows) — so a missing dependency is never reported as
    "nothing to nudge about". Never raises: any store read/parse failure
    degrades to `None`, the same fail-open contract `load_rows` itself already
    carries for a missing or corrupt store file."""
    if store is None:
        return None
    try:
        rows = store.load_rows()
        sourced = [r for r in rows if r.get("source") == store.SOURCE_IMPROVEMENT_SCAN]
        return store.advisory_open(sourced)
    except Exception:
        return None


def report(findings: "list[dict]") -> None:
    if not findings:
        return
    print(
        f"improvement-scan: {len(findings)} open finding(s) from a prior scan — "
        "invoke the `improvement-scan` skill to review them, re-run the "
        "producers, and get a fresh ranked report (this hook only counts "
        "stored findings; it never runs the scan itself).",
        file=sys.stderr,
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, never touches the throttle")
    parser.add_argument("--force-run", action="store_true", help="report now regardless of throttle, without consuming it")
    args = parser.parse_args(argv)

    now_ts = time.time()

    if args.dry_run:
        report(open_findings() or [])
        return 0

    if not args.force_run:
        prev = last_run()
        if prev is not None and (now_ts - prev) < THROTTLE_DAYS * 86400.0:
            return 0

    findings = open_findings()
    if findings is not None:
        report(findings)

    if not args.force_run:
        record_run(now_ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
