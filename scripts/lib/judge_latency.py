"""Measured judge latency, and every ceiling and floor computed from it.

Difficulty removed: the timeouts around this repo's `claude -p` judge calls were
each picked from a different undated recollection of "how slow the judge is" —
one from a four-sample note inside a comment, one from a haiku/sonnet baseline
taken before the prompts grew, one from nothing at all. Several landed BELOW the
judge's own fastest measured run, so the harness or the subprocess killed the
call on every invocation and the verdict was computed and thrown away. Nothing
in the code said which number came from which observation, so no reader could
tell a calibrated ceiling from a guess.

This module is the one table those numbers come from. Every ceiling and floor in
a judge-calling hook is COMPUTED here from a measured row by a named rule; the
hooks hold the results as their own constants and the test-suite asserts each
one still equals what this module computes. A number that cannot be derived from
a row here has no business being a judge timeout.

The rows summarise the raw samples committed under ``samples/judge-latency/``.
Each row names its own provenance as (file, series) pairs, and
``tests/test_judge_latency.py`` re-derives n / min / median / p90 / max from
those files with the estimators below — so the summary cannot drift away from
the evidence it claims to summarise.

``samples/judge-latency/topup-sample.json`` is deliberately absent from every
provenance below: its first calls were taken while other work held the same
machine, and its own README records why that makes them unusable.

The KEY of the table is the model constant that actually reaches the judge's
argv (``advisor._JUDGE_MODEL``), not the neighbouring ``_ADVISOR_MODEL`` that
the non-judge advisory calls use. Latency is a property of the model that ran,
so a row filed under the wrong constant would be measured evidence for a call
that never happens.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentctl import advisor  # noqa: E402

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples" / "judge-latency"


def p90(xs: "list[float]") -> float:
    """The 90th percentile by NEAREST RANK: ``sorted(xs)[ceil(0.9*n) - 1]``.

    Nearest rank, not interpolation and not the truncating
    ``sorted(xs)[int(0.9*n) - 1]``: truncation picks the rank BELOW the 90th
    percentile, which on these samples silently understates the tail (the
    deferring row moves 37.58 -> 29.94, the outage row 19.16 -> 18.58) and so
    hands every floor computed from it a number the judge beats less often than
    the name claims.
    """
    ordered = sorted(xs)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def median(xs: "list[float]") -> float:
    ordered = sorted(xs)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


@dataclass(frozen=True)
class Row:
    """One judge's measured latency, or an explicit hole where none exists.

    ``n == 0`` marks a judge nobody has sampled. Such a row carries None for
    every statistic rather than a plausible-looking borrowed number, so a caller
    reaching for a ceiling it has no evidence for fails loudly instead of
    inheriting a neighbour's tail.
    """

    judge: str
    n: int
    min_s: "float | None"
    median_s: "float | None"
    p90_s: "float | None"
    max_s: "float | None"
    provenance: "tuple[tuple[str, str], ...]"
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.n > 0


UNMEASURED_NOTE = (
    "UNMEASURED: no latency sample exists for this judge. It runs outside any "
    "hook, so no harness timeout kills it and the last-resort ceiling applies."
)

MEASURED: "dict[str, dict[str, Row]]" = {
    advisor._JUDGE_MODEL: {
        "deferring_disposition": Row(
            judge="deferring_disposition",
            n=18, min_s=10.29, median_s=17.43, p90_s=37.58, max_s=39.99,
            provenance=(("latency-sample.json", "defer"),
                        ("ab-sample.json", "defer_std")),
        ),
        "outage_escalation": Row(
            judge="outage_escalation",
            # Merged across the same two regimes as the two rows below, and read
            # for a different purpose: this judge was re-sampled not because its
            # own rate had gone pathological but because required_budget_s takes
            # its p90 as the trailing term of the turn-end inequality, and the
            # live ledger had already produced two kills at ~27.03s against the
            # 25.96 max the old series alone declared. The new series settles
            # that: it reaches 27.13 and 30.26 on its own, so the kills were
            # drift and not a stuck subprocess. Movement from the old series
            # alone is large — median 10.89 -> 18.58, p90 19.16 -> 25.96, max
            # 25.96 -> 53.42.
            #
            # That max is ONE observation (53.42) sitting at more than 1.7x the
            # next slowest call in the same arm (30.26), with no neighbour
            # between them. It is the same never-returned shape the plan tracks
            # as residual A, and `ceil(max) + 1` propagates it into a 55s
            # per-call ceiling that no turn-end budget can hold. Recorded here
            # rather than trimmed: dropping an inconvenient observation from the
            # population is exactly the failure this table exists to prevent.
            # Whether the ceiling rule should be applied to it is stage 3's
            # decision, made against a number it can see.
            n=48, min_s=7.19, median_s=18.58, p90_s=25.96, max_s=53.42,
            provenance=(("latency-sample.json", "outage"),
                        ("ab-sample.json", "outage_std"),
                        ("drift-sample.json", "outage"),
                        ("drift-sample.json", "not_outage")),
        ),
        "feedback_signal": Row(
            judge="feedback_signal",
            # Two regimes, like approval_ask below, and for a stronger reason
            # than a slower model: the new series ran through the LEAN judge
            # invocation (host_llm.build_prompt_argv(..., lean=True)) while the
            # old one ran through the bare `claude -p` that loads the ambient
            # CLAUDE.md. This judge's own question topically collides with that
            # content, and under the bare invocation it stopped answering at all
            # — 0/3 in a contention-free A/B, against 3/3 lean. So the old
            # series does not merely describe a faster machine, it describes a
            # different call path; it stays in the provenance because it remains
            # a valid observation of that path and keeps min conservative.
            # Old series alone: median 11.86, p90 13.34, max 14.05. Merged, the
            # median moves modestly (+1.45, the old regime being half the
            # population) but p90 and max move materially (+4.20 and +5.54) —
            # the tail is where the regime change shows.
            n=58, min_s=10.73, median_s=13.30, p90_s=17.54, max_s=19.59,
            provenance=(("latency-sample.json", "feedback"),
                        ("topup2-sample.json", "feedback"),
                        ("drift-sample.json", "feedback"),
                        ("drift-sample.json", "not_feedback")),
        ),
        "silent_closure": Row(
            judge="silent_closure",
            # One process, 8 signal (genuine fork-point decision / completion,
            # no question) + 8 not_signal (near-misses that still trip the
            # prefilter: a real question despite the wording, a
            # only-one-option decision, a routine sub-step, or a status
            # naming remaining work) calls, alternating arms — see
            # sample_silent_closure.py. 15/16 matched the arm's expected
            # verdict; the one miss (signal[2], a genuine decision whose text
            # also said "moving on to the next file") is real data kept
            # standing, not trimmed, same discipline the outage_escalation
            # row's own comment states for its own single-observation max.
            n=16, min_s=3.30, median_s=4.40, p90_s=6.66, max_s=34.78,
            provenance=(("silent-closure-sample.json", "signal"),
                        ("silent-closure-sample.json", "not_signal")),
        ),
        "binary_ask": Row(
            judge="binary_ask",
            # Same two regimes and the same lean/bare split as feedback_signal,
            # but this judge answered correctly under both invocations (5/5 std,
            # 5/5 lean in the same A/B) — for it the regime difference is
            # latency alone, and the ranges are disjoint: the old series ran
            # 5.93-11.52 and the new one 13.40-19.20. Everything moved and by a
            # lot (median 7.46 -> 15.755, p90 11.06 -> 18.57, max 11.52 ->
            # 19.20, i.e. the whole distribution roughly doubled), which is why
            # the live ledger showed this judge killed on 69 of 76 calls at the
            # ceiling of 13 that the old series alone computed.
            n=48, min_s=5.93, median_s=15.75, p90_s=18.57, max_s=19.20,
            provenance=(("topup2-sample.json", "binary_ask"),
                        ("drift-sample.json", "binary_ask"),
                        ("drift-sample.json", "not_binary_ask")),
        ),
        "landing_discipline": Row(
            judge="landing_discipline",
            # 8 pr_proposing + 8 direct_push calls in one file, one merged
            # population: unlike approval_ask's two-regime merge, both arms ran
            # in the same session with no observed contention or timeout-driven
            # regime shift, so a single combined row is the plain, not the
            # exceptional, case.
            n=16, min_s=3.88, median_s=4.96, p90_s=6.37, max_s=15.38,
            provenance=(("landing-discipline-sample.json", "pr_proposing"),
                        ("landing-discipline-sample.json", "direct_push")),
        ),
        "approval_ask": Row(
            judge="approval_ask",
            # Merges two non-overlapping regimes: approval-sample.json's 32
            # calls ran 5.88-11.42s; approval2-sample.json's 32 calls, taken
            # after the judge was observed timing out in production against
            # that first sample's ceiling, ran 14.12-19.14s with an empty gap
            # between the two ranges. The merged median (12.77) therefore
            # falls IN that gap and describes no call that ever ran. That is
            # safe to leave standing only because this judge's median is never
            # used for sizing a ceiling or a floor — required_budget_s sums the
            # medians of the calls PRECEDING the last one on a shared budget,
            # and HOOK_CALL_SEQUENCE declares K=1 for hook-plan-delivery-gate.py
            # (see samples/judge-latency/README.md), so only p90_s and max_s
            # ever reach a constant. approval-sample.json stays in the
            # provenance rather than being dropped: it is a valid observation
            # of an earlier regime (unlike topup-sample.json, taken under
            # contended load), and keeping it is what keeps max_s conservative.
            n=64, min_s=5.88, median_s=12.77, p90_s=17.29, max_s=19.14,
            provenance=(("approval-sample.json", "approval"),
                        ("approval-sample.json", "not_approval"),
                        ("approval2-sample.json", "approval"),
                        ("approval2-sample.json", "not_approval")),
        ),
        "acceptance_judge": Row(
            judge="acceptance_judge",
            n=0, min_s=None, median_s=None, p90_s=None, max_s=None,
            provenance=(), note=UNMEASURED_NOTE,
        ),
        "question_materiality": Row(
            judge="question_materiality",
            n=0, min_s=None, median_s=None, p90_s=None, max_s=None,
            provenance=(), note=UNMEASURED_NOTE,
        ),
        "published_attachment": Row(
            judge="published_attachment",
            n=0, min_s=None, median_s=None, p90_s=None, max_s=None,
            provenance=(), note=UNMEASURED_NOTE,
        ),
    },
}


def rows(model: "str | None" = None) -> "dict[str, Row]":
    return MEASURED[model if model is not None else advisor._JUDGE_MODEL]


def row(judge: str, model: "str | None" = None) -> Row:
    return rows(model)[judge]


def _measured_row(judge: str, model: "str | None" = None) -> Row:
    r = row(judge, model)
    if not r.measured:
        raise KeyError(f"{judge} is unmeasured: {r.note}")
    return r


def call_floor_s(judge: str, model: "str | None" = None) -> int:
    """``ceil(p90)`` — below this much remaining budget, do not start the call.

    The floor is what a hook refuses to spend the last of its budget on: a call
    started with less than this left is expected to be cut off mid-answer, and a
    cut-off judge is a fail-open no-op that cost the whole remaining wait. The
    p90 basis says the judge finishes inside it nine times in ten on the sample;
    it does NOT say a shorter call always fails, which is why the floor is a
    start condition and never a claim about the tail.
    """
    return math.ceil(_measured_row(judge, model).p90_s)


def call_ceiling_s(judge: str, model: "str | None" = None) -> int:
    """``ceil(max) + 1`` — the longest a single call may run.

    One second past the slowest run anybody observed. The ``+ 1`` is head-room
    over a REALIZED observation rather than over an estimate: at exactly
    ``ceil(max)`` the worst run already seen would be killed by rounding alone.

    This is the per-call ceiling for a hook that makes two or more calls on one
    budget, where a ceiling is what stops the first judge from eating the whole
    invocation. A hook making exactly one call passes its whole-invocation budget
    as the ceiling instead — there is no later call to protect, and capping the
    only call below the budget would forfeit time for nothing.
    """
    return math.ceil(_measured_row(judge, model).max_s) + 1


def last_resort_ceiling_s(model: "str | None" = None) -> int:
    """``ceil(max over EVERY measured row of this model) + 1``.

    The ceiling for a judge call made outside any hook — the last-resort default
    on the ``judge_*`` signatures, and the unmeasured ``acceptance_judge``. Such
    a call has no harness timeout above it and no budget beside it, so the number
    has to cover the worst thing this model has been seen to do on ANY prompt,
    not the worst on one prompt.

    Deliberately not the slowest row's own ceiling: that would make a global
    default a function of one hook's budget and one hook's call count, so
    re-tuning that hook would silently move a timeout nowhere near it.
    """
    observed = [r.max_s for r in rows(model).values() if r.measured]
    return math.ceil(max(observed)) + 1


LAST_RESORT_CEILING_S = last_resort_ceiling_s()

# The judges each hook calls, in the order it calls them. The ORDER is
# load-bearing, not documentation: on one shared budget the earlier calls decide
# how much is left for the last one, so the size inequality
# `budget >= sum(median of preceding) + floor(last) + headroom` can only be
# stated against a declared sequence. `lib.hook_wiring.TIMEOUT_REQUIREMENT_CALLS`
# holds the same K as a count next to the budget it constrains; the test-suite
# asserts the two agree.
HOOK_CALL_SEQUENCE: "dict[str, tuple[str, ...]]" = {
    "hook-escalation-diagnosis-gate.py": ("outage_escalation",),
    "hook-deferring-disposition-gate.py": ("deferring_disposition",),
    "hook-turn-end-gate.py": ("feedback_signal", "binary_ask", "silent_closure", "outage_escalation"),
    "hook-plan-delivery-gate.py": ("approval_ask",),
    "hook-resolution-reminder.py": ("landing_discipline",),
    "hook-published-text-writer-gate.py": ("published_attachment",),
}

# Head-room the whole-invocation budget must keep beyond the calls it plans, for
# everything that is not a judge call: interpreter start is outside the budget,
# but transcript reading, prefiltering and state loads are inside it. One second
# is not measured — it is the smallest amount that keeps the inequality strict,
# so a budget that satisfies it only exactly is reported as too tight.
SIZE_HEADROOM_S = 1


def required_budget_s(hook: str, model: "str | None" = None) -> float:
    """The smallest whole-invocation budget that lets `hook` reach its last call.

    Typical cost for every call but the last (their medians), plus the last
    call's floor — the budget must still be above the floor when the final judge
    is reached, or that judge is structurally unreachable — plus SIZE_HEADROOM_S.
    Medians, not ceilings: a budget covering every call's worst case at once
    would have to be several times the harness timeout, and the design accepts
    that the last judge is cut off on a tail (see the hooks' own comments).
    """
    sequence = HOOK_CALL_SEQUENCE[hook]
    preceding = sum(_measured_row(j, model).median_s for j in sequence[:-1])
    return preceding + call_floor_s(sequence[-1], model) + SIZE_HEADROOM_S
