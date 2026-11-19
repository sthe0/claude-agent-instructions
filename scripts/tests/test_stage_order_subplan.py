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


# --- 2. The submission checks and their two carriers ------------------------------------------

from argparse import Namespace  # noqa: E402

from agentctl import cli, cli_common, submission  # noqa: E402
from agentctl.state import (  # noqa: E402
    FinalCheck, GateRecord, Node, Partition, Route, StageStatus, WeightClass,
)
from agentctl.submission import StageOrderContext  # noqa: E402

BINDING = ({"id": "B1", "text": "no push", "set_by": "user"},)


def _doc(stage_extra=None, record=None):
    raw = _raw_plan({"place": "the engine", **(stage_extra or {})})
    if record is not None:
        raw["meta"].update(record)
    return parse_plan(raw)


def _problems(doc, ctx=None):
    return submission.submission_violations(doc, stage_order=ctx)


def _ctx(**kw):
    return StageOrderContext(expected_stage=2, binding_conditions=BINDING, thresholds=TIGHT, **kw)


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
    problems = _problems(_doc({"means": "Edit", "method": "split it"}))
    assert any("stage 1 states a place" in p and "no means, method" in p for p in problems), problems


def test_a_described_stage_that_states_conditions_is_refused():
    problems = _problems(_doc({"conditions": "- the module imports [risk: unverified]"}))
    assert any("no conditions" in p for p in problems), problems


def test_a_description_alone_draws_no_stage_order_violation():
    assert submission._stage_order_violations(_doc(), _ctx()) == []


def test_an_old_form_plan_draws_no_stage_order_violation():
    old_form = parse_plan(_raw_plan({"means": "Edit", "method": "m"}))
    assert submission._stage_order_violations(old_form, _ctx(record_exists=lambda i: False)) == []


def test_below_threshold_stage_needs_only_description():
    doc = _doc({"declared_size": 5, "expected_minutes": 2})
    assert submission._stage_order_violations(doc, _ctx(record_exists=lambda i: False)) == []


def test_an_above_threshold_stage_needs_a_record_before_the_parent_is_approved():
    doc = _doc({"declared_size": 400})
    problems = submission._stage_order_violations(doc, _ctx(record_exists=lambda i: False))
    assert any("stage 1 is above the size line and has no sub-plan record" in p for p in problems)
    assert submission._stage_order_violations(doc, _ctx(record_exists=lambda i: True)) == []
    assert submission._stage_order_violations(doc, _ctx()) == []  # the submit seam does not ask yet


def test_a_lazy_stage_decides_its_sub_plan_later():
    doc = _doc({"declared_size": 400, "lazy": True})
    assert submission._stage_order_violations(doc, _ctx(record_exists=lambda i: False)) == []


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


def test_every_binding_condition_is_kept_or_released_by_its_setter():
    two = (*BINDING, {"id": "B2", "text": "stay in the worktree", "set_by": "user"})
    ctx = StageOrderContext(expected_stage=2, binding_conditions=two, thresholds=TIGHT)
    assert any("does not say how it keeps binding condition 'B2'" in p
               for p in _record_problems(_record(), ctx))
    released = _record(setter_decisions=[
        {"condition": "B1", "value": "kept", "setter": "user", "decided_by": "user"},
        {"condition": "B2", "value": "withdrawn", "setter": "user", "decided_by": "user"}])
    assert _record_problems(released, ctx) == []


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


def _described_parent(store, sid):
    from agentctl.state import Stage as _Stage

    parent = _Stage(
        index=1, title="stage 1", subject=Subject(material="m", result="img"),
        means=Means(means="", method=""), actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
        outcome=Outcome(status=StageStatus.ACTIVE.value), place="the engine",
        binding_conditions=[dict(c) for c in BINDING])
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
    by_contour = _problems(flawed, cli_common._stage_order_context(contour))
    assert by_frame == by_contour and len(by_frame) == 2, (by_frame, by_contour)
    assert cli_common._stage_order_context(framed).expected_stage == 1
    assert cli_common._stage_order_context(contour).expected_stage == 1


def test_the_context_resolves_searches_in_the_carrying_session():
    carrying = _session("c", order_searches=[{"id": "S1"}],
                        parent_link={"stage_index": 1, "norm": {}})
    ctx = cli_common._stage_order_context(carrying)
    assert ctx.search_resolver("S1") and not ctx.search_resolver("S2")
    assert ctx.binding_conditions is None


def test_the_approve_context_finds_a_record_in_the_frame_and_in_a_linked_contour(store):
    parent = _described_parent(store, "p")
    child = _session("child", parent_link={"parent_session": "p", "stage_index": 1, "norm": {}})
    sub_plan.store(child, sub_plan.SubPlanRecord(stage=1, method="m"))
    store.save(child)
    ctx = cli_common._stage_order_context(parent, store=store, require_records=True)
    assert ctx.record_exists(1) and not ctx.record_exists(2)
    assert cli_common._stage_order_context(parent).record_exists is None


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
    assert sub_plan.read(store.load("w"), 1).how_kept[0].condition == "B1"


def test_a_good_record_is_stored_at_submit_with_all_its_elements_and_its_building_interval(
        store, tmp_path):
    accepted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")
    state = store.load("w")
    record = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["1"])
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


# --- 3. The stage-description review ---------------------------------------------------------

from agentctl import cli_review, method_statement as M, render  # noqa: E402
from agentctl.plan import load_plan  # noqa: E402
from test_method_statement_rule import (  # noqa: E402
    Judge, _dispatch_on_new_form_plan, _plan_file, answering,
)

PLACE_TEXT = "the release engine, relied on by stage 2"
HOW_BUILT_TEXT = "the release command is a shell script"


def _described_doc(tmp_path, *, two_sided=True, name="plan", **fields):
    doc = load_plan(_plan_file(tmp_path, two_sided=two_sided, name=name))
    stage = doc.stages[0]
    stage.place = fields.pop("place", PLACE_TEXT)
    stage.subject.result = fields.pop("result", stage.subject.result)
    for key, value in fields.items():
        setattr(stage, key, value)
    return doc


def test_description_method_field_refused():
    judge = Judge(answering(M.METHOD))
    problems = _problems(_doc({"method": "write a shell script", "procedure": "1. do it"}))
    assert any("stage 1 states a place" in p and "method" in p and "procedure" in p
               for p in problems), problems
    assert judge.calls == []


def test_description_binding_conditions_not_judged(tmp_path):
    doc = _described_doc(tmp_path, binding_conditions=[
        {"id": "B1", "text": "the build host runs Linux", "set_by": "user"}])
    judge = Judge(answering(M.METHOD))

    render.stage_description_site_defects(doc, 1, runner=judge)

    assert judge.calls and judge.places == {M.STAGE_DESCRIPTION}
    assert not any("build host runs Linux" in text for text in judge.texts)


def test_description_how_built_is_defect(tmp_path):
    doc = _described_doc(tmp_path, result=HOW_BUILT_TEXT)
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION, containing=HOW_BUILT_TEXT))

    lines = render.stage_description_site_defects(doc, 1, runner=judge)

    assert len(lines) == 1, lines
    assert "stage 1 result (stage description)" in lines[0]
    assert "states how the product is built" in lines[0]


def test_description_extra_field_how_built_reported(tmp_path):
    doc = _described_doc(tmp_path, place=HOW_BUILT_TEXT)
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION, containing=HOW_BUILT_TEXT))

    lines = render.stage_description_site_defects(doc, 1, runner=judge)

    assert len(lines) == 1 and "stage 1 place (stage description)" in lines[0], lines
    assert "states how the product is built" in lines[0]


def test_description_old_form_not_judged(tmp_path):
    doc = _described_doc(tmp_path, two_sided=False)
    judge = Judge(answering(M.METHOD))

    assert render.stage_description_site_defects(doc, 1, runner=judge) == []
    assert M.plan_statements(doc) == []
    assert judge.calls == []


def test_description_could_not_read_is_defect(tmp_path):
    doc = _described_doc(tmp_path)
    judge = Judge(fail=True)

    findings = render.stage_description_site_findings(doc, 1, runner=judge)

    assert len(findings) == 1 and findings[0].verdict == M.COULD_NOT_READ
    assert len(judge.calls) == 1
    assert "the judge did not answer" in M.defect_line(findings[0])


def test_means_condition_not_judged(tmp_path):
    doc = _described_doc(tmp_path, conditions="- a means condition [risk: the build host is slow]")
    doc.stages[0].means.method = "use the shell script"
    judge = Judge(answering(M.METHOD))

    render.stage_description_site_defects(doc, 1, runner=judge)

    assert judge.calls
    for text in ("a means condition", "the build host is slow", "use the shell script"):
        assert not any(text in judged for judged in judge.texts), text


def test_place_is_one_of_the_statements_of_the_description(tmp_path):
    doc = _described_doc(tmp_path)
    labels = {s.label: s for s in M.description_statements(doc, 1)}
    assert labels["stage 1 place"].text == PLACE_TEXT
    assert labels["stage 1 place"].place == M.STAGE_DESCRIPTION


def _plan_review_args(path):
    return Namespace(verdict="pass", scope="", target=str(path))


def test_description_every_prose_field_judged(tmp_path):
    doc = _described_doc(tmp_path, knowledge="what is known", preconditions="stage 0 done")
    statements = M.description_statements(doc, 1)
    labels = [s.label for s in statements]
    for field in ("title", "place", "result", "material", "invariants", "knowledge",
                  "preconditions", "done criterion"):
        assert f"stage 1 {field}" in labels, labels
    assert {s.place for s in statements} == {M.STAGE_DESCRIPTION}
    assert not any("binding condition" in label for label in labels)


def test_description_no_order_not_judged(tmp_path, monkeypatch):
    _assert_flow_makes_no_judge_call(tmp_path, monkeypatch, "no-order")


def test_description_dry_run_not_judged(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION))

    directive, _ = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, dry_run=True)

    assert directive.ok and directive.action == "preview"
    assert judge.calls == [] and not M.cache_dir().exists()


def test_description_child_contour_not_judged(tmp_path, monkeypatch):
    _assert_flow_makes_no_judge_call(tmp_path, monkeypatch, "child-contour")


def _assert_flow_makes_no_judge_call(tmp_path, monkeypatch, flow):
    path = _plan_file(tmp_path, two_sided=False)
    assert not plan.is_two_sided(load_plan(path).meta.order)
    if flow == "no-order":
        orderless = parse_plan(_raw_plan({"place": PLACE_TEXT}))
        assert orderless.meta.order is None
        monkeypatch.setattr(cli_review, "load_plan", lambda target: orderless)
    parent_link = ({"parent_session": "p", "stage_index": 1, "norm": {}}
                   if flow == "child-contour" else None)
    state = _session("s", plan_path=str(path), parent_link=parent_link)
    judge = Judge(answering(M.METHOD))

    assert cli_review._method_statement_plan_pass_refusal(
        _plan_review_args(path), state, judge) is None
    assert judge.calls == []


def test_the_submit_flow_makes_no_judge_call(store, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the stage-order checks reached the method-statement judge")

    monkeypatch.setattr(M, "site_defects", forbidden)
    accepted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert accepted.ok, accepted.data.get("problems")


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
