"""The estimate of a stage: who writes it, what agrees it, what prices a stage from it.

The executor sets a figure and its basis in the sub-plan record; the record write guard
(`sub_plan.confirm_estimate`, run by `record_carried_by` for a frame's record and a child
contour's alike) is structural and replaces the figure freely until the parent agrees one. The
parent-side copy (an entry of `stage_approvals`, keyed by the order revision) is written only by
the parent's engine: on the per-stage resource approval, on `approve`, and by `estimate-correct`.
A stage's budget follows the agreed figure; the user's line follows only a figure the user
approved. Every guard test here goes red under a planted defect in `order_agreement_mutants.py`
(`stage_budget_tier_only`, `user_line_on_corrected_estimate`) or under a reverted hand edit named
in the test; nothing reaches a model judge or a model CLI.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, cli_approval, cli_common, effort, effort_segments, gates, own_contour, sub_plan
from agentctl.config import Thresholds
from agentctl.state import OrderAgreement, SessionState, estimate_correction_marker
from lib import widening_targets

import test_subplan_presentation_dispatch as base
from test_stage_order_subplan import _Store, _frame, _session
from test_subplan_presentation_dispatch import (  # noqa: F401  (fixtures)
    SESSION, Spawns, _agent_approves_stage_resources, _agree_estimate, _described, _dispatch,
    _env, _executing, _plan_approval_sets_resources, _present, _put, _rec, _refused,
    _start_blockers, home_store, ns,
)
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

SCRIPTS = Path(__file__).resolve().parent.parent
THR = Thresholds()
MARKED = "chosen with the marker"


@pytest.fixture
def store(home_store):
    return home_store


def _estimate(minutes=30, usd=1.0, basis="sized by the test", **kw):
    return sub_plan.Estimate(minutes=minutes, usd=usd, author="executor", confirmer="executor",
                             basis=basis, **kw)


def _doc(record):
    return SimpleNamespace(meta=SimpleNamespace(sub_plan=record))


def _record(minutes=30, usd=1.0, basis="sized by the test", stage=1, **kw):
    record = _rec(stage)
    record.reviews = []
    record.estimate = _estimate(minutes, usd, basis, **kw)
    return record


def _frame_session(sid="p"):
    """A session building the sub-plan of stage 1 of its outer plan in a pushed frame (the
    parent-side copy is read once the frame is popped, `plan_stack = []`)."""
    return _session(sid, plan_stack=[_frame(1)])


def _arming(state, stage=1):
    uid = state.stage_uid(stage)
    return effort_segments.open_arming_of(
        state.effort_segments, effort_segments.stage_segment(uid)) if uid else None


def _stage_one(store, venued_fixtures_dir, *, lazy=False, record=True, minutes=None):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=lazy, minutes=minutes)
    if record:
        _put(store, _rec())
    return store.load(SESSION)


def _arm(store, now=1000.0):
    state = store.load(SESSION)
    arming = cli_common._ensure_stage_arming(state, state.stage(1), now, "agent")
    store.save(state)
    return arming


def _estimate_correct(store, minutes=None, usd=None, basis="the work grew", by="agent",
                      sid=SESSION, stage=1):
    return cli_approval.cmd_estimate_correct(
        ns(session=sid, stage=stage, minutes=minutes, usd=usd, basis=basis, by=by), store=store)


def _user_choice(marker: str, *, session=SESSION, at=None, choose=True) -> int:
    """The user's answer to an ask offering `marker` beside an unmarked option, recorded by the
    real hook function."""
    spec = importlib.util.spec_from_file_location("hook_ask_answer_record_est",
                                                  SCRIPTS / "hook-ask-answer-record.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    question = "Does the corrected figure stand?"
    marked = f"Yes, it stands {marker}"
    other = "No, keep the figure the user approved"
    payload = {
        "tool_name": "AskUserQuestion", "session_id": session,
        "tool_input": {"questions": [{"question": question,
                                      "options": [{"label": marked}, {"label": other}]}]},
        "tool_response": {"answers": {question: marked if choose else other}},
    }
    return module.record(payload, now=time.time() + 5 if at is None else at)


# --- the record write guard ---------------------------------------------------------------

def test_unagreed_change_leaves_budget():
    """Before the parent agrees a figure the executor's writes replace figure and basis freely
    (the writer becomes author and confirmer), and no budget follows: the stage stays priced at
    its tier. After the agreement the same figure and basis is a no-op and a different one is
    refused with a named reason; a record without a basis never gets in."""
    state = _frame_session()
    carrier = _Store(state)
    stage = SimpleNamespace(index=1)
    tier = effort.stage_budget("medium", THR)

    for minutes, basis in ((30, "first sizing"), (90, "second sizing")):
        sub_plan.record_carried_by(state, _doc(_record(minutes, 2.0, basis)), carrier)
        stored = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]).estimate
        assert (stored.minutes, stored.basis) == (minutes, basis)
        assert stored.author == stored.confirmer == "p"
        copy = sub_plan.estimate_copy(state, 1, level=0)
        assert copy["agreed"] is False and copy["minutes"] == minutes
        state.plan_stack = []
        assert cli_common._agreed_budgets(state, stage, tier) == (tier, None)
        assert effort.composed_estimate(state, sub_plan.copies_of(state), THR)["stages"] == []
        state.plan_stack = [_frame(1)]

    state.plan_stack = []
    sub_plan.agree_copy(state, 1, minutes=90, usd=2.0, basis="second sizing", author="p",
                        agreer="p", authority=sub_plan.AUTHORITY_USER)
    state.plan_stack = [_frame(1)]
    again = _record(90, 2.0, "second sizing")
    sub_plan.record_carried_by(state, _doc(again), carrier)
    for other in (_record(120, 2.0, "second sizing"), _record(90, 2.0, "a new basis")):
        with pytest.raises(sub_plan.SubPlanError, match="estimate-correct"):
            sub_plan.record_carried_by(state, _doc(other), carrier)
    with pytest.raises(sub_plan.SubPlanError, match="no basis"):
        sub_plan.record_carried_by(state, _doc(_record(90, 2.0, "  ")), carrier)
    stored = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]).estimate
    assert (stored.minutes, stored.basis) == (90, "second sizing")

    # a tier change on resubmission or replan moves neither the agreed figure nor its budget
    for tier in ("small", "medium", "large"):
        priced = effort.stage_budget(tier, THR)
        state.plan_stack = []
        budget, user_budget = cli_common._agreed_budgets(state, stage, priced, tier)
        assert (budget["minutes"], budget["usd"]) == (90.0, 2.0), tier
        assert (user_budget["minutes"], user_budget["usd"]) == (priced["minutes"], priced["usd"]), tier
        state.plan_stack = [_frame(1)]
        sub_plan.record_carried_by(state, _doc(_record(90, 2.0, "second sizing")), carrier)
    copy = sub_plan.estimate_copy(state, 1, level=0)
    assert (copy["minutes"], copy["usd"], copy["basis"]) == (90.0, 2.0, "second sizing")


def test_stored_forged_child_record_is_refused_on_submission_and_not_corrected_by_it():
    """A child's stored record that carries a figure the linked parent never agreed (written
    around the guard) is refused when the child submits it, and a rewrite to the agreed figure is
    taken: the copy in the parent's state stays what the parent agreed throughout."""
    parent = _session("parent")
    child = _session("child", parent_link={"parent_session": "parent", "stage_index": 1})
    carrier = _Store(parent, child)
    sub_plan.agree_copy(parent, 1, minutes=30, usd=1.0, basis="child sizing", author="child",
                        agreer="parent", authority=sub_plan.AUTHORITY_USER)
    forged = _record(10, 1.0, "child sizing")
    sub_plan.store(child, forged, None)
    stored = sub_plan.SubPlanRecord.from_dict(child.sub_plan_records[sub_plan.record_key(1, None)])
    assert stored.estimate.minutes == 10.0
    with pytest.raises(sub_plan.SubPlanError, match="estimate-correct"):
        sub_plan.record_carried_by(child, _doc(stored), carrier)
    entry_before = dict(sub_plan.estimate_copy(parent, 1))
    sub_plan.record_carried_by(child, _doc(_record(30, 1.0, "child sizing")), carrier)
    assert sub_plan.estimate_copy(parent, 1) == entry_before
    assert sub_plan.agreed_figure(parent, 1) == {"minutes": 30.0, "usd": 1.0}


def test_child_estimate_correct_in_its_own_contour(store, venued_fixtures_dir, monkeypatch):
    """A linked child corrects the stages of its own plan only: inside the allowed overrun the
    correction is agreed under the parent's authority, past it the call is refused (the figure
    goes to the parent, nothing is held in the child), a decision `--by <user>` is refused in the
    child, and a call naming another session than the one the contour speaks for is refused."""
    _stage_one(store, venued_fixtures_dir)
    state = store.load(SESSION)
    state.parent_link = {"parent_session": "above", "stage_index": 3}
    store.save(state)
    _plan_approval_sets_resources(store)
    monkeypatch.setenv(cli_approval.OWN_SESSION_ENV, SESSION)

    inside = _estimate_correct(store, minutes=40, basis="the work grew")
    assert inside.ok, inside
    copy = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert copy["minutes"] == 40.0 and copy["authority"] == sub_plan.AUTHORITY_PARENT
    assert copy["corrections"][-1]["agreer"] == SESSION

    past = _estimate_correct(store, minutes=900, basis="a rewrite")
    assert past.ok is False and past.data is None and "more than the allowed overrun" in past.detail
    assert not sub_plan.estimate_copy(store.load(SESSION), 1).get("held")

    by_user = _estimate_correct(store, minutes=45, basis="the user said so", by="fedor")
    assert by_user.ok is False and gates.CHILD_ESCALATES_THE_ESTIMATE in by_user.detail

    monkeypatch.setenv(cli_approval.OWN_SESSION_ENV, "someone-else")
    foreign = _estimate_correct(store, minutes=41, basis="from another contour")
    assert foreign.ok is False and "calling session's own plan" in foreign.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1)["minutes"] == 40.0


def test_record_cannot_write_a_correction():
    """`estimate.corrections` is written by `estimate-correct` alone: a record carrying an entry
    the parent-side copy does not hold is refused on either carrier, and an entry the copy holds
    is carried back unchanged."""
    state = _frame_session()
    carrier = _Store(state)
    entry = {"from": {"minutes": 30.0, "usd": 1.0}, "to": {"minutes": 40.0, "usd": 1.0},
             "basis": "self-written", "by": "executor", "agreer": "executor", "authority": "agent"}
    forged = _record(corrections=[entry])
    with pytest.raises(sub_plan.SubPlanError, match="cannot write"):
        sub_plan.record_carried_by(state, _doc(forged), carrier)
    assert "0:1" not in state.sub_plan_records

    state.plan_stack = []
    sub_plan.agree_copy(state, 1, minutes=30, usd=1.0, basis="sized by the test", author="p",
                        agreer="p", authority=sub_plan.AUTHORITY_AGENT)
    sub_plan.add_correction(state, 1, entry)
    state.plan_stack = [_frame(1)]
    carried = _record(corrections=[entry])
    sub_plan.record_carried_by(state, _doc(carried), carrier)
    assert sub_plan.SubPlanRecord.from_dict(
        state.sub_plan_records["0:1"]).estimate.corrections == [entry]
    sub_plan.record_carried_by(state, _doc(_record(corrections=[])), carrier)
    assert sub_plan.SubPlanRecord.from_dict(
        state.sub_plan_records["0:1"]).estimate.corrections == [entry]


def test_agreed_basis_is_compared_without_its_surrounding_whitespace():
    """A record that carries the agreed figure and basis back is the same estimate whatever
    whitespace surrounds the basis; a different basis is still a correction."""
    state = _session("p")
    carrier = _Store(state)
    sub_plan.agree_copy(state, 1, minutes=30, usd=1.0, basis="sized by the test", author="p",
                        agreer="p", authority=sub_plan.AUTHORITY_AGENT)
    state.plan_stack = [_frame(1)]
    sub_plan.record_carried_by(state, _doc(_record(basis="  sized by the test \n")), carrier)
    with pytest.raises(sub_plan.SubPlanError, match="is a correction"):
        sub_plan.record_carried_by(state, _doc(_record(basis="sized otherwise")), carrier)


def test_child_contour_carrier_is_guarded_against_the_parent_copy():
    """The child contour's record is guarded against the copy in the linked parent's state: once
    the parent agreed a figure, the child's rewrite of it is refused; before, it is replaced."""
    parent = _session("parent")
    child = _session("child", parent_link={"parent_session": "parent", "stage_index": 1})
    carrier = _Store(parent, child)
    sub_plan.record_carried_by(child, _doc(_record(30, 1.0, "child sizing")), carrier)
    assert sub_plan.estimate_copy(parent, 1) == {}
    sub_plan.agree_copy(parent, 1, minutes=30, usd=1.0, basis="child sizing", author="child",
                        agreer="parent", authority=sub_plan.AUTHORITY_USER)
    with pytest.raises(sub_plan.SubPlanError, match="estimate-correct"):
        sub_plan.record_carried_by(child, _doc(_record(60, 1.0, "child sizing")), carrier)
    sub_plan.record_carried_by(child, _doc(_record(30, 1.0, "child sizing")), carrier)


def test_child_is_guarded_against_the_copy_at_the_level_its_parent_spawned_from():
    """A parent that has pushed a frame since it spawned the child holds the stage's copy at the
    level it spawned from (`parent_level`), not at the level it stands on now."""
    parent = _session("parent")
    sub_plan.agree_copy(parent, 1, minutes=30, usd=1.0, basis="child sizing", author="child",
                        agreer="parent", authority=sub_plan.AUTHORITY_USER)
    parent.plan_stack = [_frame(1)]
    assert sub_plan.estimate_copy(parent, 1) == {}
    child = _session("child", parent_link={"parent_session": "parent", "stage_index": 1,
                                           "parent_level": 0})
    carrier = _Store(parent, child)
    with pytest.raises(sub_plan.SubPlanError, match="estimate-correct"):
        sub_plan.record_carried_by(child, _doc(_record(60, 1.0, "child sizing")), carrier)
    sub_plan.record_carried_by(child, _doc(_record(30, 1.0, "child sizing")), carrier)


# --- the parent-side copy: who writes it -----------------------------------------------------

def _case_agent_inside_set(store, vfd, tmp_path):
    _stage_one(store, vfd)
    _plan_approval_sets_resources(store)
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_USER
    assert (copy["minutes"], copy["usd"], copy["basis"]) == (30.0, 1.0, "sized by the test")
    assert copy["user_approved_figure"] == {"minutes": 30.0, "usd": 1.0}
    assert copy["agreer"] == SESSION


def _case_resource_approval_by_agent(store, vfd, tmp_path):
    _stage_one(store, vfd)
    state = store.load(SESSION)
    sub_plan.write_parent_approval(state, 1, sub_plan.read(state, 1, store).resource_request)
    store.save(state)
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    assert _agent_approves_stage_resources(store).ok
    copy = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_AGENT
    assert copy["user_approved_figure"] == {"minutes": copy["minutes"], "usd": copy["usd"]}
    assert sub_plan.user_figure(store.load(SESSION), 1) == {"minutes": 30.0, "usd": 1.0}


def _case_resource_approval_by_user(store, vfd, tmp_path):
    _stage_one(store, vfd)
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    _present(store, tmp_path)
    done = cli_approval.cmd_approve_resources(ns(session=SESSION, stage=1, by="fedor"), store=store)
    assert done.ok, done
    copy = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_USER
    assert copy["user_approved_figure"] == {"minutes": 30.0, "usd": 1.0}


def _case_no_basis_refused(store, vfd, tmp_path):
    _stage_one(store, vfd, record=False)
    record = _rec()
    record.estimate.basis = ""
    _put(store, record)
    _plan_approval_sets_resources(store)
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    refused = cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=1), store=store)
    assert refused.ok is False and "no estimate with a basis" in refused.detail


def _case_changed_after_user_sub_plan_approval(store, vfd, tmp_path):
    _stage_one(store, vfd, lazy=True)
    assert base._present_and_approve(store, tmp_path).ok
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    changed = _record(31, 1.0, "resized after the user approved")
    _put(store, changed)
    refused = _agent_approves_stage_resources(store)
    assert refused.ok is False and "changed since the sub-plan the user approved" in refused.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}


def _case_decided_later_first_figure_past_margin_held(store, vfd, tmp_path):
    _stage_one(store, vfd, lazy=True)
    huge = _record(400, 1.0, "the whole module is rewritten")
    _put(store, huge)
    assert base._present_and_approve(store, tmp_path, record=huge).ok
    held = _agent_approves_stage_resources(store)
    assert held.ok is False and held.data == {"estimate": "held"}
    assert "AskUserQuestion" in held.detail and estimate_correction_marker(1, 400.0, 1.0) in held.detail
    copy = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert not copy.get("agreed") and copy["held"]["minutes"] == 400.0
    assert "escalated" not in copy["held"]
    assert sub_plan.agreed_figure(store.load(SESSION), 1) is None
    refused = _dispatch(store, SESSION, Spawns())
    command = f"estimate-correct --stage 1 {sub_plan.figure_args(copy['held'])} --basis"
    _refused(refused, "held past the allowed overrun", command, "--by <their name>")
    assert command in held.detail


def _case_child_past_margin_escalates(store, vfd, tmp_path):
    state = _stage_one(store, vfd, lazy=True)
    state.parent_link = {"parent_session": "above", "stage_index": 3}
    store.save(state)
    huge = _record(400, 1.0, "the whole module is rewritten")
    _put(store, huge)
    state = store.load(SESSION)
    outcome, text = cli_approval._agree_first_figure(
        state, state.stage(1), huge, authority=sub_plan.AUTHORITY_PARENT,
        approved_digest=sub_plan.estimate_digest(huge.estimate))
    assert outcome == "escalate" and gates.CHILD_ESCALATES_THE_ESTIMATE in text
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["held"]["escalated"] is True and copy["held"]["minutes"] == 400.0
    assert not copy.get("agreed") and sub_plan.agreed_figure(state, 1) is None
    blockers = gates.estimate_dispatch_blockers(state, state.stage(1), huge)
    assert any(gates.CHILD_ESCALATES_THE_ESTIMATE in b and "held" in b for b in blockers), blockers


def _case_child_stage_resource_approval_records_the_escalation(store, vfd, tmp_path, monkeypatch):
    assert base._present_and_approve(
        store, tmp_path, record=_put_huge_decided_later(store, vfd)).ok
    state = store.load(SESSION)
    state.parent_link = {"parent_session": "above", "stage_index": 3}
    store.save(state)
    monkeypatch.setattr(cli_approval, "_agent_approval_outside", lambda *a, **k: [])
    held = _agent_approves_stage_resources(store)
    assert held.ok is False and held.data == {"estimate": "escalate"}
    assert gates.CHILD_ESCALATES_THE_ESTIMATE in held.detail
    state = store.load(SESSION)
    assert sub_plan.estimate_copy(state, 1)["held"]["escalated"] is True
    assert sub_plan.approval_entry(state, 1).get("parent_resource_approval")
    blockers = gates.estimate_dispatch_blockers(state, state.stage(1), sub_plan.read(state, 1, store))
    assert any(gates.CHILD_ESCALATES_THE_ESTIMATE in b for b in blockers), blockers


def _put_huge_decided_later(store, vfd, minutes=400, basis="the whole module is rewritten"):
    _stage_one(store, vfd, lazy=True)
    huge = _record(minutes, 1.0, basis)
    _put(store, huge)
    return huge


def _tier_price(state, stage=1) -> dict:
    price = effort.tier_figure(state.stage(stage), THR)
    return {"minutes": price[effort_segments.SCALE_MINUTES], "usd": price[effort_segments.SCALE_USD]}


def _case_decided_later_agreed_by_the_agent_sets_the_line_at_the_tier_price(store, vfd, tmp_path):
    _stage_one(store, vfd, lazy=True)
    assert base._present_and_approve(store, tmp_path).ok
    assert _agent_approves_stage_resources(store).ok
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_AGENT
    assert (copy["minutes"], copy["usd"]) == (30.0, 1.0)
    assert sub_plan.user_figure(state, 1) == _tier_price(state) != {"minutes": 30.0, "usd": 1.0}


def _case_decided_later_agreed_by_the_user_past_the_margin_sets_the_line_at_the_tier_price(
        store, vfd, tmp_path):
    huge = _put_huge_decided_later(store, vfd, minutes=200, basis="a large module")
    assert base._present_and_approve(store, tmp_path, record=huge).ok
    _plan_approval_sets_resources(store)
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_USER and copy["minutes"] == 200.0
    assert sub_plan.user_figure(state, 1) == _tier_price(state) == {"minutes": 10.0, "usd": 1.0}


def _case_held_figure_decided_by_the_user_reaches_dispatch(store, vfd, tmp_path):
    huge = _put_huge_decided_later(store, vfd)
    assert base._present_and_approve(store, tmp_path, record=huge).ok
    assert _agent_approves_stage_resources(store).data == {"estimate": "held"}
    refused = _dispatch(store, SESSION, Spawns())
    _refused(refused, "estimate-correct --stage 1 --minutes 400.0 --usd 1.0 --basis")

    assert _user_choice(estimate_correction_marker(1, 400.0, 1.0)) == 1
    decided = _estimate_correct(store, minutes=400, usd=1.0, basis="the user decided", by="fedor")
    assert decided.ok, decided
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_USER and not copy.get("held")
    assert sub_plan.user_figure(state, 1) == {"minutes": 400.0, "usd": 1.0}
    assert _start_blockers(store) == []
    assert _dispatch(store, SESSION, Spawns()).ok is not False


def _case_user_approval_agrees_a_figure_past_the_margin_as_the_line(store, vfd, tmp_path):
    _stage_one(store, vfd)
    huge = _record(400, 1.0, "the whole module is rewritten")
    _put(store, huge)
    _present(store, tmp_path)
    done = cli_approval.cmd_approve_resources(ns(session=SESSION, stage=1, by="fedor"), store=store)
    assert done.ok, done
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_USER and not copy.get("held")
    assert sub_plan.user_figure(state, 1) == {"minutes": 400.0, "usd": 1.0}


def _case_estimate_changed_after_the_presentation_refused_by_resource_approval_of_the_user(
        store, vfd, tmp_path):
    _stage_one(store, vfd)
    _present(store, tmp_path)
    _put(store, _record(31, 1.0, "resized after it was shown"))
    refused = cli_approval.cmd_approve_resources(ns(session=SESSION, stage=1, by="fedor"), store=store)
    assert refused.ok is False and refused.data == {"estimate": "refused"}
    assert "changed since the sub-plan the user approved" in refused.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    assert not sub_plan.approval_entry(store.load(SESSION), 1).get("parent_resource_approval")
    _present(store, tmp_path)
    assert cli_approval.cmd_approve_resources(
        ns(session=SESSION, stage=1, by="fedor"), store=store).ok


def _case_estimate_changed_after_the_presentation_refused_by_resource_approval_of_the_agent(
        store, vfd, tmp_path):
    _stage_one(store, vfd)
    _present(store, tmp_path)
    state = store.load(SESSION)
    sub_plan.write_parent_approval(state, 1, sub_plan.read(state, 1, store).resource_request)
    store.save(state)
    _put(store, _record(31, 1.0, "resized after it was shown"))
    refused = _agent_approves_stage_resources(store)
    assert refused.ok is False and refused.data == {"estimate": "refused"}
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}


def _case_estimate_changed_after_the_presentation_refused_by_plan_approval(store, vfd, tmp_path):
    _stage_one(store, vfd)
    _present(store, tmp_path)
    _put(store, _record(31, 1.0, "resized after it was shown"))
    _plan_approval_sets_resources(store)
    state = store.load(SESSION)
    assert sub_plan.estimate_copy(state, 1) == {}
    [event] = [h for h in state.history if h.get("event") == "estimate_not_agreed"]
    assert event["outcome"] == "refused" and "changed since" in event["reason"]


_CASES = {
    "plan-approval-agrees-the-named-figure": _case_agent_inside_set,
    "agent-resource-approval-agrees-the-figure-as-the-user-line": _case_resource_approval_by_agent,
    "user-resource-approval-sets-the-user-line": _case_resource_approval_by_user,
    "user-resource-approval-agrees-a-figure-past-the-margin-as-the-line":
        _case_user_approval_agrees_a_figure_past_the_margin_as_the_line,
    "no-basis-refused": _case_no_basis_refused,
    "changed-after-the-users-subplan-approval-refused": _case_changed_after_user_sub_plan_approval,
    "estimate-changed-after-the-presentation-refused-at-plan-approval":
        _case_estimate_changed_after_the_presentation_refused_by_plan_approval,
    "estimate-changed-after-the-presentation-refused-at-the-users-resource-approval":
        _case_estimate_changed_after_the_presentation_refused_by_resource_approval_of_the_user,
    "estimate-changed-after-the-presentation-refused-at-the-agents-resource-approval":
        _case_estimate_changed_after_the_presentation_refused_by_resource_approval_of_the_agent,
    "decided-later-first-figure-past-margin-held": _case_decided_later_first_figure_past_margin_held,
    "decided-later-agreed-by-the-agent-sets-the-line-at-the-tier-price":
        _case_decided_later_agreed_by_the_agent_sets_the_line_at_the_tier_price,
    "decided-later-agreed-by-the-user-past-the-margin-sets-the-line-at-the-tier-price":
        _case_decided_later_agreed_by_the_user_past_the_margin_sets_the_line_at_the_tier_price,
    "held-figure-decided-by-the-user-sets-the-line-and-reaches-dispatch":
        _case_held_figure_decided_by_the_user_reaches_dispatch,
    "child-past-margin-escalates": _case_child_past_margin_escalates,
    "child-stage-resource-approval-records-the-escalation":
        _case_child_stage_resource_approval_records_the_escalation,
}


@pytest.mark.parametrize("case", list(_CASES))
def test_first_figure_agreed_on_resource_approval(case, store, venued_fixtures_dir, tmp_path,
                                                  monkeypatch):
    """Every first-figure agreement path: the line it sets (the tier price for a decided-later
    stage, the approved figure otherwise), the digest it binds, the hold or escalation of a figure
    past the margin and how dispatch refuses a held one."""
    case_fn = _CASES[case]
    if "monkeypatch" in case_fn.__code__.co_varnames[:case_fn.__code__.co_argcount]:
        case_fn(store, venued_fixtures_dir, tmp_path, monkeypatch)
    else:
        case_fn(store, venued_fixtures_dir, tmp_path)


def test_plan_approval_agrees_the_figures_then_freezes_the_composed_task_estimate(
        store, venued_fixtures_dir):
    """`approve` fixes the task estimate the user approves after the stages' figures are agreed:
    the figure the plan was armed with plus each agreed stage's figure less the tier price it
    replaces, with no overrun margin in the way (the user decides, the engine does not measure
    them). Run again over the same figures the result is the same: a stage counted once is not
    counted on top of itself."""
    _stage_one(store, venued_fixtures_dir)
    state = store.load(SESSION)
    armed = {effort.SCALE_SPEND: 4.0, effort.SCALE_WALL_CLOCK: 40.0}
    tier = effort.tier_figure(state.stage(1), THR)
    expected = {
        effort.SCALE_SPEND: 4.0 + 1.0,
        effort.SCALE_WALL_CLOCK: 40.0 + 30.0 - tier[effort_segments.SCALE_MINUTES],
    }

    first = cli_approval._agree_figures_and_freeze_task_estimate(state, store, dict(armed))
    assert first == expected != armed
    assert sub_plan.estimate_copy(state, 1)["agreed"] and "frozen_in" in sub_plan.estimate_copy(state, 1)
    assert state.order_effort_frozen == armed, "the caller records the composed figure, not this function"

    second = cli_approval._agree_figures_and_freeze_task_estimate(state, store, dict(armed))
    assert second == first


def test_decided_later_stage_without_a_figure_is_skipped_by_plan_approval(store, venued_fixtures_dir):
    _stage_one(store, venued_fixtures_dir, lazy=True, record=False)
    _plan_approval_sets_resources(store)
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}


def test_below_threshold_stage_priced_at_size_class_without_confirmation_or_review(
        store, venued_fixtures_dir):
    """A stage below the size line is priced at its tier price, agreed by the plan's approval,
    with no record, basis, confirmation or method review; an old-form stage keeps its tier price
    and has no copy."""
    _executing(store, venued_fixtures_dir)
    _described(store, minutes=5)
    _plan_approval_sets_resources(store)
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["agreed"] and copy["authority"] == sub_plan.AUTHORITY_TIER
    assert copy["basis"] == sub_plan.TIER_BASIS and copy["author"] == ""
    assert sub_plan.read(state, 1, store) is None
    assert _start_blockers(store) == []
    composed = effort.composed_estimate(state, sub_plan.copies_of(state), THR)
    row = next(r for r in composed["stages"] if r["stage"] == 1)
    assert row["source"] == "agreed" and row["authority"] == sub_plan.AUTHORITY_TIER

    stage = state.stage(1)
    priced = effort.stage_budget(effort.stage_tier(stage), THR)
    assert cli_common._agreed_budgets(state, stage, priced) == (priced, None)

    refused = _estimate_correct(store, minutes=40, basis="grew")
    assert refused.ok and sub_plan.estimate_copy(store.load(SESSION), 1)["minutes"] == 40.0
    assert sub_plan.estimate_copy(store.load(SESSION), 1)["authority"] == sub_plan.AUTHORITY_AGENT

    old_form = store.load(SESSION)
    old_form.stage(2).place = None
    store.save(old_form)
    nothing = _estimate_correct(store, minutes=40, basis="grew", stage=2)
    assert nothing.ok is False and "old-form stage" in nothing.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 2) == {}


# --- estimate-correct ---------------------------------------------------------------------

def test_estimate_boundary_shares_fire_margin():
    """The 'past the allowed overrun' test of an estimate is `scale_crossed` under
    `fire_margin`: the same predicate and the same margin the fire route judges a segment by,
    at and around its boundary on each scale."""
    margin = effort_segments.fire_margin(THR)
    for scale, limit in ((effort_segments.SCALE_MINUTES, 25.0), (effort_segments.SCALE_USD, 3.0),
                         (effort_segments.SCALE_MINUTES, 100.0)):
        boundary = max(margin.multiple * limit, limit + margin.floor[scale])
        for actual in (limit, boundary - 0.01, boundary, boundary + 0.01, boundary * 3):
            crossed = effort_segments.scale_crossed(actual, limit, margin.multiple, margin.floor[scale])
            past = effort.estimate_past_margin({scale: actual}, {scale: limit}, THR)
            assert (past == [scale]) is crossed, (scale, limit, actual)
    assert effort.estimate_past_margin({"minutes": 1000.0}, {}, THR) == []
    arming = {"segment": "stage:x", "budget": {"minutes": 25.0, "usd": 3.0},
              "carried": {"minutes": 0.0, "usd": 0.0}, "booked": {"minutes": 125.0, "usd": 0.0}}
    judged = effort_segments.judge(arming, margin.multiple, margin.floor)
    assert judged == effort.estimate_past_margin({"minutes": 125.0, "usd": 0.0},
                                                 arming["budget"], THR)


def test_reagreement_rekeys_parent_copy(store, venued_fixtures_dir):
    """The copy is keyed by the order revision: a re-agreed order makes the earlier entries a
    record that prices nothing, the new revision's copy is written beside them, and the stage
    is refused at dispatch until its figure is agreed again."""
    _stage_one(store, venued_fixtures_dir, lazy=True)
    state = store.load(SESSION)
    _agree_estimate(state)
    assert sub_plan.agreed_figure(state, 1) == {"minutes": 30.0, "usd": 1.0}
    first = dict(sub_plan.estimate_copy(state, 1))
    frozen = {effort.SCALE_SPEND: 7.0, effort.SCALE_WALL_CLOCK: 70.0}
    state.order_effort_frozen = dict(frozen)

    state.order_agreement_history.append({"agreed_at": 1.0})
    assert sub_plan.order_revision(state) == 1
    assert sub_plan.estimate_copy(state, 1) == {}
    assert sub_plan.agreed_figure(state, 1) is None
    row = effort.composed_estimate(state, sub_plan.copies_of(state), THR)["stages"][0]
    assert row["source"] == "tier"
    refused = gates.stage_start_blockers(state, state.stage(1), store=store)
    assert any("confirmed but not agreed" in b for b in refused), refused

    sub_plan.agree_copy(state, 1, minutes=45, usd=1.5, basis="new revision", author="x",
                        agreer=SESSION, authority=sub_plan.AUTHORITY_USER)
    estimates = sub_plan.approval_entry(state, 1)["estimates"]
    assert set(estimates) == {"0", "1"}
    assert estimates["0"] == first and estimates["1"]["minutes"] == 45.0
    assert sub_plan.agreed_figure(state, 1) == {"minutes": 45.0, "usd": 1.5}
    assert sub_plan.estimate_copy(state, 1)["corrections"] == []
    assert not any("confirmed but not agreed" in b
                   for b in gates.stage_start_blockers(state, state.stage(1), store=store))
    assert state.order_effort_frozen == frozen, "the user-approved estimate is fixed by `approve` alone"


def test_overrun_without_user_decision_unapplied(store, venued_fixtures_dir):
    """A correction past the allowed overrun is not applied by the agent: at the root it is held
    beside the copy and the stage is refused at dispatch until the user decides; a linked child
    is sent to its parent; `--by <user>` without the user's marked answer is refused naming the
    marker. In every case the agreed figure, the corrections and the budget are unchanged."""
    _stage_one(store, venued_fixtures_dir)
    _plan_approval_sets_resources(store)
    arming = _arm(store)
    assert arming["budget"]["minutes"] == 30.0
    before = dict(sub_plan.estimate_copy(store.load(SESSION), 1))

    held = _estimate_correct(store, minutes=400, basis="much more than thought")
    assert held.ok is False and held.data == {"estimate": "held"}
    assert estimate_correction_marker(1, 400.0, 1.0) in held.detail or "marked" in held.detail
    state = store.load(SESSION)
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["minutes"] == 30.0 and copy["corrections"] == [] and copy["held"]["minutes"] == 400.0
    assert _arming(state)["budget"]["minutes"] == 30.0
    refused = base._dispatch(store, SESSION, Spawns(), dry_run=False)
    assert refused.ok is False and "held" in (refused.detail + " ".join(refused.data.get("blockers", [])))

    marker = estimate_correction_marker(1, 400.0, 1.0)
    no_answer = _estimate_correct(store, minutes=400, usd=1.0, basis="the user agreed", by="fedor")
    assert no_answer.ok is False and marker in no_answer.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1)["minutes"] == 30.0

    assert _user_choice(marker) == 1
    still_agent = _estimate_correct(store, minutes=400, usd=1.0, basis="the agent applies it")
    assert still_agent.ok is False and still_agent.data == {"estimate": "held"}
    after_choice = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert after_choice["minutes"] == 30.0 and after_choice["corrections"] == []
    assert _arming(store.load(SESSION))["budget"]["minutes"] == 30.0

    state = store.load(SESSION)
    _arming(state)["carried"]["minutes"] = 500.0
    store.save(state)
    unrelated = _estimate_correct(store, minutes=40, usd=1.0, basis="inside the margin")
    assert unrelated.ok, "a stage's overrun in actual effort does not make a figure correction past the margin"
    assert sub_plan.estimate_copy(store.load(SESSION), 1)["minutes"] == 40.0

    state = store.load(SESSION)
    state.parent_link = {"parent_session": "above", "stage_index": 2}
    store.save(state)
    sent_up = _estimate_correct(store, minutes=900, basis="more still")
    assert sent_up.ok is False and gates.CHILD_ESCALATES_THE_ESTIMATE in sent_up.detail
    user_at_depth = _estimate_correct(store, minutes=400, usd=1.0, basis="x", by="fedor")
    assert user_at_depth.ok is False and gates.CHILD_ESCALATES_THE_ESTIMATE in user_at_depth.detail
    after = sub_plan.estimate_copy(store.load(SESSION), 1)
    assert after["minutes"] == 40.0 and len(after["corrections"]) == 1
    assert after["user_approved_figure"] == before["user_approved_figure"]


def test_budget_follows_agreed_correction(store, venued_fixtures_dir):
    """An agreed figure prices the stage's open arming (the first agreement and each agreed
    correction); the correction is recorded with its author, agreer, authority and basis, and the
    record's estimate carries it; a below-line copy and a figure the parent never agreed price
    nothing."""
    _stage_one(store, venued_fixtures_dir)
    arming = _arm(store)
    assert arming["budget"] == effort.stage_budget(arming["declared_tier"], THR)
    assert "user_budget" not in arming

    _plan_approval_sets_resources(store)
    state = store.load(SESSION)
    assert _arming(state)["budget"] == {"minutes": 30.0, "usd": 1.0}
    assert _arming(state)["user_budget"] == {"minutes": 30.0, "usd": 1.0}
    assert _arming(state)["budget_source"] == effort_segments.SOURCE_AGREED

    done = _estimate_correct(store, minutes=60, usd=2.0, basis="two modules instead of one")
    assert done.ok, done
    state = store.load(SESSION)
    assert _arming(state)["budget"] == {"minutes": 60.0, "usd": 2.0}
    assert _arming(state)["user_budget"] == {"minutes": 30.0, "usd": 1.0}
    copy = sub_plan.estimate_copy(state, 1)
    assert copy["authority"] == sub_plan.AUTHORITY_AGENT and copy["agreer"] == SESSION
    [entry] = copy["corrections"]
    assert entry["from"] == {"minutes": 30.0, "usd": 1.0} and entry["to"] == {"minutes": 60.0, "usd": 2.0}
    assert entry["basis"] == "two modules instead of one" and entry["authority"] == "agent"
    record = sub_plan.read(state, 1, store)
    assert (record.estimate.minutes, record.estimate.usd) == (60.0, 2.0)
    assert record.estimate.corrections == [entry]
    composed = effort.composed_estimate(state, sub_plan.copies_of(state), THR)
    assert next(r for r in composed["stages"] if r["stage"] == 1)["minutes"] == 60.0


def test_user_line_ignores_corrections(store, venued_fixtures_dir, monkeypatch):
    """The user's line stays at the figure the user approved when an agent corrects: a stage
    past its corrected figure but not past the approved one fires on the difficulty line only;
    stage 6's overrun case. A correction decided by the user (on the ledger answer choosing the
    marked option, once) moves it."""
    _stage_one(store, venued_fixtures_dir)
    _plan_approval_sets_resources(store)
    _arm(store)
    assert _estimate_correct(store, minutes=60, usd=2.0, basis="two modules").ok
    state = store.load(SESSION)
    arming = _arming(state)
    assert sub_plan.user_figure(state, 1) == {"minutes": 30.0, "usd": 1.0}
    assert arming["user_budget"] == {"minutes": 30.0, "usd": 1.0}
    margin = effort_segments.fire_margin(THR)
    act = sorted(effort_segments.judging_acts_of(arming["segment"]))[0]

    arming["carried"]["minutes"] = 160.0
    fires = effort_segments.lines_to_fire(arming, act, margin.multiple, margin.floor)
    assert fires[effort_segments.LINE_USER] == [effort_segments.SCALE_MINUTES]
    assert fires[effort_segments.LINE_DIFFICULTY] == []
    arming["carried"]["minutes"] = 310.0
    fires = effort_segments.lines_to_fire(arming, act, margin.multiple, margin.floor)
    assert fires[effort_segments.LINE_DIFFICULTY] == fires[effort_segments.LINE_USER] == [
        effort_segments.SCALE_MINUTES]

    # (b) a figure is measured against the user's line, not the agent-corrected figure: 150 min is
    # inside the margin of the corrected 60 and past that of the approved 30
    stage = state.stage(1)
    approved = sub_plan.estimate_copy(state, 1)["user_approved_figure"]
    assert cli_approval._overrun_scales(state, stage, {"minutes": 149.0, "usd": 2.0}, THR, approved) == []
    assert cli_approval._overrun_scales(
        state, stage, {"minutes": 150.0, "usd": 2.0}, THR, approved) == [effort_segments.SCALE_MINUTES]
    assert _arming(state)["user_budget"] == {"minutes": 30.0, "usd": 1.0}

    # (c) the composed task estimate is measured against the one the user approved
    inside = {"minutes": 120.0, "usd": 1.0}
    assert cli_approval._overrun_scales(state, stage, inside, THR, approved) == []
    state.order_effort_frozen = {effort.SCALE_SPEND: 5.0, effort.SCALE_WALL_CLOCK: 10.0}
    assert cli_approval._overrun_scales(
        state, stage, inside, THR, approved) == [effort_segments.SCALE_MINUTES]
    state.order_effort_frozen = None

    marker = estimate_correction_marker(1, 80.0, 2.0)
    assert _user_choice(marker) == 1
    decided = _estimate_correct(store, minutes=80, usd=2.0, basis="the user decided", by="fedor")
    assert decided.ok, decided
    state = store.load(SESSION)
    assert sub_plan.user_figure(state, 1) == {"minutes": 80.0, "usd": 2.0}
    assert sub_plan.estimate_copy(state, 1)["authority"] == sub_plan.AUTHORITY_USER
    assert _arming(state)["user_budget"] == {"minutes": 80.0, "usd": 2.0}
    again = _estimate_correct(store, minutes=80, usd=2.0, basis="replayed", by="fedor")
    assert again.ok is False, "one answer licenses one correction"
    wrong = estimate_correction_marker(1, 90.0, 2.0)
    assert _user_choice(wrong, choose=False) == 1
    other = _estimate_correct(store, minutes=90, usd=2.0, basis="x", by="fedor")
    assert other.ok is False and wrong in other.detail

    # (e) the user is not margin-measured: a decision past the margin of the user's own line is
    # applied on the marked answer and moves the line to the decided figure
    huge = estimate_correction_marker(1, 600.0, 2.0)
    assert _user_choice(huge) == 1
    big = _estimate_correct(store, minutes=600, usd=2.0, basis="the user decided it is big", by="fedor")
    assert big.ok, big
    state = store.load(SESSION)
    assert sub_plan.user_figure(state, 1) == {"minutes": 600.0, "usd": 2.0}
    assert not sub_plan.estimate_copy(state, 1).get("held")

    # (f) a re-agreed order does not hand the spent answer back: it licenses no second decision
    state.order_agreement_history.append(OrderAgreement(
        understanding_sha256="re-agreed", parts={}, draft_path="", at=1.0))
    sub_plan.agree_copy(state, 1, minutes=30, usd=1.0, basis="the re-agreed order", author="x",
                        agreer=SESSION, authority=sub_plan.AUTHORITY_USER)
    store.save(state)
    replay = _estimate_correct(store, minutes=600, usd=2.0, basis="replayed after re-agreement",
                               by="fedor")
    assert replay.ok is False and huge in replay.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1)["minutes"] == 30.0


def test_correction_before_first_approval(store, venued_fixtures_dir):
    """Until the first figure is agreed or held, nothing is corrected: the executor rewrites its
    own estimate; the refusal names that way on, and the way on works."""
    _stage_one(store, venued_fixtures_dir)
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    early =_estimate_correct(store, minutes=60, basis="grew")
    assert early.ok is False and "rewrites the estimate in its own record" in early.detail
    assert sub_plan.estimate_copy(store.load(SESSION), 1) == {}
    _plan_approval_sets_resources(store)
    assert _estimate_correct(store, minutes=60, basis="grew").ok


def test_agreed_correction_after_approve_subplan_keeps_dispatchable(
        store, venued_fixtures_dir, tmp_path):
    """The estimate is outside the digest of the user's sub-plan approval: a correction agreed
    after `approve-subplan` leaves that approval standing, and the decided-later stage starts."""
    _stage_one(store, venued_fixtures_dir, lazy=True)
    assert base._present_and_approve(store, tmp_path).ok
    done = _agent_approves_stage_resources(store)
    assert done.ok, done
    assert _start_blockers(store) == []
    assert _estimate_correct(store, minutes=45, usd=1.5, basis="a second file").ok
    state = store.load(SESSION)
    assert sub_plan.estimate_copy(state, 1)["minutes"] == 45.0
    assert _start_blockers(store) == []
    spawns = Spawns()
    assert base._dispatch(store, SESSION, spawns).ok is not False


def test_presentation_names_authorship_agreement_correction_and_the_users_figure():
    from agentctl import render
    est = _estimate(minutes=90, usd=3.0, basis="two files")
    copy = {"agreed": True, "agreer": "parent-session", "authority": "parent",
            "corrections": [{"to": {"minutes": 120.0, "usd": 4.0}, "agreer": "parent-session",
                             "basis": "a third file"}],
            "user_approved_figure": {"minutes": 90.0, "usd": 3.0}}
    text = render._estimate_text(est, copy)
    assert "(basis: two files; written by executor)" in text
    assert "agreed by parent-session (parent authority)" in text
    assert "corrected to 120.0 min / $4.0 by parent-session (a third file)" in text
    assert "the user approved 90.0 min / $3.0" in text
    held = render._estimate_text(est, {"held": {"minutes": 900.0, "usd": 30.0}})
    assert "held at 900.0 min / $30.0, awaiting the user's decision" in held
    assert "agreed by" not in held


def test_presentation_shows_the_composed_total_and_marks_decided_later_stages_provisional(
        store, venued_fixtures_dir):
    """What the user approves is shown as a number: the composed task estimate with each stage
    above the line at the figure its sub-plan states, and a decided-later stage named as provisional
    at its tier price (its line and the total's note), never as a figure that is not yet written."""
    from agentctl import render

    _stage_one(store, venued_fixtures_dir)
    state = store.load(SESSION)

    def total(owner):
        return render._composed_total_text(owner, lambda index: sub_plan.read(owner, index, store))

    stated = effort.composed_estimate(state, {1: {"agreed": True, "minutes": 30.0, "usd": 1.0}}, THR)["total"]
    shown = total(state)
    assert render._figure_text({"minutes": stated["minutes"], "usd": stated["usd"]}) in shown
    assert "decided later" not in shown

    later = _stage_one(store, venued_fixtures_dir, lazy=True)
    shown = total(later)
    assert "stage 1 decided later and provisional at its tier price until it is planned" in shown
    tier_only = effort.composed_estimate(later, {}, THR)["total"]
    assert render._figure_text({"minutes": tier_only["minutes"], "usd": tier_only["usd"]}) in shown
    [line] = render._subplan_stage_lines([later.stage(1)], lambda index: None, {}, "")
    assert "decided later — provisional estimate: its tier" in line


def test_estimate_correct_is_the_agents_verb_and_not_the_users():
    assert "estimate-correct" in own_contour.OWN_CONTOUR_VERBS
    assert "estimate-correct" not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert "estimate-correct" in cli_common._SESSION_COMMANDS
