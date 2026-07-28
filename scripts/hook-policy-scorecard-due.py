#!/usr/bin/env python3
"""SessionStart hook: refresh the policy-scorecard ledger in the background.

The policy effectiveness/efficiency loop (memory-global/leaves/
policy-effectiveness-tracking.md) only closes if the ledger
(~/.local/log/claude-policy-ledger.jsonl) stays current and someone reviews
`policy-scorecard.py`'s Flags. This hook used to print a reminder to run the
upsert by hand; that reminder fired on schedule and the ledger still went 25
days without a new row (2026-06-28 -> 2026-07-23), because "run this command"
is not a reliable trigger. So this hook now performs the upsert itself.

The original abstention rested on two claims, both re-checked here and one
of them false: "that would slow every session start and burn tokens
unprompted". Verified by reading policy-scorecard.py: it makes ZERO model
calls — its only subprocess calls are to `git` (repo-history lookups for the
instructions-commit-range rendering). The token-burn half of the premise was
simply wrong. The session-start-latency half is real for a *synchronous*
call, so it is removed by detaching instead: the upsert runs via
proc_tree.launch_supervised (start_new_session=True) and this hook never
waits on it, so a slow or hung scan cannot delay the session.

Throttled to at most once per 7 days via a stamp file, mirroring
hook-context-growth-reminder.py's per-band throttle. Cron is deliberately
avoided — recurring crons auto-expire after 7 days, which would silently
break a weekly cadence; a throttled SessionStart hook survives restarts and
never expires.

Two env overrides exist purely for testability (and are what let this hook's
own end-to-end proof run without mutating live state):
  CLAUDE_POLICY_LEDGER          passed through to policy-scorecard.py as
                                 --ledger, redirecting the upsert target.
  CLAUDE_POLICY_SCORECARD_STAMP redirects the throttle stamp file.
Both default to the real paths, so live behaviour is unchanged when unset.

Output goes to stderr (SessionStart convention here). Fail-open: a launch
failure is swallowed and the hook still exits 0 — a broken cadence nudge
must never be worse than no nudge at all.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proc_tree import launch_supervised

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_SCORECARD = SCRIPT_DIR / "policy-scorecard.py"
STAMP = Path.home() / ".local" / "state" / "claude-policy-scorecard.stamp"
THROTTLE_DAYS = 7
SCORECARD_WINDOW_DAYS = 7


def _stamp_path() -> Path:
    override = os.environ.get("CLAUDE_POLICY_SCORECARD_STAMP")
    return Path(override) if override else STAMP


def last_nudge(stamp: Path) -> dt.datetime | None:
    try:
        raw = stamp.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_nudge(now: dt.datetime, stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        pass


def ledger_upsert_cmd() -> list[str]:
    """Command for the background `policy-scorecard.py --ledger-only` upsert.
    CLAUDE_POLICY_LEDGER, when set, is passed through as --ledger."""
    cmd = [sys.executable, str(POLICY_SCORECARD), "--ledger-only",
           "--days", str(SCORECARD_WINDOW_DAYS)]
    ledger_override = os.environ.get("CLAUDE_POLICY_LEDGER")
    if ledger_override:
        cmd += ["--ledger", ledger_override]
    return cmd


def default_launch(cmd: list[str]) -> None:
    launch_supervised(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)


def main(argv: list[str] | None = None, launch=default_launch) -> int:
    try:
        json.load(sys.stdin)  # drain payload; we don't need any field
    except Exception:
        pass

    stamp = _stamp_path()
    now = dt.datetime.now()
    prev = last_nudge(stamp)
    if prev is not None:
        days = (now - prev).total_seconds() / 86400
        if days < THROTTLE_DAYS:
            return 0  # refreshed within the throttle window
        ago = f"last refreshed {days:.0f}d ago"
    else:
        ago = "first refresh"

    try:
        launch(ledger_upsert_cmd())
    except Exception:
        pass  # fail-open: a launch failure must never break session start

    record_nudge(now, stamp)
    print(
        f"📊 policy-scorecard ledger refreshed in the background — review with "
        f"`scripts/policy-scorecard.py --days 7` (efficiency + effectiveness), "
        f"then rate a few flagged sessions. ({ago})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
