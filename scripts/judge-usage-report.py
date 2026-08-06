#!/usr/bin/env python3
"""Count what the judge-calling hooks actually did, from the execution ledger.

Difficulty removed: stage 5 made every judge outcome recordable, but a ledger
nobody reads is silence with extra steps. The specific silence this report
breaks is that all but three of the recordable outcomes END IN THE SAME
OBSERVABLE — the gate allows, the hook exits 0 — so "the judge said no",
"the judge timed out", "the hook was killed mid-call" and "the judge was
never wired up at all" are indistinguishable from outside. This report names
each of them separately and, crucially, prints the ones whose count is ZERO:
a taxonomy row reading 0 is how "the judge has never once finished a
judgement on this machine" becomes visible instead of inferred.

Read-only. It opens the ledger and nothing else — no judge is called, no
subprocess is spawned, no session state is written.

Two deliberate design points, both easy to get wrong:

* The duration statistics are computed over outcomes 4 and 5 ONLY, for two
  DIFFERENT reasons (see DURATION_POPULATION_IDS). Widening the population
  either way makes a sicker judge look faster.
* Every count is derived from the declared OUTCOMES table, so the prose here
  contains no hardcoded tally of how many outcomes there are or how many of
  them fail open. A fifteenth outcome changes the printed numbers by itself.

Usage:
    scripts/judge-usage-report.py [--ledger PATH]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import judge_latency  # noqa: E402
from lib import judge_ledger  # noqa: E402

# An outcome is recorded at one of two granularities, and mixing them silently
# is how a fail-open total becomes meaningless: a hook invocation can hold
# several judge decision points (hook-turn-end-gate.py holds three), so
# "7 fail-open outcomes" means different things at the two levels.
LEVEL_JUDGE = "judge"  # one judge decision point inside one invocation
LEVEL_INVOCATION = "invocation"  # one hook process, whatever it decided
LEVEL_UNOBSERVABLE = "unobservable"  # no ledger line exists to count


@dataclass(frozen=True)
class Outcome:
    """One recordable end-state of a judge decision point or hook invocation.

    ``fail_open`` says the gate let the turn through WITHOUT a judgement
    behind it — a permissive result that is indistinguishable, from outside,
    from an honest allow. It is declared per row here and cross-checked
    against NOT_FAIL_OPEN_IDS below, so the two statements of the same fact
    cannot drift apart unnoticed."""

    id: str
    label: str
    level: str
    fail_open: bool

    @property
    def observable(self) -> bool:
        return self.level != LEVEL_UNOBSERVABLE


# The stage-5 taxonomy, verbatim in scope. Order is the order a reader walks
# an invocation: never started, prefiltered, budget, called, then the ways a
# call or a delivery can end badly.
OUTCOMES: "tuple[Outcome, ...]" = (
    Outcome("1", "hook never entered", LEVEL_UNOBSERVABLE, False),
    Outcome("2", "entered, prefiltered before the judge", LEVEL_JUDGE, False),
    Outcome("3", "budget exhausted before the call", LEVEL_JUDGE, True),
    Outcome("4", "judge called and finished honestly", LEVEL_JUDGE, False),
    Outcome("5", "call timed out on its own ceiling", LEVEL_JUDGE, True),
    Outcome("6", "hook killed by the harness during the call", LEVEL_JUDGE, True),
    Outcome("7", "fast refusal without a judgement", LEVEL_JUDGE, True),
    Outcome("7a", "unparseable / off-vocabulary answer", LEVEL_JUDGE, True),
    Outcome("7b", "exception with no result", LEVEL_JUDGE, True),
    Outcome("7c", "judge disabled or nothing to judge", LEVEL_JUDGE, True),
    Outcome("8", "verdict discarded after judging", LEVEL_INVOCATION, True),
    Outcome("9", "verdict rendered but not emitted", LEVEL_INVOCATION, True),
    Outcome("10", "killed after the verdict, before emission", LEVEL_INVOCATION, True),
    Outcome("11", "killed or returned early, before any verdict", LEVEL_INVOCATION, True),
)

# The three outcomes that leave the gate having actually done its job: the
# hook never ran at all (nothing was let through unjudged because nothing was
# asked), the prefilter declined to call the judge (a decision, not a
# failure), and the judge answered. EVERY other outcome allowed the turn
# through with no judgement behind it. Declared independently of the per-row
# `fail_open` flags above so the test can assert the two agree.
NOT_FAIL_OPEN_IDS = frozenset({"1", "2", "4"})

OUTCOME_BY_ID = {o.id: o for o in OUTCOMES}
FAIL_OPEN_OUTCOMES = tuple(o for o in OUTCOMES if o.id not in NOT_FAIL_OPEN_IDS)

# Durations are pinned to these two outcomes, and the two exclusions they
# imply have DIFFERENT causes:
#
#   * 7 / 7a / 7b / 7c are calls that produced no judgement. Their wall-clock
#     is an order of magnitude below a real one (a killswitch check is
#     microseconds; a non-zero exit is a process that died on startup) or
#     absent entirely. Averaging them in drags every statistic down, so the
#     deader the judge, the healthier the latency table looks — the exact
#     inversion this report exists to prevent.
#   * 8 / 9 / 10 / 11 are invocation-level lines with no measured duration of
#     their own. The calls underneath them are ALREADY counted by the
#     outcome-4 rows of the same invocation_id, so counting the invitation
#     too would double-count the one call it wraps.
DURATION_POPULATION_IDS = frozenset({"4", "5"})

# advisor._judge_unavailable writes three no-call stages, but the taxonomy
# names only one row for them (7c). They are folded into that row and broken
# out per stage in the rendering, so the widening stays visible rather than
# being smuggled in under a label that says "killswitch".
NO_CALL_STAGES = {
    "killswitch": "kill switch set",
    "no_runner": "no runner injected",
    "no_text": "no text to judge",
}

# A hook invocation that reached `emitted` normally is not one of the declared
# outcomes — it is the healthy case. It also absorbs the benign shape of a `final`
# carrying no directive with no `emitted` after it: the hook died having
# decided there was nothing to deliver, which changed no outcome.
RESIDUAL_LABEL = "completed and delivered"

# Above this the whole-file read stops being free and the operator should
# truncate or archive. See scripts/README.md § Judge execution ledger for why
# nothing rotates the file automatically; this line is the tripwire that keeps
# that decision honest rather than merely hoped-for.
LEDGER_SIZE_ADVISORY_BYTES = 64 * 1024 * 1024

UNATTRIBUTED = "unattributed"
NO_HOOK = "(engine path, no hook)"


@dataclass(frozen=True)
class JudgePoint:
    """One judge-level outcome, with the two keys the report breaks down by."""

    hook: str
    judge: str
    outcome_id: str
    duration: "float | None"


def _text(value, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _number(value) -> "float | None":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _hook_of(records: "list[dict]") -> str:
    """The hook one invocation belongs to. Read off the FIRST record because
    hook_start is written before anything else in the process, so a later
    line's absent `hook` field cannot rename the invocation mid-way."""
    return _text(records[0].get("hook") if records else None, NO_HOOK)


def group_by_invocation(records: "list[dict]") -> "tuple[dict[str, list[dict]], list[str]]":
    """Bind each record to its invocation via invocation_id, never via line
    order: two hooks running concurrently interleave their lines in the file,
    so position says nothing about which invitation a line belongs to.

    Returns the groups (in first-appearance order) and the complaints about
    lines that could not be bound at all."""
    groups: "dict[str, list[dict]]" = {}
    unbound: "list[str]" = []
    for record in records:
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            unbound.append(f"record with no invocation_id: kind={record.get('kind')!r}")
            continue
        groups.setdefault(invocation_id, []).append(record)
    return groups, unbound


def classify_decided(record: dict) -> "str | None":
    """Map one `decided` line onto its outcome id, or None if its shape is not
    one this taxonomy covers.

    The ladder tests STRUCTURAL fields before falling back to the one string
    comparison it cannot avoid. That last step is a field-EMPTINESS test, not
    a semantic match: advisor._classify documents that it returns an empty
    reason if and only if it parsed a YES/NO out of the judge, and fills a
    non-empty reason on every failure path. A reason absent altogether means
    lib.judge_ledger._encode dropped it to fit the line cap — which only
    happens to long free text, i.e. never to the empty string — so an absent
    reason is left unclassified rather than guessed at."""
    stage = record.get("stage")
    if stage == "budget":
        return "3"
    if stage in NO_CALL_STAGES:
        return "7c"
    if stage != "call":
        return None
    if record.get("timed_out") is True:
        return "5"
    if record.get("malformed") is True:
        return "7a"
    if record.get("timed_out") is None and not record.get("runner_legacy"):
        # advisor._record_raised is the only writer of this shape: it reports
        # no timeout answer AND does not claim a legacy runner. A genuinely
        # legacy runner sets runner_legacy, and its verdict is knowable from
        # the reason below.
        return "7b"
    reason = record.get("reason")
    if reason == "":
        return "4"
    if isinstance(reason, str):
        return "7"
    return None


def classify_judge_points(records: "list[dict]") -> "tuple[list[JudgePoint], list[str]]":
    """Every judge-level outcome inside one invocation's records."""
    hook = _hook_of(records)
    points: "list[JudgePoint]" = []
    complaints: "list[str]" = []
    # A `started` with no `call` after it is a process killed mid-subprocess:
    # subprocess_runner writes `started` immediately before the call and
    # exactly one `call` on each of its three exits, so the only way to lose
    # the pair is to die in between.
    pending_started: "dict[str, int]" = defaultdict(int)
    for record in records:
        kind = record.get("kind")
        judge = _text(record.get("judge"), UNATTRIBUTED)
        if kind == "entered":
            if not record.get("prefilter_fired"):
                points.append(JudgePoint(hook, judge, "2", None))
        elif kind == "decided":
            outcome_id = classify_decided(record)
            if outcome_id is None:
                complaints.append(
                    f"unclassifiable decided line: stage={record.get('stage')!r} "
                    f"judge={judge}"
                )
                continue
            duration = _number(record.get("duration"))
            points.append(JudgePoint(hook, judge, outcome_id, duration))
        elif kind == "started":
            pending_started[judge] += 1
        elif kind == "call":
            if pending_started[judge] > 0:
                pending_started[judge] -= 1
            else:
                complaints.append(f"call line with no preceding started: judge={judge}")
    for judge, count in pending_started.items():
        for _ in range(count):
            points.append(JudgePoint(hook, judge, "6", None))
    return points, complaints


def classify_invocation(records: "list[dict]") -> "str | None":
    """The invocation-level outcome of one hook process, or None when the
    records are not a hook invocation (an engine-path judge call has no
    hook_start) or when it completed and delivered.

    The branches are a PRIORITY ladder, not independent tests, so every
    invocation lands in exactly one bucket: an invocation that both discarded
    and failed to emit is counted once, under the earlier cause."""
    kinds = [record.get("kind") for record in records]
    if "hook_start" not in kinds:
        return None
    if "discarded" in kinds:
        return "8"
    emitted = [r for r in records if r.get("kind") == "emitted"]
    if any(r.get("ok") is False for r in emitted):
        return "9"
    if not emitted:
        finals = [r for r in records if r.get("kind") == "final"]
        if any(r.get("has_directive") is True for r in finals):
            return "10"
        if not finals and "decided" not in kinds and "started" not in kinds:
            return "11"
    return None


@dataclass
class Tally:
    """Everything the rendering needs, so counting and printing stay apart."""

    ledger_path: Path
    record_count: int = 0
    size_bytes: int = 0
    invocation_count: int = 0
    judge_points: "list[JudgePoint]" = field(default_factory=list)
    invocation_outcomes: "list[tuple[str, str]]" = field(default_factory=list)  # (hook, id)
    residual: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    no_call_stages: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    complaints: "list[str]" = field(default_factory=list)

    def counts_by_outcome(self) -> "dict[str, int]":
        counts: "dict[str, int]" = {outcome.id: 0 for outcome in OUTCOMES}
        for point in self.judge_points:
            counts[point.outcome_id] += 1
        for _hook, outcome_id in self.invocation_outcomes:
            counts[outcome_id] += 1
        return counts

    def durations_by_judge(self) -> "dict[str, list[float]]":
        """The pinned population, split by judge. The single place the
        population filter is written, so the per-judge table and the overall
        one cannot come to disagree about which calls count."""
        by_judge: "dict[str, list[float]]" = defaultdict(list)
        for point in self.judge_points:
            if point.outcome_id in DURATION_POPULATION_IDS and point.duration is not None:
                by_judge[point.judge].append(point.duration)
        return by_judge

    def durations(self) -> "list[float]":
        return [
            duration
            for values in self.durations_by_judge().values()
            for duration in values
        ]


def tally(records: "list[dict]", ledger_path: Path) -> Tally:
    groups, unbound = group_by_invocation(records)
    result = Tally(ledger_path=ledger_path, record_count=len(records))
    try:
        result.size_bytes = ledger_path.stat().st_size
    except OSError:
        result.size_bytes = 0
    result.complaints.extend(unbound)
    for invocation_records in groups.values():
        points, complaints = classify_judge_points(invocation_records)
        result.judge_points.extend(points)
        result.complaints.extend(complaints)
        hook = _hook_of(invocation_records)
        outcome_id = classify_invocation(invocation_records)
        if outcome_id is None:
            if any(r.get("kind") == "hook_start" for r in invocation_records):
                result.invocation_count += 1
                result.residual[hook] += 1
            continue
        result.invocation_count += 1
        result.invocation_outcomes.append((hook, outcome_id))
    for record in records:
        if record.get("kind") == "decided" and record.get("stage") in NO_CALL_STAGES:
            result.no_call_stages[record["stage"]] += 1
    return result


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    return f"{size_bytes / (1024 * 1024):.1f} MiB"


def format_taxonomy(result: Tally) -> "list[str]":
    """Every declared outcome with its total, ZEROS INCLUDED — a row reading 0
    against outcome 4 is the whole point of the report."""
    counts = result.counts_by_outcome()
    lines = ["Outcomes recorded (every declared outcome, including the zeros):"]
    width = max(len(outcome.id) for outcome in OUTCOMES) + 2
    for outcome in OUTCOMES:
        note = " [not observable in the ledger]" if not outcome.observable else ""
        marker = "fail-open" if outcome.fail_open else "judged   "
        tag = f"({outcome.id})"
        lines.append(
            f"  {tag:<{width}}  {counts[outcome.id]:>6}  {marker}  "
            f"{outcome.label} [{outcome.level}]{note}"
        )
    if result.no_call_stages:
        detail = ", ".join(
            f"{NO_CALL_STAGES[stage]}: {count}"
            for stage, count in sorted(result.no_call_stages.items())
        )
        lines.append(f"    outcome 7c breaks down as — {detail}")
    return lines


def format_fail_open(result: Tally) -> "list[str]":
    counts = result.counts_by_outcome()
    total = sum(counts[o.id] for o in FAIL_OPEN_OUTCOMES)
    seen = sum(1 for o in FAIL_OPEN_OUTCOMES if counts[o.id])
    return [
        f"Fail-open: {len(FAIL_OPEN_OUTCOMES)} of the {len(OUTCOMES)} declared outcomes "
        f"let a turn through with no judgement behind it.",
        f"  {total} such outcomes recorded here, across {seen} distinct reasons.",
    ]


def format_by_hook_and_judge(result: Tally) -> "list[str]":
    """The breakdown by BOTH keys: judge_outage_escalation is called from two
    different hooks, so a per-judge table alone cannot say which hook is
    silent."""
    grouped: "dict[tuple[str, str], dict[str, int]]" = defaultdict(lambda: defaultdict(int))
    for point in result.judge_points:
        grouped[(point.hook, point.judge)][point.outcome_id] += 1
    lines = ["Judge decision points, by hook and judge:"]
    if not grouped:
        lines.append("  (none recorded)")
        return lines
    for (hook, judge), counts in sorted(grouped.items()):
        lines.append(f"  {hook} / {judge}")
        for outcome in OUTCOMES:
            if counts.get(outcome.id):
                lines.append(
                    f"    ({outcome.id}) {outcome.label}: {counts[outcome.id]}"
                )
    return lines


def format_by_invocation(result: Tally) -> "list[str]":
    grouped: "dict[str, dict[str, int]]" = defaultdict(lambda: defaultdict(int))
    for hook, outcome_id in result.invocation_outcomes:
        grouped[hook][outcome_id] += 1
    hooks = sorted(set(grouped) | set(result.residual))
    lines = ["Hook invocations, by hook:"]
    if not hooks:
        lines.append("  (none recorded)")
        return lines
    for hook in hooks:
        lines.append(f"  {hook}")
        for outcome in OUTCOMES:
            count = grouped.get(hook, {}).get(outcome.id)
            if count:
                lines.append(f"    ({outcome.id}) {outcome.label}: {count}")
        if result.residual.get(hook):
            lines.append(f"    {RESIDUAL_LABEL}: {result.residual[hook]}")
    return lines


def _stats_line(label: str, values: "list[float]") -> str:
    return (
        f"  {label}: n={len(values)} min={min(values):.2f} "
        f"median={judge_latency.median(values):.2f} "
        f"p90={judge_latency.p90(values):.2f} max={max(values):.2f}"
    )


def format_durations(result: Tally) -> "list[str]":
    population = ", ".join(sorted(DURATION_POPULATION_IDS))
    lines = [
        f"Call duration in seconds, over outcomes {population} only "
        f"(see DURATION_POPULATION_IDS for why the others are excluded):"
    ]
    by_judge = result.durations_by_judge()
    if not by_judge:
        lines.append("  (no completed call carries a duration)")
        return lines
    for judge, values in sorted(by_judge.items()):
        lines.append(_stats_line(judge, values))
    overall = result.durations()
    if len(by_judge) > 1:
        lines.append(_stats_line("all judges", overall))
    return lines


def format_verdict(result: Tally) -> "list[str]":
    """One line a reader can act on.

    There is no tolerance band between HEALTHY and DEGRADED on purpose: any
    "acceptable" fail-open share would be a number nobody has measured, and
    the breakdown above already says how bad it is."""
    counts = result.counts_by_outcome()
    honest = counts["4"]
    fail_open = sum(counts[o.id] for o in FAIL_OPEN_OUTCOMES)
    if not result.record_count:
        return ["Verdict: NO DATA — the ledger is empty; no hook has written to it."]
    if honest == 0:
        return [
            "Verdict: NOT RUNNING — no judge call in this ledger reached an honest "
            "verdict. Every gate that consulted a judge allowed its turn unjudged."
        ]
    if fail_open == 0:
        return [f"Verdict: HEALTHY — {honest} honest verdicts, no fail-open outcome."]
    return [
        f"Verdict: DEGRADED — {honest} honest verdicts alongside {fail_open} "
        f"fail-open outcomes."
    ]


def format_report(result: Tally) -> "list[str]":
    lines = [
        f"Judge execution ledger: {result.ledger_path}",
        f"  {result.record_count} records, {result.invocation_count} hook invocations, "
        f"{_format_size(result.size_bytes)} on disk",
    ]
    if result.size_bytes >= LEDGER_SIZE_ADVISORY_BYTES:
        lines.append(
            f"  NOTE: past {_format_size(LEDGER_SIZE_ADVISORY_BYTES)} — nothing rotates "
            f"this file; truncate or archive it (scripts/README.md § Judge execution "
            f"ledger)."
        )
    lines.append("")
    lines.extend(format_taxonomy(result))
    lines.append("")
    lines.extend(format_fail_open(result))
    lines.append("")
    lines.extend(format_by_hook_and_judge(result))
    lines.append("")
    lines.extend(format_by_invocation(result))
    lines.append("")
    lines.extend(format_durations(result))
    if result.complaints:
        lines.append("")
        lines.append(
            f"Unclassified ledger lines ({len(result.complaints)}) — counted nowhere "
            f"above, reported rather than dropped:"
        )
        for complaint in result.complaints:
            lines.append(f"  {complaint}")
    lines.append("")
    lines.extend(format_verdict(result))
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="ledger path (default: the configured judge execution ledger)",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.ledger if args.ledger is not None else judge_ledger.ledger_path()
    records = judge_ledger.read_records(path)
    print("\n".join(format_report(tally(records, path))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
