"""Every way a sub-plan estimate or its parent-side copy can be written, and what sees an edit.

Writers and readers of each guarded value are listed in `scripts/agentctl/README.md`
(§ Estimates composed per stage); each write path is walked here as the allowed writer or as a
refusal that names the way on. Nothing reaches a model judge or a model CLI.
"""
from __future__ import annotations

import copy

import pytest

from agentctl import cli_approval, gates, sub_plan
from agentctl.plan import diff_plans, load_plan, stage_carry_key, stage_question_key
from agentctl.stage_norm import StageNorm
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

import test_method_statement_rule as methods
import test_subplan_presentation_dispatch as base
from test_subplan_presentation_dispatch import (  # noqa: F401  (fixtures)
    CHILD, PARENT, SESSION, Spawns, _agent_approves_stage_resources, _described, _dispatch, _executing,
    _next_stage, _plan_approval_sets_resources, _put, _rec, _refused, _start_blockers, contour,
    home_store, ns,
)
from test_stage_estimates import _estimate_correct, _stage_one, _user_choice  # noqa: F401


@pytest.fixture
def store(home_store):
    return home_store


# --- the record: submit, resubmit and the child's carrier ---------------------------------------


def _resubmit(contour, *, minutes=40, basis="one module split and a suite run", extra=""):
    text = contour.text().replace("minutes = 40", f"minutes = {minutes}").replace(
        'basis = "one module split and a suite run"', f'basis = "{basis}"') + extra
    plan = contour.write(text)
    with contour.as_child():
        return contour.child.run("submit_plan", session=CHILD, plan=plan)


def test_submit_resubmit_and_restamp_write_the_record_only_through_the_guard(contour, tmp_path):
    """`submit-plan` is the one writer of a record's estimate. Before the parent agrees a figure a
    resubmission replaces it; submitting writes no figure into the parent's copy that prices
    anything; after the agreement the same figure passes and another is refused with the way on
    named; a re-agreement by `estimate-correct` restamps what the child must now carry."""
    assert contour.submit()["ok"] is True
    store = contour.store
    parent = store.load(PARENT)
    assert sub_plan.agreed_figure(parent, 1) is None
    assert not sub_plan.estimate_copy(parent, 1).get("agreed")

    again = _resubmit(contour, minutes=55, basis="two modules")
    assert again["ok"] is True, again
    assert sub_plan.read(store.load(PARENT), 1, store).estimate.minutes == 55
    assert sub_plan.agreed_figure(store.load(PARENT), 1) is None

    assert base._present_and_approve(store, tmp_path, sid=PARENT).ok
    assert _agent_approves_stage_resources(store, sid=PARENT).ok
    assert sub_plan.agreed_figure(store.load(PARENT), 1)["minutes"] == 55

    assert _resubmit(contour, minutes=55, basis="two modules")["ok"] is True
    for minutes, basis in ((70, "two modules"), (55, "a new basis")):
        refused = _resubmit(contour, minutes=minutes, basis=basis)
        assert refused["ok"] is False and refused["action"] == "fix_plan", refused
        assert "estimate-correct" in refused["detail"]
        assert sub_plan.read(store.load(PARENT), 1, store).estimate.minutes == 55
    forged = _resubmit(contour, minutes=55, basis="two modules", extra=(
        "[[meta.sub_plan.estimate.corrections]]\nto = { minutes = 99.0 }\nbasis = \"mine\"\n"))
    assert forged["ok"] is False and "cannot write" in forged["detail"], forged

    assert _estimate_correct(store, minutes=60, usd=2.0, basis="a third file", sid=PARENT).ok
    assert _resubmit(contour, minutes=55, basis="two modules")["ok"] is False
    assert _resubmit(contour, minutes=60, basis="a third file")["ok"] is True


# --- the verbs around the guard -------------------------------------------------------------------


def test_every_route_around_the_estimate_condition_is_refused_until_the_figure_is_agreed(
        store, venued_fixtures_dir, tmp_path):
    """A decided-later stage the user approved the sub-plan of, its figure not yet agreed:
    dispatch, a correction of a figure nobody agreed and the user's correction without an answer
    each leave the stage unstarted and the copy as it was, and a user approval of the sub-plan
    lifts the resource condition and not this one; the one way on (the parent's approval of the
    stage's resources, which agrees the named figure) starts it."""
    _stage_one(store, venued_fixtures_dir, lazy=True)
    assert base._present_and_approve(store, tmp_path).ok
    before = sub_plan.estimate_copy(store.load(SESSION), 1)
    status = store.load(SESSION).stage(1).outcome.status
    assert before == {}
    assert any("estimate is confirmed but not agreed" in line for line in _start_blockers(store))
    for attempt in (
        lambda: _dispatch(store, SESSION, Spawns()),
        lambda: _estimate_correct(store, minutes=60, basis="grew"),
        lambda: _estimate_correct(store, minutes=60, usd=1.0, basis="the user said so", by="fedor"),
    ):
        assert attempt().ok is False
        assert sub_plan.estimate_copy(store.load(SESSION), 1) == before
        assert store.load(SESSION).stage(1).outcome.status == status
    assert _agent_approves_stage_resources(store).ok
    assert _start_blockers(store) == []
    assert _dispatch(store, SESSION, Spawns()).ok is not False


def test_a_dry_run_of_a_spawned_stage_previews_and_changes_no_estimate(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    _put(store, _rec(1))
    before = store.path(SESSION).read_bytes()
    out = _dispatch(store, spawns=Spawns("spawn-specialist.py --kind developer\n"), dry_run=True)
    assert out.action == "preview", out
    assert store.path(SESSION).read_bytes() == before


def test_an_old_form_stage_keeps_its_tier_price_and_meets_none_of_the_estimate_conditions(
        store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = store.load(SESSION)
    assert state.stage(1).place is None
    assert gates.estimate_dispatch_blockers(state, state.stage(1), None) == []
    refused = _estimate_correct(store, minutes=60, basis="grew")
    assert refused.ok is False and "old-form" in refused.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}


# --- what sees an edit of a new field ----------------------------------------------------------------


def _new_form(tmp_path):
    doc = load_plan(methods._two_stage_plan(tmp_path))
    for stage in doc.stages:
        stage.place = "the engine"
        stage.binding_conditions = []
    return doc


def _seen_by(doc, edited):
    first, second = doc.stages[0], edited.stages[0]
    return {
        "carry_key": stage_carry_key(first) != stage_carry_key(second),
        "norm_carry_key": StageNorm.from_stage(first, doc=doc).carry_key()
        != StageNorm.from_stage(second, doc=edited).carry_key(),
        "question_key": any(
            stage_question_key(first, element) != stage_question_key(second, element)
            for element in ("method", "means", "conditions", "result", "done_criterion")),
        "diff_plans": diff_plans(doc, edited) != "no_change",
    }


_EDITS = {
    "place": (lambda d: setattr(d.stages[0], "place", "somewhere else")),
    "binding_conditions": (lambda d: setattr(
        d.stages[0], "binding_conditions", [{"id": "c1", "text": "a condition", "set_by": "user"}])),
    "expected_minutes": (lambda d: setattr(d.stages[0], "expected_minutes", 90)),
}


@pytest.mark.parametrize("field", sorted(_EDITS))
def test_change_decision_functions_and_the_edit_of_a_stage_description_field(tmp_path, field):
    doc = _new_form(tmp_path)
    edited = copy.deepcopy(doc)
    _EDITS[field](edited)
    assert _seen_by(doc, edited) == {
        "carry_key": True, "norm_carry_key": True, "question_key": False, "diff_plans": True}


@pytest.mark.parametrize("field", ["basis", "minutes", "corrections"])
def test_change_decision_functions_do_not_see_an_edit_of_the_sub_plan_estimate(tmp_path, field):
    """The estimate lives in the sub-plan record, outside the stage: an edit of it moves none of
    the three change-decision functions, so a changed figure is never taken as a plan change (the
    record's own guard and the parent's copy decide it)."""
    doc = _new_form(tmp_path)
    doc.meta.sub_plan = _rec(1)
    edited = copy.deepcopy(doc)
    estimate = edited.meta.sub_plan.estimate
    setattr(estimate, field, {"basis": "another basis", "minutes": 400.0,
                              "corrections": [{"to": {"minutes": 1.0}}]}[field])
    assert _seen_by(doc, edited) == {
        "carry_key": False, "norm_carry_key": False, "question_key": False, "diff_plans": False}
