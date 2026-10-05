#!/usr/bin/env python3
"""Submit a Core difficulty record to the configured channel.

Non-author machines use this to file difficulties they cannot fix directly
(they lack Core push rights). The author-side core-difficulty-digest.py then
clusters and flags accumulated reports.

Usage::
    python3 file-difficulty.py --target CLAUDE.md --ground 'gate wording ambiguous' --severity high
    python3 file-difficulty.py ... --dry-run   # prints the record; no submission

Before filing, the channel's open records are checked for the same difficulty (lexical overlap
nominates, a model judge decides). On a judged match nothing new is filed: the evidence is
posted as a comment on the matched record and the matched ref is the last stdout line.
Exit codes: 0 filed or commented, 1 error, 2 refused, 3 matched with --no-comment-on-match,
4 --comment-on-issue found no match on that issue.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
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
from difficulty_channel.adapters.github import record_to_fields as _gh_record_to_fields  # noqa: E402
from lib import config_root  # noqa: E402
from lib import semantic_join  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent

EXIT_MATCH_REFUSED = 3
EXIT_COMMENT_ON_ISSUE_REFUSED = 4
CANDIDATE_BODY_CHARS = 2000


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


class _Dedup:
    def __init__(self, outcome, listed=0, nominated=0, stats=None, matched=None, channel=None):
        self.outcome = outcome
        self.listed = listed
        self.nominated = nominated
        self.stats = stats
        self.matched = matched
        self.channel = channel

    def line(self, outcome: str | None = None) -> str:
        s = self.stats or semantic_join.JoinStats()
        text = (
            f"dedup: {outcome or self.outcome} listed={self.listed} nominated={self.nominated} "
            f"judged={s.judged_calls} cached={s.cached_hits} unjudged={s.unjudged_pairs}"
        )
        if self.matched is not None:
            text += f" ref={self.matched.ref}"
        return text


def _candidate_text(rec: dc.DifficultyRecord) -> str:
    body = f"{rec.functional_ground}\n{rec.evidence}"
    return f"{rec.title or rec.functional_ground}\n\n{body[:CANDIDATE_BODY_CHARS]}"


def _run_dedup(record: dc.DifficultyRecord, channel_name: str, submit_kwargs: dict) -> _Dedup:
    """Judge ``record`` against the channel's open records. Never raises: a failed listing or an
    unjudged candidate is an outcome, and both end in a filing (a lost report is worse than a
    duplicate)."""
    budget = semantic_join.env_budget()
    try:
        channel = dc.get_channel(channel_name, **submit_kwargs)
        open_records = channel.list_open()
    except Exception:
        return _Dedup("search-failed")
    query = record.functional_ground
    nominated = len(semantic_join.nominate(
        query, open_records, _candidate_text, semantic_join.K_FILING))
    result = semantic_join.judged_match(
        query, open_records, _candidate_text, budget=budget, k=semantic_join.K_FILING)
    if result.outcome == "match":
        outcome = "match"
    elif result.outcome == "unjudged":
        outcome = "judge-unavailable"
    else:
        outcome = "no-match" if nominated else "no-candidates"
    return _Dedup(outcome, len(open_records), nominated, result.stats, result.candidate, channel)


def _ref_tail(ref: str) -> str:
    """The record's own identifier: ``7`` for ``7``, ``#7``, ``owner/repo#7`` or ``.../issues/7``."""
    return ref.strip().rstrip("/").rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _comment_on_issue_refusal(args: argparse.Namespace, dedup: _Dedup) -> int | None:
    """--comment-on-issue N allows a comment on N only; every other outcome is refused."""
    if args.comment_on_issue is None:
        return None
    if (dedup.outcome == "match" and dedup.matched is not None
            and _ref_tail(dedup.matched.ref) == _ref_tail(args.comment_on_issue)):
        return None
    ref = f" ref={dedup.matched.ref}" if dedup.matched is not None else ""
    print(
        f"comment-on-issue: no comment on {args.comment_on_issue}: dedup {dedup.outcome}{ref}; "
        "nothing filed or commented",
        file=sys.stderr,
    )
    return EXIT_COMMENT_ON_ISSUE_REFUSED


def _org_neutral_check(text: str) -> tuple[int, str]:
    """(0 clean | 1 hits | 2 checker failure, report) from check-org-neutral.py."""
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "check-org-neutral.py"), "-"],
            input=text, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 2, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _term_gate(text: str, what: str) -> int | None:
    """Blocking gate: no org-internal term may ride along in text bound for a PUBLIC channel.

    Fails closed on a hit; fails OPEN (flagged UNCHECKED rather than silently passed) when no
    ruleset is installed, mirroring check-org-neutral.py's missing-config behavior. Returns the
    exit code to refuse with, or None when the text may go out.
    """
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
        return None
    hits = tr.scan(text, term_rulesets)
    if hits:
        print(
            f"error: org-internal term(s) found in the {what} "
            "(do not file to a public channel):",
            file=sys.stderr,
        )
        for h in hits:
            print(f"  {h.format()}", file=sys.stderr)
        return 1
    return None


def _comment_on_match(args: argparse.Namespace, channel_name: str, dedup: _Dedup) -> int:
    """A judged match: nothing new is filed. The evidence goes to the matched record as a
    comment (unless --no-comment-on-match); the matched ref is always the last stdout line."""
    matched = dedup.matched
    print(f"matched: {matched.title or matched.functional_ground}")
    if args.no_comment_on_match:
        print(matched.ref)
        return EXIT_MATCH_REFUSED
    body = args.evidence or ""
    if body:
        refused = _term_gate(body, "comment body")
        if refused is not None:
            return refused
        if channel_name == "github":
            rc, report = _org_neutral_check(body)
            if rc != 0:
                print(
                    "error: comment body failed the org-neutral check:" if rc == 1
                    else "error: org-neutral checker failed; comment not posted:",
                    file=sys.stderr,
                )
                print(report, file=sys.stderr)
                return rc
        try:
            dedup.channel.add_comment(matched.ref, body)
        except Exception as exc:
            print(f"error commenting on {matched.ref}: {exc}", file=sys.stderr)
            print(dedup.line("match-comment-failed"))
            print(matched.ref)
            return 1
    print(matched.ref)
    return 0


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
    p.add_argument("--no-comment-on-match", action="store_true",
                   help="on a judged match with an open record, refuse instead of commenting: "
                        "file and comment nothing, print the matched ref, exit 3")
    p.add_argument("--comment-on-issue", default=None, metavar="N",
                   help="comment on record N only: when the judged match is N, comment as a "
                        "match does; in every other case exit 4 having filed and commented "
                        "nothing (never falls back to filing)")
    p.add_argument("--filing-preview", default=None, metavar="PATH",
                   help="with --dry-run on the github channel: write the issue title and body "
                        "the real filing would post to PATH")
    args = p.parse_args(argv)
    if args.no_comment_on_match and args.comment_on_issue is not None:
        p.error("--comment-on-issue cannot be combined with --no-comment-on-match")
    if args.filing_preview is not None and not args.dry_run:
        p.error("--filing-preview is only valid with --dry-run")

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
        if args.filing_preview is not None:
            if channel_name != "github":
                print("error: --filing-preview is only available on the github channel",
                      file=sys.stderr)
                return 2
            fields = _gh_record_to_fields(record, stream=args.stream)
            Path(args.filing_preview).write_bytes(
                (fields["title"] + "\n\n" + fields["body"]).encode("utf-8"))
        dedup = _run_dedup(record, channel_name, submit_kwargs)
        print(dedup.line())
        refused = _comment_on_issue_refusal(args, dedup)
        if refused is not None:
            return refused
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

    dedup = _run_dedup(record, channel_name, submit_kwargs)
    print(dedup.line())
    refused = _comment_on_issue_refusal(args, dedup)
    if refused is not None:
        return refused
    if dedup.outcome == "match":
        return _comment_on_match(args, channel_name, dedup)

    # Blocking gate: a difficulty record is about to leave this machine for a PUBLIC channel
    # (the report stream lands in the Core repo's issue tracker) — no org-internal term may
    # ride along in ANY field the adapter publishes, hence record.scan_text() rather than a
    # hand-picked subset: the adapter body also carries layer, reporter and ts.
    refused = _term_gate(record.scan_text(), "difficulty record body")
    if refused is not None:
        return refused

    try:
        handle = authority.file_core_difficulty(record, channel=channel_name, **submit_kwargs)
    except Exception as exc:
        print(f"error submitting to channel {channel_name!r}: {exc}", file=sys.stderr)
        return 1

    print(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
