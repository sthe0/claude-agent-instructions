"""Stage 5 / item A: the cross-axis friction ceiling and the code-review axis's new
round-release valve (GitHub issue #96 — this axis previously had none at all).

Group 1 locks `gates.cross_axis_friction_release_active` directly — the SUM-not-max
behavior that closes the gap where plan-review and code-review each stayed under
their own threshold (2 + 2) while jointly exceeding it (4 >= 3), the shape real
session baa1daea hit. Group 2 locks the new `code_review_round_release_active` /
`code_review_blockers` wrap, mirroring test_plan_review_round_budget.py's Group 1.
Group 3 is the combined done-criterion scenario, exercised through the actual gate
functions rather than the bare primitive. Group 4 pins backward compatibility: a
session that never touches the new counter/bag behaves byte-identically to before
this stage on both axes."""
from __future__ import annotations

import pytest

from agentctl import gates
from agentctl.config import Thresholds
from agentctl.state import (
    Actor,
    CodeReview,
    Criterion,
    Means,
    Node,
    Outcome,
    PlanReview,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "1")


def _dev_stage(index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Edit", method="implement"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _subst(*, plan_review_rounds=0, code_review_rounds=0, code_reviews=(), plugins=None, **kw):
    return SessionState(
        session_id="s", task_id="t", weight_class="SUBSTANTIVE",
        plan_path="/plan.toml", plan_verified=True,
        stages=[_dev_stage()],
        plan_review_rounds=plan_review_rounds,
        code_review_rounds=code_review_rounds,
        code_reviews=list(code_reviews),
        plugins=plugins or {},
        **kw,
    )


def _review(verdict="revise", reviewer="code-reviewer", note="", code_sha256=""):
    return CodeReview(stage_index=1, verdict=verdict, reviewer=reviewer, note=note,
                       code_sha256=code_sha256)


# --- 1. cross_axis_friction_release_active: the bare predicate ----------------

def test_inactive_when_every_axis_is_zero():
    assert gates.cross_axis_friction_release_active(_subst()) is False


def test_inactive_when_every_axis_is_individually_below_and_sum_is_too():
    s = _subst(plan_review_rounds=1, code_review_rounds=1)
    assert gates.cross_axis_friction_release_active(s) is False


def test_active_on_the_sum_even_though_every_individual_axis_is_below_threshold():
    """The done-criterion scenario: 2 plan-review rounds + 2 code-review rounds, each
    individually under the threshold of 3, but the SUM (4) reaches it."""
    s = _subst(plan_review_rounds=2, code_review_rounds=2)
    assert gates.plan_review_round_release_active(s) is False
    assert gates.code_review_round_release_active(s) is False
    assert gates.cross_axis_friction_release_active(s) is True


def test_active_when_a_single_axis_alone_already_clears_it():
    s = _subst(plan_review_rounds=3)
    assert gates.cross_axis_friction_release_active(s) is True


def test_reads_the_plan_enumerate_axis_from_the_premise_plugin_bag():
    s = _subst(plan_review_rounds=1, code_review_rounds=1,
               plugins={"premise": {"enumerate_pass": 1}})
    assert gates.cross_axis_friction_release_active(s) is True


def test_missing_premise_bag_degrades_to_zero_for_that_axis():
    s = _subst(plan_review_rounds=1, code_review_rounds=1, plugins={})
    assert gates.cross_axis_friction_release_active(s) is False


def test_none_state_is_inactive():
    assert gates.cross_axis_friction_release_active(None) is False


def test_threshold_comes_from_config_not_a_literal():
    retuned = Thresholds({"effort-replan-absolute": "5"})
    s = _subst(plan_review_rounds=2, code_review_rounds=2)
    assert gates.cross_axis_friction_release_active(s, retuned) is False
    s2 = _subst(plan_review_rounds=3, code_review_rounds=2)
    assert gates.cross_axis_friction_release_active(s2, retuned) is True


# --- 2. code_review_round_release_active / code_review_blockers wrap ----------

def test_code_review_release_inactive_below_threshold(gates_on):
    assert gates.code_review_round_release_active(_subst(code_review_rounds=2)) is False


def test_code_review_release_active_at_and_past_threshold(gates_on):
    assert gates.code_review_round_release_active(_subst(code_review_rounds=3)) is True
    assert gates.code_review_round_release_active(_subst(code_review_rounds=4)) is True


def test_code_review_below_threshold_still_blocks_normally(gates_on):
    s = _subst(code_review_rounds=2, code_reviews=[_review("revise")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert b and "revise" in b[0]


def test_code_review_at_threshold_collapses_to_one_routing_message(gates_on):
    s = _subst(code_review_rounds=3, code_reviews=[_review("revise")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert len(b) == 1
    assert "no further code-reviewer pass is required" in b[0]
    assert "round budget exhausted at round 3" in b[0]
    assert "code-review --verdict override" in b[0]


def test_code_review_release_never_empties_the_blockers_list(gates_on):
    """Never auto-pass: the release replaces the wording, not the fact that
    record-result is still refused."""
    s = _subst(code_review_rounds=5, code_reviews=[_review("revise")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert b != []
    assert "at round 5" in b[0]


def test_code_review_release_wraps_the_missing_review_branch_too(gates_on):
    """Whichever sub-reason produced the blocker — no review at all here, a `revise`
    verdict elsewhere — collapses to the same one routing message once released."""
    s = _subst(code_review_rounds=3)  # no CodeReview recorded at all
    b = gates.code_review_blockers(s, s.stages[0])
    assert len(b) == 1 and "round budget exhausted" in b[0]


def test_code_review_a_recorded_pass_still_clears_regardless_of_rounds(gates_on):
    s = _subst(code_review_rounds=3, code_reviews=[_review("pass")])
    assert gates.code_review_blockers(s, s.stages[0]) == []


def test_code_review_release_threshold_comes_from_config_not_a_literal(gates_on):
    retuned = Thresholds({"effort-replan-absolute": "2"})
    assert gates.code_review_round_release_active(_subst(code_review_rounds=2), retuned) is True
    assert gates.code_review_round_release_active(_subst(code_review_rounds=1), retuned) is False


# --- 3. the combined scenario, through the real gate functions ----------------

def test_cross_axis_release_wraps_plan_review_blockers_even_though_solo_is_under(gates_on):
    s = _subst(plan_review_rounds=2, code_review_rounds=2,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    assert gates.plan_review_round_release_active(s) is False
    b = gates.plan_review_blockers(s, s.plan_path)
    assert len(b) == 1
    assert "no further thinker review is required" in b[0]
    # The message names the LIVE plan-review count, not the cross-axis sum.
    assert "round budget exhausted at round 2" in b[0]
    assert "released by the COMBINED cross-axis friction ceiling" in b[0]


def test_cross_axis_release_wraps_code_review_blockers_even_though_solo_is_under(gates_on):
    s = _subst(plan_review_rounds=2, code_review_rounds=2, code_reviews=[_review("revise")])
    assert gates.code_review_round_release_active(s) is False
    b = gates.code_review_blockers(s, s.stages[0])
    assert len(b) == 1
    assert "no further code-reviewer pass is required" in b[0]
    assert "round budget exhausted at round 2" in b[0]
    assert "released by the COMBINED cross-axis friction ceiling" in b[0]


def test_solo_axis_release_message_carries_no_cross_axis_footnote(gates_on):
    """When an axis's OWN count clears the threshold, the message must stay exactly
    what it was before the cross-axis ceiling existed — no footnote appended."""
    s = _subst(plan_review_rounds=3, code_review_rounds=0,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    b = gates.plan_review_blockers(s, s.plan_path)
    assert len(b) == 1
    assert "released by the COMBINED cross-axis friction ceiling" not in b[0]


# --- 4. backward compat: untouched sessions behave exactly as before ----------

def test_default_session_state_carries_zero_code_review_rounds():
    assert SessionState(session_id="s", task_id="t").code_review_rounds == 0


def test_legacy_session_without_code_review_rounds_key_loads_with_default():
    import json
    s = SessionState(session_id="s", task_id="t", code_review_rounds=2)
    raw = json.loads(s.to_json())
    raw.pop("code_review_rounds", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.code_review_rounds == 0


def test_plan_review_blockers_unaffected_when_no_axis_is_near_threshold(gates_on):
    s = _subst(plan_review_rounds=1, code_review_rounds=1,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    b = gates.plan_review_blockers(s, s.plan_path)
    assert len(b) == 1
    assert "round budget exhausted" not in b[0]


def test_code_review_blockers_unaffected_when_no_axis_is_near_threshold(gates_on):
    s = _subst(plan_review_rounds=1, code_review_rounds=1, code_reviews=[_review("revise")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert len(b) == 1
    assert "round budget exhausted" not in b[0]


# --- 5. diagnosing_replan: the customer-renegotiation round-release axis ------

def _diagnosing_state():
    return _subst(node=Node.DIAGNOSING.value)


def test_diagnosing_replan_below_threshold_is_empty():
    assert gates.diagnosing_replan_blockers(_diagnosing_state(), task_replan_count=2) == []


def test_diagnosing_replan_at_threshold_blocks():
    thr = Thresholds().effort_replan_absolute()
    b = gates.diagnosing_replan_blockers(_diagnosing_state(), task_replan_count=thr)
    assert len(b) == 1
    assert "renegotiation-decision" in b[0]


def test_diagnosing_replan_past_threshold_blocks():
    thr = Thresholds().effort_replan_absolute()
    b = gates.diagnosing_replan_blockers(_diagnosing_state(), task_replan_count=thr + 5)
    assert len(b) == 1


def test_diagnosing_replan_ignored_off_the_diagnosing_node():
    s = _subst(node="BLOCKED")
    thr = Thresholds().effort_replan_absolute()
    assert gates.diagnosing_replan_blockers(s, task_replan_count=thr + 5) == []


def test_diagnosing_replan_none_state_is_empty():
    thr = Thresholds().effort_replan_absolute()
    assert gates.diagnosing_replan_blockers(None, task_replan_count=thr) == []


def test_diagnosing_replan_round_release_active_threshold_comes_from_config():
    thr = Thresholds().effort_replan_absolute()
    assert gates.diagnosing_replan_round_release_active(thr - 1) is False
    assert gates.diagnosing_replan_round_release_active(thr) is True


def test_diagnosing_replan_reset_then_reclimb_refires_independently():
    """Proves the gate re-fires on a SECOND, independent Rule-of-Three crossing after
    stage 3's task_accumulator.reset() zeroes the live counter, rather than
    remembering the first decision was already made."""
    thr = Thresholds().effort_replan_absolute()
    state = _diagnosing_state()

    first = gates.diagnosing_replan_blockers(state, task_replan_count=thr)
    assert len(first) == 1

    after_reset = gates.diagnosing_replan_blockers(state, task_replan_count=0)
    assert after_reset == []

    second = gates.diagnosing_replan_blockers(state, task_replan_count=thr)
    assert len(second) == 1
