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
from difficulty_channel.adapters import (  # noqa: E402
    AdapterPluginBroken,
    BUILTIN_NAMES,
    load_adapter,
)
from difficulty_channel.adapters.github import DIFFICULTY_LABEL as _GH_DIFFICULTY_LABEL, BACKLOG_LABEL as _GH_BACKLOG_LABEL  # noqa: E402
from difficulty_channel.project_queue import resolve_project_queue  # noqa: E402
from lib import config_root  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent


def _fix_first_guard_applies(args: argparse.Namespace, project_q: str | None, authority_mod) -> bool:
    """True when a core-tier filing headed for org-wide queues is a fix-first deferral.

    Needs no adapter — its five inputs (args.layer, project_q, args.queue,
    args.force_report, authority_mod.is_author()) are all available whether or not
    ``load_adapter`` succeeded, which is what lets it be evaluated on the
    plugin-broken path too.
    """
    return (
        args.layer == "core"
        and project_q is None
        and not args.queue
        and not args.force_report
        and authority_mod.is_author()
    )


def _print_fix_first_refusal() -> None:
    print(
        "error: author machine: propose the fix directly (fix-first); "
        "backlog -> --channel github --stream backlog "
        "(or name a queue explicitly with --queue)",
        file=sys.stderr,
    )


def _now_iso() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _build_record(args: argparse.Namespace, ts: str | None = None) -> dc.DifficultyRecord:
    if args.cost is not None:
        cost_estimate = args.cost
    elif args.cost_not_estimable is not None:
        cost_estimate = f"not estimable: {args.cost_not_estimable}"
    else:
        cost_estimate = ""
    return dc.DifficultyRecord(
        ts=ts or _now_iso(),
        layer=args.layer,
        target=args.target,
        functional_ground=args.ground,
        severity=dc.Severity.parse(args.severity),
        reporter=args.reporter or os.environ.get("USER", "unknown"),
        evidence=args.evidence or "",
        cost_estimate=cost_estimate,
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
    if record.cost_estimate:
        print(f"  cost_estimate:     {record.cost_estimate!r}")


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
    p.add_argument("--cost", default=None,
                   help="what the problem costs per occurrence or per week, in whatever unit "
                        "fits: '~8k tokens per session', '$3/week', '2 replans per ticket' "
                        "(mutually exclusive with --cost-not-estimable; exactly one is required "
                        "to actually file)")
    p.add_argument("--cost-not-estimable", default=None, metavar="REASON",
                   help="explicit reason no cost estimate is possible (mutually exclusive with "
                        "--cost; exactly one is required to actually file)")
    p.add_argument("--reporter", default="",
                   help="who/what is filing (default: $USER)")
    p.add_argument("--channel", default=None,
                   help="channel override; default: from agent-identity.local")
    p.add_argument("--queue", default=None,
                   help="explicit queue override for a queue-routed channel (e.g. PROJ)")
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

    # Resolve effective routing destination before submit or print. The built-in channels
    # route by label on one public repo; every other channel is a machine-local plugin
    # adapter (ADR-0001 B1) that routes by queue and names its own queues.
    if channel_name in BUILTIN_NAMES:
        if args.queue:
            project_q = None
        else:
            project_q = resolve_project_queue(Path(args.target).resolve())
        # Subject-awareness guard (mirrors the queue branch's project_q resolution):
        # project queues exist only on a queue-routed channel and the built-in Core repo
        # is public, so a project-scoped difficulty has no honest destination here —
        # refuse rather than silently dumping it into the public Core repo. Fires on
        # --dry-run too (the preview must show the refusal, not fake a routing). --queue
        # is the explicit override that lifts the refusal (subject already user-decided).
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
        submit_kwargs: dict = {"stream": args.stream}
        routing_lines = [f"label: {resolved_label}"]
    else:
        try:
            adapter = load_adapter(channel_name)
        except FileNotFoundError as exc:
            if not dc.is_registered(channel_name):
                print(f"error: {exc}", file=sys.stderr)
                return 1
            # A channel registered in-process (a test double, an embedded channel) has no
            # plugin file and names no queues: submit with no routing hints.
            adapter = None
        except AdapterPluginBroken as exc:
            if dc.is_registered(channel_name):
                # Mirrors the FileNotFoundError branch above: a broken plugin file says
                # nothing about a channel that was registered without one, so filing
                # proceeds — but a real diagnostic on the way here should not be
                # silently swallowed.
                print(
                    f"warning: plugin failed to load: {exc}; channel registered "
                    "in-process, filing anyway",
                    file=sys.stderr,
                )
                adapter = None
            else:
                project_q = (
                    None if args.queue
                    else resolve_project_queue(Path(args.target).resolve())
                )
                if _fix_first_guard_applies(args, project_q, authority):
                    _print_fix_first_refusal()
                    return 2
                print(f"error: {exc}", file=sys.stderr)
                return 1
        if adapter is None:
            submit_kwargs = {}
            routing_lines = []
        else:
            if args.queue:
                resolved_queue = args.queue
                project_q = None
            else:
                project_q = resolve_project_queue(Path(args.target).resolve())
                resolved_queue = project_q or (
                    adapter.BACKLOG_QUEUE if args.stream == "backlog" else adapter.QUEUE
                )
            # Fix-first guard (policy.md § Author machine: fix-first, backlog-second):
            # a core-tier filing (no explicit --queue, no project-queue resolution)
            # headed for the channel's org-wide queues from a machine that can edit
            # Core directly is a deferral-by-default — refuse with the hint. Fires on
            # --dry-run too (the preview must show the refusal, not fake a routing).
            if _fix_first_guard_applies(args, project_q, authority):
                _print_fix_first_refusal()
                return 2
            submit_kwargs = {"queue": resolved_queue}
            routing_lines = [f"queue: {resolved_queue}"]

    if args.dry_run:
        _print_record(record)
        print(f"channel: {channel_name}")
        print(f"stream: {args.stream}")
        for line in routing_lines:
            print(line)
        return 0

    if (args.cost is not None) == (args.cost_not_estimable is not None):
        got = "both" if args.cost is not None else "neither"
        print(
            "error: exactly one of --cost or --cost-not-estimable is required to file "
            "(so a fixable loss is never left unmeasured, and a genuinely non-estimable one "
            f"is never silently skipped) — got {got}",
            file=sys.stderr,
        )
        return 2

    if authority.is_author() and not args.force_report:
        print(
            "error: this machine has Core push rights — edit Core directly via the "
            "planner -> approval -> developer spine instead of filing a report "
            "(use --force-report to file anyway)",
            file=sys.stderr,
        )
        return 2

    # Blocking gate: a difficulty record is about to leave this machine for a
    # PUBLIC channel (the report stream lands in the Core repo's issue
    # tracker) — no org-internal term may ride along in ANY field the adapter
    # publishes, hence record.scan_text() rather than a hand-picked subset:
    # the adapter body also carries layer, reporter and ts. Fails closed on a
    # hit; fails OPEN (files anyway, flagged UNCHECKED rather than silently
    # passed) when no ruleset is installed, mirroring check-org-neutral.py's
    # missing-config behavior.
    try:
        term_rulesets = tr.discover_rulesets(
            agent_home=config_root.agent_home(),
            project_dir=REPO_ROOT,
            guarded_repo_root=REPO_ROOT,
        )
    except tr.RulesetError as exc:
        print(f"error loading term ruleset: {exc}", file=sys.stderr)
        return 2

    if not term_rulesets:
        print("UNCHECKED: no term ruleset installed")
    else:
        hits = tr.scan(record.scan_text(), term_rulesets)
        if hits:
            print(
                "error: org-internal term(s) found in the difficulty record body "
                "(do not file to a public channel):",
                file=sys.stderr,
            )
            for h in hits:
                print(f"  {h.format()}", file=sys.stderr)
            return 1

    try:
        handle = authority.file_core_difficulty(record, channel=channel_name, **submit_kwargs)
    except Exception as exc:
        print(f"error submitting to channel {channel_name!r}: {exc}", file=sys.stderr)
        return 1

    print(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
