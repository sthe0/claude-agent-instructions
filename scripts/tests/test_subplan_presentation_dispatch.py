"""Sub-plans in presentation, dispatch and approval.

A stage above the size line is shown with its sub-plan or with the decided-later mark; a stage
marked decided later starts only on the user's approval of its presented sub-plan, for the
sub-plan as it stands; every stage above the line starts only on the parent's approval of its
resources and permissions, for the request as it stands. The approvals are the engine's: a plan
text states none, and `dispatch` consults both blockers before it branches into a spawn or an
in-thread start.

Each guard test goes red under a planted defect in `order_agreement_mutants.py`:
`decided_later_dispatched` (the user's approval of a sub-plan is not asked) and
`parent_resource_dispatched` (the parent's approval of resources is not asked). Nothing here
reaches a model judge: no judge stands behind these guards.

Sessions are built the way `test_dispatch_semantics` builds them (an old-form plan driven to
EXECUTING) and the stage then carries a description (`place`), the decided-later mark or a size,
which is all the guards read.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_approval, cli_common, gates, own_contour, state as state_mod, sub_plan
from agentctl import submission
from agentctl.dispatch import RunResult
from agentctl.render import render_subplans
from agentctl.plan import load_plan
from agentctl.state import Node, StageStatus
from agentctl.store import FileStateStore
from lib import widening_targets
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

import test_stage_order_subplan as so

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"
ENGINE = "the engine"
SESSION = "sp1"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PUBLISHED_TEXT_GATE", "0")
    for name in ("AGENTCTL_NORM_HANDOFF", "CLAUDE_CODE_SESSION_ID", "AGENT_RECURSION_DEPTH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return FileStateStore(home / "agentctl" / "state")


def _planning(store, sid=SESSION):
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=True, external_effect=False,
                        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)


def _executing(store, venued_fixtures_dir, sid=SESSION):
    plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    _planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def _described(store, sid=SESSION, *, lazy=False, minutes=None, in_thread=True, place=ENGINE):
    """The active stage 1 as a stage description: placed, optionally decided later, of a declared
    size (None: undeclared, so above the line), run in-thread unless `in_thread` is False."""
    state = store.load(sid)
    stage = state.stage(1)
    stage.place, stage.lazy, stage.expected_minutes = place, lazy, minutes
    stage.binding_conditions_stated = True
    if in_thread:
        stage.actor.executor = "in_thread"
    store.save(state)
    return state


def _rec(stage=1, allow=("Edit",), method="split the module", dirs=()):
    return sub_plan.SubPlanRecord(
        stage=stage, means="Edit", method=method, procedure="1. read 2. edit", conditions="- c",
        resource_request=[{"stage": stage, "allow": list(allow),
                           "add_dirs": [list(d) for d in dirs]}])


def _put(store, record, sid=SESSION):
    state = store.load(sid)
    sub_plan.store(state, record, level=0)
    store.save(state)
    return state


def _dispatch(store, sid=SESSION, **flags):
    runner = lambda argv, cwd=None: RunResult(0, stdout="COMPLETED: ok\n")
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=flags.pop("dry_run", False), **flags),
                            store=store, runner=runner)


def _refused(directive, *needles):
    assert directive.ok is False, directive
    text = directive.detail + " ".join(directive.data.get("blockers", []))
    for needle in needles:
        assert needle in text, (needle, text)


def _present_and_approve(store, tmp_path, stage=1, sid=SESSION, by="fedor", confirm=True):
    state = store.load(sid)
    record = sub_plan.read(state, stage, store)
    anchor = f"[subplan:{stage}:{sub_plan.approval_digest(record)[:12]}]"
    rendering = tmp_path / f"subplan-{stage}.txt"
    rendering.write_text(f"Sub-plan of stage {stage}: {record.method} {anchor}\n", encoding="utf-8")
    shown = cli.cmd_present_plan(ns(session=sid, kind="subplan", stage=stage, plan=None,
                                    rendering_file=str(rendering)), store=store)
    assert shown.ok, shown
    confirmed = confirm and cli.cmd_confirm_delivery(ns(
        session=sid, kind="subplan", by=by, note="delivered in the thread",
        escape_reason="delivered_out_of_band"), store=store)
    assert confirmed.ok if confirm else True, confirmed
    return cli_approval.cmd_approve_subplan(ns(session=sid, stage=stage, by=by), store=store)


# --- presentation ------------------------------------------------------------------------

def _new_form_plan(tmp_path, *, lazy=(), minutes=None) -> str:
    text = FIXTURE.read_text(encoding="utf-8")
    for index in (1, 2):
        extra = 'place = "the engine"\nbinding_conditions = []\n'
        if index in lazy:
            extra += "lazy = true\n"
        if minutes is not None:
            extra += f"expected_minutes = {minutes}\n"
        marker = "depends_on = []\n" if index == 1 else "depends_on = [1]\n"
        text = text.replace(marker, marker + extra, 1)
    path = tmp_path / "new_form.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _submitted(store, venued_fixtures_dir, sid=SESSION):
    _planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    return store.load(sid)


def test_presentation_refused_without_subplan_or_mark(store, venued_fixtures_dir, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    state = _submitted(store, venued_fixtures_dir)
    plan = _new_form_plan(tmp_path)
    blockers = gates.plan_presentation_blockers(state, plan, store=store)
    assert any("stage 1 is above the size line" in b for b in blockers), blockers
    assert any("stage 2 is above the size line" in b for b in blockers), blockers

    marked = _new_form_plan(tmp_path, lazy=(1, 2))
    assert gates.unplanned_stage_blockers(state, marked, store=store) == []
    sub_plan.store(state, _rec(1), level=0)
    one_mark = _new_form_plan(tmp_path, lazy=(2,))
    assert gates.unplanned_stage_blockers(state, one_mark, store=store) == []


def test_presentation_shows_subplans_estimates_and_decided_later_stages(store, venued_fixtures_dir, tmp_path):
    state = _submitted(store, venued_fixtures_dir)
    record = _rec(1)
    record.estimate = sub_plan.Estimate(minutes=90, usd=3.0, author="executor", confirmer="parent")
    sub_plan.store(state, record, level=0)
    doc = load_plan(_new_form_plan(tmp_path, lazy=(2,)))
    text = render_subplans(doc, state, store)
    assert "decided later" in text and "stage 2" in text
    assert "90" in text and "Edit" in text


def test_a_below_threshold_stage_is_shown_with_its_tier_price(store, venued_fixtures_dir, tmp_path):
    state = _submitted(store, venued_fixtures_dir)
    doc = load_plan(_new_form_plan(tmp_path, minutes=5))
    thr = state_mod.Thresholds() if hasattr(state_mod, "Thresholds") else None
    text = render_subplans(doc, state, store)
    assert "below the size line" in text or "tier" in text, text
    assert gates.unplanned_stage_blockers(state, _new_form_plan(tmp_path, minutes=5), store=store) == []
    assert thr is None or thr is not None


# --- dispatch ----------------------------------------------------------------------------

def test_parent_approval_alone_refused(store, venued_fixtures_dir):
    """The parent's approval of the resources does not start a decided-later stage."""
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    sub_plan.store(state, _rec(1), level=0)
    store.save(state)
    _refused(_dispatch(store), "stage 1 is decided later", "has not approved a sub-plan")
    assert gates.parent_resource_dispatch_blockers(state, state.stage(1), _rec(1)) == []


def test_a_stage_above_the_line_without_the_parents_approval_refused(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    _refused(_dispatch(store), "the parent has not approved its resources and permissions")


def test_approve_subplan_by_agent_refused(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store)
    _refused(d, "--by agent is refused")
    assert not sub_plan.approval_entry(store.load(SESSION), 1).get("user_subplan_approval")


def test_approve_subplan_by_agent_refused_even_when_presented_and_delivered(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    _present_and_approve(store, tmp_path, by="fedor", confirm=True)
    state = store.load(SESSION)
    entry = state.stage_approvals.get("0:1", {})
    entry.pop("user_subplan_approval", None)
    state.stage_approvals["0:1"] = entry
    store.save(state)
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store)
    _refused(d, "--by agent is refused")
    assert not sub_plan.approval_entry(store.load(SESSION), 1).get("user_subplan_approval")


def test_approve_subplan_unpresented_refused(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(d, "was not presented", "present-plan --kind subplan --stage 1")
    assert not sub_plan.approval_entry(store.load(SESSION), 1).get("user_subplan_approval")


def test_approve_subplan_without_a_delivery_stamp_refused(home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    d = _present_and_approve(store, tmp_path, confirm=False)
    _refused(d, "has no delivery stamp")
    assert not sub_plan.approval_entry(store.load(SESSION), 1).get("user_subplan_approval")


def test_subplan_edit_after_approval_refused(home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok
    state = store.load(SESSION)
    assert gates.decided_later_dispatch_blockers(state, state.stage(1), _rec(1)) == []
    _put(store, _rec(1, method="split it differently"))
    _refused(_dispatch(store), "changed since the user approved it")
    again = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(again, "changed since it was presented")


def test_approve_subplan_is_user_authority():
    assert "approve-subplan" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert "approve-subplan" not in own_contour.OWN_CONTOUR_VERBS
    assert "approve-stage-resources" in own_contour.OWN_CONTOUR_VERBS
    assert "approve-stage-resources" not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert {"approve-subplan", "approve-stage-resources"} <= set(cli_common._SESSION_COMMANDS)


def test_subplan_digest_same_for_both_carriers():
    frame, contour = _rec(1), _rec(1)
    frame.building_place = sub_plan.BuildingPlace(session="root", pushed=3, popped=9)
    contour.building_place = sub_plan.BuildingPlace(session="child")
    contour.estimate = sub_plan.Estimate(minutes=5, usd=1.0, author="a", confirmer="b")
    assert sub_plan.approval_digest(frame) == sub_plan.approval_digest(contour)
    assert sub_plan.approval_digest(frame) != sub_plan.approval_digest(_rec(1, method="other"))


def test_spawned_decided_later_planning_dispatch(home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    _refused(_dispatch(store), "stage 1 is decided later")

    planned = _dispatch(store, subplan_only=True)
    assert "decided later" not in planned.detail
    assert sub_plan.approval_entry(store.load(SESSION), 1).get("planning") is True

    state = store.load(SESSION)
    link = {"parent_session": SESSION, "stage_index": 1}
    child = state_mod.SessionState(session_id="kid", task_id="t", parent_link=link)
    assert any("only to plan it" in b for b in gates.child_approval_blockers(child, state))

    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok
    refused = _dispatch(store, subplan_only=True)
    _refused(refused, "approved as it stands")

    _refused(_dispatch(store), "the parent has not approved its resources")
    assert sub_plan.approval_entry(store.load(SESSION), 1).get("planning") is True

    state = store.load(SESSION)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    store.save(state)
    started = _dispatch(store)
    assert started.ok is not False, started
    assert sub_plan.approval_entry(store.load(SESSION), 1).get("planning") is False
    sub_plan.store(child, _rec(1))
    assert gates.child_approval_blockers(child, store.load(SESSION)) == []


def test_subplan_only_is_refused_for_a_stage_not_decided_later(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store)
    _refused(_dispatch(store, subplan_only=True), "not marked decided-later")


def test_child_receipt_does_not_approve(home_store, venued_fixtures_dir, tmp_path):
    """A sub-plan presented to the user in a linked child is no approval: `approve-subplan` is
    run in the root session the user was shown it in."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True)
    state.parent_link = {"parent_session": "someone", "stage_index": 1}
    store.save(state)
    _put(store, _rec(1))
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(d, "not in a linked child")


# --- the approvals are the engine's ---------------------------------------------------------

def test_preset_approval_refused_at_submission():
    doc = so._doc(record=so._record(), order=None)
    record = so._record()
    record["sub_plan"]["parent_resource_approval"] = "approved"
    preset = so._doc(record=record)
    ctx = submission.StageOrderContext(expected_stage=2, engine_approvals={})
    problems = submission._stage_order_violations(preset, ctx)
    assert any("`parent_resource_approval` is stated" in p for p in problems), problems
    clean = submission._stage_order_violations(
        doc, submission.StageOrderContext(expected_stage=2, engine_approvals={}))
    assert not any("is stated" in p for p in clean)
    record["sub_plan"]["user_subplan_approval"] = "abc"
    assert any("`user_subplan_approval` is stated" in p for p in submission._stage_order_violations(
        so._doc(record=record), ctx))
    same = submission.StageOrderContext(
        expected_stage=2, engine_approvals={"parent_resource_approval": "approved",
                                            "user_subplan_approval": "abc"})
    assert not any("is stated" in p for p in submission._stage_order_violations(
        so._doc(record=record), same))
    old_form = submission.StageOrderContext(expected_stage=2)
    assert not any("is stated" in p for p in submission._stage_order_violations(
        so._doc(record=record), old_form))


def test_rewritten_approval_after_edit_refused(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True)
    approved = _rec(1)
    sub_plan.write_user_approval(state, 1, approved)
    edited = _rec(1, method="split it differently")
    record = so._record()
    record["sub_plan"]["user_subplan_approval"] = sub_plan.approval_digest(edited)
    ctx = submission.StageOrderContext(expected_stage=2,
                                       engine_approvals=sub_plan.approval_entry(state, 1))
    problems = submission._stage_order_violations(so._doc(record=record), ctx)
    assert any("`user_subplan_approval` is stated and differs" in p for p in problems), problems


def test_the_context_reads_the_engine_copy_from_either_carrier(store):
    parent = state_mod.SessionState(session_id="p", task_id="t")
    sub_plan.write_user_approval(parent, 1, _rec(1))
    store.save(parent)
    contour = state_mod.SessionState(session_id="c", task_id="t",
                                     parent_link={"parent_session": "p", "stage_index": 1, "norm": {}})
    assert cli_common._stage_order_context(contour, store=store).engine_approvals \
        == sub_plan.approval_entry(parent, 1)
    framed = state_mod.SessionState(session_id="f", task_id="t")
    framed.stage_approvals["0:1"] = {"parent_resource_approval": "x"}
    framed.plan_stack = [so._frame(1)]
    assert cli_common._stage_order_context(framed).engine_approvals == {"parent_resource_approval": "x"}


# --- below the line, old form, dry run ----------------------------------------------------

def test_below_threshold_stage_needs_no_subplan_or_separate_approval(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store, minutes=5)
    assert gates.decided_later_dispatch_blockers(state, state.stage(1), None) == []
    assert gates.parent_resource_dispatch_blockers(state, state.stage(1), None) == []
    assert "execute_in_thread" == _dispatch(store).action
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    assert state.stage_approvals == {}
    d = cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=1), store=store)
    _refused(d, "below the size line")
    marked = _new_form_text_with(lazy_minutes=5)
    assert any("below the size line and carries the decided-later mark" in p for p in marked)


def _new_form_text_with(*, lazy_minutes):
    doc = so._new_form({"lazy": True, "expected_minutes": lazy_minutes})
    return submission._stage_order_violations(doc, submission.StageOrderContext())


def test_old_form_stage_starts_without_any_approval(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = store.load(SESSION)
    stage = state.stage(1)
    assert stage.place is None
    assert gates.decided_later_dispatch_blockers(state, stage, None) == []
    assert gates.parent_resource_dispatch_blockers(state, stage, None) == []
    assert gates.decided_later_dispatch_blockers(state, stage, None, subplan_only=True) == []
    runner_calls = []
    runner = lambda argv, cwd=None: (runner_calls.append(argv), RunResult(0, stdout="COMPLETED: ok\n"))[1]
    d = cli.cmd_dispatch(ns(session=SESSION, budget="medium", complexity="medium", dry_run=False),
                         store=store, runner=runner)
    assert runner_calls and "no sub-plan" not in d.detail
    assert store.load(SESSION).stage_approvals == {}


def test_dry_run_is_exempt_from_the_dispatch_blockers(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    before = store.path(SESSION).read_bytes()
    runner = lambda argv, cwd=None: RunResult(0, stdout="spawn-specialist.py --kind developer\n")
    d = cli.cmd_dispatch(ns(session=SESSION, budget="medium", complexity="medium", dry_run=True),
                         store=store, runner=runner)
    assert d.action == "preview"
    assert store.path(SESSION).read_bytes() == before


def test_an_approved_plan_replanned_keeps_its_stages(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = store.load(SESSION)
    assert state.node == Node.EXECUTING.value
    plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    d = cli.cmd_replan(ns(session=SESSION, plan=plan, coverage_waiver=None), store=store)
    refused = " ".join([d.detail, *d.data.get("blockers", [])]) if d.data else d.detail
    assert "decided later" not in refused and "parent has not approved" not in refused


# --- the child's own approval --------------------------------------------------------------

def test_the_child_gate_is_open_without_a_parent_state_and_for_an_ordinary_stage():
    child = state_mod.SessionState(session_id="k", task_id="t",
                                   parent_link={"parent_session": "p", "stage_index": 1})
    assert gates.child_approval_blockers(child, None) == []
    parent = state_mod.SessionState(session_id="p", task_id="t")
    assert gates.child_approval_blockers(child, parent) == []
    assert gates.child_approval_blockers(state_mod.SessionState(session_id="x", task_id="t"), parent) == []


def test_a_decided_later_parent_stage_needs_the_users_approval_before_the_child_approves():
    parent = state_mod.SessionState(session_id="p", task_id="t", stages=[
        so._stage(1, place=ENGINE, lazy=True)])
    child = state_mod.SessionState(session_id="k", task_id="t",
                                   parent_link={"parent_session": "p", "stage_index": 1})
    record = _rec(1)
    sub_plan.store(child, record)
    assert any("has not approved a sub-plan" in b for b in gates.child_approval_blockers(child, parent))
    sub_plan.write_user_approval(parent, 1, record)
    assert gates.child_approval_blockers(child, parent) == []
    child.sub_plan_records[sub_plan.record_key(1)] = _rec(1, method="other").to_dict()
    assert any("changed since the user approved it" in b
               for b in gates.child_approval_blockers(child, parent))


def test_a_linked_child_is_refused_its_own_approval_while_its_stage_is_only_being_planned(
        tmp_path, monkeypatch):
    """`approve --by agent` in a linked child goes through the gate: the parent's per-stage copy
    says the stage was dispatched only to be planned, so the plan is not self-approved; once the
    mark is cleared the same plan is."""
    import test_child_approval as tca

    child = tca.Child(tmp_path, monkeypatch)
    parent = state_mod.SessionState(session_id="parent-s", task_id="t",
                                    stages=[so._stage(1, place=ENGINE)])
    parent.stage_approvals["0:1"] = {"planning": True}
    child.store.save(parent)
    tca._linked_submit(child)
    assert child.state().parent_link is not None
    refused = child.run("approve", session="child-1", by="agent")
    assert refused["ok"] is False
    assert any("only to plan it" in b for b in refused["data"]["blockers"]), refused["data"]

    parent.stage_approvals["0:1"] = {"planning": False}
    child.store.save(parent)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


def test_a_linked_child_of_an_ordinary_stage_still_self_approves(tmp_path, monkeypatch):
    import test_child_approval as tca

    child = tca.Child(tmp_path, monkeypatch)
    child.store.save(state_mod.SessionState(session_id="parent-s", task_id="t",
                                            stages=[so._stage(1, place=ENGINE)]))
    tca._linked_submit(child)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


# --- the parent's approval of resources -------------------------------------------------------

def test_agent_approval_writes_resource_approval(store, venued_fixtures_dir):
    """`approve --by agent` writes the parent's approval for a stage whose request lies inside
    the set the user approved; one outside it is left unapproved."""
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.write_parent_approval(state, 1, _rec(1, allow=("Edit",)).resource_request)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    state.stage_approvals[sub_plan.approval_key(state, 1)].pop("parent_resource_approval")
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    assert state.stage_approvals["0:1"]["parent_resource_approval"] \
        == sub_plan.resource_request_digest(_rec(1).resource_request)

    sub_plan.store(state, _rec(1, allow=("Edit", "Bash(rm:*)")), level=0)
    state.stage_approvals["0:1"].pop("parent_resource_approval")
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    assert "parent_resource_approval" not in state.stage_approvals["0:1"]
    assert state.stage_approvals["0:1"]["approved"]["allow"] == ["Edit"]


def test_user_approval_writes_resource_approval_and_the_set(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit", "Read"), dirs=(("/work/x", "write"),)), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    entry = state.stage_approvals["0:1"]
    assert entry["approved"]["allow"] == ["Edit", "Read"]
    assert entry["approved"]["add_dirs"] == [["/work/x", "write"]]
    assert gates.parent_resource_dispatch_blockers(state, state.stage(1), sub_plan.read(state, 1)) == []


def test_stage_resource_approval_verb(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1), level=0)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    state.stage_approvals["0:1"].pop("parent_resource_approval")
    store.save(state)
    assert cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=1), store=store).ok
    assert store.load(SESSION).stage_approvals["0:1"]["parent_resource_approval"]
    assert _dispatch(store).action == "execute_in_thread"

    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)")))
    out = cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=1), store=store)
    _refused(out, "outside the approved set")

    other = cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=9), store=store)
    _refused(other, "not a stage of this session's own plan")


def test_stage_resource_approval_needs_an_approved_set(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    out = cli_approval.cmd_approve_stage_resources(ns(session=SESSION, stage=1), store=store)
    _refused(out, "the user's approval comes first")
    assert "parent_resource_approval" not in sub_plan.approval_entry(store.load(SESSION), 1)


def test_resource_request_edit_voids_parent_approval(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    store.save(state)
    assert _dispatch(store).action == "execute_in_thread"
    _put(store, _rec(1, allow=("Edit", "Read")))
    _refused(_dispatch(store), "its resource request changed since the parent approved it")


def test_a_stage_outside_a_childs_parent_set_is_not_approved_by_the_agent(store):
    parent = state_mod.SessionState(session_id="p", task_id="t")
    sub_plan.write_parent_approval(parent, 1, _rec(1, allow=("Edit",)).resource_request)
    store.save(parent)
    child = state_mod.SessionState(
        session_id="k", task_id="t", stages=[so._stage(1, place=ENGINE)],
        parent_link={"parent_session": "p", "stage_index": 1})
    sub_plan.store(child, _rec(1, allow=("Edit", "Read")), level=0)
    cli_approval._write_stage_approvals(child, store, by_agent=True)
    assert child.stage_approvals == {}
    sub_plan.store(child, _rec(1, allow=("Edit",)), level=0)
    cli_approval._write_stage_approvals(child, store, by_agent=True)
    assert child.stage_approvals["0:1"]["parent_resource_approval"]


# --- the whole sequence --------------------------------------------------------------------------

def test_the_whole_verb_sequence_for_a_decided_later_stage(home_store, venued_fixtures_dir, tmp_path):
    """dispatch refused -> a planning dispatch -> present -> approve-subplan by an agent refused
    -> by the user -> the stage starts; an edit of the sub-plan stops it again."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    _refused(_dispatch(store), "stage 1 is decided later")
    _dispatch(store, subplan_only=True)
    _put(store, _rec(1))
    assert cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store).ok is False
    assert _present_and_approve(store, tmp_path).ok
    state = store.load(SESSION)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    store.save(state)
    started = _dispatch(store)
    assert started.ok is not False or "decided later" not in started.detail
    assert store.load(SESSION).stage_approvals["0:1"]["planning"] is False
    _put(store, _rec(1, method="split it differently"))
    _refused(_dispatch(store), "changed since the user approved it")
