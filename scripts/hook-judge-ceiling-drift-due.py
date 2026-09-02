#!/usr/bin/env python3
"""SessionStart hook: throttled nudge when a judge's live latency has drifted
up to meet its declared ceiling.

Runs `judge-usage-report.py --check-drift` (DEFAULT mode only — never
--strict, which would turn advisory signal into a session-start gate) and
speaks only when it exits non-zero, i.e. at least one (hook, judge) pair's
median already sits at or above its declared ceiling. Mirrors
hook-budget-calibration-due.py's shape: a throttled stamp file, a bounded
subprocess, fail-open, exit 0 always.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
STAMP = Path.home() / ".local" / "state" / "claude-judge-ceiling-drift.stamp"
THROTTLE_DAYS = 7
CHECK_TIMEOUT_S = 15


def last_nudge() -> "dt.datetime | None":
    try:
        raw = STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_nudge(now: "dt.datetime") -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        pass


def run_check() -> "str | None":
    """Return the drift report's output when it FAILs, or None when it passes
    or cannot run at all (fail-open — a broken check must never speak)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "judge-usage-report.py"), "--check-drift"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode == 0:
        return None
    return (proc.stdout or "").strip() or None


def main() -> int:
    try:
        json.load(sys.stdin)  # drain payload; no field needed
    except Exception:
        pass

    try:
        now = dt.datetime.now()
        prev = last_nudge()
        if prev is not None:
            days = (now - prev).total_seconds() / 86400
            if days < THROTTLE_DAYS:
                return 0  # nudged within the throttle window

        report = run_check()
        # Stamp regardless of outcome so a clean check also resets the 7-day
        # window (avoids re-running the check every session while it holds).
        record_nudge(now)
        if report:
            print(
                "\N{WARNING SIGN} A judge's live latency has drifted up to meet its "
                "declared ceiling:\n" + report + "\nRe-sample and re-derive the row "
                "(samples/judge-latency), then re-raise the ceiling.",
                file=sys.stderr,
            )
    except Exception:
        pass  # fail-open: a nudge must never break session start
    return 0


if __name__ == "__main__":
    sys.exit(main())
