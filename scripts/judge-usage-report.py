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

Three deliberate design points, all easy to get wrong:

* The duration statistics are computed over outcomes 4 and 5 ONLY, for two
  DIFFERENT reasons (see DURATION_POPULATION_IDS). Widening the population
  either way makes a sicker judge look faster. --latency reuses that same
  membership rather than filtering the raw ledger again, so the two surfaces
  cannot come to disagree about which calls count.
* Every count is derived from the declared OUTCOMES table, so the prose here
  contains no hardcoded tally of how many outcomes there are or how many of
  them fail open. A fifteenth outcome changes the printed numbers by itself.
* --latency splits every judge by the CEILING its calls ran under. This ledger
  spans a ceiling change, so a rate pooled across one describes neither regime
  and can hide a repair as easily as a regression.

Usage:
    scripts/judge-usage-report.py [--ledger PATH]
    scripts/judge-usage-report.py --latency [--since WINDOW] [--ledger PATH]
    scripts/judge-usage-report.py --check-drift [--strict] [--since WINDOW] [--ledger PATH]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
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
    Outcome("12", "died importing its own dependencies, before hook_start", LEVEL_INVOCATION, True),
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

# The two members of that population, named so the latency view splits it by the
# same field the tabulation classified it with. Reading the timeout numerator off
# `timed_out` instead would give this report two answers to "was it a timeout"
# — the classifier's and its own — which can then disagree.
TIMEOUT_OUTCOME_ID = "5"

# NOT part of the duration population, and never folded into it. A judge skipped
# for want of remaining budget produced no verdict, exactly like one killed on
# its ceiling — but it never became a call, so no statistic computed over calls
# can see it. hook-turn-end-gate.py runs three judges in sequence on one fixed
# budget, so raising an early judge's ceiling spends what a later one needs:
# the skip channel is a route to "the verdict silently stopped existing" that
# leaves every timeout number green. It gets its own column.
BUDGET_SKIP_OUTCOME_ID = "3"

# advisor._judge_unavailable writes three no-call stages, but the taxonomy
# names only one row for them (7c). They are folded into that row and broken
# out per stage in the rendering, so the widening stays visible rather than
# being smuggled in under a label that says "killswitch".
NO_CALL_STAGES = {
    "killswitch": "kill switch set",
    "no_runner": "no runner injected",
    "no_text": "no text to judge",
}

# The one `decided` stage that reports how an actual judge CALL ended, and so
# the only one that can leave a verdict behind to lose. Every other stage —
# budget above, the three NO_CALL_STAGES — is a stop taken before the call, and
# maps to outcome 3 or 7c rather than to a verdict.
CALL_STAGE = "call"

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
# This line names the invocation WITHOUT adding a declared outcome to it. The
# rule is not "outcome 6 already counted it" — an invocation can hold both a
# finished call and an unfinished one, and then it counts outcome 6 at the judge
# level AND outcome 10 here. The rule is that a call still running when the
# harness fired never produced a verdict, so nothing was lost at the invocation
# level; the loss is the killed call, and outcome 6 records it where it happened.
KILLED_IN_CALL_LABEL = (
    "killed during a judge call — no verdict had been rendered, so nothing was "
    "lost at the invocation level (the killed call is outcome 6)"
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
    """One judge-level outcome, with the two keys the report breaks down by.

    ``ceiling`` and ``ts`` are AXES, not membership: they widen what a point can
    be grouped and windowed by without touching which points count. The latency
    view needs both — the ledger spans a ceiling change, so a rate pooled across
    one describes no regime that ever ran, and "a fresh window" is the unit a
    post-repair measurement is stated in — while the outcome tabulation above
    reads neither."""

    hook: str
    judge: str
    outcome_id: str
    duration: "float | None"
    ceiling: "float | None" = None
    ts: "float | None" = None


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
    if stage != CALL_STAGE:
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
        ts = _number(record.get("ts"))
        if kind == "entered":
            if not record.get("prefilter_fired"):
                points.append(JudgePoint(hook, judge, "2", None, None, ts))
        elif kind == "decided":
            outcome_id = classify_decided(record)
            if outcome_id is None:
                complaints.append(
                    f"unclassifiable decided line: stage={record.get('stage')!r} "
                    f"judge={judge}"
                )
                continue
            duration = _number(record.get("duration"))
            ceiling = _number(record.get("ceiling"))
            points.append(JudgePoint(hook, judge, outcome_id, duration, ceiling, ts))
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

    The discriminator for outcome 10 is an unpaired `decided` FROM A CALL — a
    verdict was rendered and no `emitted` line followed it. The stage matters:
    a `decided` whose stage is `budget` or one of the NO_CALL_STAGES reports a
    stop taken before the judge was called, so it is outcome 3 or 7c and there
    was never a verdict for the kill to come after. Gating on any `decided` at
    all filed those invocations under "killed after the verdict".

    `final(has_directive=True)` refines the discriminator (a hook can produce a
    directive with no judge decision point behind it) but cannot be the sole
    gate: `final` is written after decide() RETURNS, so an invocation killed
    between its last `decided` and that return has no `final` at all, and gating
    on one filed the whole shape as the healthy case.

    A process that died importing its own dependencies never reaches
    hook_start() at all — its ONLY line is `import_failed`, written by the one
    module (lib.judge_ledger) guaranteed to already be imported. That shape
    used to be indistinguishable from the engine path's own no-hook_start
    calls (outcome 1's neighbour, None); it is outcome 12 instead, checked
    before the `hook_start` gate rather than folded into the ladder below it,
    since nothing past this point assumes a `hook_start` line exists."""
    kinds = [record.get("kind") for record in records]
    if "hook_start" not in kinds:
        return "12" if "import_failed" in kinds else None
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
    decided_a_call = any(
        record.get("kind") == "decided" and record.get("stage") == CALL_STAGE
        for record in records
    )
    if decided_a_call or any(r.get("has_directive") is True for r in finals):
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
        number that overstates the surface and belongs to no population.

        They reach their DENOMINATORS differently, because their outcome tables
        differ in a way that is easy to miss. At the judge level every decision
        point lands on a declared row, so summing the rows is the population. At
        the invocation level every declared row is fail-open — a healthy process
        is filed under INVOCATION_COMPLETED, which no row names — so summing the
        rows would make the denominator equal the numerator and print "N of N"
        no matter how healthy the machine was. The population there is every
        invocation the same walk classified; test_the_two_ways_to_count_an_
        invocation_population_agree pins it against the buckets it is made of,
        so it stays a derived figure rather than a second number to maintain."""
        counts = self.counts_by_outcome()
        fail_open = sum(counts[o.id] for o in FAIL_OPEN_OUTCOMES if o.level == level)
        if level == LEVEL_INVOCATION:
            return self.invocation_count, fail_open
        recorded = sum(counts[o.id] for o in OUTCOMES if o.level == level)
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


@dataclass
class Stats:
    """One (judge, ceiling) group's latency, its timeout rate, and — held apart
    from both — how often the judge was skipped before it could be called."""

    durations: "list[float]" = field(default_factory=list)
    timeouts: int = 0
    budget_skips: int = 0

    @property
    def n(self) -> int:
        return len(self.durations)

    @property
    def rate(self) -> "float | None":
        """Timeouts as a share of the CALLS. None when the group holds none —
        a group that exists only because the judge was skipped has no rate, and
        printing 0% there would read as "this judge never times out"."""
        return self.timeouts / self.n if self.n else None

    @property
    def min_s(self) -> "float | None":
        return min(self.durations) if self.durations else None

    @property
    def median_s(self) -> "float | None":
        return judge_latency.median(self.durations) if self.durations else None

    @property
    def p90_s(self) -> "float | None":
        return judge_latency.p90(self.durations) if self.durations else None

    @property
    def max_s(self) -> "float | None":
        return max(self.durations) if self.durations else None


def latency_by_judge(
    points: "list[JudgePoint]", since: "float | None" = None
) -> "dict[tuple[str, float | None], Stats]":
    """Latency and timeout rate per (judge, ceiling), over `points` alone.

    Pure: no file is read and nothing is printed, because the drift check that
    consumes these statistics must reach them as values rather than by scraping
    this script's stdout.

    The population is DURATION_POPULATION_IDS — the SAME membership rule
    ``Tally.durations_by_judge`` uses, deliberately not a second filter over raw
    ledger fields. A filter like ``stage == "call"`` looks equivalent and is not:
    it also admits the calls outcomes 4 and 5 exclude (a fast refusal, an
    off-vocabulary answer, an exception with no result), which on this repo's own
    ledger is 4 rows of outage_escalation's 16. Two population definitions living
    in one script under one word ("duration", "rate") is how this view and the
    duration table above it come to disagree about a judge in the same output.

    ``since`` is an epoch cutoff; a point with no recorded ``ts`` cannot be shown
    to fall inside a window, so a window excludes it rather than assuming it."""
    groups: "dict[tuple[str, float | None], Stats]" = defaultdict(Stats)
    for point in points:
        if since is not None and (point.ts is None or point.ts < since):
            continue
        if point.outcome_id in DURATION_POPULATION_IDS:
            if point.duration is None:
                continue
            stats = groups[(point.judge, point.ceiling)]
            stats.durations.append(point.duration)
            if point.outcome_id == TIMEOUT_OUTCOME_ID:
                stats.timeouts += 1
        elif point.outcome_id == BUDGET_SKIP_OUTCOME_ID:
            groups[(point.judge, point.ceiling)].budget_skips += 1
    return dict(groups)


def parse_since(value: str, now: "float | None" = None) -> float:
    """Epoch seconds for a ``--since`` argument: ``Nd`` (N days back from now)
    or an ISO date / datetime. A bare date is read in local time, which is the
    reading an operator naming a day means."""
    text = value.strip()
    if text.endswith("d") and text[:-1].isdigit():
        days = int(text[:-1])
        return (time.time() if now is None else now) - days * 86400
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a window: {value!r} — expected an ISO date/datetime or Nd"
        ) from None


# --check-drift thresholds. A pair below MIN_DRIFT_CALLS has not accumulated
# enough of the current regime to say anything — reported, never judged.
MIN_DRIFT_CALLS = 30
DRIFT_TIMEOUT_RATE_WARN = 0.15
DRIFT_CLUSTER_FRACTION_WARN = 0.5
DRIFT_CLUSTER_WINDOW_S = 1.0
DRIFT_SKIP_RATE_WARN = 0.05

DRIFT_INSUFFICIENT = "insufficient data"
DRIFT_FAIL = "FAIL"
DRIFT_WARN = "WARN"
DRIFT_OK = "OK"


@dataclass(frozen=True)
class DriftFinding:
    """One (hook, judge) pair's drift verdict.

    ``reference_ceiling`` is judge_latency.call_ceiling_s(judge) alone — never
    a hook's own (possibly padded) enforced timeout — so this check can never
    disagree with the one table every ceiling is computed from. ``chosen_ceiling``
    is the smallest ceiling actually recorded in the ledger for this pair that
    clears the reference: the CURRENT regime, picked without hardcoding any
    single hook's padding rule. A pair with no such ceiling among its points has
    never run a single call under an adequate ceiling — INSUFFICIENT DATA, not a
    zero rate."""

    hook: str
    judge: str
    reference_ceiling: float
    chosen_ceiling: "float | None"
    n: int
    median_s: "float | None"
    timeout_rate: "float | None"
    cluster_fraction: "float | None"
    skip_rate: "float | None"
    status: str
    reasons: "tuple[str, ...]" = ()


def _in_window(point: JudgePoint, since: "float | None") -> bool:
    return since is None or (point.ts is not None and point.ts >= since)


def check_drift(
    points: "list[JudgePoint]", since: "float | None" = None
) -> "list[DriftFinding]":
    """One finding per (hook, judge) pair declared in HOOK_CALL_SEQUENCE.

    Grouped by hook FIRST — filtering `points` to one hook before ever calling
    latency_by_judge — so a judge declared by two hooks at two (possibly
    IDENTICAL) ceilings is never pooled into one row: the two hooks' points
    never share a Stats object, regardless of what ceiling either recorded.

    Pure: computes over `points` alone, prints nothing, blocks nothing."""
    findings: "list[DriftFinding]" = []
    for hook_basename, sequence in judge_latency.HOOK_CALL_SEQUENCE.items():
        hook_name = judge_ledger.HOOK_NAME_BY_BASENAME[hook_basename]
        hook_points = [p for p in points if p.hook == hook_name]
        groups = latency_by_judge(hook_points, since)
        for judge in sequence:
            if judge_latency.row(judge).measured:
                reference = float(judge_latency.call_ceiling_s(judge))
            else:
                # An UNMEASURED judge (e.g. published_attachment) has no sampled
                # max to derive a per-call ceiling from -- production falls back
                # to the last-resort ceiling for it (judge_latency.Row.note), so
                # that is the meaningful reference to drift-check live calls
                # against, not a crash.
                reference = float(judge_latency.last_resort_ceiling_s())
            candidates = sorted(
                ceiling
                for (g_judge, ceiling) in groups
                if g_judge == judge and ceiling is not None and ceiling >= reference
            )
            if not candidates:
                findings.append(DriftFinding(
                    hook=hook_basename, judge=judge, reference_ceiling=reference,
                    chosen_ceiling=None, n=0, median_s=None, timeout_rate=None,
                    cluster_fraction=None, skip_rate=None, status=DRIFT_INSUFFICIENT,
                ))
                continue
            chosen = candidates[0]
            stats = groups[(judge, chosen)]
            if stats.n < MIN_DRIFT_CALLS:
                findings.append(DriftFinding(
                    hook=hook_basename, judge=judge, reference_ceiling=reference,
                    chosen_ceiling=chosen, n=stats.n, median_s=stats.median_s,
                    timeout_rate=stats.rate, cluster_fraction=None, skip_rate=None,
                    status=DRIFT_INSUFFICIENT,
                ))
                continue
            median = stats.median_s
            if median is not None and median >= chosen:
                findings.append(DriftFinding(
                    hook=hook_basename, judge=judge, reference_ceiling=reference,
                    chosen_ceiling=chosen, n=stats.n, median_s=median,
                    timeout_rate=stats.rate, cluster_fraction=None,
                    skip_rate=None, status=DRIFT_FAIL,
                    reasons=(f"median {median:.2f}s >= ceiling {chosen:g}s",),
                ))
                continue
            timeout_points = [
                p for p in hook_points
                if p.judge == judge and p.ceiling == chosen
                and p.outcome_id == TIMEOUT_OUTCOME_ID and _in_window(p, since)
                and p.duration is not None
            ]
            if timeout_points:
                near = [
                    p for p in timeout_points
                    if p.duration >= chosen - DRIFT_CLUSTER_WINDOW_S
                ]
                cluster_fraction = len(near) / len(timeout_points)
            else:
                cluster_fraction = None
            decision_points = stats.n + stats.budget_skips
            skip_rate = stats.budget_skips / decision_points if decision_points else 0.0
            reasons = []
            if stats.rate is not None and stats.rate >= DRIFT_TIMEOUT_RATE_WARN:
                reasons.append(
                    f"timeout rate {stats.rate:.1%} >= {DRIFT_TIMEOUT_RATE_WARN:.0%}"
                )
            if cluster_fraction is not None and cluster_fraction > DRIFT_CLUSTER_FRACTION_WARN:
                reasons.append(
                    f"{cluster_fraction:.0%} of timeouts land within "
                    f"{DRIFT_CLUSTER_WINDOW_S:g}s of the ceiling"
                )
            if skip_rate > DRIFT_SKIP_RATE_WARN:
                reasons.append(f"budget-skip rate {skip_rate:.1%} > {DRIFT_SKIP_RATE_WARN:.0%}")
            findings.append(DriftFinding(
                hook=hook_basename, judge=judge, reference_ceiling=reference,
                chosen_ceiling=chosen, n=stats.n, median_s=median,
                timeout_rate=stats.rate, cluster_fraction=cluster_fraction,
                skip_rate=skip_rate, status=DRIFT_WARN if reasons else DRIFT_OK,
                reasons=tuple(reasons),
            ))
    return findings


def _drift_fails(finding: DriftFinding, strict: bool) -> bool:
    if finding.status == DRIFT_FAIL:
        return True
    return strict and finding.status == DRIFT_WARN


def format_drift(findings: "list[DriftFinding]", strict: bool) -> "list[str]":
    mode = "strict" if strict else "default"
    lines = [
        f"Ceiling drift check ({mode} mode) — one finding per (hook, judge) pair, "
        f"ceilings read from lib.judge_latency:",
    ]
    for finding in sorted(findings, key=lambda f: (f.hook, f.judge)):
        pair = f"{finding.hook} / {finding.judge}"
        if finding.status == DRIFT_INSUFFICIENT:
            lines.append(
                f"  {pair}: INSUFFICIENT DATA — n={finding.n} calls recorded at or "
                f"above the declared ceiling {finding.reference_ceiling:g}s "
                f"(need >={MIN_DRIFT_CALLS})"
            )
            continue
        status = DRIFT_FAIL if (strict and finding.status == DRIFT_WARN) else finding.status
        detail = (
            f"n={finding.n} median={finding.median_s:.2f}s "
            f"ceiling={finding.chosen_ceiling:g}s"
        )
        if status == DRIFT_OK:
            timeout_rate = (
                f"{finding.timeout_rate:.1%}" if finding.timeout_rate is not None else "n/a"
            )
            skip_rate = f"{finding.skip_rate:.1%}" if finding.skip_rate is not None else "n/a"
            lines.append(
                f"  {pair}: OK — {detail} timeout-rate={timeout_rate} skip-rate={skip_rate}"
            )
        elif status == DRIFT_WARN:
            lines.append(f"  {pair}: WARN — {detail}; " + "; ".join(finding.reasons))
        else:  # DRIFT_FAIL, whether structural or a strict-promoted WARN
            reason = "; ".join(finding.reasons) if finding.reasons else "structural"
            lines.append(
                f"  {pair}: FAIL — {detail}; {reason}. Remedy: take a fresh sample "
                f"and re-derive the row (samples/judge-latency)."
            )
    if any(_drift_fails(f, strict) for f in findings):
        lines.append("Verdict: FAIL — at least one (hook, judge) pair needs re-sampling.")
    else:
        lines.append("Verdict: no ceiling needs re-deriving right now.")
    return lines


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
    # The invocation denominator is every invocation the walk classified, and
    # that includes the UNCLASSIFIED residual — the invocations whose shape the
    # taxonomy could not read, i.e. exactly the ones that MIGHT be fail-open and
    # were not counted as such. An unqualified rate reads as "the rest are
    # healthy", so the residual is said out loud whenever there is one.
    residual = sum(result.residual.values())
    invocation_line = (
        f"  hook invocations:      {inv_fail_open} fail-open of "
        f"{inv_recorded} recorded"
    )
    if residual:
        invocation_line += (
            f" — of which {residual} fell in the UNCLASSIFIED residual, whose "
            f"fail-open status could not be read either way"
        )
    return [
        f"Fail-open: {len(FAIL_OPEN_OUTCOMES)} of the {len(OUTCOMES)} declared outcomes "
        f"let a turn through with no judgement behind it.",
        f"  judge decision points: {judge_fail_open} fail-open of "
        f"{judge_recorded} recorded",
        invocation_line,
        "  The two are reported separately and never added: one invocation holds "
        "several judge decision points, so a sum counts the same allowed turn twice.",
        f"  {seen} distinct fail-open {_plural(seen, 'reason')} recorded here.",
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


def _format_ceiling(ceiling: "float | None") -> str:
    if ceiling is None:
        return "unrecorded"
    return f"{ceiling:g}"


def _seconds(value: "float | None") -> str:
    """A measured second, or a dash where there is nothing to measure. A group
    reached only through the skip column has no call in it, and a 0.00 there
    would read as a very fast judge."""
    return f"{value:>6.2f}" if value is not None else f"{'-':>6}"


def _latency_row(judge: str, ceiling: "float | None", stats: Stats, width: int) -> str:
    key = f"{judge} @ {_format_ceiling(ceiling)}s"
    # Likewise "n/a" and not 0.0%: a judge that was only ever skipped has no
    # timeout rate, and the flattering reading of a printed zero is the opposite
    # of what the row says.
    rate = f"{stats.rate * 100:>5.1f}%" if stats.rate is not None else f"{'n/a':>6}"
    return (
        f"  {key:<{width}}  n={stats.n:>5}  timeouts={stats.timeouts:>5}  "
        f"rate={rate}  min={_seconds(stats.min_s)} "
        f"median={_seconds(stats.median_s)} p90={_seconds(stats.p90_s)} "
        f"max={_seconds(stats.max_s)}  budget-skips={stats.budget_skips}"
    )


def format_latency(
    result: Tally, since: "float | None" = None, window: str = ""
) -> "list[str]":
    """The per-(judge, ceiling) latency and timeout-rate view."""
    if result.missing or result.read_error:
        return format_verdict(result)
    population = ", ".join(sorted(DURATION_POPULATION_IDS))
    lines = [
        f"Judge execution ledger: {result.ledger_path}",
        f"  {result.record_count} records, {_format_size(result.size_bytes)} on disk",
        f"  window: {window if window else 'the whole ledger'}",
        "",
        f"Call latency and timeout rate per judge and per CEILING, over outcomes "
        f"{population} only",
        "  (the same population as the duration table — see "
        "DURATION_POPULATION_IDS). Rows are split by the ceiling the call ran",
        "  under: this ledger spans a ceiling change, and a rate pooled across "
        "one describes no regime that ever ran.",
        f"  budget-skips counts outcome ({BUDGET_SKIP_OUTCOME_ID}) "
        f"{OUTCOME_BY_ID[BUDGET_SKIP_OUTCOME_ID].label} — held OUT of n and out "
        f"of the rate,",
        "  because such a judge never became a call and no rate over calls can "
        "see it.",
    ]
    groups = latency_by_judge(result.judge_points, since)
    if not groups:
        lines.append("  (no call in this window carries a duration)")
        return lines
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1]))
    width = max(len(f"{judge} @ {_format_ceiling(ceiling)}s") for judge, ceiling in groups)
    for (judge, ceiling), stats in ordered:
        lines.append(_latency_row(judge, ceiling, stats, width))
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
    # Three of the four verdicts below assert a UNIVERSAL NEGATIVE — nothing was
    # written, nothing was judged, nothing failed open — and a torn line is
    # exactly the counter-example that would refute one. The "N malformed lines
    # were skipped" paragraph printed above qualifies the tables; it does not
    # qualify a sentence that says a thing never happened, so each of the three
    # answers for the surviving lines only when the file has torn lines in it.
    # DEGRADED needs no such branch: its claim is existential and already bad
    # news, and a skipped line could only make it worse.
    torn = result.dropped_lines
    if not result.record_count:
        if torn:
            return [
                f"Verdict: UNKNOWN — every line in this ledger was unreadable "
                f"({torn} skipped); nothing below speaks for what the judges did."
            ]
        return ["Verdict: NO DATA — the ledger is empty; no hook has written to it."]
    if honest == 0:
        if torn:
            return [
                f"Verdict: UNKNOWN — no surviving line records an honest verdict, "
                f"but {torn} {_plural(torn, 'line')} could not be read and any of "
                f"them may have been one; 'not running' is not established."
            ]
        return [
            "Verdict: NOT RUNNING — no judge call in this ledger reached an honest "
            "verdict. Every gate that consulted a judge allowed its turn unjudged."
        ]
    if judge_fail_open == 0 and inv_fail_open == 0:
        if torn:
            return [
                f"Verdict: HEALTHY (QUALIFIED) — {honest} honest verdicts and no "
                f"fail-open outcome among the surviving lines, but {torn} "
                f"{_plural(torn, 'line')} could not be read and a fail-open "
                f"outcome may be among them."
            ]
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
    parser.add_argument(
        "--latency",
        action="store_true",
        help="print the per-judge, per-ceiling latency and timeout-rate view "
        "instead of the outcome report",
    )
    parser.add_argument(
        "--since",
        type=parse_since,
        default=None,
        metavar="WINDOW",
        help="restrict --latency or --check-drift to calls at or after this point: "
        "an ISO date / datetime, or Nd for N days back",
    )
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="report, per (hook, judge) pair, whether its live latency has drifted "
        "up to meet its declared ceiling. Report-only: never blocks. Exits "
        "non-zero iff a pair FAILs",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="with --check-drift, promote its WARN findings (timeout rate, "
        "ceiling-clustered survivors, budget-skip rate) to FAIL as well",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.since is not None and not args.latency and not args.check_drift:
        parser.error("--since applies to --latency or --check-drift, which was not asked for")
    if args.strict and not args.check_drift:
        parser.error("--strict applies to --check-drift, which was not asked for")
    path = args.ledger if args.ledger is not None else judge_ledger.ledger_path()
    read = judge_ledger.read_ledger(path)
    result = tally(read, path)
    if args.check_drift:
        findings = check_drift(result.judge_points, args.since)
        print("\n".join(format_drift(findings, args.strict)))
        return 1 if any(_drift_fails(f, args.strict) for f in findings) else 0
    if args.latency:
        window = (
            f"calls at or after {datetime.fromtimestamp(args.since).isoformat(' ')}"
            if args.since is not None
            else ""
        )
        print("\n".join(format_latency(result, args.since, window)))
        return 0
    print("\n".join(format_report(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
