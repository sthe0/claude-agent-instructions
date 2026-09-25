#!/usr/bin/env python3
"""Assert what a spawned child's own transcript actually recorded for a specific
Bash command: whether it ran, was blocked, and in what order relative to other
named commands. A thin CLI wrapper over lib/transcript_stops.py's own parser —
never a reimplementation of it, and a different concern from agentctl/cli.py's
_classify_transcript_denials (which triages a "permission-denial" stop against a
stage's grant coverage into materialization_defect/planning_miss). This script
has no notion of grant coverage at all; it answers a narrower, transcript-only
question a test can assert on directly: did THIS command, at THIS point in the
transcript, get blocked — and, for --expect-guard-block, blocked specifically by
a named PreToolUse guard.

Difficulty removed: a plan stage that claims "the guard blocked X before Y ran"
or "X was denied, not silently allowed" has no observable to check that claim
against besides hand-reading a transcript JSONL — tedious and error-prone once
a transcript has more than a handful of Bash tool_uses. This script makes that
observable a single command with a pass/fail exit code, reusable from a plan's
own verify_command or a pytest assertion.

Two independent modes:

  Assertion mode (default): resolve exactly one Bash tool_use in --transcript
  whose command contains --command-contains, optionally narrowed by --after /
  --before (each also a substring, each also required to resolve to exactly one
  match) to disambiguate a command that appears more than once. --anchor-stopped
  additionally requires --after's own match to itself be a stopped (denial-shaped)
  call — proving ordering relative to an ACTUAL stop, not just an attempt.
  --expect-blocked asserts the resolved match's stop_kind is one of
  DENIAL_KINDS (permission-denial / hook-block / user-rejected — deliberately
  excluding "failed", an ordinary non-zero exit with no denial marker at all,
  and "ran"). --expect-guard-block --guard-log PATH is the narrower claim: the
  resolved match's stop_kind is specifically "hook-block" (a PreToolUse hook's
  own refusal, distinct from a permission-rule denial — see transcript_stops.py's
  module docstring for how the two are told apart) AND PATH's own text contains
  the blocked command — proving THIS specific command appears in THIS specific
  guard's own record, not just that some hook fired somewhere. --guard-log's
  exact writer is deliberately unspecified here (a later plan stage's guard
  supplies it): this script only requires PATH to exist and contain the command
  text, so it works against whatever log format that guard eventually writes.

  --list-denied mode: print every denied Bash call (stop_kind in DENIAL_KINDS)
  instead of asserting on one. Either --transcript PATH directly, or --kind K
  --since Nd to scan every spawn-costs ledger row (~/.local/log/claude-spawn-
  costs.jsonl) for that specialization kind within the window, resolving each
  row's own transcript_path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import transcript_stops  # noqa: E402

# Deliberately excludes "failed" (an ordinary non-zero exit with no denial
# marker — see transcript_stops.py's module docstring) and "ran". A command
# "denied" is one refused before it ever executed, by any of the three
# mechanisms transcript_stops.py itself distinguishes.
DENIAL_KINDS = frozenset({"permission-denial", "hook-block", "user-rejected"})

COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"

_TRUNCATE = 100


def _abbreviate(command: str) -> str:
    return command if len(command) <= _TRUNCATE else command[: _TRUNCATE - 3] + "..."


def _matches(uses: list[transcript_stops.BashToolUse], substring: str) -> list[transcript_stops.BashToolUse]:
    return [u for u in uses if substring in u.command]


def _resolve_anchor(
    uses: list[transcript_stops.BashToolUse], substring: str, *, flag: str,
) -> tuple[transcript_stops.BashToolUse | None, str | None]:
    pool = _matches(uses, substring)
    if not pool:
        return None, f"no Bash tool_use found whose command contains {flag}={substring!r}"
    if len(pool) > 1:
        lines = ", ".join(str(m.line_no) for m in pool)
        return None, f"{len(pool)} Bash tool_use(s) match {flag}={substring!r} (lines {lines}) — ambiguous anchor"
    return pool[0], None


def resolve_target(
    uses: list[transcript_stops.BashToolUse], args: argparse.Namespace,
) -> tuple[transcript_stops.BashToolUse | None, str | None]:
    """Resolve --command-contains against the transcript, narrowed by
    --after/--before. Returns (match, None) on success or (None, error)."""
    pool = _matches(uses, args.command_contains)
    if not pool:
        return None, f"no Bash tool_use found whose command contains --command-contains={args.command_contains!r}"

    if args.after:
        anchor, err = _resolve_anchor(uses, args.after, flag="--after")
        if err:
            return None, err
        if args.anchor_stopped and anchor.stop_kind not in DENIAL_KINDS:
            return None, (
                f"--anchor-stopped: --after={args.after!r} (line {anchor.line_no}) "
                f"was not stopped (stop_kind={anchor.stop_kind!r})"
            )
        pool = [u for u in pool if u.line_no > anchor.line_no]
        if not pool:
            return None, (
                f"no --command-contains={args.command_contains!r} occurrence found "
                f"after --after={args.after!r} (line {anchor.line_no})"
            )

    if args.before:
        anchor, err = _resolve_anchor(uses, args.before, flag="--before")
        if err:
            return None, err
        pool = [u for u in pool if u.line_no < anchor.line_no]
        if not pool:
            return None, (
                f"no --command-contains={args.command_contains!r} occurrence found "
                f"before --before={args.before!r} (line {anchor.line_no})"
            )

    if len(pool) > 1:
        lines = ", ".join(str(m.line_no) for m in pool)
        return None, (
            f"{len(pool)} --command-contains={args.command_contains!r} occurrence(s) "
            f"remain (lines {lines}) — ambiguous, narrow further with --after/--before"
        )
    return pool[0], None


def check_outcome(target: transcript_stops.BashToolUse, args: argparse.Namespace) -> str | None:
    """None if the resolved `target` satisfies the requested --expect-* claim
    (or no claim was requested); otherwise the failure reason."""
    if args.expect_blocked:
        if target.stop_kind not in DENIAL_KINDS:
            return (
                f"expected --command-contains={args.command_contains!r} "
                f"(line {target.line_no}) to be blocked, but stop_kind={target.stop_kind!r}"
            )
        return None
    if args.expect_guard_block:
        if target.stop_kind != "hook-block":
            return (
                f"expected a hook-guard block for --command-contains={args.command_contains!r} "
                f"(line {target.line_no}), but stop_kind={target.stop_kind!r}"
            )
        guard_log = args.guard_log
        if not guard_log.exists():
            return f"--guard-log {guard_log} does not exist"
        text = guard_log.read_text(encoding="utf-8", errors="replace")
        if target.command not in text:
            return (
                f"--guard-log {guard_log} does not record the blocked command "
                f"(line {target.line_no}): {target.command!r} not found in its text"
            )
        return None
    return None


def _parse_since(value: str) -> dt.timedelta:
    if not value.endswith("d") or not value[:-1].isdigit():
        raise ValueError(f"--since must look like '14d' (days), got {value!r}")
    return dt.timedelta(days=int(value[:-1]))


def _iter_ledger_transcripts(kind: str, since: dt.timedelta) -> Iterator[tuple[str | None, Path]]:
    """Yield (child_session_id, transcript_path) for spawn-costs ledger rows
    matching `kind`, timestamped within `since` of now, whose transcript_path
    still resolves to a real file."""
    if not COST_LOG.exists():
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - since
    with COST_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "spawn" or row.get("kind") != kind:
                continue
            ts_raw, path_raw = row.get("ts"), row.get("transcript_path")
            if not ts_raw or not path_raw:
                continue
            try:
                ts = dt.datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts < cutoff:
                continue
            path = Path(path_raw)
            if path.exists():
                yield row.get("child_session_id"), path


def list_denied(sources: Iterable[tuple[str, list[transcript_stops.BashToolUse]]]) -> int:
    """Print one line per denied Bash call across `sources` (label, uses) pairs.
    Returns the total count printed."""
    count = 0
    for label, uses in sources:
        for u in uses:
            if u.stop_kind in DENIAL_KINDS:
                count += 1
                print(f"{label}\t{u.stop_kind}\tline={u.line_no}\t{_abbreviate(u.command)}")
    return count


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--transcript", type=Path, help="path to a harness transcript JSONL")
    p.add_argument("--command-contains", help="substring identifying the Bash tool_use to assert on")
    p.add_argument("--after", help="substring identifying an anchor Bash tool_use that must precede --command-contains")
    p.add_argument(
        "--anchor-stopped", action="store_true",
        help="require --after's own match to itself be a stopped (denied/blocked) call",
    )
    p.add_argument("--before", help="substring identifying an anchor Bash tool_use that must follow --command-contains")
    outcome = p.add_mutually_exclusive_group()
    outcome.add_argument(
        "--expect-blocked", action="store_true",
        help="assert the resolved call's stop_kind is denial-shaped (permission-denial/hook-block/user-rejected)",
    )
    outcome.add_argument(
        "--expect-guard-block", action="store_true",
        help="assert the resolved call's stop_kind is hook-block AND --guard-log records the blocked command text",
    )
    p.add_argument("--guard-log", type=Path, help="required with --expect-guard-block: a guard's own log/record file to cross-reference")
    p.add_argument("--list-denied", action="store_true", help="list mode: print every denied Bash call instead of asserting on one")
    p.add_argument("--kind", help="--list-denied only: restrict to spawn-costs ledger rows for this specialization kind")
    p.add_argument("--since", metavar="Nd", help="--list-denied --kind only: time window, e.g. 14d")
    return p


def _run_list_denied(args: argparse.Namespace) -> int:
    if args.command_contains or args.after or args.before or args.expect_blocked or args.expect_guard_block:
        print(
            "--list-denied is a separate mode: it does not combine with "
            "--command-contains/--after/--before/--expect-*", file=sys.stderr,
        )
        return 2
    if args.transcript and args.kind:
        print("--list-denied takes --transcript OR --kind (with --since), not both", file=sys.stderr)
        return 2
    if args.transcript:
        if not args.transcript.exists():
            print(f"--transcript {args.transcript} does not exist", file=sys.stderr)
            return 2
        uses = transcript_stops.parse_bash_tool_uses(str(args.transcript))
        count = list_denied([(str(args.transcript), uses)])
    elif args.kind:
        if not args.since:
            print("--list-denied --kind requires --since (e.g. --since 14d)", file=sys.stderr)
            return 2
        try:
            since = _parse_since(args.since)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        sources = []
        for child_session_id, path in _iter_ledger_transcripts(args.kind, since):
            uses = transcript_stops.parse_bash_tool_uses(str(path))
            sources.append((child_session_id or str(path), uses))
        count = list_denied(sources)
    else:
        print("--list-denied requires --transcript or --kind", file=sys.stderr)
        return 2
    print(f"{count} denied Bash call(s) listed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_denied:
        return _run_list_denied(args)

    if not args.transcript or not args.command_contains:
        print("--transcript and --command-contains are required outside --list-denied mode", file=sys.stderr)
        return 2
    if not args.transcript.exists():
        print(f"--transcript {args.transcript} does not exist", file=sys.stderr)
        return 2
    if args.expect_guard_block and not args.guard_log:
        print("--expect-guard-block requires --guard-log", file=sys.stderr)
        return 2
    if args.anchor_stopped and not args.after:
        print("--anchor-stopped requires --after", file=sys.stderr)
        return 2

    uses = transcript_stops.parse_bash_tool_uses(str(args.transcript))
    target, err = resolve_target(uses, args)
    if err:
        print(f"FAIL — {err}", file=sys.stderr)
        return 1
    outcome_err = check_outcome(target, args)
    if outcome_err:
        print(f"FAIL — {outcome_err}", file=sys.stderr)
        return 1
    print(
        f"OK — --command-contains={args.command_contains!r} resolved to line "
        f"{target.line_no} (stop_kind={target.stop_kind!r})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
