#!/usr/bin/env python3
"""SessionStart hook: periodic nudge to read the σ build-trigger sentinel.

The σ (principle-revision) operator is deferred behind a pre-registered build-trigger
(docs/sigma-build-trigger.md, ADR-0002). The trigger only stays observable if someone
periodically runs `sigma-sentinel.py` and checks whether condition (A) has fired.
That is a slow, easy-to-forget cadence — so this hook emits a one-line reminder,
throttled to at most once per 30 days via a stamp file (the same throttle shape as
hook-policy-scorecard-due.py, longer period because the σ-sentinel moves slowly).

It is a NUDGE only: it does NOT run the digest (that would read the corpus on every
session start and burn tokens unprompted). Cron is deliberately avoided — recurring
crons auto-expire, silently breaking the cadence; a throttled SessionStart hook
survives restarts and never expires.

Output goes to stderr (SessionStart convention here). Exit 0 always.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

STAMP = Path.home() / ".local" / "state" / "claude-sigma-sentinel.stamp"
THROTTLE_DAYS = 30


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
        ago = f"last checked {days:.0f}d ago"
    else:
        ago = "first reminder"

    record_nudge(now)
    print(
        f"🛡  σ build-trigger sentinel due — run `scripts/sigma-sentinel.py` "
        f"to check whether condition (A) (re-refutation of a promoted principle) has "
        f"fired. Read-only; flags only. See docs/sigma-build-trigger.md. ({ago})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
