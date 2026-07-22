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
from difficulty_channel.adapters.startrek import QUEUE as _ST_QUEUE, BACKLOG_QUEUE as _ST_BACKLOG_QUEUE  # noqa: E402
from difficulty_channel.adapters.github import DIFFICULTY_LABEL as _GH_DIFFICULTY_LABEL, BACKLOG_LABEL as _GH_BACKLOG_LABEL  # noqa: E402
from difficulty_channel.project_queue import resolve_project_queue  # noqa: E402


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
    p.add_argument("--queue", default=None,
                   help="explicit startrek queue override (e.g. DEEPAGENT)")
    p.add_argument("--stream", default="report", choices=["report", "backlog"],
                   help="flow selector: report (default) or backlog")
    p.add_argument("--dry-run", action="store_true",
                   help="print the record and resolved routing without submitting")
    p.add_argument("--force-report", action="store_true",
                   help="file via the report channel even though this machine has Core push "
                        "rights (deliberate override, e.g. filing on behalf of another org)")
    args = p.parse_args(argv)

    try:
        record = _build_record(args, ts=_ts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    channel_name = args.channel or authority.read_configured_channel()

    # Resolve effective routing destination before submit or print.
    if channel_name == "startrek":
        if args.queue:
            resolved_queue = args.queue
            project_q = None
        else:
            project_q = resolve_project_queue(Path(args.target).resolve())
            resolved_queue = project_q or (_ST_BACKLOG_QUEUE if args.stream == "backlog" else _ST_QUEUE)
        # Fix-first guard (policy.md § Author machine: fix-first, backlog-second):
        # a core-tier filing (no explicit --queue, no project-queue resolution)
        # headed for the org-wide startrek queues from a machine that can edit
        # Core directly is a deferral-by-default — refuse with the hint. Fires on
        # --dry-run too (the preview must show the refusal, not fake a routing).
        if (args.layer == "core" and project_q is None and not args.queue
                and not args.force_report and authority.is_author()):
            print(
                "error: author machine: propose the fix directly (fix-first); "
                "backlog -> --channel github --stream backlog "
                "(or name a queue explicitly with --queue)",
                file=sys.stderr,
            )
            return 2
        submit_kwargs: dict = {"queue": resolved_queue}
        routing_lines = [f"queue: {resolved_queue}"]
    elif channel_name in ("github", "external"):
        if args.queue:
            project_q = None
        else:
            project_q = resolve_project_queue(Path(args.target).resolve())
        # Subject-awareness guard (mirrors the startrek branch's project_q resolution):
        # project queues are Startrek-only and the github Core repo is public, so a
        # project-scoped difficulty has no honest destination on this channel — refuse
        # rather than silently dumping it into the public Core repo. Fires on --dry-run
        # too (the preview must show the refusal, not fake a routing). --queue is the
        # explicit override that lifts the refusal (subject already user-decided).
        if project_q is not None:
            print(
                "error: this is a project-scoped difficulty (target resolves to project "
                f"queue {project_q}) and the {channel_name} channel cannot deliver it to a "
                "project queue; file it against the project's own tracker, or if it is "
                "genuinely a Core difficulty, target a Core file (or pass --queue to file "
                "it explicitly)",
                file=sys.stderr,
            )
            return 2
        resolved_label = _GH_BACKLOG_LABEL if args.stream == "backlog" else _GH_DIFFICULTY_LABEL
        submit_kwargs = {"stream": args.stream}
        routing_lines = [f"label: {resolved_label}"]
    else:
        submit_kwargs = {}
        routing_lines = []

    if args.dry_run:
        _print_record(record)
        print(f"channel: {channel_name}")
        print(f"stream: {args.stream}")
        for line in routing_lines:
            print(line)
        return 0

    if authority.is_author() and not args.force_report:
        print(
            "error: this machine has Core push rights — edit Core directly via the "
            "planner -> approval -> developer spine instead of filing a report "
            "(use --force-report to file anyway)",
            file=sys.stderr,
        )
        return 2

    try:
        handle = authority.file_core_difficulty(record, channel=channel_name, **submit_kwargs)
    except Exception as exc:
        print(f"error submitting to channel {channel_name!r}: {exc}", file=sys.stderr)
        return 1

    print(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
