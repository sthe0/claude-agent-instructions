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

# Three invocation shapes that are not declared outcomes. They are NOT one
# bucket, because only the first of them is good news, and a single permissive
# label over all three is how an invocation killed mid-flight printed as
# "completed and delivered" while the verdict read HEALTHY.
#
# The one shape that earns the word "delivered" is an `emitted` line saying the
# delivery step ran and did not raise. Everything short of that is either a
# declared outcome or one of the two labels below.
INVOCATION_COMPLETED = "completed"
INVOCATION_KILLED_IN_CALL = "killed_in_call"
INVOCATION_UNCLASSIFIED = "unclassified"

COMPLETED_LABEL = "completed and delivered"
# Already counted at the judge level, so this line names the invocation
# WITHOUT adding a second count: outcome 6 is the kill, and it is recorded
# against the judge whose call was running when the harness fired.
KILLED_IN_CALL_LABEL = (
    "killed during a judge call — counted as outcome 6 at the judge level, "
    "not again here"
)
UNCLASSIFIED_LABEL = "shape the taxonomy does not cover — unclassifiable"

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


def unpaired_started(records: "list[dict]") -> "tuple[dict[str, int], list[str]]":
    """Per judge, how many `started` lines this invocation never closed with a
    `call`, plus the reverse complaint.

    A `started` with no `call` after it is a process killed mid-subprocess:
    subprocess_runner writes `started` immediately before the call and exactly
    one `call` on each of its three exits, so the only way to lose the pair is
    to die in between. Both the judge-level classifier (which turns each leftover
    into outcome 6) and the invocation-level one (which must not call such an
    invocation completed) need this answer, and they must agree on it — so it is
    computed here once instead of twice."""
    pending: "dict[str, int]" = defaultdict(int)
    complaints: "list[str]" = []
    for record in records:
        kind = record.get("kind")
        judge = _text(record.get("judge"), UNATTRIBUTED)
        if kind == "started":
            pending[judge] += 1
        elif kind == "call":
            if pending[judge] > 0:
                pending[judge] -= 1
            else:
                complaints.append(f"call line with no preceding started: judge={judge}")
    return {judge: count for judge, count in pending.items() if count > 0}, complaints


def classify_judge_points(records: "list[dict]") -> "tuple[list[JudgePoint], list[str]]":
    """Every judge-level outcome inside one invocation's records."""
    hook = _hook_of(records)
    points: "list[JudgePoint]" = []
    complaints: "list[str]" = []
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
    pending, pairing_complaints = unpaired_started(records)
    complaints.extend(pairing_complaints)
    for judge, count in pending.items():
        for _ in range(count):
            points.append(JudgePoint(hook, judge, "6", None))
    return points, complaints


def classify_invocation(records: "list[dict]") -> "str | None":
    """How one hook process ended: a declared outcome id, one of the three
    INVOCATION_* labels above, or None when these records are not a hook
    invocation at all (an engine-path judge call writes no hook_start).

    The branches are a PRIORITY ladder, not independent tests, so every
    invocation lands in exactly one bucket: an invocation that both discarded
    and failed to emit is counted once, under the earlier cause.

    The discriminator for outcome 10 is an unpaired `decided` — a verdict was
    rendered and no `emitted` line followed it. `final(has_directive=True)`
    refines that (a hook can produce a directive with no judge decision point
    behind it) but cannot be the sole gate: `final` is written after decide()
    RETURNS, so an invocation killed between its last `decided` and that
    return has no `final` at all, and gating on one filed the whole shape as
    the healthy case."""
    kinds = [record.get("kind") for record in records]
    if "hook_start" not in kinds:
        return None
    if "discarded" in kinds:
        return "8"
    emitted = [r for r in records if r.get("kind") == "emitted"]
    if emitted:
        if any(r.get("ok") is False for r in emitted):
            return "9"
        if all(r.get("ok") is True for r in emitted):
            return INVOCATION_COMPLETED
        # An `emitted` whose `ok` is neither True nor False — a truncated or
        # hand-written line. It says the delivery step was reached and nothing
        # about whether it worked, which is not a claim of completion.
        return INVOCATION_UNCLASSIFIED
    finals = [r for r in records if r.get("kind") == "final"]
    if "decided" in kinds or any(r.get("has_directive") is True for r in finals):
        return "10"
    if unpaired_started(records)[0]:
        return INVOCATION_KILLED_IN_CALL
    return "11"


@dataclass
class Tally:
    """Everything the rendering needs, so counting and printing stay apart."""

    ledger_path: Path
    record_count: int = 0
    size_bytes: int = 0
    invocation_count: int = 0
    judge_points: "list[JudgePoint]" = field(default_factory=list)
    invocation_outcomes: "list[tuple[str, str]]" = field(default_factory=list)  # (hook, id)
    completed: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    killed_in_call: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    residual: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    no_call_stages: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    complaints: "list[str]" = field(default_factory=list)
    dropped_lines: int = 0
    read_error: str = ""
    missing: bool = False

    def counts_by_outcome(self) -> "dict[str, int]":
        counts: "dict[str, int]" = {outcome.id: 0 for outcome in OUTCOMES}
        for point in self.judge_points:
            counts[point.outcome_id] += 1
        for _hook, outcome_id in self.invocation_outcomes:
            counts[outcome_id] += 1
        return counts

    def level_totals(self, level: str) -> "tuple[int, int]":
        """(recorded, fail-open) restricted to ONE granularity.

        The two levels are never summed. One hook invocation can hold several
        judge decision points, so a combined total counts the same allowed turn
        once per judge and once more for the process that wrapped them — a
        number that overstates the surface and belongs to no population."""
        counts = self.counts_by_outcome()
        recorded = sum(counts[o.id] for o in OUTCOMES if o.level == level)
        fail_open = sum(counts[o.id] for o in FAIL_OPEN_OUTCOMES if o.level == level)
        return recorded, fail_open

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


def tally(read: "judge_ledger.LedgerRead", ledger_path: Path) -> Tally:
    """Count one READ of the ledger, not one list of records: whether the file
    was readable at all, and how many of its lines the reader had to skip, are
    part of what the report has to say — a shorter list is otherwise
    indistinguishable from a quieter machine."""
    records = read.records
    groups, unbound = group_by_invocation(records)
    result = Tally(
        ledger_path=ledger_path,
        record_count=len(records),
        dropped_lines=read.dropped_lines,
        read_error=read.error,
        missing=read.missing,
    )
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
            continue
        result.invocation_count += 1
        if outcome_id == INVOCATION_COMPLETED:
            result.completed[hook] += 1
        elif outcome_id == INVOCATION_KILLED_IN_CALL:
            result.killed_in_call[hook] += 1
        elif outcome_id == INVOCATION_UNCLASSIFIED:
            result.residual[hook] += 1
        else:
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
    seen = sum(1 for o in FAIL_OPEN_OUTCOMES if counts[o.id])
    judge_recorded, judge_fail_open = result.level_totals(LEVEL_JUDGE)
    inv_recorded, inv_fail_open = result.level_totals(LEVEL_INVOCATION)
    return [
        f"Fail-open: {len(FAIL_OPEN_OUTCOMES)} of the {len(OUTCOMES)} declared outcomes "
        f"let a turn through with no judgement behind it.",
        f"  judge decision points: {judge_fail_open} fail-open of "
        f"{judge_recorded} recorded",
        f"  hook invocations:      {inv_fail_open} fail-open of "
        f"{inv_recorded} recorded",
        "  The two are reported separately and never added: one invocation holds "
        "several judge decision points, so a sum counts the same allowed turn twice.",
        f"  {seen} distinct fail-open reasons appear here.",
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
    hooks = sorted(
        set(grouped) | set(result.completed) | set(result.killed_in_call)
        | set(result.residual)
    )
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
        for bucket, label in (
            (result.completed, COMPLETED_LABEL),
            (result.killed_in_call, KILLED_IN_CALL_LABEL),
            (result.residual, UNCLASSIFIED_LABEL),
        ):
            if bucket.get(hook):
                lines.append(f"    {label}: {bucket[hook]}")
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


def _plural(count: int, noun: str) -> str:
    """The verdict line is the one sentence an operator reads, and "1 fail-open
    judge decision points" reads as a template rather than a finding."""
    return noun if count == 1 else noun + "s"


def format_verdict(result: Tally) -> "list[str]":
    """One line a reader can act on.

    There is no tolerance band between HEALTHY and DEGRADED on purpose: any
    "acceptable" fail-open share would be a number nobody has measured, and
    the breakdown above already says how bad it is."""
    if result.missing:
        return [
            "Verdict: NO DATA — no ledger file exists at this path; no hook has "
            "written to it."
        ]
    if result.read_error:
        # NOT "empty". An unreadable ledger is the one state in which this
        # report knows nothing, and printing it as a clean sheet would make a
        # permissions or I/O fault read as a quiet machine.
        return [
            f"Verdict: UNKNOWN — the ledger could not be read ({result.read_error}); "
            f"nothing below speaks for what the judges did."
        ]
    counts = result.counts_by_outcome()
    honest = counts["4"]
    _judge_recorded, judge_fail_open = result.level_totals(LEVEL_JUDGE)
    _inv_recorded, inv_fail_open = result.level_totals(LEVEL_INVOCATION)
    if not result.record_count:
        return ["Verdict: NO DATA — the ledger is empty; no hook has written to it."]
    if honest == 0:
        return [
            "Verdict: NOT RUNNING — no judge call in this ledger reached an honest "
            "verdict. Every gate that consulted a judge allowed its turn unjudged."
        ]
    if judge_fail_open == 0 and inv_fail_open == 0:
        return [f"Verdict: HEALTHY — {honest} honest verdicts, no fail-open outcome."]
    return [
        f"Verdict: DEGRADED — {honest} honest verdicts alongside {judge_fail_open} "
        f"fail-open judge decision {_plural(judge_fail_open, 'point')} and "
        f"{inv_fail_open} fail-open hook "
        f"{_plural(inv_fail_open, 'invocation')}."
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
    if result.dropped_lines:
        lines.append("")
        lines.append(
            f"Malformed ledger lines skipped by the reader ({result.dropped_lines}) — "
            f"torn or non-JSON, so they never reached the counts above; every total "
            f"here speaks for the surviving lines only."
        )
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
    read = judge_ledger.read_ledger(path)
    print("\n".join(format_report(tally(read, path))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
