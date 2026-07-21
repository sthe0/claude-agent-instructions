#!/usr/bin/env python3
"""SessionStart hook: throttled nudge to recalibrate the spawn budget tiers.

The budget-calibration loop (memory-global/leaves/policy-effectiveness-tracking.md)
only closes if someone runs `budget-calibration.py` and, on a flag, adjusts the
config.md tier values via self-improvement. Unlike hook-policy-scorecard-due.py
(a bare reminder), this hook actually runs `budget-calibration.py --check` — a
cheap read over two jsonl ledgers — and stays SILENT unless a tier looks
miscalibrated, so it only speaks when there is something to act on.

Throttled to at most once per 7 days via a stamp file, mirroring
hook-policy-scorecard-due.py. NUDGE only: it never blocks, never mutates a
ledger, and is fail-open (any error ⇒ exit 0, no output).

Output goes to stderr (SessionStart convention here). Exit 0 always.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
STAMP = Path.home() / ".local" / "state" / "claude-budget-calibration.stamp"
THROTTLE_DAYS = 7
CHECK_TIMEOUT_S = 10


def last_nudge() -> dt.datetime | None:
    try:
        raw = STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_nudge(now: dt.datetime) -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        pass


def run_check() -> str | None:
    """Return the one-line calibration flag from `budget-calibration.py --check`,
    or None when nothing is flagged / the check cannot run (fail-open)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "budget-calibration.py"), "--check"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
        )
    except Exception:
        return None
    line = (proc.stdout or "").strip()
    return line or None


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

        flag = run_check()
        # Stamp regardless of outcome so a clean check also resets the 7-day window
        # (avoids re-running the check every session while calibration holds).
        record_nudge(now)
        if flag:
            print(
                f"💵 Budget tiers may be miscalibrated: {flag}. "
                f"Run `scripts/budget-calibration.py` and, if confirmed, self-improvement "
                f"to adjust the config.md tier values.",
                file=sys.stderr,
            )
    except Exception:
        pass  # fail-open: a nudge must never break session start
    return 0


if __name__ == "__main__":
    sys.exit(main())
