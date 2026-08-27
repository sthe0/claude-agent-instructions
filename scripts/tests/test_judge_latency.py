"""Tests for lib/judge_latency.py — the one table every judge timeout is computed from.

Two directions are asserted here, and the second is what makes the first mean
anything:

  * every SUMMARY in the table re-derives from the raw samples it cites, with the
    estimators the module names — so the row cannot drift away from its evidence;
  * every CONSTANT in a judge-calling hook equals what the table computes for
    that judge, AND the computation stops equalling it once the row is replaced
    by a slower one.

The hooks hold literals — they must, since a hook is a subprocess that cannot
afford to import a sample-reading module on every turn — so this file is the
whole of the binding. The second half is what keeps the first honest: on its own,
`assert 38 == call_floor_s("deferring_disposition")` would pass just as happily
if `call_floor_s` ignored its row and returned a hard-coded 38.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from agentctl import advisor
from agentctl.dispatch import RunResult
from lib import hook_wiring, judge_budget, judge_latency

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_hook(filename: str):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").removesuffix(".py"), SCRIPTS_DIR / filename
    )
    mod = importlib.util.module_from_spec(spec)
    # Register before exec, as test_hook_turn_end_gate.py does: a frozen
    # dataclass in a hook resolves its stringified annotations through
    # sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_TURN_END = _load_hook("hook-turn-end-gate.py")
_DEFERRING = _load_hook("hook-deferring-disposition-gate.py")
_ESCALATION = _load_hook("hook-escalation-diagnosis-gate.py")
_APPROVAL = _load_hook("hook-plan-delivery-gate.py")


def _samples(row: judge_latency.Row) -> "list[float]":
    """Every latency the row claims to summarise, read back out of the committed
    raw samples rather than out of the row."""
    observations: list[float] = []
    for filename, series in row.provenance:
        data = json.loads((judge_latency.SAMPLES_DIR / filename).read_text(encoding="utf-8"))
        observations.extend(float(entry["latency_s"]) for entry in data[series])
    return observations


_MEASURED_JUDGES = sorted(j for j, r in judge_latency.rows().items() if r.measured)


# --- the table against its evidence ------------------------------------------

def test_the_table_is_keyed_by_the_model_that_reaches_the_judges_argv():
    """Latency belongs to the model that ran. The judges take `_JUDGE_MODEL`;
    the non-judge advisory calls take the neighbouring `_ADVISOR_MODEL`, and a
    row filed under that one would be evidence for a call that never happens."""
    assert set(judge_latency.MEASURED) == {advisor._JUDGE_MODEL}

    seen: dict = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return RunResult(0, "NO", "")

    advisor.judge_binary_ask("Продолжаем?", run, enabled=True)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] in judge_latency.MEASURED


@pytest.mark.parametrize("judge", _MEASURED_JUDGES)
def test_every_row_re_derives_from_the_samples_it_cites(judge):
    row = judge_latency.row(judge)
    observations = _samples(row)
    assert row.n == len(observations)
    assert row.min_s == pytest.approx(min(observations))
    assert row.max_s == pytest.approx(max(observations))
    assert row.p90_s == pytest.approx(judge_latency.p90(observations))
    # n / min / max / p90 are single recorded observations and match exactly; the
    # median of an even sample averages two of them and so can land on a half
    # centisecond the table rounds away (binary_ask 7.455 -> 7.46). Half a
    # centisecond is two orders of magnitude below the second-granular ceil()
    # every derived number goes through, so the tolerance is the table's stated
    # precision — not slack for a re-measurement.
    assert row.median_s == pytest.approx(judge_latency.median(observations), abs=0.005)


def test_the_p90_estimator_is_nearest_rank_not_the_truncating_variant():
    """`sorted[ceil(0.9n)-1]` vs `sorted[int(0.9n)-1]`: the two agree whenever
    0.9n is integral, so a sample where they DISAGREE is what pins the choice.
    The deferring row is such a sample (37.58 vs 29.94), and it is the row whose
    p90 sets the largest floor in the repo."""
    observations = _samples(judge_latency.row("deferring_disposition"))
    ordered = sorted(observations)
    n = len(ordered)
    nearest_rank = ordered[math.ceil(0.9 * n) - 1]
    truncated = ordered[int(0.9 * n) - 1]
    assert nearest_rank != truncated, (
        "this sample no longer discriminates between the two estimators — pick a "
        "row where 0.9*n is not integral, or the test is asserting nothing"
    )
    assert judge_latency.p90(observations) == nearest_rank


def test_the_median_of_an_even_sample_averages_the_two_middles():
    assert judge_latency.median([1.0, 2.0, 3.0]) == 2.0
    assert judge_latency.median([1.0, 2.0, 3.0, 6.0]) == 2.5
    assert judge_latency.median([3.0, 1.0, 2.0]) == 2.0  # unsorted input


def test_an_unmeasured_row_carries_no_borrowed_statistic():
    row = judge_latency.row("acceptance_judge")
    assert not row.measured and row.n == 0 and row.provenance == ()
    assert (row.min_s, row.median_s, row.p90_s, row.max_s) == (None, None, None, None)
    assert row.note.startswith("UNMEASURED")
    # Loudly, not by inheriting a neighbour's tail: this judge's own ceiling is
    # the family-wide last resort, which is a stated fallback, not a measurement.
    with pytest.raises(KeyError):
        judge_latency.call_floor_s("acceptance_judge")
    with pytest.raises(KeyError):
        judge_latency.call_ceiling_s("acceptance_judge")


def test_a_call_started_at_the_floor_could_have_finished_the_fastest_run():
    """The floor's own reachability: a floor below the fastest run ever measured
    would refuse to start calls that were in fact affordable, which is invisible
    recall loss (the judge is skipped, the hook fails open, nothing is logged as
    wrong). ceil(min) is the weakest form of that check that needs no estimator."""
    for judge in _MEASURED_JUDGES:
        row = judge_latency.row(judge)
        assert judge_latency.call_floor_s(judge) >= math.ceil(row.min_s), judge


# --- the hooks' constants against the table ----------------------------------

# (constant holder, attribute, judge, rule) for every per-call number in a
# judge-calling hook. The rule is the module function that must produce it —
# named, so a reader can check the arithmetic without reading the hook.
_DERIVED_CONSTANTS = [
    (_ESCALATION, "_JUDGE_MIN_CALL_S", "outage_escalation", judge_latency.call_floor_s),
    (_DEFERRING, "_ASK_JUDGE_MIN_CALL_S", "deferring_disposition", judge_latency.call_floor_s),
    (_APPROVAL, "_APPROVAL_ASK_JUDGE_MIN_CALL_S", "approval_ask", judge_latency.call_floor_s),
    # _APPROVAL_ASK_JUDGE_BUDGET_S is deliberately ABSENT from this table now.
    # It used to be listed here, tied by EQUALITY to call_ceiling_s("approval_ask")
    # — this hook's own claim, not a family rule, per the comment that used to
    # sit above this entry. That tie broke the tie's own reason for existing:
    # the ceiling was computed from a 32-call population, the population then
    # moved (a second sample taken after production timeouts ran entirely above
    # the first sample's max), and an equality-pinned budget would have had to
    # be re-derived and re-pinned on every such move with zero headroom in
    # between. The budget now sits ABOVE the ceiling instead, joining the other
    # two single-call hooks' `>=` shape — see
    # test_a_single_call_hooks_budget_is_never_what_truncates_its_call below
    # (still covers all three, unweakened) and
    # test_the_approval_ask_budgets_headroom_over_its_ceiling_is_real (which
    # pins the reason: the headroom itself, not a coincidence).
    (_TURN_END, "_TURN_FEEDBACK_MIN_CALL_S", "feedback_signal", judge_latency.call_floor_s),
    (_TURN_END, "_TURN_FEEDBACK_CALL_CAP_S", "feedback_signal", judge_latency.call_ceiling_s),
    (_TURN_END, "_TURN_BINARY_ASK_MIN_CALL_S", "binary_ask", judge_latency.call_floor_s),
    (_TURN_END, "_TURN_BINARY_ASK_CALL_CAP_S", "binary_ask", judge_latency.call_ceiling_s),
    (_TURN_END, "_TURN_OUTAGE_MIN_CALL_S", "outage_escalation", judge_latency.call_floor_s),
    (_TURN_END, "_TURN_OUTAGE_CALL_CAP_S", "outage_escalation", judge_latency.call_ceiling_s),
]


@pytest.mark.parametrize(
    "hook,attr,judge,rule",
    _DERIVED_CONSTANTS,
    ids=[f"{attr}" for _h, attr, _j, _r in _DERIVED_CONSTANTS],
)
def test_every_per_call_constant_equals_what_the_table_computes(hook, attr, judge, rule):
    assert getattr(hook, attr) == rule(judge), (
        f"{attr} must be {rule.__name__}({judge!r}) = {rule(judge)}"
    )


@pytest.mark.parametrize(
    "hook,attr,judge,rule",
    _DERIVED_CONSTANTS,
    ids=[f"{attr}" for _h, attr, _j, _r in _DERIVED_CONSTANTS],
)
def test_every_per_call_constant_moves_when_its_row_moves(hook, attr, judge, rule, monkeypatch):
    """The mutation proof for the test above, run as a test of its own: with the
    row replaced by a slower measurement, the rule must produce a DIFFERENT
    number than the hook holds. That is what makes the equality above a tie to
    THIS measurement rather than a tautology — a rule that returned its number
    without reading the row would satisfy the equality and fail here."""
    row = judge_latency.row(judge)
    slower = judge_latency.Row(
        judge=row.judge, n=row.n, min_s=row.min_s, median_s=row.median_s,
        p90_s=row.p90_s + 5, max_s=row.max_s + 5, provenance=row.provenance,
    )
    monkeypatch.setitem(judge_latency.rows(), judge, slower)
    assert rule(judge) != getattr(hook, attr)


def test_a_single_call_hooks_budget_is_never_what_truncates_its_call():
    """At the declared K=1 the whole-invocation budget IS the per-call ceiling,
    so the budget must clear the ceiling the table computes — otherwise the only
    call the hook makes is capped below the slowest run already observed and the
    ceiling rule is being applied to a number that cannot honour it."""
    single = {
        "hook-escalation-diagnosis-gate.py": _ESCALATION._JUDGE_BUDGET_S,
        "hook-deferring-disposition-gate.py": _DEFERRING._ASK_JUDGE_BUDGET_S,
        "hook-plan-delivery-gate.py": _APPROVAL._APPROVAL_ASK_JUDGE_BUDGET_S,
    }
    for hook, budget in single.items():
        sequence = judge_latency.HOOK_CALL_SEQUENCE[hook]
        assert hook_wiring.TIMEOUT_REQUIREMENT_CALLS[hook] == 1 == len(sequence)
        assert budget >= judge_latency.call_ceiling_s(sequence[0]), hook


def test_the_approval_ask_budgets_headroom_over_its_ceiling_is_real():
    """Pins the REASON _APPROVAL_ASK_JUDGE_BUDGET_S dropped its equality tie to
    call_ceiling_s, not just the `>=` fact test_a_single_call_hooks_budget_is_
    never_what_truncates_its_call already covers: the 9s gap between the 30s
    budget and the 21s ceiling this row currently computes must be headroom
    over a real measurement, not an artifact of a budget nobody re-checked
    against a moved row. A budget that happened to clear the ceiling only
    because the ceiling itself had drifted out from under it would pass the
    plain `>=` check just as happily — this is a headroom assertion, not a
    claim that the tail can never move again."""
    ceiling = judge_latency.call_ceiling_s("approval_ask")
    headroom = _APPROVAL._APPROVAL_ASK_JUDGE_BUDGET_S - ceiling
    assert headroom > 0, (
        f"budget {_APPROVAL._APPROVAL_ASK_JUDGE_BUDGET_S} must clear the "
        f"measured ceiling {ceiling}"
    )


def test_the_turn_end_budgets_own_floor_is_the_least_restrictive_of_its_three():
    """The budget object's constructor floor is a fallback for a future call site
    that forgets to name its judge's floor. It must be the SMALLEST of the three:
    a larger fallback would skip a call the remainder could in fact have carried."""
    floors = [judge_latency.call_floor_s(j)
              for j in judge_latency.HOOK_CALL_SEQUENCE["hook-turn-end-gate.py"]]
    assert _TURN_END._TURN_JUDGE_MIN_CALL_S == min(floors)


def test_the_last_resort_ceiling_is_the_family_maximum_plus_one():
    """The default on a judge signature covers a call with no harness timeout
    above it and no budget beside it, so it is the worst thing this model has
    been seen to do on ANY prompt — not the worst on one hook's prompt, and not
    that hook's budget (which also depends on its call count)."""
    slowest = max(r.max_s for r in judge_latency.rows().values() if r.measured)
    assert judge_latency.LAST_RESORT_CEILING_S == math.ceil(slowest) + 1
    for constant in (advisor._BINARY_ASK_TIMEOUT_S,
                     advisor._DEFERRING_DISPOSITION_TIMEOUT_S,
                     advisor._ACCEPTANCE_JUDGE_TIMEOUT_S,
                     advisor._APPROVAL_ASK_TIMEOUT_S,
                     advisor._LANDING_DISCIPLINE_LAST_RESORT_TIMEOUT_S):
        assert constant == judge_latency.LAST_RESORT_CEILING_S


def test_required_budget_covers_the_preceding_medians_and_the_last_floor():
    feedback, binary_ask, outage = (
        judge_latency.row("feedback_signal"),
        judge_latency.row("binary_ask"),
        judge_latency.row("outage_escalation"),
    )
    assert judge_latency.required_budget_s("hook-turn-end-gate.py") == pytest.approx(
        feedback.median_s + binary_ask.median_s
        + judge_latency.call_floor_s("outage_escalation")
        + judge_latency.SIZE_HEADROOM_S
    )
    # A one-call hook has no preceding calls to pay for: floor + headroom only.
    assert judge_latency.required_budget_s("hook-escalation-diagnosis-gate.py") == pytest.approx(
        judge_latency.call_floor_s("outage_escalation") + judge_latency.SIZE_HEADROOM_S
    )


# --- every judge call carries a timeout of its own ---------------------------

# The six judge entry points and the constant each one's default must be. Every
# one is called with an explicit timeout from inside a hook; the default is what
# a caller OUTSIDE a hook gets, and `test_advisor.py` reads it structurally.
_JUDGE_CALLS = {
    "judge_binary_ask": (lambda run: advisor.judge_binary_ask("Продолжаем?", run, enabled=True)),
    "judge_feedback_signal": (lambda run: advisor.judge_feedback_signal("не так", run, enabled=True)),
    "judge_outage_escalation": (lambda run: advisor.judge_outage_escalation("500 от API", run, enabled=True)),
    "judge_deferring_disposition": (lambda run: advisor.judge_deferring_disposition("меню", run, enabled=True)),
    "judge_landing_discipline_ask": (lambda run: advisor.judge_landing_discipline_ask("меню", run, enabled=True)),
    "acceptance_judge": (lambda run: advisor.acceptance_judge("наблюдение", "ожидание", run, enabled=True)),
}


@pytest.mark.parametrize("name", sorted(_JUDGE_CALLS))
def test_no_judge_call_rides_the_runners_own_default_timeout(name):
    """`subprocess_runner`'s default is `_ADVISOR_TIMEOUT_S` — the ADVISORY
    number, sized for the sonnet advisory calls. A judge that omitted its
    timeout would silently inherit it, which is how several judge calls came to
    be killed below their own fastest measured run."""
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return RunResult(0, "NO\nreason", "")

    _JUDGE_CALLS[name](run)
    assert "timeout" in seen, f"{name} passes no timeout at all"
    assert seen["timeout"] == judge_latency.LAST_RESORT_CEILING_S, (
        f"{name} must default to the family last-resort ceiling, not "
        f"{seen['timeout']}"
    )
    assert seen["argv"][seen["argv"].index("--model") + 1] == advisor._JUDGE_MODEL


@pytest.mark.parametrize(
    "name,expected_timeout",
    [
        ("enumerate_claims", "ENUMERATE_TIMEOUT_S"),
        ("enumerate_questions_health", "ENUMERATE_TIMEOUT_S"),
        ("judge", "_ADVISOR_TIMEOUT_S"),
    ],
)
def test_the_non_judge_advisory_calls_carry_the_advisory_timeout(name, expected_timeout):
    """The other half of the same defect: these three passed no timeout, so they
    ran on the runner's default by accident rather than by decision. They now name
    their ceiling explicitly, so moving a judge ceiling cannot move them and vice
    versa.

    The two enumeration entry points name ENUMERATE_TIMEOUT_S, not the advisory
    20s: a whole-plan enumeration is a different cost class from a binary judge,
    and the advisory number truncated every real call — the F3b defect. `judge`
    keeps the advisory number. What the two arms share, and what this test is
    actually for, is that each names its ceiling AT THE CALL SITE."""
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return RunResult(0, "", "")

    calls = {
        "enumerate_claims": lambda: advisor.enumerate_claims("текст", run),
        "enumerate_questions_health": lambda: advisor.enumerate_questions_health(
            "цель", "критерий", "план", run
        ),
        "judge": lambda: advisor.judge("weight_classification", {"x": 1}, run, enabled=True),
    }
    calls[name]()
    assert seen.get("timeout") == getattr(advisor, expected_timeout)


# --- the per-call floor is a per-call parameter -------------------------------

def test_remaining_and_timeout_takes_a_per_call_floor_and_falls_back_to_the_budgets():
    """The signature change stage 3 rests on: a hook calling three different
    judges on one budget needs a floor per judge, but the two hooks calling one
    judge must keep naming their floor once, at construction."""
    clock = iter([0.0, 0.0, 0.0, 0.0])
    budget = judge_budget.JudgeBudget(52, 12, clock=lambda: next(clock))
    # Named floor wins over the constructor's.
    assert budget.remaining_and_timeout(16, min_call_s=14)[1] == 16
    # Omitted -> the constructor's floor, and the cap still applies.
    assert budget.remaining_and_timeout(13)[1] == 13
