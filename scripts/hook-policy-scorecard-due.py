#!/usr/bin/env python3
"""SessionStart hook: weekly nudge to run the policy scorecard.

The policy effectiveness/efficiency loop (memory-global/leaves/
policy-effectiveness-tracking.md) only closes if someone actually runs
`policy-scorecard.py` and acts on its Flags. Without a signal that's easy to
forget — so this hook emits a one-line reminder, throttled to at most once per
7 days via a stamp file, mirroring hook-context-growth-reminder.py's
per-band throttle.

It is a NUDGE only: it does NOT run the scan (that would slow every session
start and burn tokens unprompted). Cron is deliberately avoided — recurring
crons auto-expire after 7 days, which would silently break a weekly cadence; a
throttled SessionStart hook survives restarts and never expires.

Output goes to stderr (SessionStart convention here). Exit 0 always.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

STAMP = Path.home() / ".local" / "state" / "claude-policy-scorecard.stamp"
THROTTLE_DAYS = 7


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


def main() -> int:
    try:
        json.load(sys.stdin)  # drain payload; we don't need any field
    except Exception:
        pass

    now = dt.datetime.now()
    prev = last_nudge()
    if prev is not None:
        days = (now - prev).total_seconds() / 86400
        if days < THROTTLE_DAYS:
            return 0  # nudged within the throttle window
        ago = f"last reminded {days:.0f}d ago"
    else:
        ago = "first reminder"

    record_nudge(now)
    print(
        f"📊 Policy scorecard due — run `scripts/policy-scorecard.py --days 7` "
        f"to review model/sub-agent policy (efficiency + effectiveness), then "
        f"rate a few flagged sessions. ({ago})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
