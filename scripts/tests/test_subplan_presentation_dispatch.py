"""Sub-plans in presentation, dispatch and approval.

A stage above the size line is shown with its sub-plan or with the decided-later mark; a stage
marked decided later starts only on the user's approval of its presented sub-plan, for the
sub-plan as it stands; every stage above the line starts only on the parent's approval of its
resources and permissions, for the request as it stands. The approvals are the engine's: a plan
text states none. Every way of starting a stage consults both blockers (`dispatch`, a dry run of
it, and `next-stage` for an in-thread stage), and a decided-later stage is planned by a child
that is told it only plans.

Each guard test goes red under a planted defect in `order_agreement_mutants.py`:
`decided_later_dispatched` (the user's approval of a sub-plan is not asked) and
`parent_resource_dispatched` (the parent's approval of resources is not asked). Nothing here
reaches a model judge or a real model CLI: the spawn runner is a stub and no judge stands behind
these guards.

Sessions are built the way `test_dispatch_semantics` builds them (an old-form plan driven to
EXECUTING) and the stage then carries a description (`place`), the decided-later mark or a size,
which is all the guards read. Each refusal is asserted by its named reason and has the case that
lifts it beside it; the controls call the verbs (`present-plan`, `approve-subplan`,
`approve-stage-resources`, `next-stage`, `dispatch`, `approve`), not the guards alone.
"""
from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_approval, cli_common, gates, own_contour, state as state_mod, sub_plan
from agentctl import render as render_mod
from agentctl import submission
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan
from agentctl.state import Node
from agentctl.store import FileStateStore
from lib import planning_only, widening_targets
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

import test_stage_order_subplan as so

SCRIPTS = Path(__file__).resolve().parent.parent
ENGINE = "the engine"
SESSION = "sp1"
SPAWNED = pytest.mark.parametrize("in_thread", [False, True], ids=["spawned", "in-thread"])


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PUBLISHED_TEXT_GATE", "0")
    for name in ("AGENTCTL_NORM_HANDOFF", "CLAUDE_CODE_SESSION_ID", "AGENT_RECURSION_DEPTH",
                 own_contour.OWN_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return FileStateStore(home / "agentctl" / "state")


class Spawns:
    """The spawn runner of a dispatch: records the argv of every child it is asked to start."""

    def __init__(self, out: str = "COMPLETED: ok\n"):
        self.argvs: list[list[str]] = []
        self.out = out

    def __call__(self, argv, cwd=None):
        self.argvs.append(list(argv))
        return RunResult(0, stdout=self.out)


def _planning(store, sid=SESSION):
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=True, external_effect=False,
                        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)


def _partitioned(store, venued_fixtures_dir, sid=SESSION, plan="plan_two_stage.toml"):
    _planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)


def _executing(store, venued_fixtures_dir, sid=SESSION):
    _partitioned(store, venued_fixtures_dir, sid)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def _described(store, sid=SESSION, *, lazy=False, minutes=None, in_thread=True, place=ENGINE):
    """Stage 1 as a stage description: placed, optionally decided later, of a declared
    size (None: undeclared, so above the line), run in-thread unless `in_thread` is False."""
    state = store.load(sid)
    stage = state.stage(1)
    stage.place, stage.lazy, stage.expected_minutes = place, lazy, minutes
    stage.binding_conditions_stated = True
    stage.actor.executor = "in_thread" if in_thread else "spawn:developer"
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


def _entry(store, stage=1, sid=SESSION) -> dict:
    return sub_plan.approval_entry(store.load(sid), stage)


def _dispatch(store, sid=SESSION, spawns=None, **flags):
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=flags.pop("dry_run", False), **flags),
                            store=store, runner=spawns or Spawns())


def _next_stage(store, sid=SESSION):
    return cli.cmd_next_stage(ns(session=sid), store=store)


def _refused(directive, *needles):
    assert directive.ok is False, directive
    text = directive.detail + " ".join(directive.data.get("blockers", []))
    for needle in needles:
        assert needle in text, (needle, text)


def _started(directive, spawns, in_thread):
    """The stage began: an in-thread one is handed back to this session, a spawned one ran a child."""
    assert directive.ok is not False, directive
    if in_thread:
        assert directive.action == "execute_in_thread", directive
    else:
        assert spawns.argvs, directive


def _not_started(directive, spawns, *needles):
    _refused(directive, *needles)
    assert not spawns.argvs


def _user_approves_resources(store, sid=SESSION):
    state = store.load(sid)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    store.save(state)


def _present_and_approve(store, tmp_path, stage=1, sid=SESSION, by="fedor", confirm=True):
    state = store.load(sid)
    record = sub_plan.read(state, stage, store)
    anchor = f"[subplan:{stage}:{sub_plan.approval_digest(record)[:12]}]"
    rendering = tmp_path / f"subplan-{stage}-{anchor[-6:-1]}.txt"
    rendering.write_text(f"Sub-plan of stage {stage}: {record.method} {anchor}\n", encoding="utf-8")
    shown = cli.cmd_present_plan(ns(session=sid, kind="subplan", stage=stage, plan=None,
                                    rendering_file=str(rendering)), store=store)
    assert shown.ok, shown
    if confirm:
        confirmed = cli.cmd_confirm_delivery(ns(
            session=sid, kind="subplan", by=by, note="delivered in the thread",
            escape_reason="delivered_out_of_band"), store=store)
        assert confirmed.ok, confirmed
    return cli_approval.cmd_approve_subplan(ns(session=sid, stage=stage, by=by), store=store)


def _approve_resources(store, stage=1, sid=SESSION):
    return cli_approval.cmd_approve_stage_resources(ns(session=sid, stage=stage), store=store)


def _new_form_plan(base_dir: Path, tmp_path, *, lazy=(), minutes=None, in_thread=()) -> str:
    """The venued two-stage fixture with both stages described (`place`), optionally decided
    later, of a declared size, run in-thread."""
    text = (base_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    for index in (1, 2):
        extra = 'place = "the engine"\nbinding_conditions = []\n'
        if index in lazy:
            extra += "lazy = true\n"
        if minutes is not None:
            extra += f"expected_minutes = {minutes}\n"
        marker = "depends_on = []\n" if index == 1 else "depends_on = [1]\n"
        text = text.replace(marker, marker + extra, 1)
    for index, title in ((1, "Scaffold module"), (2, "Add tests")):
        if index in in_thread:
            head = f'index = {index}\ntitle = "{title}"\nexecutor = '
            text = text.replace(head + '"spawn:developer"', head + '"in_thread"', 1)
    path = tmp_path / "new_form.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _submitted(store, venued_fixtures_dir, sid=SESSION):
    _planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    return store.load(sid)


# --- presentation ------------------------------------------------------------------------

def _essence_text(store, plan, *, with_subplans: bool) -> str:
    doc = load_plan(plan)
    text = "Essence of the plan.\n\n" + render_mod.render_plan_grants(doc, fmt="compact") + "\n"
    if with_subplans:
        text += "\n" + render_mod.render_subplans(doc, store.load(SESSION), store)
    return text


def _present_essence(store, tmp_path, plan, *, with_subplans: bool):
    path = tmp_path / "essence.txt"
    path.write_text(_essence_text(store, plan, with_subplans=with_subplans), encoding="utf-8")
    return cli.cmd_present_plan(ns(session=SESSION, kind="essence", rendering_file=str(path),
                                   emit_skeleton=False), store=store)


def _essence_session(store, venued_fixtures_dir, tmp_path, monkeypatch, **plan_kw) -> str:
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    state = _submitted(store, venued_fixtures_dir)
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, **plan_kw)
    state.order_agreement_armed = False
    state.order_agreement_exemption = ""
    state.plan_path = plan
    store.save(state)
    return plan


def test_presentation_refused_without_subplan_or_mark(store, venued_fixtures_dir, tmp_path,
                                                      monkeypatch):
    """`present-plan --kind essence` stamps nothing while a stage above the line has neither a
    sub-plan nor the decided-later mark, and nothing while the rendering leaves out the block of
    sub-plans; it stamps once both are in place."""
    plan = _essence_session(store, venued_fixtures_dir, tmp_path, monkeypatch, lazy=(2,))
    refused = _present_essence(store, tmp_path, plan, with_subplans=True)
    _refused(refused, "stage 1 is above the size line", "stamping nothing")
    assert "stage 2 is above" not in refused.detail
    assert store.load(SESSION).plan_presentations == []

    _put(store, _rec(1))
    omits = _present_essence(store, tmp_path, plan, with_subplans=False)
    _refused(omits, "omits the sub-plans, resources and permissions block")
    assert store.load(SESSION).plan_presentations == []

    shown = _present_essence(store, tmp_path, plan, with_subplans=True)
    assert shown.ok is True, shown
    assert [p.kind for p in store.load(SESSION).plan_presentations] == ["essence"]


def test_a_stage_below_the_line_is_presented_without_a_subplan_or_mark(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    plan = _essence_session(store, venued_fixtures_dir, tmp_path, monkeypatch, minutes=5)
    assert _present_essence(store, tmp_path, plan, with_subplans=True).ok is True


def test_presentation_shows_subplans_estimates_and_decided_later_stages(
        store, venued_fixtures_dir, tmp_path):
    state = _submitted(store, venued_fixtures_dir)
    record = _rec(1, dirs=(("/work/x", "write"),))
    record.estimate = sub_plan.Estimate(minutes=90, usd=3.0, author="executor", confirmer="parent")
    sub_plan.store(state, record, level=0)
    doc = load_plan(_new_form_plan(venued_fixtures_dir, tmp_path, lazy=(2,)))
    text = render_mod.render_subplans(doc, state, store)
    assert "stage 2" in text and "decided later" in text
    assert "estimate 90.0 min / $3.0, confirmed by parent" in text
    assert "`Edit`" in text and "`/work/x` (write)" in text


def test_presentation_shows_every_level_of_a_three_level_plan_and_a_childs_records(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    state = _submitted(store, venued_fixtures_dir)
    top = _rec(1)
    top.estimate = sub_plan.Estimate(minutes=60, usd=2.0, author="a", confirmer="b")
    sub_plan.store(state, top, level=0)
    mid, deep = _rec(2, allow=("Read",)), _rec(3, allow=("Bash(make:*)",))
    deep.estimate = sub_plan.Estimate(minutes=7, usd=0.5, author="a", confirmer="b")
    sub_plan.store(state, mid, level=1)
    sub_plan.store(state, deep, level=2)
    child = state_mod.SessionState(session_id="kid", task_id="t")
    sub_plan.store(child, _rec(4, allow=("Glob",)), level=3)
    monkeypatch.setattr(sub_plan, "linked_children", lambda st, store=None: [child])
    text = render_mod.render_subplans(
        load_plan(_new_form_plan(venued_fixtures_dir, tmp_path)), state, store)
    assert "level 1, stage 2: sub-plan recorded" in text and "`Read`" in text
    assert "level 2, stage 3: sub-plan recorded — estimate 7.0 min / $0.5" in text
    assert "`Bash(make:*)`" in text
    assert "child kid, level 3, stage 4" in text and "`Glob`" in text


def test_a_below_threshold_stage_is_shown_with_its_tier_price(
        store, venued_fixtures_dir, tmp_path, config_row):
    """The price shown is the one the config's tier constants hold: moving a constant moves the
    figure, for the spawned (medium) and the in-thread (small) default tier."""
    config_row("budget-medium-usd", 4.25)
    config_row("effort-stage-minutes-medium", 33)
    config_row("budget-small-usd", 1.75)
    config_row("effort-stage-minutes-small", 12)
    state = _submitted(store, venued_fixtures_dir)
    doc = load_plan(_new_form_plan(venued_fixtures_dir, tmp_path, minutes=5, in_thread=(2,)))
    text = render_mod.render_subplans(doc, state, store)
    assert "stage 1" in text and "below the size line, priced at tier medium: $4.25, 33 min" in text
    assert "stage 2" in text and "below the size line, priced at tier small: $1.75, 12 min" in text
    assert gates.unplanned_stage_blockers(state, str(tmp_path / "new_form.toml"), store=store) == []


# --- dispatch ----------------------------------------------------------------------------

@SPAWNED
def test_parent_approval_alone_refused(store, venued_fixtures_dir, in_thread):
    """The parent's approval of the resources does not start a decided-later stage; with the
    user's approval of the sub-plan beside it, it does."""
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True, in_thread=in_thread)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    sub_plan.store(state, _rec(1), level=0)
    store.save(state)
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns), spawns, "stage 1 is decided later",
                 "has not approved a sub-plan")
    assert gates.parent_resource_dispatch_blockers(state, state.stage(1), _rec(1)) == []

    state = store.load(SESSION)
    sub_plan.write_user_approval(state, 1, _rec(1))
    store.save(state)
    _started(_dispatch(store, spawns=spawns), spawns, in_thread)


@SPAWNED
def test_a_stage_above_the_line_without_the_parents_approval_refused(
        store, venued_fixtures_dir, in_thread):
    _executing(store, venued_fixtures_dir)
    _described(store, in_thread=in_thread)
    _put(store, _rec(1))
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns), spawns,
                 "the parent has not approved its resources and permissions")
    _user_approves_resources(store)
    _started(_dispatch(store, spawns=spawns), spawns, in_thread)


def test_next_stage_refuses_an_in_thread_stage_without_the_approvals(store, venued_fixtures_dir):
    """`next-stage` is the normal way to start an in-thread stage and carries the same two guards
    as `dispatch`: the stage stays PENDING while one refuses, and starts once both are met."""
    _partitioned(store, venued_fixtures_dir)
    _described(store, in_thread=True)
    _put(store, _rec(1))
    refused = _next_stage(store)
    _refused(refused, "the parent has not approved its resources and permissions")
    assert store.load(SESSION).stage(1).outcome.status == "PENDING"
    _user_approves_resources(store)
    started = _next_stage(store)
    assert started.ok is True and started.action == "execute_in_thread", started
    assert store.load(SESSION).stage(1).outcome.status == "ACTIVE"


def test_next_stage_refuses_a_decided_later_in_thread_stage_until_the_users_approval(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _partitioned(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    _refused(_next_stage(store), "stage 1 is decided later", "has not approved a sub-plan")
    assert store.load(SESSION).stage(1).outcome.status == "PENDING"
    assert _present_and_approve(store, tmp_path).ok
    _refused(_next_stage(store), "the parent has not approved its resources")
    assert _approve_resources(store).ok
    assert _next_stage(store).action == "execute_in_thread"


def test_a_dry_run_of_an_in_thread_stage_is_no_start_without_the_approvals(
        store, venued_fixtures_dir):
    """A dry run of an in-thread stage returns the directive of a real start, so it is refused
    the way a real start is; a spawned stage's dry run only previews a command and is exempt."""
    _executing(store, venued_fixtures_dir)
    _described(store, in_thread=True)
    _put(store, _rec(1))
    before = store.path(SESSION).read_bytes()
    _refused(_dispatch(store, dry_run=True), "the parent has not approved its resources")
    assert store.path(SESSION).read_bytes() == before
    _user_approves_resources(store)
    assert _dispatch(store, dry_run=True).ok is True


def test_a_dry_run_of_a_spawned_stage_previews_without_the_approvals(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    before = store.path(SESSION).read_bytes()
    spawns = Spawns("spawn-specialist.py --kind developer\n")
    d = _dispatch(store, spawns=spawns, dry_run=True)
    assert d.action == "preview", d
    assert store.path(SESSION).read_bytes() == before


def test_approve_subplan_by_agent_refused(home_store, venued_fixtures_dir, tmp_path):
    """The agent cannot set the field, presented and delivered or not; the user can."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    unpresented = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"),
                                                   store=store)
    _refused(unpresented, "--by agent is refused")
    assert not _entry(store).get("user_subplan_approval")
    _present_and_approve(store, tmp_path, by="fedor", confirm=True)
    state = store.load(SESSION)
    state.stage_approvals["0:1"].pop("user_subplan_approval")
    store.save(state)
    refused = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store)
    _refused(refused, "--by agent is refused")
    assert not _entry(store).get("user_subplan_approval")
    by_user = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    assert by_user.ok is True and _entry(store)["user_subplan_approval"]


def test_approve_subplan_unpresented_refused(home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(d, "was not presented", "present-plan --kind subplan --stage 1")
    assert not _entry(store).get("user_subplan_approval")
    assert _present_and_approve(store, tmp_path).ok
    assert _entry(store)["user_subplan_approval"]


def test_approve_subplan_without_a_delivery_stamp_refused(home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    d = _present_and_approve(store, tmp_path, confirm=False)
    _refused(d, "has no delivery stamp")
    assert not _entry(store).get("user_subplan_approval")


@SPAWNED
def test_subplan_edit_after_approval_refused(home_store, venued_fixtures_dir, tmp_path, in_thread):
    """An edit of the approved sub-plan stops the stage; the user's approval of the edited
    sub-plan, presented again, starts it. A restamp moves the approval only when the resource
    request is unchanged; an edit of the estimate alone never needed one."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=in_thread)
    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok
    assert _approve_resources(store).ok
    spawns = Spawns()
    state = store.load(SESSION)
    assert gates.decided_later_dispatch_blockers(state, state.stage(1), _rec(1)) == []

    edited = _rec(1, method="split it differently")
    _put(store, edited)
    _not_started(_dispatch(store, spawns=spawns), spawns, "changed since the user approved it")
    again = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(again, "changed since it was presented")

    assert _present_and_approve(store, tmp_path).ok
    _started(_dispatch(store, spawns=spawns), spawns, in_thread)


def test_restamp_follows_an_edit_that_leaves_the_resource_request_alone(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok
    assert _approve_resources(store).ok
    edited = _rec(1, method="split it differently")
    _put(store, edited)
    assert _dispatch(store).ok is False
    state = store.load(SESSION)
    assert state_mod.restamp_subplan_digest(state, 1, edited) is True
    store.save(state)
    assert _dispatch(store).action == "execute_in_thread"

    estimated = _rec(1, method="split it differently")
    estimated.estimate = sub_plan.Estimate(minutes=400, usd=9.0, author="a", confirmer="b")
    _put(store, estimated)
    assert _dispatch(store).action == "execute_in_thread"


def test_restamp_leaves_an_unset_approval_unset(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    assert state_mod.restamp_subplan_digest(state, 1, _rec(1)) is False
    assert not _entry(store).get("user_subplan_approval")
    _refused(_dispatch(store), "has not approved a sub-plan")


@pytest.mark.parametrize("changed", [
    {"allow": ("Edit", "Read")},
    {"dirs": (("/work/x", "write"),)},
], ids=["allow-rules", "added-directories"])
def test_restamp_never_moves_the_approval_over_a_changed_request(
        home_store, venued_fixtures_dir, tmp_path, changed):
    """After approve-subplan a change of the allow rules or the added directories is not carried
    by a restamp: the user's approval keeps its old digests, dispatch refuses by its digest reason,
    the agent's approve-stage-resources refuses and writes nothing; the user's approval of the
    changed, presented sub-plan lifts both."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok
    approved = dict(_entry(store))
    edited = _rec(1, **changed)
    _put(store, edited)
    state = store.load(SESSION)
    assert state_mod.restamp_subplan_digest(state, 1, edited) is False
    store.save(state)
    assert _entry(store) == approved
    _refused(_dispatch(store), "changed since the user approved it")
    out = _approve_resources(store)
    _refused(out, "the user's approval does not cover the live resource request")
    assert "parent_resource_approval" not in _entry(store)

    assert _present_and_approve(store, tmp_path).ok
    assert _approve_resources(store).ok
    assert _dispatch(store).action == "execute_in_thread"


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


# --- the planning dispatch -------------------------------------------------------------------

def test_spawned_decided_later_planning_dispatch(home_store, venued_fixtures_dir, tmp_path):
    """The planning dispatch starts a child that is told it only plans and never records the
    stage's result; it is let through by both blockers while the sub-plan is unapproved, refused
    once approved as it stands, and the work dispatch stays refused until both approvals are in."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns), spawns, "stage 1 is decided later")

    planned = _dispatch(store, spawns=spawns, subplan_only=True)
    assert planned.action == "await_subplan_approval", planned
    assert planned.data["planning_dispatch"] is True
    assert len(spawns.argvs) == 1 and planning_only.FLAG in spawns.argvs[0]
    assert "is not done when you return" in " ".join(spawns.argvs[0])
    assert store.load(SESSION).stage(1).outcome.status != "PASSED"
    assert _entry(store).get("planning") is True

    link = {"parent_session": SESSION, "stage_index": 1}
    child = state_mod.SessionState(session_id="kid", task_id="t", parent_link=link)
    assert any("only to plan it" in b
               for b in gates.child_approval_blockers(child, store.load(SESSION)))

    _put(store, _rec(1))
    second = _dispatch(store, spawns=spawns, subplan_only=True)
    assert second.action == "await_subplan_approval" and len(spawns.argvs) == 2
    assert _present_and_approve(store, tmp_path).ok
    _not_started(_dispatch(store, spawns=Spawns(), subplan_only=True), Spawns(),
                 "approved as it stands")
    work = Spawns()
    _not_started(_dispatch(store, spawns=work), work, "the parent has not approved its resources")
    assert _entry(store).get("planning") is True

    _put(store, _rec(1, method="split it differently"))
    third = Spawns()
    assert _dispatch(store, spawns=third, subplan_only=True).action == "await_subplan_approval"
    _put(store, _rec(1))
    assert _approve_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and started.argvs
    assert planning_only.FLAG not in started.argvs[0]
    assert _entry(store).get("planning") is False
    sub_plan.store(child, _rec(1))
    assert gates.child_approval_blockers(child, store.load(SESSION)) == []


def test_subplan_only_is_refused_for_a_stage_not_decided_later(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, in_thread=False)
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns, subplan_only=True), spawns,
                 "not marked decided-later")


def test_subplan_only_on_an_in_thread_stage_hands_out_no_work_order(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    spawns = Spawns()
    d = _dispatch(store, spawns=spawns, subplan_only=True)
    _not_started(d, spawns, "is in-thread", "push-subplan")
    assert d.action != "execute_in_thread"
    dry = _dispatch(store, spawns=spawns, subplan_only=True, dry_run=True)
    _not_started(dry, spawns, "is in-thread")


def test_subplan_only_on_an_old_form_stage_is_refused_with_a_named_reason(
        store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    assert store.load(SESSION).stage(1).place is None
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns, subplan_only=True), spawns,
                 "is an old-form stage with no sub-plan record")


def _load_spawn_script():
    spec = importlib.util.spec_from_file_location("spawn_specialist_planning", SCRIPTS / "spawn-specialist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_planning_child_is_told_it_plans_and_holds_no_execution_grants(tmp_path):
    spawn = _load_spawn_script()
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n**<<this step>>**\n", encoding="utf-8")
    base = ["--kind", "developer", "--plan", str(plan), "--done-criterion", "dc",
            "--criterion-type", "measurable", "--effort", "low", "--complexity", "medium"]
    parser = spawn.build_parser()
    planning = parser.parse_args(base + [planning_only.FLAG])
    ordinary = parser.parse_args(base)
    assert planning.planning_only is True and ordinary.planning_only is False
    assert "## Planning only" in spawn.assemble_prompt(planning, 1, "permissions")
    assert "## Planning only" not in spawn.assemble_prompt(ordinary, 1, "permissions")
    writes = [{"path": "/work", "mode": "write"}]
    assert spawn.resolve_permission_mode(planning, writes) == "default"
    assert spawn.resolve_permission_mode(ordinary, writes) == "acceptEdits"
    entries = [{"path": "/work", "mode": "write"}, {"path": "/docs", "mode": "read"},
               {"rule": "Bash(make:*)"}]
    assert planning_only.grants(entries) == [{"path": "/docs", "mode": "read"}]
    assert planning_only.grants(None) is None


def test_the_planning_child_of_a_dry_run_main_holds_no_rule_grants(
        store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    """Through `spawn-specialist.py main()`: the stage's declared rule grant reaches an ordinary
    child's settings and does not reach a planning-only child's, which is told it only plans."""
    import test_spawn_grants_integration as sgi
    from test_stage_grants import _to_executing, _write_plan_with_grants_block

    rule = '"Bash(make lint:*)"'
    monkeypatch.setattr(sgi.MOD, "SKILLS_DIR", SCRIPTS.parent / "skills" / "specializations")
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(sgi.MOD, "plans_dir", lambda: plans)
    plan_path = _write_plan_with_grants_block(
        venued_fixtures_dir, plans, f"[stage.grants]\nallow = [{rule}]\n", "plan_declared_lint.toml")
    _to_executing(store, "planning-grants", venued_fixtures_dir, plan_path=plan_path)
    argv = sgi._dry_run_argv("planning-grants", "developer", 1, tmp_path / "state", Path(plan_path))

    assert sgi.MOD.main(argv) == 0
    ordinary = capsys.readouterr().out
    assert rule in sgi._command_section(ordinary)
    assert "## Planning only" not in ordinary

    assert sgi.MOD.main([*argv, planning_only.FLAG]) == 0
    planning = capsys.readouterr().out
    assert "## Planning only" in planning
    assert rule not in sgi._command_section(planning)


def test_child_receipt_does_not_approve(home_store, venued_fixtures_dir, tmp_path):
    """A sub-plan presented to the user in a linked child is no approval: `approve-subplan` is
    run in the root session the user was shown it in."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    state = _described(store, lazy=True)
    _put(store, _rec(1))
    assert _present_and_approve(store, tmp_path).ok is True
    state = store.load(SESSION)
    state.stage_approvals["0:1"].pop("user_subplan_approval")
    state.parent_link = {"parent_session": "someone", "stage_index": 1}
    store.save(state)
    d = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(d, "not in a linked child")
    assert not _entry(store).get("user_subplan_approval")


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
    record["sub_plan"]["user_subplan_approval"] = sub_plan.approval_digest(approved)
    same = submission._stage_order_violations(so._doc(record=record), ctx)
    assert not any("is stated" in p for p in same), same


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

def test_below_threshold_stage_needs_no_subplan_or_separate_approval(
        store, venued_fixtures_dir, config_row):
    """A stage below the line starts on the user's approval of the plan with no sub-plan and no
    parent approval; a stage above it does not. The line is the configured one: moving the
    threshold moves the stage across it."""
    _executing(store, venued_fixtures_dir)
    state = _described(store, minutes=5)
    assert gates.stage_start_blockers(state, state.stage(1), store=store) == []
    spawns = Spawns()
    assert _dispatch(store, spawns=spawns).action == "execute_in_thread"
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    assert state.stage_approvals == {}
    _refused(_approve_resources(store), "below the size line")

    config_row("substantive-wall-clock-min", 3)
    assert gates.stage_start_blockers(state, state.stage(1), store=store) \
        and "the parent has not approved" in gates.stage_start_blockers(
            state, state.stage(1), store=store)[0]
    _refused(_dispatch(store), "the parent has not approved its resources")
    _put(store, _rec(1))
    _user_approves_resources(store)
    assert _dispatch(store).action == "execute_in_thread"


def test_a_below_threshold_stage_carrying_the_mark_is_refused_at_submission():
    doc = so._new_form({"lazy": True, "expected_minutes": 5})
    problems = submission._stage_order_violations(doc, submission.StageOrderContext())
    assert any("below the size line and carries the decided-later mark" in p for p in problems)


def test_old_form_stage_starts_without_any_approval(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = store.load(SESSION)
    stage = state.stage(1)
    assert stage.place is None
    assert gates.stage_start_blockers(state, stage, store=store) == []
    spawns = Spawns()
    d = _dispatch(store, spawns=spawns)
    assert spawns.argvs and "no sub-plan" not in d.detail
    assert store.load(SESSION).stage_approvals == {}


def test_a_replan_of_the_unchanged_plan_leaves_the_approvals_and_the_guards_as_they_were(
        store, venued_fixtures_dir, tmp_path):
    """A replan that changes nothing is a no-op: the engine's approvals stay, so the stage the
    guards refused is still refused and the stage they let through still starts."""
    _planning(store)
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, in_thread=(1, 2))
    cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    _put(store, _rec(1))
    cli.cmd_approve(ns(session=SESSION, by="user"), store=store)
    cli.cmd_partition(ns(session=SESSION, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    approvals = dict(store.load(SESSION).stage_approvals)
    assert approvals["0:1"]["parent_resource_approval"]
    replan = cli.cmd_replan(ns(session=SESSION, plan=plan, coverage_waiver=None), store=store)
    assert "no-op" in replan.detail, replan
    assert store.load(SESSION).stage_approvals == approvals
    assert _next_stage(store).action == "execute_in_thread"


def test_the_user_approval_of_a_plan_writes_the_resource_approval_of_each_stage(
        store, venued_fixtures_dir, tmp_path):
    """`approve --by user` of a plan whose stages are above the line records the parent's
    approval and the approved set; without that call a stage of the plan could not start."""
    _planning(store)
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, in_thread=(1, 2))
    cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    state = store.load(SESSION)
    sub_plan.store(state, _rec(1, allow=("Edit", "Read")), level=0)
    sub_plan.store(state, _rec(2, allow=("Bash(make:*)",)), level=0)
    store.save(state)
    d = cli.cmd_approve(ns(session=SESSION, by="user"), store=store)
    assert d.ok is True, d
    entries = store.load(SESSION).stage_approvals
    assert set(entries) == {"0:1", "0:2"}, entries
    assert entries["0:1"]["parent_resource_approval"] \
        == sub_plan.resource_request_digest(_rec(1, allow=("Edit", "Read")).resource_request)
    assert entries["0:1"]["approved"]["allow"] == ["Edit", "Read"]
    assert entries["0:2"]["approved"]["allow"] == ["Bash(make:*)"]


# --- the child's own approval --------------------------------------------------------------

def test_the_child_gate_is_open_for_an_ordinary_stage_and_closed_for_an_unreadable_parent():
    child = state_mod.SessionState(session_id="k", task_id="t",
                                   parent_link={"parent_session": "p", "stage_index": 1})
    parent = state_mod.SessionState(session_id="p", task_id="t")
    assert gates.child_approval_blockers(child, parent) == []
    assert gates.child_approval_blockers(state_mod.SessionState(session_id="x", task_id="t"),
                                         None) == []
    blockers = gates.child_approval_blockers(child, None)
    assert len(blockers) == 1
    assert "parent session p cannot be read" in blockers[0]
    assert "report this to your parent" in blockers[0]


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
    assert any("changed since the user approved it"
               in b for b in gates.child_approval_blockers(child, parent))


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


def test_a_linked_child_whose_parent_state_cannot_be_read_is_refused_its_own_approval(
        tmp_path, monkeypatch):
    """The parent's state is not on disk: the child has no copy to check against, so its
    approval fails closed with a message that names the way out."""
    import test_child_approval as tca

    child = tca.Child(tmp_path, monkeypatch)
    tca._linked_submit(child)
    refused = child.run("approve", session="child-1", by="agent")
    assert refused["ok"] is False
    blockers = refused["data"]["blockers"]
    assert any("cannot be read" in b and "report this to your parent" in b for b in blockers), blockers
    child.store.save(state_mod.SessionState(session_id="parent-s", task_id="t",
                                            stages=[so._stage(1, place=ENGINE)]))
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


# --- the parent's approval of resources -------------------------------------------------------

def test_agent_approval_writes_resource_approval(store, venued_fixtures_dir):
    """`approve --by agent` writes the parent's approval for a stage whose request lies inside
    the set the user approved, and never widens that set; one outside it is left unapproved."""
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.write_parent_approval(state, 1, _rec(1, allow=("Edit",)).resource_request)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    state.stage_approvals["0:1"].pop("parent_resource_approval")
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    assert state.stage_approvals["0:1"]["parent_resource_approval"] \
        == sub_plan.resource_request_digest(_rec(1).resource_request)
    store.save(state)
    assert _dispatch(store).action == "execute_in_thread"

    sub_plan.store(state, _rec(1, allow=("Edit", "Bash(rm:*)")), level=0)
    state.stage_approvals["0:1"].pop("parent_resource_approval")
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    assert "parent_resource_approval" not in state.stage_approvals["0:1"]
    assert state.stage_approvals["0:1"]["approved"]["allow"] == ["Edit"]
    store.save(state)
    _refused(_dispatch(store), "the parent has not approved its resources")


def test_the_agents_own_approval_does_not_seed_the_approved_set(store, venued_fixtures_dir):
    """With no set the user approved, the agent's approval of a plan starts its stage but leaves
    no set behind, so a later request is not tested against what the agent itself approved."""
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    store.save(state)
    entry = _entry(store)
    assert entry["parent_resource_approval"] and "approved" not in entry
    assert _dispatch(store).action == "execute_in_thread"
    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)")))
    _refused(_approve_resources(store), "the user's approval comes first")


def test_a_childs_own_plan_is_approved_inside_the_set_its_parent_approved(tmp_path, monkeypatch):
    """A linked child's `approve --by agent` of its own new-form plan, through the CLI: the stage
    starts after it when the request lies inside the resources the parent approved for the
    stage the child was handed, and does not when it lies outside."""
    import test_child_approval as tca

    child = tca.Child(tmp_path, monkeypatch)
    parent = state_mod.SessionState(session_id="parent-s", task_id="t")
    sub_plan.write_parent_approval(parent, 1, _rec(1, allow=("Edit",)).resource_request)
    child.store.save(parent)
    text = tca._spliced(parent_norm=child.digest, final_checks=((tca.PARENT_CHECK, 0),))
    text = text.replace("depends_on = []\n", 'depends_on = []\nplace = "the engine"\n'
                        'binding_conditions = []\n', 1)
    tca._linked_submit(child, plan_text=text)
    state = child.state()
    state.stages[0].place = ENGINE
    state.stages[0].binding_conditions_stated = True
    sub_plan.store(state, _rec(1, allow=("Edit", "Read")), level=0)
    child.store.save(state)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True
    assert "parent_resource_approval" not in child.state().stage_approvals.get("0:1", {})


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
    assert _approve_resources(store).ok
    assert store.load(SESSION).stage_approvals["0:1"]["parent_resource_approval"]
    assert _dispatch(store).action == "execute_in_thread"

    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)")))
    _refused(_approve_resources(store), "outside the approved set")

    other = _approve_resources(store, stage=9)
    _refused(other, "not a stage of this session's own plan")


def test_stage_resource_approval_for_a_decided_later_stage_follows_the_users_approval(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    _refused(_approve_resources(store), "has not approved a sub-plan")
    assert "parent_resource_approval" not in _entry(store)
    assert _present_and_approve(store, tmp_path).ok
    assert _approve_resources(store).ok
    assert _entry(store)["parent_resource_approval"]


def test_stage_resource_approval_needs_an_approved_set(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    out = _approve_resources(store)
    _refused(out, "the user's approval comes first")
    assert "parent_resource_approval" not in _entry(store)


def test_stage_resource_approval_is_refused_for_a_stage_of_another_session(
        store, venued_fixtures_dir, monkeypatch):
    """A spawned child names its own session through agentctl-own.py: the verb refuses to
    approve a stage of the session that handed the child its stage, and writes nothing."""
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1), level=0)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    state.stage_approvals["0:1"].pop("parent_resource_approval")
    store.save(state)
    monkeypatch.setenv(own_contour.OWN_SESSION_ENV, "the-childs-own-session")
    _refused(_approve_resources(store), "calling session's own plan")
    assert "parent_resource_approval" not in _entry(store)
    monkeypatch.setenv(own_contour.OWN_SESSION_ENV, SESSION)
    assert _approve_resources(store).ok


def test_resource_request_edit_voids_parent_approval(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    store.save(state)
    assert _dispatch(store).action == "execute_in_thread"
    _put(store, _rec(1, allow=("Edit", "Read")))
    _refused(_dispatch(store), "its resource request changed since the parent approved it")
    wide = _approve_resources(store)
    _refused(wide, "outside the approved set")
    _user_approves_resources(store)
    assert _dispatch(store).action == "execute_in_thread"


def test_an_edit_inside_the_approved_set_is_reapproved_by_the_verb(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit", "Read")), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    store.save(state)
    _refused(_dispatch(store), "its resource request changed since the parent approved it")
    assert _approve_resources(store).ok
    assert _dispatch(store).action == "execute_in_thread"


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
    -> by the user -> approve-stage-resources -> the stage starts; an edit stops it again."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns), spawns, "stage 1 is decided later")
    _dispatch(store, spawns=spawns, subplan_only=True)
    assert len(spawns.argvs) == 1
    _put(store, _rec(1))
    agent = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store)
    assert agent.ok is False
    assert _present_and_approve(store, tmp_path).ok
    assert _approve_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and len(started.argvs) == 1
    assert _entry(store)["planning"] is False
    _put(store, _rec(1, method="split it differently"))
    again = Spawns()
    _not_started(_dispatch(store, spawns=again), again, "changed since the user approved it")
