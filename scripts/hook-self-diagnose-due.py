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

The worklist also lands in the durable keyed store `self_diagnose_store.py`,
which is what gives a finding closure state: the open ACTIONABLE rows re-surface
at the turn boundary via hook-turn-end-gate.py until they are fixed, acked or
snoozed, and the advisory remainder reaches ADVISORY_CHANNEL. Printing alone was
the drain this store closes — the same finding scrolled past unread every
session for four days.

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
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import self_diagnose_store as store
except Exception:  # pragma: no cover - fail-open if the store is unavailable
    store = None

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


def run_scanner() -> "list[dict]":
    """Run self-diagnose.py and return its worklist as kind/path/detail records.

    Uses the scanner's own --json mode rather than re-parsing its display lines,
    so the (kind, path) pair the store keys on arrives structured.

    Never raises — a missing script, a timeout, a crash, or a non-UTF8
    surprise all yield an empty worklist so the hook stays fail-open."""
    if not SELF_DIAGNOSE.is_file():
        return []
    try:
        out = subprocess.run(
            [sys.executable, str(SELF_DIAGNOSE), "--json"],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S,
        )
    except Exception:
        return []
    try:
        records = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [r for r in records if isinstance(r, dict) and r.get("kind")]


def _as_line(record: dict) -> str:
    return f"{record.get('kind')}: {record.get('path')} — {record.get('detail')}"


def persist(findings: "list[dict]") -> "list[dict]":
    """Merge the scan into the durable store and route the advisory remainder.

    Fail-open: a store that cannot be read or written must never cost a session
    start, so every failure degrades to "printed but not persisted"."""
    if store is None:
        return []
    try:
        rows = store.upsert_findings(findings)
        store.route_advisory(rows)
        return rows
    except Exception:
        return []


def report(findings: "list[dict]", rows: "list[dict] | None" = None) -> None:
    if not findings:
        return
    print(
        f"self-diagnose: {len(findings)} self-friction item(s) found — this is a "
        "STANDING difficulty (CLAUDE.md § When the work is stuck): work it through "
        "overcome-difficulty proactively, don't wait for the user to notice.",
        file=sys.stderr,
    )
    for record in findings[:MAX_PRINTED_LINES]:
        print(f"  - {_as_line(record)}", file=sys.stderr)
    remaining = len(findings) - MAX_PRINTED_LINES
    if remaining > 0:
        print(f"  ... and {remaining} more (run scripts/self-diagnose.py for the full list)", file=sys.stderr)
    if rows and store is not None:
        try:
            blocking = store.open_actionable(rows)
            advisory = store.advisory_open(rows)
        except Exception:
            return
        print(
            f"  ({len(blocking)} actionable will re-surface at the turn boundary until "
            f"closed; {len(advisory)} advisory in the digest — "
            "scripts/self_diagnose_store.py --list)",
            file=sys.stderr,
        )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, never touches the throttle")
    parser.add_argument("--force-run", action="store_true", help="scan now regardless of throttle, without consuming it")
    args = parser.parse_args(argv)

    now_ts = time.time()

    if args.dry_run:
        # Verification mode: report without perturbing any durable state, the
        # store included.
        report(run_scanner())
        return 0

    if not args.force_run:
        prev = last_run()
        if prev is not None and (now_ts - prev) < THROTTLE_HOURS * 3600.0:
            return 0

    findings = run_scanner()
    report(findings, persist(findings))

    if not args.force_run:
        record_run(now_ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
