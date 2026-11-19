"""The stage as an order and the executor's sub-plan record.

A stage describes the product, its place with reliance, the acceptance checks and the binding
conditions, and states no means, method or procedure: those are the executor's sub-plan record
(`agentctl.sub_plan`), which one reader reads from either carrier (a pushed frame, a linked
child's contour). This module pins the model (section 1), the submission checks over it and
their carriers (section 2) and the stage-description review (section 3).

A stage whose declared size is below the line classify draws needs only its description
(`test_below_threshold_stage_needs_only_description`).

Each guard test goes red under a planted defect in `order_agreement_mutants.py`:
`size_predicate_hardcoded`, `stage_order_checks_unchecked`, `stage_description_rule_unwired`.
"""
from __future__ import annotations

import json

import pytest

from agentctl import classify, plan, sub_plan
from agentctl.config import Thresholds
from agentctl.plan import PlanError, parse_plan, stage_above_threshold
from agentctl.state import (
    Actor, Criterion, Means, Outcome, SessionState, Stage, Subject,
)
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

TIGHT = Thresholds({
    "small-change-max-lines": "50",
    "substantive-wall-clock-min": "10",
    "effort-stage-minutes-small": "5",
    "effort-stage-minutes-medium": "8",
    "effort-stage-minutes-large": "20",
})


def _stage(index=1, *, tier=None, **kw) -> Stage:
    return Stage(
        index=index, title=f"stage {index}",
        subject=Subject(material="m", result="the product"),
        means=Means(means="", method=""), actor=Actor(executor="spawn:developer", cost_tier=tier),
        criterion=Criterion(criterion_type="measurable", done_criterion="the check passes"),
        outcome=Outcome(), **kw)


# --- 1. The model -----------------------------------------------------------------------

def test_a_stage_without_a_description_loads_as_old_form():
    state = SessionState(session_id="s", task_id="t", stages=[_stage(1)])
    raw = json.loads(state.to_json())
    for key in ("place", "binding_conditions", "declared_size", "size_flags", "expected_minutes"):
        raw["stages"][0].pop(key)
    raw.pop("sub_plan_records")
    back = SessionState.from_json(json.dumps(raw))
    stage = back.stages[0]
    assert (stage.place, stage.binding_conditions, stage.declared_size, stage.size_flags,
            stage.expected_minutes) == (None, [], None, [], None)
    assert back.sub_plan_records == {}


def test_the_stage_description_survives_a_state_round_trip():
    stage = _stage(1, place="the engine, relied on by stage 2",
                   binding_conditions=[{"id": "B1", "text": "no push", "set_by": "user"}],
                   declared_size=120, size_flags=["architectural"], expected_minutes=45)
    back = SessionState.from_json(SessionState(session_id="s", task_id="t", stages=[stage]).to_json())
    assert back.stages[0] == stage


def _raw_plan(stage_extra: dict, *, weight=None) -> dict:
    meta = {"task_id": "t", "goal": "g", "done_criterion": "d"}
    if weight:
        meta.update(weight_class=weight, external_research="not warranted")
    stage = {"title": "s", "executor": "in_thread", "expected_result_image": "the product",
             "done_criterion": "the check passes", **stage_extra}
    return {"meta": meta, "stage": [stage]}


def test_the_loader_reads_the_stage_description():
    doc = parse_plan(_raw_plan({
        "place": "the engine, relied on by stage 2", "declared_size": 120,
        "size_flags": ["architectural"], "expected_minutes": 45,
        "binding_conditions": [{"id": "B1", "text": "no push", "set_by": "user"}, "bare"],
    }))
    stage = doc.stages[0]
    assert stage.place == "the engine, relied on by stage 2"
    assert stage.binding_conditions == [{"id": "B1", "text": "no push", "set_by": "user"}]
    assert (stage.declared_size, stage.size_flags, stage.expected_minutes) == (
        120, ["architectural"], 45)


def test_the_strict_loader_leaves_means_to_the_executor_of_a_described_stage():
    described = {"place": "the engine", "material": "m", "invariants": "i",
                 "capability_required": "c", "verify_command": "true",
                 "principle": {"statement": "s1", "source": "src", "derivation": "d1",
                               "confidence": "high", "refutation": "r1"}}
    parse_plan(_raw_plan(described, weight="substantive"), strict=True)
    old_form = {k: v for k, v in described.items() if k != "place"}
    with pytest.raises(PlanError, match="missing 'means'"):
        parse_plan(_raw_plan(old_form, weight="substantive"), strict=True)


def test_the_description_is_a_part_of_the_stage_change_keys():
    base, edited = _stage(1, place="a"), _stage(1, place="b")
    assert plan.description_place(_stage(1)) == ()
    assert plan.description_place(base) != plan.description_place(edited)


@pytest.mark.parametrize("size, flags, minutes, above", [
    (60, [], None, True),         # past small-change-max-lines=50
    (50, [], None, False),
    (10, [], 10, True),           # substantive-wall-clock-min=10 reached
    (10, [], 9, False),
    (10, ["architectural"], 5, True),
    (10, ["not cohesive"], 5, True),
    (10, ["external effect"], 5, True),
    (10, ["new dependency"], 5, True),
    (10, ["public api change"], 5, True),
    (10, ["something else"], 5, False),
    (None, [], None, False),
])
def test_above_threshold_is_classify_under_the_given_thresholds(size, flags, minutes, above):
    assert classify.above_threshold(size, flags, minutes, TIGHT) is above


def test_the_line_moves_with_the_thresholds_it_is_given():
    default = Thresholds()
    assert classify.above_threshold(60, [], 5, TIGHT)
    assert not classify.above_threshold(60, [], 5, default)


def test_a_stage_declaring_no_minutes_is_judged_on_its_cost_tier():
    assert stage_above_threshold(_stage(tier="large", place="p"), TIGHT)
    assert not stage_above_threshold(_stage(tier="small", place="p"), TIGHT)
    assert not stage_above_threshold(_stage(tier="large", place="p", expected_minutes=2), TIGHT)


def test_the_stage_predicate_hands_the_declared_size_to_classify(monkeypatch):
    seen = []
    monkeypatch.setattr(classify, "above_threshold",
                        lambda *a: seen.append(a) or True, raising=True)
    stage = _stage(place="p", declared_size=7, size_flags=["new dependency"], expected_minutes=3)
    assert stage_above_threshold(stage, TIGHT) is True
    assert seen == [(7, ["new dependency"], 3, TIGHT)]


# The sub-plan record ---------------------------------------------------------------------

_META = {
    "sub_plan": {
        "stage": 2, "means": "Edit", "method": "split the module", "procedure": "1. read 2. edit",
        "conditions": "- the module imports [probe: python3 -c 'import m' => ok]",
        "estimate": {"minutes": 90, "usd": 3.0, "author": "executor", "confirmer": "parent"},
        "setter_decisions": [{"condition": "B1", "value": "kept", "setter": "user",
                              "decided_by": "user"}],
        "resource_request": [{"stage": 2, "allow": ["Edit"]}],
        "parent_resource_approval": "approved", "user_subplan_approval": "",
    },
    "compared_plans": {"entries": [{"ref": "plan-a", "departure": "no migration"}]},
    "how_kept": [{"condition": "B1", "how": "no push verb is called"}],
}


def test_the_record_reads_from_the_plan_meta():
    record = sub_plan.from_meta(_META)
    assert record.stage == 2 and record.method == "split the module"
    assert record.estimate.agreed and record.estimate.minutes == 90
    assert record.compared_plans.entries == [{"ref": "plan-a", "departure": "no migration"}]
    assert [(h.condition, h.how) for h in record.how_kept] == [("B1", "no push verb is called")]
    assert record.setter_decisions[0].value == "kept" and record.setter_decisions[0].objection == ""
    assert record.malformed == ()


def test_a_plan_without_a_sub_plan_table_has_no_record():
    assert sub_plan.from_meta({"task_id": "t"}) is None


def test_the_plan_loader_carries_the_record_on_the_meta():
    raw = _raw_plan({}, weight=None)
    raw["meta"].update(_META)
    assert parse_plan(raw).meta.sub_plan.stage == 2


def test_a_malformed_record_degrades_and_says_which_part():
    record = sub_plan.from_meta({"sub_plan": {"stage": "two", "estimate": "soon",
                                              "how_kept": ["bare"], "building_place": 3}})
    assert record.stage == 0 and record.estimate is None and record.how_kept == []
    assert set(record.malformed) == {"stage", "estimate", "how_kept", "building_place"}
    assert sub_plan.from_meta({"sub_plan": "text"}).malformed == ("sub_plan",)


def test_the_record_round_trips_through_its_stored_form():
    record = sub_plan.from_meta(_META)
    record.building_place = sub_plan.BuildingPlace(session="s", pushed=4, popped=None)
    assert sub_plan.SubPlanRecord.from_dict(record.to_dict()) == record


def test_a_review_or_an_objection_does_not_change_the_digest_and_an_edit_does():
    record = sub_plan.from_meta(_META)
    before = sub_plan.digest(record)
    record.reviews.append({"verdict": "pass"})
    record.objections.append({"id": "O1", "text": "too slow"})
    assert sub_plan.digest(record) == before
    record.method = "a different method"
    assert sub_plan.digest(record) != before


def test_an_agreed_estimate_rewritten_with_the_same_figure_changes_nothing():
    record = sub_plan.from_meta(_META)
    assert sub_plan.agree_estimate(record, sub_plan.Estimate(90, 3.0, "someone", "someone")) is False
    assert record.estimate.author == "executor"
    with pytest.raises(sub_plan.SubPlanError, match="agreed"):
        sub_plan.agree_estimate(record, sub_plan.Estimate(120, 3.0, "executor", "parent"))
    unagreed = sub_plan.SubPlanRecord(stage=1, estimate=sub_plan.Estimate(10, None, "executor"))
    assert sub_plan.agree_estimate(unagreed, sub_plan.Estimate(20, None, "executor")) is True
    assert unagreed.estimate.minutes == 20


def test_a_lazy_stage_is_the_one_whose_sub_plan_is_decided_later():
    assert sub_plan.is_decided_later(_stage(lazy=True))
    assert not sub_plan.is_decided_later(_stage())


def _session(session_id, **kw) -> SessionState:
    return SessionState(session_id=session_id, task_id="t", **kw)


class _Store:
    """The slice of the state store `read` needs: sessions by id, listed with their links."""

    def __init__(self, *states):
        self.states = {s.session_id: s for s in states}

    def session_ids(self):
        return list(self.states)

    def load(self, session_id):
        return self.states[session_id]


def test_one_reader_reads_a_frames_record_and_a_child_contours_record():
    record = sub_plan.from_meta(_META)
    own = _session("parent")
    sub_plan.store(own, record)
    assert sub_plan.read(own, 2).method == "split the module"
    assert sub_plan.read(own, 3) is None

    parent = _session("parent")
    child = _session("child", parent_link={"parent_session": "parent"})
    grandchild = _session("grandchild", parent_link={"parent_session": "child"})
    sub_plan.store(grandchild, record)
    store = _Store(parent, child, grandchild)
    assert sub_plan.read(parent, 2, store).method == "split the module"
    assert sub_plan.read(parent, 2) is None


def test_subplan_review_record_by_stage():
    state = _session("s")
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m2"))
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=3, method="m3"))
    sub_plan.add_review(state, 2, {"verdict": "revise"})
    assert state.sub_plan_records["2"]["reviews"] == [{"verdict": "revise"}]
    assert state.sub_plan_records["3"]["reviews"] == []
    with pytest.raises(sub_plan.SubPlanError):
        sub_plan.add_review(state, 9, {"verdict": "pass"})


def test_a_rewritten_record_keeps_its_reviews():
    state = _session("s")
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m"))
    sub_plan.add_review(state, 2, {"verdict": "pass"})
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m, revised"))
    assert sub_plan.read(state, 2).reviews == [{"verdict": "pass"}]
