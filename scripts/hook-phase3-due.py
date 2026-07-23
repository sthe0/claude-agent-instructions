#!/usr/bin/env python3
"""SessionStart hook: fire the Phase-3 forcing trigger — "is collapsing
proven-deliverable CLAUDE.md prose to pointers due yet?" — the moment the
mechanical predicate in rule-salience-report.py says both warranted and safe.

Difficulty removed: the parent programme (instruction-surface-governance.toml)
defers its compression phase with no attached condition, so today only a
session happening to remember it would ever resume it. `phase3_readiness()`
already mechanizes the DECIDABLE half (pressure / data-sufficiency /
reclaimable, all named-constant-driven); this hook is what actually DELIVERS
that verdict to a live session, mirroring hook-budget-calibration-due.py and
hook-self-diagnose-due.py rather than inventing a sixth shape.

Cron is deliberately avoided, per hook-policy-scorecard-due.py's recorded
reason: a recurring cron auto-expires after 7 days, silently breaking a
cadence measured in weeks; a throttled SessionStart hook survives restarts
and never expires.

Two stamps, two different lifetimes:
  * BASELINE_STAMP — write-ONCE. rule-salience-report.py reads it
    (`read_baseline_age_days`) but never writes it (its module docstring's
    "no writes" contract) — establishing it is this hook's job. It is the
    zero point of the data-sufficiency arm's day-window; refreshing it would
    silently push Phase 3's eligibility forward forever, so once written it
    is never touched again.
  * THROTTLE_STAMP — written on every real (non-dry-run, non-force-run)
    invocation REGARDLESS of verdict, mirroring hook-budget-calibration-due.py
    ("stamp regardless of outcome so a clean check also resets the window").

The predicate itself is never reimplemented here — `run_check()` shells out to
`rule-salience-report.py --check-due`, which already exits 0 on every path
(DUE included) and prints the verdict plus all three arms' numbers. This hook
only decides whether to print that verdict on stderr and stamps the throttle.

Two manual modes, mirroring hook-self-diagnose-due.py (and, like it, NOT
draining stdin — both modes are meant to be run directly from a shell, and
blocking on `json.load(sys.stdin)` would hang a manual invocation against a
terminal):
  --dry-run    — fully read-only: never touches either stamp; always runs the
                 check and reports on DUE, for verification.
  --force-run  — evaluates now regardless of the throttle window and still
                 establishes the baseline stamp if absent (that must happen
                 the first time the hook ever runs, throttle or not), but
                 does not consume/reset the throttle window.

Fail-open: the entire body is wrapped so any exception — a missing checker
script, a subprocess timeout, an unwritable state dir, a malformed stamp —
yields a silent exit 0. A sentinel that can break or slow a session start is a
sentinel someone disables the first time it misfires.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
CHECKER = SCRIPTS_DIR / "rule-salience-report.py"

STATE_DIR = Path.home() / ".local" / "state"
# Must equal rule-salience-report.py's BASELINE_STAMP_PATH — that module reads
# this stamp but, by its own write-free contract, never writes it.
BASELINE_STAMP = STATE_DIR / "claude-phase3-baseline.stamp"
THROTTLE_STAMP = STATE_DIR / "claude-phase3-due.stamp"

# Matches the repo's other *-due SessionStart sentinels; the underlying
# inputs (surface chars, OBSERVED firing states) move on a scale of weeks, so
# a tighter window would only re-print the same verdict.
THROTTLE_DAYS = 7
# The check scans transcripts; hook-self-diagnose-due.py uses the same bound
# for a comparable scan.
CHECK_TIMEOUT_S = 15
# The verdict is read from a hook injection, where an unbounded block would
# cost more surface than the compression it proposes; matches the repo's
# digest-first log-reading cap (CLAUDE.md § Log-reading discipline).
MAX_PRINTED_LINES = 10

DUE_PREFIX = "phase3-readiness: DUE"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def last_throttle() -> "dt.datetime | None":
    try:
        raw = THROTTLE_STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_throttle(now: dt.datetime) -> None:
    try:
        THROTTLE_STAMP.parent.mkdir(parents=True, exist_ok=True)
        THROTTLE_STAMP.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        pass


def ensure_baseline(now: dt.datetime) -> None:
    """Write BASELINE_STAMP with `now` iff it does not already exist. Never
    overwrites an existing stamp — see the module docstring on why."""
    try:
        if BASELINE_STAMP.exists():
            return
        BASELINE_STAMP.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_STAMP.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        pass


def run_check() -> "str | None":
    """Run `rule-salience-report.py --check-due` and return its stdout, or
    None when the subprocess itself could not be run (fail-open). --check-due
    always exits 0, so None here means "could not check", never "NOT-DUE"."""
    try:
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--check-due"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def report(verdict_text: str) -> None:
    """Print a bounded [phase3-due] block to stderr. Called only when
    `verdict_text` is a DUE verdict."""
    lines = verdict_text.splitlines()
    print(
        "[phase3-due] Phase 3 (instruction-surface compression) is DUE — resume "
        "~/.claude-agent/plans/instruction-surface-governance.toml stages 5-8. "
        "Verdict below (OBSERVED tier>=1 prose only; NEVER-OBSERVED is a risk "
        "signal about the delivery mechanism, never a licence to compress):",
        file=sys.stderr,
    )
    for line in lines[:MAX_PRINTED_LINES]:
        print(f"  {line}", file=sys.stderr)
    remaining = len(lines) - MAX_PRINTED_LINES
    if remaining > 0:
        print(
            f"  ... ({remaining} more line(s); run "
            "`scripts/rule-salience-report.py --check-due` for the full verdict)",
            file=sys.stderr,
        )


def _maybe_report(out: "str | None") -> None:
    if out and out.splitlines()[0].startswith(DUE_PREFIX):
        report(out)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only; never touches either stamp")
    parser.add_argument(
        "--force-run", action="store_true",
        help="evaluate now regardless of the throttle window; still establishes the "
             "baseline stamp if absent, but does not consume the throttle window",
    )
    args = parser.parse_args(argv)

    try:
        now = _now()

        if args.dry_run:
            _maybe_report(run_check())
            return 0

        ensure_baseline(now)

        if not args.force_run:
            prev = last_throttle()
            if prev is not None and (now - prev).total_seconds() / 86400.0 < THROTTLE_DAYS:
                return 0

        out = run_check()

        if not args.force_run:
            record_throttle(now)

        _maybe_report(out)
    except Exception:
        pass  # fail-open: a sentinel must never break or slow a session start
    return 0


if __name__ == "__main__":
    sys.exit(main())
