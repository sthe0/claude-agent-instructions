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
from types import SimpleNamespace

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


TWO_SIDED_ORDER = {
    "customer_id": "user", "customer": "the position that posed the task",
    "functional_place": "the norm governing the act",
    "requirements": [{"id": "R1", "text": "the product works", "derivation": "the task"}],
    "coverage": {"R1": ["stage 1 verify_command"], "A1": ["stage 1 verify_command"]},
    "customer_side": {
        "step": "cutting a release", "missing": "a command that tags it",
        "product": "the release command", "criterion_type": "measurable",
        "checks": [{"id": "A1", "kind": "direct", "text": "the command tags a release"}],
    },
}


AGREED_OLD_FORM_ORDER = {
    **{k: v for k, v in TWO_SIDED_ORDER.items() if k != "customer_side"},
    "coverage": {"R1": ["stage 1 verify_command"]},
}


def _raw_plan(stage_extra: dict, *, weight=None, order=None, drop=()) -> dict:
    """A one-stage plan; `order` is the `[meta.order]` table (a plan with none is on no order),
    `drop` the stage keys to leave out."""
    meta = {"task_id": "t", "goal": "g", "done_criterion": "d"}
    if weight:
        meta.update(weight_class=weight, external_research="not warranted")
    if order is not None:
        meta["order"] = json.loads(json.dumps(order))
    stage = {"title": "s", "executor": "in_thread", "expected_result_image": "the product",
             "done_criterion": "the check passes", **stage_extra}
    for key in drop:
        stage.pop(key, None)
    return {"meta": meta, "stage": [stage]}


def test_the_loader_reads_the_stage_description():
    doc = parse_plan(_raw_plan({
        "place": "the engine, relied on by stage 2", "declared_size": 120,
        "size_flags": ["architectural"], "expected_minutes": 45,
        "binding_conditions": [{"id": "B1", "text": "no push", "set_by": "user"}, "bare"],
    }))
    stage = doc.stages[0]
    assert stage.place == "the engine, relied on by stage 2"
    assert stage.binding_conditions == [{"id": "B1", "text": "no push", "set_by": "user"},
                                        {"id": "", "text": "", "set_by": "", "not_a_table": True}], \
        "a row that is not a table stays, blank and marked, for submission to refuse"
    assert stage.binding_conditions_stated
    assert (stage.declared_size, stage.size_flags, stage.expected_minutes) == (
        120, ["architectural"], 45)
    assert not parse_plan(_raw_plan({})).stages[0].binding_conditions_stated


def test_the_strict_loader_leaves_means_to_the_executor_on_a_two_sided_order():
    described = {"place": "the engine", "material": "m", "invariants": "i",
                 "capability_required": "c", "verify_command": "true",
                 "principle": {"statement": "s1", "source": "src", "derivation": "d1",
                               "confidence": "high", "refutation": "r1"}}
    parse_plan(_raw_plan(described, weight="substantive", order=TWO_SIDED_ORDER), strict=True)
    with pytest.raises(PlanError, match="missing 'means'"):
        parse_plan(_raw_plan(described, weight="substantive"), strict=True)


def test_the_form_of_a_stage_follows_the_order_not_the_place():
    """A two-sided order leaves means to the executor even for a stage naming no place; a stage
    naming a place on an order-less plan still owes its means."""
    bare = {"material": "m", "invariants": "i", "capability_required": "c", "verify_command": "true",
            "principle": {"statement": "s1", "source": "src", "derivation": "d1",
                          "confidence": "high", "refutation": "r1"}}
    parse_plan(_raw_plan(bare, weight="substantive", order=TWO_SIDED_ORDER), strict=True)
    with pytest.raises(PlanError, match="missing 'means'"):
        parse_plan(_raw_plan({**bare, "place": "the engine"}, weight="substantive"), strict=True)


def test_the_description_is_a_part_of_the_stage_change_keys():
    base, edited = _stage(1, place="a"), _stage(1, place="b")
    assert plan.description_place(_stage(1)) == ()
    assert plan.description_place(base) != plan.description_place(edited)


_OLD_FORM_STAGE = {"means": "the tools", "method": "write it", "procedure": "run it",
                   "conditions": "- the tools exist [risk: none]"}
_DESCRIPTION_STAGE = {"place": "the engine", "binding_conditions": [], "declared_size": 20}


def _form_change(described_extra=None):
    old = parse_plan(_raw_plan(_OLD_FORM_STAGE))
    new = parse_plan(_raw_plan({**_DESCRIPTION_STAGE, **(described_extra or {})}))
    return old, new


def test_putting_a_stage_into_the_description_form_moves_no_leaf_of_its_work():
    """The form takes the executor's parts out of the stage and brings the description in; the
    stage's work is the same, so the change moves nothing and the carry decision keeps a PASSED
    verdict."""
    from agentctl.stage_norm import stage_carried

    old, new = _form_change()
    assert plan.change_tier(old, new).stages[1].moved == ()
    assert stage_carried(old.stages, new.stages, 1)


def test_a_stage_put_into_the_description_form_with_another_result_still_moves():
    from agentctl.stage_norm import stage_carried

    old, new = _form_change({"expected_result_image": "another product"})
    moved = [path for path, _ in plan.change_tier(old, new).stages[1].moved]
    assert moved == ["subject.result"]
    assert not stage_carried(old.stages, new.stages, 1)


def test_a_described_stage_edited_within_its_form_still_moves_its_description():
    old = parse_plan(_raw_plan(_DESCRIPTION_STAGE))
    new = parse_plan(_raw_plan({**_DESCRIPTION_STAGE, "place": "the seam"}))
    assert [p for p, _ in plan.change_tier(old, new).stages[1].moved] == ["place"]


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
    assert stage_above_threshold(_stage(tier="large", place="p", declared_size=1), TIGHT)
    assert not stage_above_threshold(_stage(tier="small", place="p", declared_size=1), TIGHT)
    assert not stage_above_threshold(_stage(tier="large", place="p", expected_minutes=2), TIGHT)


def test_a_new_form_stage_declaring_no_size_is_above_the_line():
    """No lines, no flag and no minutes is no declaration, never a small stage."""
    assert stage_above_threshold(_stage(tier="small", place="p"), TIGHT)
    assert stage_above_threshold(_stage(tier="small", place="p"), Thresholds())
    assert not stage_above_threshold(_stage(tier="small", place="p", declared_size=1), TIGHT)


def test_an_old_form_stage_is_never_above_the_line():
    """An old-form stage states no size, which is not a declaration of none: it has no record to
    make, under the default thresholds and the tight ones, with or without a cost tier."""
    for thr in (Thresholds(), TIGHT):
        for tier in (None, "small", "large"):
            assert stage_above_threshold(_stage(tier=tier), thr) is False


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


def _frame(originating_stage, stages=()):
    """The slice of a `PlanFrame` the readers use: the stage it was pushed for and the stages of
    the plan below it."""
    return SimpleNamespace(originating_stage=originating_stage, stages=list(stages))


def test_one_reader_reads_a_frames_record_and_a_child_contours_record():
    record = sub_plan.from_meta(_META)
    own = _session("parent")
    sub_plan.store(own, record, level=0)
    assert sub_plan.read(own, 2).method == "split the module"
    assert sub_plan.read(own, 3) is None

    parent = _session("parent")
    linked = _session("child", parent_link={"parent_session": "parent", "stage_index": 2})
    sub_plan.store(linked, record)
    store = _Store(parent, linked)
    assert sub_plan.read(parent, 2, store).method == "split the module"
    assert sub_plan.read(parent, 2) is None
    assert sub_plan.read(parent, 3, store) is None


def test_the_reader_follows_the_stage_link_and_never_reaches_a_deeper_session():
    record = sub_plan.from_meta(_META)
    parent = _session("parent")
    elsewhere = _session("elsewhere", parent_link={"parent_session": "parent", "stage_index": 5})
    sub_plan.store(elsewhere, sub_plan.SubPlanRecord(stage=5, method="built for stage 5"))
    sub_plan.store(elsewhere, record)
    grandchild = _session("grandchild", parent_link={"parent_session": "elsewhere", "stage_index": 2})
    sub_plan.store(grandchild, record)
    store = _Store(parent, elsewhere, grandchild)
    assert sub_plan.read(parent, 2, store) is None, "a child linked at another stage, a grandchild"
    assert sub_plan.read(parent, 5, store).method == "built for stage 5"


def test_two_children_linked_at_one_stage_have_no_one_record_to_read():
    parent = _session("parent")
    first = _session("first", parent_link={"parent_session": "parent", "stage_index": 2})
    second = _session("second", parent_link={"parent_session": "parent", "stage_index": 2})
    sub_plan.store(first, sub_plan.SubPlanRecord(stage=2, method="built by the first"))
    store = _Store(parent, first, second)
    assert sub_plan.read(parent, 2, store).method == "built by the first", "one record is no conflict"
    sub_plan.store(second, sub_plan.SubPlanRecord(stage=2, method="built by the second"))
    with pytest.raises(sub_plan.SubPlanError, match="first, second"):
        sub_plan.read(parent, 2, store)


def test_nested_frames_keep_one_record_each_and_the_reader_asks_its_own_level():
    upper = sub_plan.SubPlanRecord(stage=1, method="built in the first frame")
    lower = sub_plan.SubPlanRecord(stage=1, method="built in the nested frame")
    state = _session("s", plan_stack=[_frame(1), _frame(1)])
    sub_plan.store(state, upper, level=0)
    sub_plan.store(state, lower, level=1)
    assert set(state.sub_plan_records) == {"0:1", "1:1"}, "the second store did not overwrite"
    assert sub_plan.read(state, 1) is None, "the plan at level 2 owns no record of its stage 1 yet"
    state.plan_stack.pop()
    assert sub_plan.read(state, 1).method == "built in the nested frame"
    state.plan_stack.pop()
    assert sub_plan.read(state, 1).method == "built in the first frame"


def test_a_contour_record_is_keyed_apart_from_a_frame_record_of_the_same_stage():
    state = _session("s", parent_link={"parent_session": "p", "stage_index": 2})
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="contour"))
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="frame"), level=0)
    assert set(state.sub_plan_records) == {"2", "0:2"}


def test_subplan_review_record_by_stage():
    state = _session("s")
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m2"), level=0)
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=3, method="m3"), level=0)
    sub_plan.add_review(state, 2, {"verdict": "revise"}, level=0)
    assert state.sub_plan_records["0:2"]["reviews"] == [{"verdict": "revise"}]
    assert state.sub_plan_records["0:3"]["reviews"] == []
    with pytest.raises(sub_plan.SubPlanError):
        sub_plan.add_review(state, 9, {"verdict": "pass"}, level=0)


def test_a_rewritten_record_keeps_its_reviews():
    state = _session("s")
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m"), level=0)
    sub_plan.add_review(state, 2, {"verdict": "pass"}, level=0)
    sub_plan.store(state, sub_plan.SubPlanRecord(stage=2, method="m, revised"), level=0)
    assert sub_plan.read(state, 2).reviews == [{"verdict": "pass"}]


# --- 2. The submission checks and their two carriers ------------------------------------------

from argparse import Namespace  # noqa: E402

from agentctl import cli, cli_common, submission  # noqa: E402
from agentctl.state import (  # noqa: E402
    FinalCheck, GateRecord, Node, Partition, Route, StageStatus, WeightClass,
)
from agentctl.submission import StageOrderContext  # noqa: E402

BINDING = ({"id": "B1", "text": "no push", "set_by": "user"},)


def _doc(stage_extra=None, record=None, *, order=None, drop=()):
    """A one-stage plan carrying a stage description (and, on a `order`, in the new form)."""
    raw = _raw_plan({"place": "the engine", "binding_conditions": [], **(stage_extra or {})},
                    order=order, drop=drop)
    if record is not None:
        raw["meta"].update(record)
    return parse_plan(raw)


def _new_form(stage_extra=None, record=None, *, drop=()):
    return _doc(stage_extra, record, order=TWO_SIDED_ORDER, drop=drop)


def _problems(doc, ctx=None):
    return submission.submission_violations(doc, stage_order=ctx)


def _ctx(**kw):
    return StageOrderContext(expected_stage=2, binding_conditions=BINDING, **kw)


def _record(**edits):
    meta = json.loads(json.dumps(_META))
    for key, value in edits.items():
        if key in ("compared_plans", "how_kept"):
            meta[key] = value
        elif value is None:
            meta["sub_plan"].pop(key, None)
        else:
            meta["sub_plan"][key] = value
    return meta


def test_a_described_stage_that_states_a_method_is_refused():
    problems = _problems(_new_form({"means": "Edit", "method": "split it"}))
    assert any("stage 1 is a stage description and carries no means, method" in p
               for p in problems), problems


def test_a_described_stage_that_states_conditions_is_refused():
    problems = _problems(_new_form({"conditions": "- the module imports [risk: unverified]"}))
    assert any("carries no conditions" in p for p in problems), problems


def test_a_description_alone_draws_no_stage_order_violation():
    assert submission._stage_order_violations(_new_form(), _ctx()) == []


def test_an_old_form_plan_draws_no_stage_order_violation():
    stage = {"means": "Edit", "method": "m"}
    agreed_old_form = parse_plan(_raw_plan(stage, order=AGREED_OLD_FORM_ORDER))
    assert not plan.is_two_sided(agreed_old_form.meta.order)
    assert submission._stage_order_violations(agreed_old_form, _ctx()) == []
    assert submission._stage_order_violations(parse_plan(_raw_plan(stage)), _ctx()) == []
    assert submission._stage_order_violations(
        _doc(stage, order=AGREED_OLD_FORM_ORDER), _ctx()) == []
    two_sided = parse_plan(_raw_plan(stage, order=TWO_SIDED_ORDER))
    assert submission._stage_order_violations(two_sided, _ctx()), "the same stage on a two-sided order is refused"


_MISSING_PARTS = [
    ("product", {"expected_result_image": " "}, (), "states no product"),
    ("place", {"place": " "}, (), "states no place"),
    ("place left out", {}, ("place",), "states no place"),
    ("acceptance checks", {"done_criterion": " "}, (), "states no acceptance checks"),
    ("binding conditions", {}, ("binding_conditions",), "states no binding conditions"),
]


@pytest.mark.parametrize("name, extra, drop, expected", _MISSING_PARTS,
                         ids=[c[0] for c in _MISSING_PARTS])
def test_a_missing_part_of_a_stage_description_is_refused(name, extra, drop, expected):
    problems = submission._stage_order_violations(_new_form(extra, drop=drop), _ctx())
    assert [p for p in problems if expected in p] and len(problems) == 1, problems


def test_the_form_of_a_stage_is_the_orders_choice_not_the_places():
    """A stage that names no place is still a stage description on a two-sided order, and a stage
    that names one is not on an order-less plan."""
    problems = submission._stage_order_violations(_new_form(drop=("place",)), _ctx())
    assert any("states no place" in p for p in problems), problems
    assert submission._stage_order_violations(_doc(drop=("place",)), _ctx()) == []
    assert submission._stage_order_violations(_doc({"place": "the engine"}), _ctx()) == []


def test_an_empty_binding_condition_list_is_a_statement_not_a_missing_part():
    assert submission._stage_order_violations(_new_form({"binding_conditions": []}), _ctx()) == []
    assert submission._stage_order_violations(
        _new_form({"binding_conditions": [dict(c) for c in BINDING]}), _ctx()) == []


_OK_ROW = {"id": "B1", "text": "no push", "set_by": "user"}
_ROW_REFUSALS = [
    ("a row that is not a table", ["bare"], "binding condition #1 is not a table"),
    ("a row with no id", [{"text": "no push", "set_by": "user"}], "binding condition #1 has no 'id'"),
    ("a row with a blank text", [{**_OK_ROW, "text": "  "}], "binding condition B1 has no 'text'"),
    ("a row with a blank set_by", [{**_OK_ROW, "set_by": ""}], "binding condition B1 has no 'set_by'"),
    ("a row whose id is not text", [{**_OK_ROW, "id": 7}], "binding condition #1 has no 'id'"),
    ("a repeated id", [_OK_ROW, {**_OK_ROW, "text": "other"}], "binding condition id(s) b1 repeat"),
    ("an id repeated in another case", [_OK_ROW, {**_OK_ROW, "id": "b1"}],
     "binding condition id(s) b1 repeat"),
]


@pytest.mark.parametrize("name, rows, expected", _ROW_REFUSALS, ids=[r[0] for r in _ROW_REFUSALS])
def test_a_malformed_stage_binding_condition_row_is_refused(name, rows, expected):
    problems = submission._stage_order_violations(
        _new_form({"binding_conditions": rows}), _ctx())
    assert [p for p in problems if expected in p and p.startswith("stage 1 ")], problems


def test_binding_condition_rows_are_checked_only_on_a_described_stage_of_a_two_sided_order():
    malformed = {"binding_conditions": ["bare", {"text": "x"}]}
    assert submission._stage_order_violations(_doc(malformed, order=AGREED_OLD_FORM_ORDER),
                                              _ctx()) == []
    assert submission._stage_order_violations(_doc(malformed), _ctx()) == []
    assert submission._stage_order_violations(_new_form({"binding_conditions": []}), _ctx()) == []
    assert not any("binding condition" in p for p in submission._stage_order_violations(
        _new_form(drop=("binding_conditions",)), _ctx()) if "no binding condition" not in p)


def _sized_plan(*sizes):
    """One in-thread described stage per entry: a line count, or (line count, size flags)."""
    declared = [(size, []) if not isinstance(size, tuple) else (size[0], list(size[1]))
                for size in sizes]
    stages = [{"title": f"s{n}", "executor": "in_thread", "expected_result_image": "the product",
               "done_criterion": "the check passes", "place": "the engine",
               "binding_conditions": [], "declared_size": size, "expected_minutes": 2,
               "size_flags": flags} for n, (size, flags) in enumerate(declared, 1)]
    raw = _raw_plan({})
    raw["stage"] = stages
    raw["meta"]["order"] = json.loads(json.dumps(TWO_SIDED_ORDER))
    return parse_plan(raw)


def _above(doc, max_lines):
    thr = Thresholds({**TIGHT._c, "small-change-max-lines": str(max_lines)})
    return [plan.stage_above_threshold(s, thr) for s in doc.stages]


def test_below_threshold_stage_needs_only_description():
    """Of three stages — a large one, a flagged one and a small one (400 lines, 5 lines with the
    `architectural` flag, 60 lines) — the ones above the size line follow the threshold, and none
    of them, above or below it, is asked a record, a means or an estimate at submission."""
    doc = _sized_plan(400, (5, ["architectural"]), 60)
    assert _above(doc, 500) == [False, True, False]
    assert _above(doc, 100) == [True, True, False], "the third stage is below a line of 100"
    assert _above(doc, 10) == [True, True, True], "and above a line of 10"
    assert _problems(doc) == []
    assert _problems(doc, _ctx()) == []
    below = _sized_plan(5, 40, 50)
    assert _above(below, 50) == [False, False, False]
    assert _problems(below) == []
    assert _problems(below, _ctx()) == []

    refused = _problems(_new_form({"declared_size": 5}, drop=("place",)), _ctx())
    assert any("states no place" in p for p in refused), refused


def test_the_size_line_moves_with_the_threshold_and_a_larger_stage_is_asked_no_record_either():
    """Of three stages (5, 60 and 400 lines) the ones above the line follow the small-change
    threshold; submission asks none of them for a record — that is decided when its stage runs."""
    doc = _sized_plan(5, 60, 400)
    assert _above(doc, 500) == [False, False, False]
    assert _above(doc, 100) == [False, False, True]
    assert _above(doc, 10) == [False, True, True]
    assert _above(doc, 1) == [True, True, True]
    assert _problems(doc) == []
    assert _problems(doc, _ctx()) == []


def test_a_new_form_stage_declaring_no_size_is_above_the_line_at_submission():
    stage = _new_form().stages[0]
    assert plan.stage_above_threshold(stage, Thresholds())
    assert not plan.stage_above_threshold(_new_form({"declared_size": 5}).stages[0], Thresholds())


def test_an_unknown_size_flag_is_refused_at_submission():
    problems = submission._stage_order_violations(
        _new_form({"declared_size": 5, "size_flags": ["architectural", "huge"]}), _ctx())
    assert len(problems) == 1 and "size flag(s) 'huge'" in problems[0], problems
    assert submission._stage_order_violations(
        _new_form({"declared_size": 5, "size_flags": ["Architectural", "new dependency"]}),
        _ctx()) == []


def test_the_record_presence_rule_is_not_asked_before_approval():
    """No verb makes a record before a stage runs, so an approve that asked for one would refuse
    every plan: a stage above the line, lazy or not, draws nothing here."""
    for extra in ({"declared_size": 400}, {"declared_size": 400, "lazy": True}, {}):
        assert submission._stage_order_violations(_new_form(extra), _ctx()) == []


def test_a_lazy_stage_is_the_one_whose_record_is_decided_later_in_a_plan_too():
    assert sub_plan.is_decided_later(_new_form({"lazy": True}).stages[0])
    assert not sub_plan.is_decided_later(_new_form().stages[0])


def test_a_complete_record_is_clean():
    assert submission._stage_order_violations(_doc(record=_record()), _ctx()) == []


_REFUSALS = [
    ("method", {"method": ""}, "states no method"),
    ("means", {"means": " "}, "states no means"),
    ("procedure", {"procedure": ""}, "states no procedure"),
    ("unsecured condition", {"conditions": "- the module imports"}, "unmarked"),
    ("estimate missing", {"estimate": None}, "has no estimate"),
    ("estimate confirmer", {"estimate": {"minutes": 9, "author": "executor"}},
     "names no confirmer"),
    ("estimate author", {"estimate": {"minutes": 9, "confirmer": "parent"}}, "names no author"),
    ("wrong stage", {"stage": 7}, "builds stage 2, not stage 7"),
]


@pytest.mark.parametrize("name, edit, expected", _REFUSALS, ids=[r[0] for r in _REFUSALS])
def test_a_record_defect_is_refused(name, edit, expected):
    problems = submission._stage_order_violations(_doc(record=_record(**edit)), _ctx())
    assert any(expected in p for p in problems), problems


def _record_problems(meta, ctx):
    return submission._stage_order_violations(_doc(record=meta), ctx)


def test_compared_plans_need_an_entry_with_its_departure_or_a_resolving_search():
    resolves = _ctx(search_resolver=lambda sid: sid == "S1")
    assert _record_problems(_record(compared_plans={"none": True, "search_id": "S1"}), resolves) == []
    unresolved = _ctx(search_resolver=lambda sid: False)
    assert any("no search recorded in this session resolves" in p for p in _record_problems(
        _record(compared_plans={"none": True, "search_id": "S1"}), unresolved))
    assert any("names no search id" in p for p in _record_problems(
        _record(compared_plans={"none": True}), resolves))
    assert any("states no compared plans" in p for p in _record_problems(
        _record(compared_plans={}), resolves))
    assert any("needs a ref and the departure" in p for p in _record_problems(
        _record(compared_plans={"entries": [{"ref": "plan-a", "departure": ""}]}), resolves))
    assert any("list entries and also say" in p for p in _record_problems(
        _record(compared_plans={"entries": [{"ref": "a", "departure": "b"}], "none": True,
                                "search_id": "S1"}), resolves))


def _cond(condition_id, set_by="user"):
    return {"id": condition_id, "text": f"condition {condition_id}", "set_by": set_by}


TWO_CONDITIONS = (*BINDING, _cond("B2"))


@pytest.mark.parametrize("label", ["user", "parent", "a middle level", ""])
def test_the_check_excuses_exactly_the_conditions_the_context_released(label):
    """The record check reads the context's released set and never a condition's `set_by`
    label: B2 is excused when released, owed when not, under any label."""
    conditions = (*BINDING, _cond("B2", label))
    released = StageOrderContext(expected_stage=2, binding_conditions=conditions,
                                 released=frozenset({"B2"}))
    assert _record_problems(_record(), released) == []
    unreleased = StageOrderContext(expected_stage=2, binding_conditions=conditions)
    assert any("does not say how it keeps binding condition 'B2'" in p
               for p in _record_problems(_record(), unreleased))
    only_b1 = StageOrderContext(expected_stage=2, binding_conditions=conditions,
                                released=frozenset({"B1"}))
    assert any("does not say how it keeps binding condition 'B2'" in p
               for p in _record_problems(_record(), only_b1)), "a release is per condition"


def test_the_carrying_record_cannot_release_the_condition_it_owes():
    """A plan does not release what was set above it: its own `withdrawn` decision leaves the
    condition owed."""
    ctx = StageOrderContext(expected_stage=2, binding_conditions=TWO_CONDITIONS)
    own_release = _record(setter_decisions=[
        {"condition": "B1", "value": "kept", "setter": "user", "decided_by": "user"},
        {"condition": "B2", "value": "withdrawn", "setter": "user", "decided_by": "user"}])
    assert any("does not say how it keeps binding condition 'B2'" in p
               for p in _record_problems(own_release, ctx))


def _setter_record(*decisions) -> dict:
    return sub_plan.SubPlanRecord(
        stage=1, setter_decisions=[sub_plan.SetterDecision(c, v, "user", "user")
                                   for c, v in decisions]).to_dict()


def _cstage(index, *conditions):
    """The slice of a stage the readers use: its index, its binding conditions and its edges."""
    return SimpleNamespace(index=index, binding_conditions=list(conditions),
                           binding_conditions_stated=True, depends_on=[])


def _norm_of(*conditions):
    """The handoff norm of a linked child: the stage description with these conditions."""
    return {"description_place": [["description", "the engine",
                                   [[c["id"], c["text"], c["set_by"]] for c in conditions],
                                   None, [], None]]}


C1, C2, C3, C9 = _cond("C1"), _cond("C2", "the middle level"), _cond("C3"), _cond("C9")


def test_the_release_is_read_from_the_record_of_the_level_that_set_the_condition():
    """Stage b (C1 carried from above, C2 and C3 set by the middle level) is built by the plan
    under test; the middle level's record of stage a releases C2 only, wherever that record
    lives. C9 is on neither stage; C1 is on stage a, so it is never the middle level's to give."""
    released = cli_common._setter_level_released
    on_b = (C1, C2, C3)
    r_a = _setter_record(("C1", "withdrawn"), ("C2", "withdrawn"), ("C3", "kept"), ("C9", "withdrawn"))
    root_stages = [_cstage(1, C1)]

    nested = _session("s", plan_stack=[_frame(1, root_stages), _frame(2)],
                      sub_plan_records={"0:1": r_a,
                                        "1:2": _setter_record(("C3", "withdrawn"))})
    assert released(nested, None, on_b) == frozenset({"C2"}), \
        "the frame below, not the carrying one; `kept` and an id on no stage release nothing"

    under_contour = _session(
        "s", plan_stack=[_frame(2)],
        parent_link={"parent_session": "p", "stage_index": 1, "norm": _norm_of(C1)},
        sub_plan_records={"1": r_a})
    assert released(under_contour, None, on_b) == frozenset({"C2"})

    parent = _session("p", parent_link={"parent_session": "g", "stage_index": 4,
                                        "norm": _norm_of(C1)},
                      sub_plan_records={"4": r_a})
    contour = _session("c", parent_link={"parent_session": "p", "stage_index": 2})
    assert released(contour, parent, on_b) == frozenset({"C2"})

    framed_parent = _session("p", plan_stack=[_frame(1, root_stages)],
                             sub_plan_records={"0:1": r_a})
    assert released(contour, framed_parent, on_b) == frozenset({"C2"}), \
        "a contour under a parent running a frame reads the record that frame carries"

    two_frames = _session("p", plan_stack=[_frame(1, root_stages), _frame(2, [_cstage(2, C1)])],
                          sub_plan_records={"0:1": _setter_record(("C3", "withdrawn")),
                                            "1:2": r_a})
    assert released(contour, two_frames, on_b) == frozenset({"C2"}), \
        "of the parent's two frames the top one's record is read, not the lowest one's"

    unknown_above = _session("s", plan_stack=[_frame(1), _frame(2)], sub_plan_records={"0:1": r_a})
    assert released(unknown_above, None, on_b) == frozenset(), \
        "a setter level whose own carried conditions are unknown releases nothing"
    assert released(nested, None, None) == frozenset(), "no condition stated on the stage"


def test_the_decision_is_by_id_never_by_the_label_a_condition_carries():
    """Whatever `set_by` says, an id the setter level carries from above is not its own: C0
    (inherited, labelled by a middle level) and C1 (relabelled by it) stay binding."""
    released = cli_common._setter_level_released
    c0 = _cond("C0", "a middle level")
    relabelled_c1 = _cond("C1", "the middle level")
    nested = _session("s", plan_stack=[_frame(1, [_cstage(1, c0, C1)]), _frame(2)],
                      sub_plan_records={"0:1": _setter_record(("C0", "withdrawn"),
                                                              ("C1", "changed"))})
    assert released(nested, None, (c0, relabelled_c1)) == frozenset()
    assert released(nested, None, (c0, relabelled_c1, C2)) == frozenset(), \
        "C2 is the middle level's but R_a made no decision on it"
    assert released(nested, None, ()) == frozenset()


def _two_level(above, on_b, *decisions):
    """The session of the plan P2 that builds stage b (index 2) of the middle plan P1; P1 is
    itself the plan built for stage a (index 1) of the root plan P0. `above` are the conditions
    stage a carries, `on_b` those stage b carries, `decisions` what the middle level's record of
    stage a decides."""
    state = _session("s", plan_stack=[_frame(1, [_cstage(1, *above)]),
                                      _frame(2, [_cstage(2, *on_b)])])
    sub_plan.store(state, sub_plan.SubPlanRecord(
        stage=1, setter_decisions=[sub_plan.SetterDecision(c, v, "the middle level", "user")
                                   for c, v in decisions]), level=0)
    return state


def _child_record(*kept):
    return _record(how_kept=[{"condition": c, "how": f"{c} is kept"} for c in kept],
                   setter_decisions=[{"condition": c, "value": "kept", "setter": "user",
                                      "decided_by": "user"} for c in kept])


def test_a_condition_the_middle_level_set_and_released_in_its_record_is_not_owed_by_the_child():
    """(a) P1 sets C2 on its stage b and records C2 withdrawn in its record of stage a: P1's own
    submission accepts that decision, and P2's plan, omitting C2, passes."""
    p1_record = _record(stage=1, how_kept=[{"condition": "C1", "how": "C1 is kept"}],
                        setter_decisions=[
                            {"condition": "C1", "value": "kept", "setter": "user", "decided_by": "user"},
                            {"condition": "C2", "value": "withdrawn", "setter": "the middle level",
                             "decided_by": "user"}])
    p1_plan = _new_form({"binding_conditions": [C1, C2]}, record=p1_record)
    p1_ctx = StageOrderContext(expected_stage=1, binding_conditions=(C1,))
    assert submission._stage_order_violations(p1_plan, p1_ctx) == []

    state = _two_level((C1,), (C1, C2))
    sub_plan.store(state, p1_plan.meta.sub_plan, level=0)
    ctx = cli_common._stage_order_context(state)
    assert ctx.released == frozenset({"C2"})
    assert _record_problems(_child_record("C1"), ctx) == []

    kept_only = _two_level((C1,), (C1, C2), ("C2", "kept"))
    owed = cli_common._stage_order_context(kept_only)
    assert any("does not say how it keeps binding condition 'C2'" in p
               for p in _record_problems(_child_record("C1"), owed)), "only a release excuses it"


def test_a_decision_in_a_levels_record_names_a_condition_it_set_on_its_own_stages():
    """Check 13 admits, besides the conditions of the stage the record's plan builds, the ones
    the plan sets on its own stages; a decision on any other id is refused."""
    p1_stages = {"binding_conditions": [C1, C2]}
    stage_a = StageOrderContext(expected_stage=1, binding_conditions=(C1,))

    def problems(condition, ctx=stage_a):
        decision = {"condition": condition, "value": "withdrawn", "setter": "x", "decided_by": "u"}
        record = _record(stage=1, how_kept=[{"condition": "C1", "how": "kept"}],
                         setter_decisions=[decision])
        return submission._stage_order_violations(_new_form(p1_stages, record=record), ctx)

    assert problems("C2") == [], "set by this level on its own stage b"
    assert any("names condition 'C0'" in p and "neither the stage nor a stage of this plan" in p
               for p in problems("C0"))
    assert problems("C0", StageOrderContext(expected_stage=1)) == [], \
        "a carrier whose stage conditions are unknown skips the known-id rule"


def _restating(text, ctx):
    """Problems of a plan whose stage restates C1 (carried by the stage it builds) as `text`."""
    restated = {"id": "C1", "text": text, "set_by": "the middle level"}
    return [p for p in submission._stage_order_violations(
        _new_form({"binding_conditions": [restated]}), ctx) if "restates" in p]


def test_a_stage_restating_an_inherited_condition_with_another_text_is_refused_by_its_id():
    ctx = StageOrderContext(expected_stage=1, binding_conditions=(C1,))
    problems = _restating("anything goes", ctx)
    assert len(problems) == 1 and "stage 1 restates binding condition 'C1'" in problems[0], problems
    assert "`changed` or `withdrawn`" in problems[0], "the refusal names the way out"
    with_record = _new_form({"binding_conditions": [{"id": "C1", "text": "anything goes",
                                                     "set_by": "x"}]},
                            record=_record(stage=1, how_kept=[{"condition": "C1", "how": "kept"}]))
    assert any("restates binding condition 'C1'" in p
               for p in submission._stage_order_violations(with_record, ctx)), \
        "a plan that also carries a record meets the same refusal"


def test_a_restated_condition_is_the_same_condition_in_any_case_of_its_id():
    ctx = StageOrderContext(expected_stage=1, binding_conditions=(C1,))
    restated = {"id": "c1", "text": "anything goes", "set_by": "x"}
    problems = [p for p in submission._stage_order_violations(
        _new_form({"binding_conditions": [restated]}), ctx) if "restates" in p]
    assert len(problems) == 1 and "'c1'" in problems[0], problems
    released = StageOrderContext(expected_stage=1, binding_conditions=(C1,),
                                 released=frozenset({"C1"}))
    assert not [p for p in submission._stage_order_violations(
        _new_form({"binding_conditions": [restated]}), released) if "restates" in p]


def test_a_restated_condition_the_setter_level_released_may_change_its_text():
    released = StageOrderContext(expected_stage=1, binding_conditions=(C1,),
                                 released=frozenset({"C1"}))
    assert _restating("anything goes", released) == []
    other = StageOrderContext(expected_stage=1, binding_conditions=(C1,),
                              released=frozenset({"C2"}))
    assert _restating("anything goes", other), "a release is per condition"


def test_a_restated_condition_with_the_same_text_passes_whatever_its_label_and_spacing():
    ctx = StageOrderContext(expected_stage=1, binding_conditions=(C1,))
    assert _restating("condition C1", ctx) == []
    assert _restating("  condition \n   C1 ", ctx) == [], "trimmed and whitespace-folded only"
    assert _restating("Condition C1", ctx), "no other normalization"
    unknown = StageOrderContext(expected_stage=1)
    assert _restating("anything goes", unknown) == [], "a carrier whose conditions are unknown"


def test_an_inherited_condition_released_by_a_non_user_labelled_middle_level_stays_binding():
    """(b) C0 stage a carries (labelled by a middle level, not the user) is decided `withdrawn`
    in R_a: the child still owes it."""
    c0 = _cond("C0", "a middle level")
    state = _two_level((c0,), (c0, C2), ("C0", "withdrawn"), ("C2", "withdrawn"))
    ctx = cli_common._stage_order_context(state)
    assert ctx.released == frozenset({"C2"})
    problems = _record_problems(_child_record(), ctx)
    assert any("does not say how it keeps binding condition 'C0'" in p for p in problems), problems
    assert not any("'C2'" in p and "keeps" in p for p in problems)
    assert _record_problems(_child_record("C0"), ctx) == []


def test_a_condition_the_middle_level_relabelled_and_released_stays_binding():
    """(c) C1 is the user's on stage a; stage b repeats it under the middle level's own label.
    R_a's `withdrawn` on C1 releases nothing, whatever stage b says it was set by."""
    relabelled = _cond("C1", "the middle level")
    state = _two_level((C1,), (relabelled, C2), ("C1", "withdrawn"), ("C2", "withdrawn"))
    ctx = cli_common._stage_order_context(state)
    assert ctx.released == frozenset({"C2"})
    problems = _record_problems(_child_record(), ctx)
    assert any("does not say how it keeps binding condition 'C1'" in p for p in problems), problems


def test_the_root_level_releases_nothing_a_record_it_does_not_carry_would_release():
    """(d) The root plan builds no stage and carries no record, so the plan built for its stage
    a is owed every condition stage a states: the one the root order stated and the one the
    root plan set on the stage itself, whatever record the carrying plan's own level holds."""
    order_condition = _cond("O1")
    root_set = _cond("S1", "the root plan")
    state = _session("s", plan_stack=[_frame(1, [_cstage(1, order_condition, root_set)])])
    sub_plan.store(state, sub_plan.SubPlanRecord(
        stage=1, setter_decisions=[sub_plan.SetterDecision("O1", "withdrawn", "root", "user"),
                                   sub_plan.SetterDecision("S1", "withdrawn", "root", "user")]),
        level=0)
    ctx = cli_common._stage_order_context(state)
    assert ctx.released == frozenset(), "the carrying record's own decisions release nothing"
    problems = _record_problems(_record(stage=1, how_kept=[], setter_decisions=[]), ctx)
    assert [c for c in ("O1", "S1") if any(f"keeps binding condition '{c}'" in p for p in problems)] \
        == ["O1", "S1"]
    assert cli_common._setter_level_released(_session("bare"), None, (order_condition, root_set)) \
        == frozenset()


def test_a_record_names_only_the_conditions_the_stage_carries():
    unknown = _record(how_kept=[{"condition": "B1", "how": "x"}, {"condition": "B9", "how": "y"}])
    assert any("'B9', which the stage does not carry" in p for p in _record_problems(unknown, _ctx()))
    stray = _record(setter_decisions=[{"condition": "B9", "value": "kept", "setter": "u",
                                       "decided_by": "u"}])
    assert any("names condition 'B9'" in p for p in _record_problems(stray, _ctx()))


def test_a_setter_decision_has_a_known_value_a_setter_and_a_decider():
    bad = _record(setter_decisions=[{"condition": "B1", "value": "ignored", "setter": "",
                                     "decided_by": ""}])
    problems = _record_problems(bad, _ctx())
    assert any("value 'ignored'" in p for p in problems)
    assert any("names no setter" in p for p in problems)
    assert any("names no who decided" in p for p in problems)


def test_a_malformed_record_part_is_refused_by_name():
    meta = _record()
    meta["sub_plan"]["building_place"] = 3
    assert any("malformed part: building_place" in p for p in _record_problems(meta, _ctx()))


def _described_parent(store, sid, conditions=BINDING):
    from agentctl.state import Stage as _Stage

    parent = _Stage(
        index=1, title="stage 1", subject=Subject(material="m", result="img"),
        means=Means(means="", method=""), actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
        outcome=Outcome(status=StageStatus.ACTIVE.value), place="the engine",
        binding_conditions=[dict(c) for c in conditions], binding_conditions_stated=True)
    state = SessionState(
        session_id=sid, task_id="parent-task", goal="g", overall_done_criterion="d",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="tester"),
        partition=Partition(verdict="single"), repo_root="/tmp/repo",
        delivery_worktree="/tmp/repo/.wt",
        final_check=[FinalCheck(command="echo ok", expected_exit=0, label="smoke")],
        stages=[parent], current_stage=1, order_searches=[{"id": "S1"}])
    store.save(state)
    return state


_NORM = {"description_place": [["description", "the engine", [["B1", "no push", "user"]],
                                None, [], None]]}


def test_a_frame_and_a_child_contour_judge_one_record_by_one_definition(store):
    _described_parent(store, "p")
    cli.cmd_push_subplan(Namespace(session="p", plan="/tmp/child.toml", task=None,
                                   originating_stage=1), store=store)
    framed = store.load("p")
    contour = _session("child", order_searches=[{"id": "S1"}],
                       parent_link={"parent_session": "p", "stage_index": 1, "norm": _NORM})
    flawed = _doc(record=_record(stage=1, how_kept=[], estimate={"minutes": 9, "author": "e"}))
    by_frame = _problems(flawed, cli_common._stage_order_context(framed))
    by_contour = _problems(flawed, cli_common._stage_order_context(contour, store=store))
    assert by_frame == by_contour and len(by_frame) == 2, (by_frame, by_contour)
    assert cli_common._stage_order_context(framed).expected_stage == 1
    assert cli_common._stage_order_context(contour).expected_stage == 1


def test_the_context_resolves_searches_in_the_carrying_session():
    carrying = _session("c", order_searches=[{"id": "S1"}],
                        parent_link={"stage_index": 1, "norm": {}})
    ctx = cli_common._stage_order_context(carrying)
    assert ctx.search_resolver("S1") and not ctx.search_resolver("S2")
    assert ctx.binding_conditions is None


def _staged(*edges):
    """Parent-plan stages 1..n, `edges` the `(stage, depends_on)` pairs."""
    deps = dict(edges)
    return [_stage(i, supplies=[plan_supply(on) for on in deps.get(i, ())]) for i in range(1, 5)]


def plan_supply(on):
    from agentctl.state import Supply

    return Supply(on=on)


_MEANS_RECORD = {"conditions": "- the engine exists [means: stage {n}]"}


def _means_problems(ctx, n):
    edits = {"conditions": _MEANS_RECORD["conditions"].format(n=n)}
    return [p for p in _record_problems(_record(**edits), ctx) if "depends_on ancestor" in p]


def test_a_means_mark_must_name_a_real_ancestor_of_the_parent_stage():
    ctx = StageOrderContext(expected_stage=2, parent_ancestors=frozenset({1}))
    assert _means_problems(ctx, 1) == []
    assert _means_problems(ctx, 3), "stage 3 exists in the parent plan but is not an ancestor"
    assert _means_problems(ctx, 2), "the stage itself is no ancestor of itself"
    unknown = StageOrderContext(expected_stage=2)
    assert _means_problems(unknown, 3) == [], "an unknown carrier skips the ancestor check"


def test_the_context_reads_the_ancestors_of_the_parent_stage_from_a_frame_and_a_contour(store):
    parent = _described_parent(store, "p")
    parent.stages = _staged((2, [1]), (3, [2]), (4, []))
    parent.stages[0].outcome = Outcome(status=StageStatus.ACTIVE.value)
    parent.current_stage = 3
    store.save(parent)
    cli.cmd_push_subplan(Namespace(session="p", plan="/tmp/child.toml", task=None,
                                   originating_stage=3), store=store)
    framed = cli_common._stage_order_context(store.load("p"))
    assert framed.expected_stage == 3 and framed.parent_ancestors == frozenset({1, 2})

    upper = _described_parent(store, "q")
    upper.stages = _staged((2, [1]), (3, [2]), (4, []))
    store.save(upper)
    contour = _session("child", parent_link={"parent_session": "q", "stage_index": 3, "norm": {}})
    by_contour = cli_common._stage_order_context(contour, store=store)
    assert by_contour.parent_ancestors == frozenset({1, 2})
    assert cli_common._stage_order_context(contour).parent_ancestors is None, "no store, unknown"


def test_the_context_names_the_released_conditions_of_the_level_above(store):
    """Parent p is a contour linked at stage 4 of its own parent, whose norm carries A1; p's
    stage 1 (the one the child builds) carries A1 and B1. p's record decides both `withdrawn`:
    only B1 is p's own to release, whatever label either carries."""
    a1, b1 = _cond("A1", "the middle level"), _cond("B1", "user")
    parent = _described_parent(store, "p")
    parent.parent_link = {"parent_session": "g", "stage_index": 4, "norm": _norm_of(a1)}
    parent.sub_plan_records["4"] = _setter_record(("A1", "withdrawn"), ("B1", "withdrawn"))
    parent.stages[0].binding_conditions = [dict(a1), dict(b1)]
    store.save(parent)
    child = _session("child", parent_link={"parent_session": "p", "stage_index": 1,
                                           "norm": _norm_of(a1, b1)})
    assert cli_common._stage_order_context(child, store=store).released == frozenset({"B1"})
    assert cli_common._stage_order_context(child).released == frozenset(), "no store, nothing read"
    states_nothing = _session("child2", parent_link={"parent_session": "p", "stage_index": 1,
                                                     "norm": {}})
    assert cli_common._stage_order_context(states_nothing, store=store).released == frozenset()

    cli.cmd_push_subplan(Namespace(session="p", plan="/tmp/child.toml", task=None,
                                   originating_stage=1), store=store)
    framed = store.load("p")
    assert cli_common._stage_order_context(framed, store=store).released == frozenset({"B1"})
    framed.parent_link = None
    assert cli_common._stage_order_context(framed, store=store).released == frozenset(), "root level"


def test_a_parent_session_without_a_state_file_is_a_named_refusal(store):
    """With no parent state the ancestors of the stage cannot be read; a plan carrying a record
    is refused by name, not accepted with every `[means: stage N]` mark unchecked."""
    contour = _session("child", parent_link={"parent_session": "ghost", "stage_index": 1,
                                             "norm": _norm_of(*BINDING)})
    ctx = cli_common._stage_order_context(contour, store=store)
    assert ctx.parent_ancestors is None and "ghost" in ctx.parent_unreadable
    marked = _record(conditions="- the engine exists [means: stage 3]", stage=1)
    problems = _record_problems(marked, ctx)
    assert any("'ghost'" in p and "has no state file" in p for p in problems), problems
    assert _record_problems(_record(stage=1), StageOrderContext(expected_stage=1,
                                                                binding_conditions=BINDING)) == []
    assert submission._stage_order_violations(_new_form(), ctx) == [], "no record, nothing owed"


_RECORD_HEAD = """
[meta.sub_plan]
stage = 1
means = "Edit and Write"
method = "split the module and keep the seam"
procedure = "1. read the module. 2. split it. 3. run the suite"
conditions = "- the suite stays green [probe: python3 -m pytest -q => passes]"

[meta.sub_plan.estimate]
minutes = 40
author = "executor"
confirmer = "parent"

[meta.compared_plans]
none = true
search_id = "S1"
"""

_HOW_KEPT = """
[[meta.how_kept]]
condition = "B1"
how = "no push verb is run"

[[meta.sub_plan.setter_decisions]]
condition = "B1"
value = "kept"
setter = "user"
decided_by = "user"
"""


def _walk_to_submit(store, tmp_path, record_toml):
    from test_submit_plan_surfaces import _write_plan

    _described_parent(store, "w")
    pushed = cli.cmd_push_subplan(Namespace(session="w", plan=str(tmp_path / "plan.toml"),
                                            task=None, originating_stage=1), store=store)
    assert pushed.ok, pushed
    cli.cmd_classify(Namespace(session="w", chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session="w"), store=store)
    plan = _write_plan(tmp_path, tier="small")
    plan.write_text(plan.read_text(encoding="utf-8") + record_toml, encoding="utf-8")
    return cli.cmd_submit_plan(Namespace(session="w", plan=str(plan)), store=store)


def _walk_to_submit_in_contour(store, tmp_path, record_toml, *, parent_saved=True):
    """The child session of a parent stage (a linked contour, no pushed frame) submits its plan;
    `parent_saved=False` leaves the parent session without a state file."""
    from test_submit_plan_surfaces import _session as _classified, _write_plan

    if parent_saved:
        _described_parent(store, "p")
    _classified(store, "c")
    child = store.load("c")
    child.parent_link = {"parent_session": "p", "stage_index": 1, "norm": _NORM}
    child.order_searches = [{"id": "S1"}]
    store.save(child)
    plan_file = _write_plan(tmp_path, tier="small")
    plan_file.write_text(plan_file.read_text(encoding="utf-8") + record_toml, encoding="utf-8")
    return cli.cmd_submit_plan(Namespace(session="c", plan=str(plan_file)), store=store)


def _stage_order_problems(directive) -> list[str]:
    return [p for p in directive.data.get("problems", []) if "sub-plan record" in p]


def test_the_whole_verb_sequence_refuses_a_missing_how_kept_and_then_accepts_the_fixed_record(
        store, tmp_path):
    refused = _walk_to_submit(store, tmp_path, _RECORD_HEAD)
    assert refused.ok is False
    assert any("does not say how it keeps binding condition 'B1'" in p
               for p in _stage_order_problems(refused)), refused.data
    assert store.load("w").sub_plan_records == {}

    plan = tmp_path / "plan.toml"
    plan.write_text(plan.read_text(encoding="utf-8").replace(_RECORD_HEAD, _RECORD_HEAD + _HOW_KEPT),
                    encoding="utf-8")
    fixed = cli.cmd_submit_plan(Namespace(session="w", plan=str(plan)), store=store)
    assert fixed.ok, fixed.data.get("problems")
    stored = sub_plan.SubPlanRecord.from_dict(store.load("w").sub_plan_records["0:1"])
    assert stored.how_kept[0].condition == "B1"

    approved = cli.cmd_approve(Namespace(session="w", by="user"), store=store)
    assert approved.ok, "approve asks for no record the verbs have not made before approval"


def test_a_good_record_is_stored_at_submit_with_all_its_elements_and_its_building_interval(
        store, tmp_path):
    accepted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")
    state = store.load("w")
    record = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"])
    assert record.means and record.method and record.procedure
    assert record.estimate.author == "executor" and record.estimate.confirmer == "parent"
    assert record.compared_plans.none and record.compared_plans.search_id == "S1"
    assert [h.condition for h in record.how_kept] == ["B1"]
    assert [d.value for d in record.setter_decisions] == ["kept"]
    place = record.building_place
    assert place.session == "w" and place.pushed is not None and place.popped is None
    assert state.history[place.pushed]["event"] == "push_subplan"

    state.node = Node.RESOLVED.value
    state.stages[0].outcome.status = StageStatus.PASSED.value
    state.partition = Partition(verdict="single")
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.current_stage = None
    store.save(state)
    popped = cli.cmd_pop_subplan(Namespace(session="w"), store=store)
    assert popped.ok, popped
    after = store.load("w")
    closed = sub_plan.read(after, 1).building_place
    assert closed.pushed == place.pushed and closed.popped is not None
    assert closed.popped > closed.pushed
    assert after.history[closed.popped - 1]["event"] != "pop_subplan"
    assert sub_plan.read(after, 1).method == record.method


_MIDDLE = "the middle level"
_C1_RELABELLED = f'{{id = "C1", text = "condition C1", set_by = "{_MIDDLE}"}}'
_C2_OF_MIDDLE = f'{{id = "C2", text = "condition C2", set_by = "{_MIDDLE}"}}'

_MIDDLE_DECISIONS = f"""
[[meta.how_kept]]
condition = "C1"
how = "no push verb is run"

[[meta.sub_plan.setter_decisions]]
condition = "C1"
value = "withdrawn"
setter = "{_MIDDLE}"
decided_by = "{_MIDDLE}"

[[meta.sub_plan.setter_decisions]]
condition = "C2"
value = "withdrawn"
setter = "{_MIDDLE}"
decided_by = "{_MIDDLE}"
"""


def _middle_level_plan(tmp_path):
    """The plan of the level between the user's stage and the deepest one: a copy of the usual
    stage as its stage 2, which carries C1 relabelled by this level and C2 that this level set."""
    from test_submit_plan_surfaces import _write_plan

    plan_file = _write_plan(tmp_path, tier="small")
    text = plan_file.read_text(encoding="utf-8")
    head, tail = text.split("[[final_check]]")
    second = head[head.index("[[stage]]"):].replace("index = 1", "index = 2").replace(
        '"Produce a file"', '"Produce another file"').replace(
        'invariants = "inv"',
        f'invariants = "inv"\nbinding_conditions = [{_C1_RELABELLED}, {_C2_OF_MIDDLE}]')
    plan_file.write_text(head + second + "[[final_check]]" + tail + _RECORD_HEAD
                         + _MIDDLE_DECISIONS, encoding="utf-8")
    return plan_file


def _push_to_the_deepest_level(store, tmp_path):
    """Push a sub-plan on the user's stage 1 (carrying C1), submit the middle level's plan with
    its record, then push again on that plan's stage 2; return the deepest level's plan file."""
    from test_submit_plan_surfaces import _write_plan

    _described_parent(store, "w", conditions=(C1,))
    cli.cmd_push_subplan(Namespace(session="w", plan=str(tmp_path / "plan.toml"), task=None,
                                   originating_stage=1), store=store)
    classify_args = Namespace(session="w", chat=False, changed_lines=200, files=5,
                              wall_clock_min=60, tracker_key=None, architectural=True,
                              external_effect=False, new_dependency=False,
                              public_api_change=False)
    cli.cmd_classify(classify_args, store=store)
    cli.cmd_plan(Namespace(session="w"), store=store)
    middle = cli.cmd_submit_plan(Namespace(session="w", plan=str(_middle_level_plan(tmp_path))),
                                 store=store)
    assert middle.ok, middle.data.get("problems")
    state = store.load("w")
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="tester")
    state.partition = Partition(verdict="single")
    state.node, state.current_stage = Node.EXECUTING.value, 2
    state.stage(2).outcome.status = StageStatus.ACTIVE.value
    store.save(state)
    deepest_dir = tmp_path / "deepest"
    deepest_dir.mkdir()
    pushed = cli.cmd_push_subplan(Namespace(session="w", plan=str(deepest_dir / "plan.toml"),
                                            task=None, originating_stage=2), store=store)
    assert pushed.ok, pushed
    assert [f.originating_stage for f in store.load("w").plan_stack] == [1, 2]
    cli.cmd_classify(classify_args, store=store)
    cli.cmd_plan(Namespace(session="w"), store=store)
    return _write_plan(deepest_dir, tier="small")


def _deepest_record(*kept):
    how = "".join(f'\n[[meta.how_kept]]\ncondition = "{c}"\nhow = "no push verb is run"\n'
                  for c in kept)
    return _RECORD_HEAD.replace("stage = 1", "stage = 2") + how


def test_the_deepest_plan_is_bound_by_the_conditions_its_level_did_not_release(store, tmp_path):
    """Two pushes, then the deepest level submits. The middle level set C2 and released it in
    its own record, so C2 does not bind the deepest plan; it relabelled C1, which the user's
    stage carries, and "withdrew" it in that record, but the user set C1 and only the user's level
    can release it, so C1 still binds the deepest plan, whatever label the condition carries."""
    deepest = _push_to_the_deepest_level(store, tmp_path)
    plan_text = deepest.read_text(encoding="utf-8")

    deepest.write_text(plan_text + _deepest_record(), encoding="utf-8")
    refused = cli.cmd_submit_plan(Namespace(session="w", plan=str(deepest)), store=store)
    named = " | ".join(_stage_order_problems(refused))
    assert refused.ok is False and "'C1'" in named and "'C2'" not in named, refused.data
    assert store.load("w").sub_plan_records.get("1:2") is None

    deepest.write_text(plan_text + _deepest_record("C1"), encoding="utf-8")
    accepted = cli.cmd_submit_plan(Namespace(session="w", plan=str(deepest)), store=store)
    assert accepted.ok, accepted.data.get("problems")
    assert [h.condition for h in sub_plan.SubPlanRecord.from_dict(
        store.load("w").sub_plan_records["1:2"]).how_kept] == ["C1"]


# --- 3. The stage-description review ---------------------------------------------------------

from agentctl import cli_review, method_statement as M, render  # noqa: E402
from agentctl.plan import load_plan  # noqa: E402
from test_method_statement_rule import (  # noqa: E402
    Judge, _dispatch_on_new_form_plan, _plan_file, answering,
)

PLACE_TEXT = "the release engine, relied on by stage 2"
HOW_BUILT_TEXT = "the release command is a shell script"


def _described_doc(order=TWO_SIDED_ORDER, **stage_extra):
    """A one-stage plan whose stage is a description: its prose fields are `stage_extra`'s."""
    return _doc({"place": PLACE_TEXT, **stage_extra}, order=order)


def _plan_review_args(path="plan.toml", scope=""):
    return Namespace(verdict="pass", scope=scope, target=str(path))


def _plan_pass(doc, judge, monkeypatch, *, scope="", state=None):
    """What the plan review's `pass` does with `doc` — the production path, with the plan loaded
    from memory instead of from a file."""
    monkeypatch.setattr(cli_review, "load_plan", lambda target: doc)
    state = state or _session("s", plan_path="plan.toml")
    return cli_review._method_statement_plan_pass_refusal(_plan_review_args(scope=scope), state, judge)


_SUB_PLAN_FIELDS = [
    ("means", {"means": "use the shell script tool"}),
    ("method", {"method": "write a shell script"}),
    ("procedure", {"procedure": "1. write it 2. run it"}),
    ("conditions", {"conditions": "- the host is slow [risk: unverified host speed]"}),
]


@pytest.mark.parametrize("field, extra", _SUB_PLAN_FIELDS, ids=[f for f, _ in _SUB_PLAN_FIELDS])
def test_description_method_field_refused(field, extra, monkeypatch):
    judge = Judge(answering(M.METHOD))
    doc = _described_doc(**extra)

    problems = submission._stage_order_violations(doc, _ctx())
    assert [p for p in problems if f"carries no {field}" in p or f", {field}" in p], problems

    _plan_pass(doc, judge, monkeypatch)
    assert judge.places == {M.STAGE_DESCRIPTION}, "the judge read the description"
    (text,) = extra.values()
    assert not any(text in judged for judged in judge.texts)


def _fresh_judge_cache(monkeypatch, tmp_path, name):
    monkeypatch.setattr(M, "cache_dir", lambda: tmp_path / name)


def test_description_binding_conditions_not_judged(monkeypatch, tmp_path):
    """What the judge is sent is the same with and without the binding conditions, and no field
    of a condition (its id, its text, who set it) is in anything it was sent."""
    condition = {"id": "BCZETA9", "text": "the build host runs Linux", "set_by": "user-qx"}
    judge = Judge(answering(M.METHOD))
    bare = Judge(answering(M.METHOD))

    _fresh_judge_cache(monkeypatch, tmp_path, "with")
    _plan_pass(_described_doc(binding_conditions=[condition]), judge, monkeypatch)
    _fresh_judge_cache(monkeypatch, tmp_path, "without")
    _plan_pass(_described_doc(binding_conditions=[]), bare, monkeypatch)

    assert judge.calls and judge.places == {M.STAGE_DESCRIPTION}
    assert judge.calls == bare.calls
    for value in condition.values():
        assert not any(value in text for text in judge.texts), value


def test_description_how_built_is_defect(monkeypatch):
    doc = _described_doc(expected_result_image=HOW_BUILT_TEXT)
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION, containing=HOW_BUILT_TEXT))

    refusal = _plan_pass(doc, judge, monkeypatch)

    assert refusal is not None and refusal.action == "fix_plan"
    (line,) = refusal.data["problems"]
    assert "stage 1 result (stage description)" in line
    assert "states how the product is built" in line


_EXTRA_FIELDS = ("place", "invariants", "knowledge", "material", "preconditions")


@pytest.mark.parametrize("field", _EXTRA_FIELDS)
def test_description_extra_field_how_built_reported(field, monkeypatch, tmp_path):
    doc = _described_doc(**{field: HOW_BUILT_TEXT})
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION, containing=HOW_BUILT_TEXT))

    refusal = _plan_pass(doc, judge, monkeypatch)

    assert refusal is not None, field
    (line,) = refusal.data["problems"]
    assert f"stage 1 {field} (stage description)" in line, line
    assert "states how the product is built" in line

    _fresh_judge_cache(monkeypatch, tmp_path, "neither")
    clean = Judge()
    assert _plan_pass(doc, clean, monkeypatch) is None, field
    assert (M.STAGE_DESCRIPTION, HOW_BUILT_TEXT) in clean.calls, f"{field} was not shown to the judge"


def test_description_old_form_not_judged(monkeypatch):
    doc = _described_doc(order=AGREED_OLD_FORM_ORDER, method="write a shell script")
    assert not plan.is_two_sided(doc.meta.order) and doc.meta.order is not None
    judge = Judge(answering(M.METHOD))

    assert _plan_pass(doc, judge, monkeypatch) is None
    assert render.stage_description_site_defects(doc, 1, runner=judge) == []
    assert M.plan_statements(doc) == []
    assert judge.calls == []


def test_description_could_not_read_is_defect(monkeypatch):
    doc = _described_doc()
    judge = Judge(fail=True)

    findings = render.stage_description_site_findings(doc, 1, runner=judge)
    assert len(findings) == 1 and findings[0].verdict == M.COULD_NOT_READ
    assert len(judge.calls) == 1
    assert "the judge did not answer" in M.defect_line(findings[0])

    refusal = _plan_pass(doc, Judge(fail=True), monkeypatch)
    assert refusal is not None and refusal.action == "fix_plan"
    assert "did not answer" in " ".join(refusal.data["problems"])


def test_means_condition_not_judged(monkeypatch):
    marked = "- the host is slow [risk: unverified host speed]"
    record = _record(conditions=marked, means="use the shell script")
    doc = _new_form(record=record)
    assert submission._stage_order_violations(doc, _ctx()) == [], "a securing-marked record is clean"
    unmarked = _new_form(record=_record(conditions="- the host is slow"))
    assert any("unmarked condition" in p
               for p in submission._stage_order_violations(unmarked, _ctx()))

    stage = doc.stages[0]
    stage.conditions, stage.means.method = marked, "use the shell script"
    judge = Judge(answering(M.METHOD))
    _plan_pass(doc, judge, monkeypatch)

    assert judge.calls
    for text in ("the host is slow", "unverified host speed", "use the shell script",
                 "split the module"):
        assert not any(text in judged for judged in judge.texts), text


def test_place_is_one_of_the_statements_of_the_description():
    doc = _described_doc()
    labels = {s.label: s for s in M.description_statements(doc, 1)}
    assert labels["stage 1 place"].text == PLACE_TEXT
    assert labels["stage 1 place"].place == M.STAGE_DESCRIPTION


def test_description_every_prose_field_judged():
    doc = _described_doc(knowledge="what is known", preconditions="stage 0 done")
    statements = M.description_statements(doc, 1)
    labels = [s.label for s in statements]
    for field in ("title", "place", "result", "material", "invariants", "knowledge",
                  "preconditions", "done criterion"):
        assert f"stage 1 {field}" in labels, labels
    assert {s.place for s in statements} == {M.STAGE_DESCRIPTION}
    assert not any("binding condition" in label for label in labels)


def test_description_no_order_not_judged(monkeypatch):
    doc = parse_plan(_raw_plan({"place": PLACE_TEXT, "method": "write a shell script"}))
    assert doc.meta.order is None
    judge = Judge(answering(M.METHOD))

    assert _plan_pass(doc, judge, monkeypatch) is None
    assert judge.calls == []


def test_description_dry_run_not_judged(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION))

    directive, _ = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, dry_run=True)

    assert directive.ok and directive.action == "preview"
    assert judge.calls == [] and not M.cache_dir().exists()


_CONTOUR = {"parent_session": "p", "stage_index": 1, "norm": _NORM}
_SUB_PLAN_TEXT = {"means": "use the shell script tool", "method": "write a shell script",
                  "procedure": "1. write it 2. run it",
                  "conditions": "- the host is slow [risk: unverified host speed]"}


def test_description_child_contour_is_judged_on_its_description_only(monkeypatch):
    """A child contour is no exemption: a plan on a new-form order has its description read there,
    and only its description — the executor's record (means, method, procedure, conditions) never.
    A plan on an old-form order is not judged in a contour either."""
    state = _session("c", plan_path="plan.toml", parent_link=_CONTOUR)
    judge = Judge(answering(M.METHOD))

    refusal = _plan_pass(_described_doc(**_SUB_PLAN_TEXT), judge, monkeypatch, state=state)

    assert refusal is not None and judge.places == {M.STAGE_DESCRIPTION}
    for text in _SUB_PLAN_TEXT.values():
        assert not any(text in judged for judged in judge.texts), text

    old_form = Judge(answering(M.METHOD))
    assert _plan_pass(_described_doc(order=AGREED_OLD_FORM_ORDER, **_SUB_PLAN_TEXT),
                      old_form, monkeypatch, state=state) is None
    assert old_form.calls == []


def test_the_submit_flow_makes_no_judge_call(store, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the stage-order checks reached the method-statement judge")

    monkeypatch.setattr(M, "site_defects", forbidden)
    accepted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")


def test_the_submit_flow_in_a_child_contour_makes_no_judge_call(store, tmp_path, monkeypatch):
    monkeypatch.setattr(M, "site_defects", lambda *a, **k: pytest.fail("the judge was called"))
    refused = _walk_to_submit_in_contour(store, tmp_path, _RECORD_HEAD)
    assert refused.ok is False
    assert any("does not say how it keeps binding condition 'B1'" in p
               for p in _stage_order_problems(refused)), refused.data


def test_a_contour_whose_parent_session_has_no_state_file_is_refused_at_submit(store, tmp_path):
    marked = _RECORD_HEAD.replace("[probe: python3 -m pytest -q => passes]",
                                  "[means: stage 3]") + _HOW_KEPT
    refused = _walk_to_submit_in_contour(store, tmp_path, marked, parent_saved=False)
    assert refused.ok is False
    assert any("'p'" in p and "has no state file" in p
               for p in _stage_order_problems(refused)), refused.data
    assert store.load("c").sub_plan_records == {}


def test_a_good_record_goes_through_the_child_contour(store, tmp_path):
    accepted = _walk_to_submit_in_contour(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")
    state = store.load("c")
    assert list(state.sub_plan_records) == ["1"], "a contour record is keyed by its stage alone"
    record = sub_plan.read(store.load("p"), 1, store)
    assert record.method and [h.condition for h in record.how_kept] == ["B1"]
    assert record.estimate.author == "executor" and record.estimate.confirmer == "parent"


def test_a_child_contours_own_plan_on_a_two_sided_order_describes_its_stages(store, tmp_path):
    """A linked child's own plan is no exemption from the form the order chooses: an old-form
    stage under its two-sided order is refused with the way out (state the place and the binding
    conditions, leave the means, method, procedure and conditions to the sub-plan record), and the
    plan with a described stage goes through."""
    from test_order_agreement import _plan
    from test_submit_plan_surfaces import _session as _classified

    _described_parent(store, "p")
    _classified(store, "c")
    child = store.load("c")
    child.parent_link = {"parent_session": "p", "stage_index": 1, "norm": _NORM}
    store.save(child)

    refused = cli.cmd_submit_plan(
        Namespace(session="c", plan=_plan(tmp_path, describe=False)), store=store)

    assert refused.ok is False and refused.action == "fix_plan"
    problems = " | ".join(refused.data["problems"])
    assert "is a stage description and carries no means, method, procedure, conditions" in problems
    assert "belong to the executor's sub-plan record" in problems
    assert "states no place (`place`)" in problems

    described = cli.cmd_submit_plan(
        Namespace(session="c", plan=_plan(tmp_path, name="described.toml")), store=store)

    assert not any("stage description" in p for p in described.data.get("problems", [])), described.data


def test_a_stage_plan_submitted_to_an_order_not_yet_agreed_is_still_refused(store, tmp_path):
    from test_submit_plan_surfaces import _session as _classified, _write_plan

    _classified(store, "o")
    state = store.load("o")
    state.order_agreement_armed = True
    store.save(state)
    plan_file = _write_plan(tmp_path, tier="small")
    plan_file.write_text(plan_file.read_text(encoding="utf-8") + _RECORD_HEAD + _HOW_KEPT,
                         encoding="utf-8")

    refused = cli.cmd_submit_plan(Namespace(session="o", plan=str(plan_file)), store=store)

    assert refused.ok is False and refused.action == "fix_plan"
    assert any("no agreed order understanding" in b for b in refused.data["blockers"])
    assert store.load("o").sub_plan_records == {}


def test_the_plan_review_pass_refuses_a_described_stage_the_judge_cannot_read(tmp_path):
    path = _plan_file(tmp_path)
    state = _session("s", plan_path=str(path))
    refused = cli_review._method_statement_plan_pass_refusal(
        _plan_review_args(path), state, Judge(fail=True))
    assert refused is not None and refused.action == "fix_plan"
    assert "did not answer" in " ".join(refused.data["problems"])

    clean = cli_review._method_statement_plan_pass_refusal(_plan_review_args(path), state, Judge())
    assert clean is None


def test_stage_order_verb_sequence_to_review(store, tmp_path):
    accepted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")
    state = store.load("w")
    two_sided = _plan_file(tmp_path, name="two-sided")
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION))
    refusal = cli_review._method_statement_plan_pass_refusal(
        _plan_review_args(two_sided), state, judge)
    assert judge.places == {M.STAGE_DESCRIPTION}
    assert refusal is not None and refusal.action == "fix_plan"
    assert "states how the product is built" in " ".join(refusal.data["problems"])
