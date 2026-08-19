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
            n=16, min_s=7.19, median_s=10.89, p90_s=19.16, max_s=25.96,
            provenance=(("latency-sample.json", "outage"),
                        ("ab-sample.json", "outage_std")),
        ),
        "feedback_signal": Row(
            judge="feedback_signal",
            n=26, min_s=10.73, median_s=11.86, p90_s=13.34, max_s=14.05,
            provenance=(("latency-sample.json", "feedback"),
                        ("topup2-sample.json", "feedback")),
        ),
        "binary_ask": Row(
            judge="binary_ask",
            n=16, min_s=5.93, median_s=7.46, p90_s=11.06, max_s=11.52,
            provenance=(("topup2-sample.json", "binary_ask"),),
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
    "hook-turn-end-gate.py": ("feedback_signal", "binary_ask", "outage_escalation"),
    "hook-plan-delivery-gate.py": ("approval_ask",),
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
