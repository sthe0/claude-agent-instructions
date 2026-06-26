#!/usr/bin/env python3
"""Submit a Core difficulty record to the configured channel.

Non-author machines use this to file difficulties they cannot fix directly
(they lack Core push rights). The author-side core-difficulty-digest.py then
clusters and flags accumulated reports.

Usage::
    python3 file-difficulty.py --target CLAUDE.md --ground 'gate wording ambiguous' --severity high
    python3 file-difficulty.py ... --dry-run   # prints the record; no submission
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import difficulty_channel as dc  # noqa: E402
import difficulty_channel.adapters  # noqa: E402,F401
from difficulty_channel import authority  # noqa: E402


def _now_iso() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _build_record(args: argparse.Namespace, ts: str | None = None) -> dc.DifficultyRecord:
    return dc.DifficultyRecord(
        ts=ts or _now_iso(),
        layer=args.layer,
        target=args.target,
        functional_ground=args.ground,
        severity=dc.Severity.parse(args.severity),
        reporter=args.reporter or os.environ.get("USER", "unknown"),
        evidence=args.evidence or "",
    )


def _print_record(record: dc.DifficultyRecord) -> None:
    print("DifficultyRecord:")
    print(f"  ts:                {record.ts}")
    print(f"  layer:             {record.layer}")
    print(f"  target:            {record.target}")
    print(f"  functional_ground: {record.functional_ground!r}")
    print(f"  severity:          {record.severity.value}")
    print(f"  reporter:          {record.reporter}")
    if record.evidence:
        print(f"  evidence:          {record.evidence!r}")


def main(argv: list[str] | None = None, _ts: str | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", required=True,
                   help="file/rule/path the difficulty is about")
    p.add_argument("--ground", "--functional-ground", dest="ground", required=True,
                   help="desired-vs-actual divergence (the cluster key)")
    p.add_argument("--severity", default="medium",
                   choices=["low", "medium", "high", "critical"],
                   help="difficulty severity (default: medium)")
    p.add_argument("--layer", default="core",
                   help="which layer the difficulty is against (default: core)")
    p.add_argument("--evidence", default="",
                   help="supporting quote, log line, or link")
    p.add_argument("--reporter", default="",
                   help="who/what is filing (default: $USER)")
    p.add_argument("--channel", default=None,
                   help="channel override; default: from agent-identity.local")
    p.add_argument("--dry-run", action="store_true",
                   help="print the record without submitting")
    args = p.parse_args(argv)

    try:
        record = _build_record(args, ts=_ts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        _print_record(record)
        return 0

    channel = args.channel or authority.read_configured_channel()
    try:
        handle = authority.file_core_difficulty(record, channel=channel)
    except Exception as exc:
        print(f"error submitting to channel {channel!r}: {exc}", file=sys.stderr)
        return 1

    print(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
