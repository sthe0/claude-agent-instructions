#!/usr/bin/env python3
"""SessionStart hook: run the self-friction scanner, surface its worklist.

Difficulty removed: proactive self-diagnosis (CLAUDE.md § When the work is
stuck; see memory-global/leaves/principles/reflexive-exit-is-base-activity-
figure.md) is a STANDING OBLIGATION, not a wait-for-a-user-complaint posture
— but its decidable half (which self-friction signals count: an oversized
memory index, a dangling memory pointer, an instruction file near its
ceiling) used to live only as forgettable prose ("notice when..."). This
hook mechanizes the trigger: it runs `self-diagnose.py` (subprocess,
read-only) and prints its worklist so a live session picks each item up as
an ordinary difficulty — declare -> investigate -> critique -> normalize via
overcome-difficulty, with any resulting edit authored through
self-improvement. The hook itself never diagnoses or edits anything; it only
surfaces the mechanically-decidable worklist.

Self-throttled via STAMP and strictly fail-open, mirroring
hook-orphan-worktree-sweep.py / hook-policy-scorecard-due.py: a scanner
timeout, crash, or missing script never blocks or slows session start — the
worst case is silence.

Two manual modes, mirroring hook-orphan-worktree-sweep.py:
  --dry-run    — never checks or writes STAMP; always runs the scanner and
                 reports, for verification.
  --force-run  — never checks the throttle window and never writes STAMP;
                 runs the scanner regardless of when it last ran, without
                 perturbing the SessionStart cadence.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SELF_DIAGNOSE = SCRIPT_DIR / "self-diagnose.py"
STAMP = Path.home() / ".local" / "state" / "claude-self-diagnose.stamp"
THROTTLE_HOURS = 24.0
SCAN_TIMEOUT_S = 15
MAX_PRINTED_LINES = 10


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


def run_scanner() -> "list[str]":
    """Run self-diagnose.py and return its worklist lines.

    Never raises — a missing script, a timeout, a crash, or a non-UTF8
    surprise all yield an empty worklist so the hook stays fail-open."""
    if not SELF_DIAGNOSE.is_file():
        return []
    try:
        out = subprocess.run(
            [sys.executable, str(SELF_DIAGNOSE)],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S,
        )
    except Exception:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def report(findings: "list[str]") -> None:
    if not findings:
        return
    print(
        f"self-diagnose: {len(findings)} self-friction item(s) found — this is a "
        "STANDING difficulty (CLAUDE.md § When the work is stuck): work it through "
        "overcome-difficulty proactively, don't wait for the user to notice.",
        file=sys.stderr,
    )
    for line in findings[:MAX_PRINTED_LINES]:
        print(f"  - {line}", file=sys.stderr)
    remaining = len(findings) - MAX_PRINTED_LINES
    if remaining > 0:
        print(f"  ... and {remaining} more (run scripts/self-diagnose.py for the full list)", file=sys.stderr)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, never touches the throttle")
    parser.add_argument("--force-run", action="store_true", help="scan now regardless of throttle, without consuming it")
    args = parser.parse_args(argv)

    now_ts = time.time()

    if args.dry_run:
        report(run_scanner())
        return 0

    if not args.force_run:
        prev = last_run()
        if prev is not None and (now_ts - prev) < THROTTLE_HOURS * 3600.0:
            return 0

    report(run_scanner())

    if not args.force_run:
        record_run(now_ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
