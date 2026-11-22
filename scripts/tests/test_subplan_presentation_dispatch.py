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

import contextlib
import importlib.util
import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_approval, cli_common, cli_dispatch, effort, gates, norm_handoff, own_contour, state as state_mod, sub_plan
from agentctl.config import Thresholds
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


def _start_blockers(store, stage=1, sid=SESSION) -> list[str]:
    state = store.load(sid)
    return gates.stage_start_blockers(state, state.stage(stage), store=store)


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


def _plan_approval_sets_resources(store, sid=SESSION):
    state = store.load(sid)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    store.save(state)


def _present(store, tmp_path, stage=1, sid=SESSION, by="fedor", confirm=True, record=None):
    """Present the sub-plan of `stage` as it stands and, with `confirm`, stamp its delivery."""
    state = store.load(sid)
    record = record or sub_plan.read(state, stage, store)
    anchor = f"[subplan:{stage}:{sub_plan.approval_digest(record)[:12]}]"
    rendering = tmp_path / f"subplan-{stage}-{anchor[-6:-1]}.txt"
    rendering.write_text(
        f"Sub-plan of stage {stage}: {record.method} {anchor}\n"
        f"{render_mod.subplan_request_text(record)}\n", encoding="utf-8")
    shown = cli.cmd_present_plan(ns(session=sid, kind="subplan", stage=stage, plan=None,
                                    rendering_file=str(rendering)), store=store)
    assert shown.ok, shown
    if confirm:
        confirmed = cli.cmd_confirm_delivery(ns(
            session=sid, kind="subplan", by=by, note="delivered in the thread",
            escape_reason="delivered_out_of_band"), store=store)
        assert confirmed.ok, confirmed


def _present_and_approve(store, tmp_path, stage=1, sid=SESSION, by="fedor", confirm=True,
                         record=None):
    _present(store, tmp_path, stage, sid, by, confirm, record)
    return cli_approval.cmd_approve_subplan(ns(session=sid, stage=stage, by=by), store=store)


def _agent_approves_stage_resources(store, stage=1, sid=SESSION):
    return cli_approval.cmd_approve_stage_resources(ns(session=sid, stage=stage), store=store)


def _new_form_plan(base_dir: Path, tmp_path, *, lazy=(), minutes=None, in_thread=()) -> str:
    """The venued two-stage fixture with both stages described (`place`), optionally decided
    later, of a declared size, run in-thread."""
    text = (base_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    for index in (1, 2):
        extra = 'place = "the engine"\nbinding_conditions = []\n'
        if index in lazy:
            extra += "lazy = true\n"
        declared = minutes.get(index) if isinstance(minutes, dict) else minutes
        if declared is not None:
            extra += f"expected_minutes = {declared}\n"
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


def test_a_plan_of_one_stage_above_and_one_below_the_line_is_presented_with_the_price(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    """Only the stage above the line is refused for want of a sub-plan; the stage below it is
    presented as it is, with the price of its cost tier read from the configured constants."""
    plan = _essence_session(store, venued_fixtures_dir, tmp_path, monkeypatch, minutes={2: 5})
    refused = _present_essence(store, tmp_path, plan, with_subplans=True)
    _refused(refused, "stage 1 is above the size line")
    assert "stage 2 is above" not in refused.detail
    _put(store, _rec(1))
    assert _present_essence(store, tmp_path, plan, with_subplans=True).ok is True
    stage_two = store.load(SESSION).stage(2)
    thr = Thresholds()
    tier = effort.stage_tier(stage_two)
    price = f"tier {tier}: ${thr.budget_usd_float(tier):.2f}, {thr.effort_stage_minutes(tier)} min"
    text = _essence_text(store, plan, with_subplans=True)
    assert f"stage 2 “Add tests”: below the size line, priced at {price}" in text


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


def _three_stages(stages, *, lazy, small):
    """Three described stages of one plan: stage 1 above the line, `lazy` (index) decided later,
    `small` (index) below the line at five minutes."""
    import copy
    out = [copy.deepcopy(stages[0]), copy.deepcopy(stages[1]), copy.deepcopy(stages[1])]
    for position, stage in enumerate(out, start=1):
        stage.index, stage.title = position, f"stage {position} of the level"
        stage.place, stage.binding_conditions_stated = ENGINE, True
        stage.lazy = position == lazy
        stage.expected_minutes = 5 if position == small else None
    return out


def _level_record(stage, minutes, allow):
    record = _rec(stage, allow=allow)
    record.estimate = sub_plan.Estimate(minutes=minutes, usd=minutes / 10, author="a", confirmer="b")
    return record


def test_presentation_shows_every_level_of_a_three_level_plan_over_real_links(
        home_store, venued_fixtures_dir, tmp_path):
    """The presented block reaches every level through the engine's own links: the plan itself,
    the child handed its stage 1, and the child that one handed its own stage 1. At each level
    every sub-plan is shown with its estimate and its resources, each stage decided later by name,
    each stage below the size line with its tier's price."""
    store = home_store
    state = _submitted(store, venued_fixtures_dir)
    doc = load_plan(_new_form_plan(venued_fixtures_dir, tmp_path))
    doc.stages[:] = _three_stages(doc.stages, lazy=2, small=3)
    kid = _linked_kid(store, tmp_path, "kid", "2026-01-01T00:00:00.000001+00:00",
                      _level_record(1, 60, ("Edit",)))
    kid.stages = _three_stages(state.stages, lazy=2, small=3)
    store.save(kid)
    grandkid = _linked_kid(store, tmp_path, "grandkid", "2026-01-01T00:00:00.000002+00:00",
                           _level_record(1, 7, ("Bash(make:*)",)), parent="kid")
    grandkid.stages = _three_stages(state.stages, lazy=1, small=2)
    store.save(grandkid)

    text = render_mod.render_subplans(doc, store.load(SESSION), store)

    price = "below the size line, priced at tier "
    assert text.count("decided later") == 3, text
    assert text.count(price) == 3, text
    assert "- stage 1 “stage 1 of the level”: sub-plan recorded — estimate 60.0 min / $6.0" in text
    assert ("- child kid, stage 1 “stage 1 of the level”: sub-plan recorded — "
            "estimate 7.0 min / $0.7") in text
    assert "`Bash(make:*)`" in text
    assert "- child grandkid, stage 1 “stage 1 of the level”: decided later" in text
    assert "- child grandkid, stage 2 “stage 2 of the level”: below the size line" in text


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
    _plan_approval_sets_resources(store)
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
    _plan_approval_sets_resources(store)
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
    assert _agent_approves_stage_resources(store).ok
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
    _plan_approval_sets_resources(store)
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


@pytest.mark.parametrize("lazy", [True, False], ids=["decided-later", "planned-up-front"])
def test_a_rendering_that_leaves_the_request_out_is_refused_and_neither_approval_rests_on_it(
        home_store, venued_fixtures_dir, tmp_path, lazy):
    """A sub-plan is presented only with the stage's resource and permission request as it
    stands, on the line `render_subplans` shows it on. A rendering with the anchor and the method
    but without the request, or with the request of an earlier record, stamps no receipt, so
    `approve-subplan` and `approve-resources` both find the sub-plan not presented."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=lazy)
    _put(store, _rec(1, allow=("Edit",)))
    record = sub_plan.read(store.load(SESSION), 1, store)
    anchor = f"[subplan:1:{sub_plan.approval_digest(record)[:12]}]"
    stale = render_mod.subplan_request_text(_rec(1, allow=("Edit", "Bash(rm:*)")))
    for name, body in (("without", f"Sub-plan: {record.method} {anchor}\n"),
                       ("stale", f"Sub-plan: {record.method} {anchor}\n{stale}\n")):
        rendering = tmp_path / f"{name}.txt"
        rendering.write_text(body, encoding="utf-8")
        shown = cli.cmd_present_plan(ns(session=SESSION, kind="subplan", stage=1, plan=None,
                                        rendering_file=str(rendering)), store=store)
        _refused(shown, "does not show the resource and permission request of stage 1",
                 render_mod.subplan_request_text(record))
        assert not store.load(SESSION).plan_presentations
    _refused(cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store),
             "was not presented")
    _refused(_user_approves_resources(store),
             "approve-subplan" if lazy else "was not presented to the user")
    _present(store, tmp_path)
    assert cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"),
                                            store=store).ok


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
    assert _agent_approves_stage_resources(store).ok
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
    assert _agent_approves_stage_resources(store).ok
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
    out = _agent_approves_stage_resources(store)
    _refused(out, "the user's approval does not cover the live resource request")
    assert "parent_resource_approval" not in _entry(store)

    assert _present_and_approve(store, tmp_path).ok
    assert _agent_approves_stage_resources(store).ok
    assert _dispatch(store).action == "execute_in_thread"


PARENT, CHILD = "parent-s", "child-1"


def _subplan_table(method="split the module", **approvals) -> str:
    """The `[meta.sub_plan]` table of a child's plan: its record of the parent's stage 1."""
    lines = ["", "[meta.sub_plan]", "stage = 1", 'means = "Edit"', f'method = "{method}"',
             'procedure = "1. read 2. edit"', 'conditions = "- the module imports [risk: unread]"']
    lines += [f'{field} = "{value}"' for field, value in approvals.items()]
    lines += ["[[meta.sub_plan.resource_request]]", "stage = 1", 'allow = ["Edit"]',
              "[meta.sub_plan.estimate]", "minutes = 40", "usd = 2.0",
              'author = "executor"', 'confirmer = "parent"',
              "[[meta.compared_plans.entries]]", 'ref = "plan-a"', 'departure = "no migration"']
    return "\n".join(lines) + "\n"


def _text_of(result) -> str:
    if isinstance(result, dict):
        return str(result.get("detail", "")) + json.dumps(result.get("data") or {})
    return result.detail + " ".join(result.data.get("blockers", []) + result.data.get("problems", []))


class Contour:
    """The parent session `parent-s` (EXECUTING, stage 1 decided later) and a real child linked
    to its stage 1 through the handoff a spawn writes, in one state directory. The child is
    driven the way a spawned child is -- with its handoff in the environment, through the engine
    and through `agentctl-own.py` -- and the user's side runs in a process with neither."""

    def __init__(self, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, *,
                 in_thread=False):
        import test_child_approval as tca

        self.tca, self.store, self.mp, self.capsys = tca, store, monkeypatch, capsys
        monkeypatch.setattr("agentctl.store.DEFAULT_ROOT", store.root)
        self.tmp, self.handoff, self.plans = tmp_path, None, 0
        _executing(store, venued_fixtures_dir, PARENT)
        _described(store, PARENT, lazy=True, in_thread=in_thread)
        work = tmp_path / "work"
        work.mkdir()
        self.child = tca.Child(work, monkeypatch)
        self.child.root, self.child.store, self.child.start_parent = store.root, store, False

    def text(self, method="split the module", **approvals) -> str:
        text = self.tca._spliced(parent_norm=self.child.digest,
                                 final_checks=((self.tca.PARENT_CHECK, 0),))
        text = text.replace("depends_on = []\n", 'depends_on = []\nplace = "the engine"\n'
                            'binding_conditions = []\n', 1)
        return text + _subplan_table(method, **approvals)

    def write(self, text: str) -> str:
        self.plans += 1
        return self.child.write(text, name=f"child-{self.plans}.toml")

    @contextlib.contextmanager
    def as_child(self):
        self.mp.setenv(norm_handoff.NORM_HANDOFF_ENV, self.handoff)
        try:
            yield
        finally:
            self.mp.delenv(norm_handoff.NORM_HANDOFF_ENV)

    def submit(self, text: str | None = None) -> dict:
        """Link the child and run its start, classify, plan and `submit-plan`."""
        plan = self.write(text or self.text())
        self.child.link(grants=self.tca._covering_grants(plan) + [{"rule": "Edit"}])
        self.handoff = os.environ[norm_handoff.NORM_HANDOFF_ENV]
        self.mp.delenv(norm_handoff.NORM_HANDOFF_ENV)
        with self.as_child():
            return self.child.submit(plan)

    def own(self, *argv):
        """One call through the channel a linked child has: (exit code, JSON body, stderr)."""
        spec = importlib.util.spec_from_file_location("agentctl_own_under_test",
                                                      SCRIPTS / "agentctl-own.py")
        channel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(channel)
        self.capsys.readouterr()
        self.mp.setenv(own_contour.OWN_SESSION_ENV, CHILD)
        try:
            with self.as_child():
                code = channel.main(list(argv))
        finally:
            self.mp.delenv(own_contour.OWN_SESSION_ENV)
        out = self.capsys.readouterr()
        try:
            body = json.loads(out.out)
        except ValueError:
            body = {}
        return code, body, out.err

    def entry(self) -> dict:
        return sub_plan.approval_entry(self.store.load(PARENT), 1)

    def child_state(self):
        return self.store.load(CHILD)


@pytest.fixture
def contour(home_store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    return Contour(home_store, venued_fixtures_dir, tmp_path, monkeypatch, capsys)


def test_approve_subplan_is_user_authority(contour, tmp_path):
    """The verb is the user's: it is in the user-authority set and out of the child's own
    contour, and a real linked child that calls it through its channel is turned away before the
    engine, with the field left unset; the user's process, shown the sub-plan, sets it."""
    assert "approve-subplan" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert "approve-subplan" not in own_contour.OWN_CONTOUR_VERBS
    assert "approve-stage-resources" in own_contour.OWN_CONTOUR_VERBS
    assert "approve-stage-resources" not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert {"approve-subplan", "approve-stage-resources"} <= set(cli_common._SESSION_COMMANDS)

    submitted = contour.submit()
    assert submitted["ok"] is True, submitted
    for verb in ("approve-subplan", "approve-resources"):
        code, _, err = contour.own(verb, "--stage", "1", "--by", "fedor")
        assert code == 2 and "outside the own contour" in err, (verb, code, err)
    assert "user_subplan_approval" not in contour.entry()
    assert _present_and_approve(contour.store, tmp_path, sid=PARENT).ok is True
    assert contour.entry()["user_subplan_approval"]


def test_subplan_digest_same_for_both_carriers(contour, tmp_path):
    """The approval is of the plan, not of where it is kept: approved on the record a child
    contour carries (read through the link), the stage starts on the same plan carried by a
    frame, whatever place it was built in and whatever its estimate; an edit stops it."""
    assert contour.submit()["ok"] is True
    store = contour.store
    through_the_link = sub_plan.read(store.load(PARENT), 1, store)
    assert through_the_link is not None and through_the_link.method == "split the module"
    assert _present_and_approve(store, tmp_path, sid=PARENT).ok is True
    approved = contour.entry()["user_subplan_approval"]

    in_a_frame = sub_plan.SubPlanRecord.from_dict(through_the_link.to_dict())
    in_a_frame.building_place = sub_plan.BuildingPlace(session="root", pushed=3, popped=9)
    in_a_frame.estimate = sub_plan.Estimate(minutes=5, usd=1.0, author="a", confirmer="b")
    _put(store, in_a_frame, PARENT)
    assert sub_plan.read(store.load(PARENT), 1, store).building_place.session == "root"
    assert sub_plan.approval_digest(in_a_frame) == approved
    assert _agent_approves_stage_resources(store, sid=PARENT).ok is True
    spawns = Spawns()
    assert _dispatch(store, PARENT, spawns).ok is not False and spawns.argvs

    in_a_frame.method = "another way"
    _put(store, in_a_frame, PARENT)
    refused = Spawns()
    _not_started(_dispatch(store, PARENT, refused), refused, "changed since the user approved it")


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
    assert planned.action == "plan_again" and "no sub-plan" in planned.detail, planned
    assert planned.data["planning_dispatch"] is True
    _put(store, _rec(1))
    planned = _dispatch(store, spawns=spawns, subplan_only=True)
    assert planned.action == "await_subplan_approval" and "is on record" in planned.detail, planned
    assert len(spawns.argvs) == 2 and planning_only.FLAG in spawns.argvs[0]
    assert "is not done when you return" in " ".join(spawns.argvs[0])
    assert store.load(SESSION).stage(1).outcome.status != "PASSED"
    assert _entry(store).get("planning") is True

    link = {"parent_session": SESSION, "stage_index": 1}
    child = state_mod.SessionState(session_id="kid", task_id="t", parent_link=link)
    assert any("only to plan it" in b
               for b in gates.child_approval_blockers(child, store.load(SESSION)))

    second = _dispatch(store, spawns=spawns, subplan_only=True)
    assert second.action == "await_subplan_approval" and len(spawns.argvs) == 3
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
    assert _agent_approves_stage_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and started.argvs
    assert planning_only.FLAG not in started.argvs[0]
    assert _entry(store).get("planning") is False
    sub_plan.store(child, _rec(1))
    assert gates.child_approval_blockers(child, store.load(SESSION)) == []


def _linked_kid(store, tmp_path, sid, created_at, record=None, *, parent=SESSION, stage_index=1):
    """A child session the way a spawn leaves it: its handoff record written and adopted through
    `norm_handoff`, so its `parent_link` is the engine's own, and its record kept under the stage
    number alone."""
    from test_child_approval import _parent_norm
    from conftest import spawn_handoff
    from agentctl import norm_handoff

    handoff = norm_handoff.build_record(
        parent_session=parent, stage_index=stage_index, kind="developer", norm=_parent_norm(tmp_path),
        grants=[], depth=1, parent_lineage=None, created_at=created_at)
    link, reason = norm_handoff.verify_env_value(spawn_handoff(store.root, handoff, sid))
    assert link is not None, reason
    kid = state_mod.SessionState(session_id=sid, task_id="t", parent_link=link)
    if record is not None:
        sub_plan.store(kid, record)
    store.save(kid)
    return kid


def test_a_stage_planned_by_two_children_is_read_from_the_one_handed_the_stage_last(
        home_store, venued_fixtures_dir, tmp_path):
    """After a second planning dispatch and after a work dispatch that followed a planning one, the
    stage has several linked children: presentation, `approve-subplan` and the start read the one
    record of the child handed the stage last, and none of them fails on 'cannot be read'. A read
    that raises on two children (or picks by session id) fails this."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    _linked_kid(store, tmp_path, "zz-first", "2026-01-01T00:00:00.000001+00:00",
                _rec(1, method="first idea"))
    _linked_kid(store, tmp_path, "aa-second", "2026-01-01T00:00:00.000002+00:00",
                _rec(1, method="second idea"))
    assert sub_plan.read(store.load(SESSION), 1, store).method == "second idea"
    assert _present_and_approve(store, tmp_path).ok
    _linked_kid(store, tmp_path, "mm-work", "2026-01-01T00:00:00.000003+00:00")
    assert sub_plan.read(store.load(SESSION), 1, store).method == "second idea"
    assert _agent_approves_stage_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and started.argvs


def _record_passed(store, sid=SESSION):
    return cli.cmd_record_result(ns(session=sid, status="passed", actual="done", control="reviewed",
                                    observation="obs"), store=store)


@pytest.mark.parametrize("returned", ["INCOMPLETE: nothing yet\n", "no marker at all\n",
                                      "PERMISSION-REQUEST:\nAction: write a file\nWhy: x\n"])
def test_a_stage_dispatched_only_to_be_planned_cannot_be_recorded_as_passed(
        home_store, venued_fixtures_dir, tmp_path, returned):
    """Through `dispatch --subplan-only`, `record-result` and the approval verbs: a planning child
    that returns anything but a recorded sub-plan leads to planning again, never to accepting the
    stage; the stage cannot be recorded as passed while the planning mark stands, the refusal
    names the way on, and a recorded, approved, started stage is no longer refused by it."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    planned = _dispatch(store, spawns=Spawns(returned), subplan_only=True)
    assert planned.action == "plan_again" and planned.data["planning_dispatch"] is True
    assert "its sub-plan is on record" not in planned.detail and "dispatch --session" in planned.detail
    state = store.load(SESSION)
    assert state.node == Node.EXECUTING.value and state.permission_request is None
    assert state.stage(1).outcome.status == "ACTIVE"

    refused = _record_passed(store)
    _refused(refused, "only to plan it", "dispatch --subplan-only", "approve-subplan")
    assert store.load(SESSION).stage(1).outcome.status == "ACTIVE"

    _put(store, _rec(1))
    assert _dispatch(store, spawns=Spawns("PLAN-READY: sub-plan recorded\n"),
                     subplan_only=True).action == "await_subplan_approval"
    _refused(_record_passed(store), "only to plan it")
    assert _present_and_approve(store, tmp_path).ok
    _refused(_record_passed(store), "only to plan it")
    assert _agent_approves_stage_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and started.argvs
    after = _record_passed(store)
    assert "only to plan it" not in after.detail and "record-result refused" not in after.detail


def test_a_record_of_a_stage_whose_approvals_are_missing_is_refused_with_the_verb(
        home_store, venued_fixtures_dir):
    """The start guards stand at the record as at the start: a stage above the line that never
    had the parent's approval is not recorded as passed, and the refusal names the verb."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    _refused(_record_passed(store), "record-result refused", "approve-resources --stage 1")
    state = store.load(SESSION)
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    store.save(state)
    assert "record-result refused" not in _record_passed(store).detail


def _push_resolve_pop(store, tmp_path, sid=SESSION):
    """A service plan is pushed from stage 1, resolved, and popped, the way the verbs run it."""
    from agentctl.state import GateRecord, Partition

    plan = tmp_path / "service.toml"
    plan.write_text("", encoding="utf-8")
    pushed = cli.cmd_push_subplan(ns(session=sid, plan=str(plan), task=None, originating_stage=1),
                                  store=store)
    assert pushed.ok, pushed
    state = store.load(sid)
    state.node = Node.RESOLVED.value
    state.partition = Partition(verdict="single")
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    store.save(state)
    return cli.cmd_pop_subplan(ns(session=sid), store=store)


@pytest.mark.parametrize("lazy", [True, False], ids=["decided-later", "planned-up-front"])
def test_a_service_plan_does_not_pass_its_stage_without_the_approvals_the_start_needs(
        home_store, venued_fixtures_dir, tmp_path, lazy):
    """`push-subplan` from an in-thread stage above the line and `pop-subplan` after the service
    plan resolved: the pop restores the parent (it is the only way out of the child, so it is
    never refused) but marks the stage satisfied only when the start guards would let it start
    now. Without the user's approvals the stage stays active, the directive names the verbs that
    go on, and `record-result` is refused until they are given; with them the pop passes it."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=lazy)
    _put(store, _rec(1))
    popped = _push_resolve_pop(store, tmp_path)
    assert popped.ok, popped
    assert store.load(SESSION).stage(1).outcome.status == "ACTIVE"
    assert not store.load(SESSION).plan_stack
    way_on = "approve-subplan" if lazy else "approve-resources --stage 1"
    assert way_on in popped.detail and "not marked satisfied" in popped.detail
    _refused(_record_passed(store), way_on)

    if lazy:
        assert _present_and_approve(store, tmp_path).ok
        assert _agent_approves_stage_resources(store).ok
    else:
        _present(store, tmp_path)
        assert _user_approves_resources(store).ok
    assert _start_blockers(store) == []
    before_recording = store.load(SESSION)
    assert _record_passed(store).ok
    store.save(before_recording)
    popped = _push_resolve_pop(store, tmp_path)
    assert popped.ok and "not marked satisfied" not in popped.detail
    assert store.load(SESSION).stage(1).outcome.status == "PASSED"


@pytest.mark.parametrize("described", [None, {"minutes": 5}], ids=["old-form", "below-the-line"])
def test_a_service_plan_passes_a_stage_the_start_guards_do_not_ask_approvals_of(
        home_store, venued_fixtures_dir, tmp_path, described):
    """An old-form stage and a stage below the size line need no approval to start, so the pop
    satisfies them as it always did."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    if described:
        _described(store, **described)
    popped = _push_resolve_pop(store, tmp_path)
    assert popped.ok and popped.action == "next_stage", popped
    assert store.load(SESSION).stage(1).outcome.status == "PASSED"


def test_a_planning_childs_run_is_not_the_stages_work(home_store, venued_fixtures_dir, monkeypatch):
    """A planning child's denial is the read-only boundary working: nothing it was denied is
    classified against the stage's grants, a once-grant is not spent, and its run is not booked as
    the stage's estimate actuals; the stage's own work dispatch does all three."""
    from agentctl import cli_dispatch

    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=False)
    state = store.load(SESSION)
    state.runtime_grants["1"] = [{"scope": "once", "consumed": False, "rule": "Bash(make:*)"}]
    store.save(state)
    classified, booked = [], []
    monkeypatch.setattr(cli_dispatch, "_classify_transcript_denials",
                        lambda *a, **k: classified.append(a))
    monkeypatch.setattr(cli_dispatch.resource_estimation, "record_stage_estimate",
                        lambda *a, **k: booked.append(a))
    _put(store, _rec(1))
    assert _dispatch(store, spawns=Spawns("INCOMPLETE: denied\n"), subplan_only=True).ok is not False
    assert not classified and not booked
    assert store.load(SESSION).runtime_grants["1"][0]["consumed"] is False
    assert store.load(SESSION).node == Node.EXECUTING.value

    assert _dispatch(store, spawns=Spawns(), subplan_only=True).ok is not False
    state = store.load(SESSION)
    sub_plan.write_user_approval(state, 1, _rec(1))
    sub_plan.write_parent_approval(state, 1, _rec(1).resource_request)
    store.save(state)
    assert _dispatch(store, spawns=Spawns()).ok is not False
    assert classified and booked
    assert store.load(SESSION).runtime_grants["1"][0]["consumed"] is True


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


@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "bypassPermissions", "dontAsk"])
def test_planning_only_cannot_be_combined_with_a_mode_that_lets_the_child_write(tmp_path, mode, capsys):
    """A planning child never runs in a writing mode: the flag parser does not offer one, and
    `resolve_permission_mode` itself refuses one handed to it; `default` and `plan` pass. The
    cursor wrapper starts its agent with `--force`, so it refuses a planning child outright."""
    spawn = _load_spawn_script()
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n**<<this step>>**\n", encoding="utf-8")
    base = ["--kind", "developer", "--plan", str(plan), "--done-criterion", "dc",
            "--criterion-type", "measurable", "--effort", "low", "--complexity", "medium",
            planning_only.FLAG]
    with pytest.raises(SystemExit):
        spawn.build_parser().parse_args(base + ["--permission-mode", mode])
    args = spawn.build_parser().parse_args(base)
    args.permission_mode = mode
    with pytest.raises(SystemExit, match="cannot be combined"):
        spawn.resolve_permission_mode(args, [{"path": "/work", "mode": "write"}])
    for allowed in planning_only.READ_ONLY_MODES:
        args.permission_mode = allowed
        assert spawn.resolve_permission_mode(args, None) == allowed

    cursor_spec = importlib.util.spec_from_file_location(
        "spawn_cursor_planning", SCRIPTS / "spawn-cursor-specialist.py")
    cursor = importlib.util.module_from_spec(cursor_spec)
    cursor_spec.loader.exec_module(cursor)
    rc = cursor.main(["--kind", "developer", "--plan", str(plan), "--done-criterion", "dc",
                      "--criterion-type", "measurable", "--budget", "small", "--workspace",
                      str(tmp_path), "--complexity", "medium", planning_only.FLAG])
    assert rc == 2 and "--force" in capsys.readouterr().err


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


def test_child_receipt_does_not_approve(contour, tmp_path):
    """A sub-plan presented, and its delivery stamped, in a real linked child is no approval:
    `approve-subplan` run in that child is refused, writes nothing in the child or in the parent,
    and the parent's stage stays refused; run in the root session the user was shown it in, it
    approves."""
    assert contour.submit()["ok"] is True
    store = contour.store
    _present_and_approve(store, tmp_path, sid=PARENT, confirm=False)
    child = contour.child_state()
    child.plan_presentations.append(store.load(PARENT).plan_presentations[-1])
    store.save(child)
    stamped = cli.cmd_confirm_delivery(ns(session=CHILD, kind="subplan", by="fedor",
                                          note="delivered in the child",
                                          escape_reason="delivered_out_of_band"), store=store)
    assert stamped.ok, stamped
    in_the_child = cli_approval.cmd_approve_subplan(ns(session=CHILD, stage=1, by="fedor"),
                                                    store=store)
    _refused(in_the_child, "not in a linked child")
    assert "user_subplan_approval" not in contour.entry()
    assert not contour.child_state().stage_approvals.get("0:1", {}).get("user_subplan_approval")
    spawns = Spawns()
    _not_started(_dispatch(store, PARENT, spawns), spawns, "stage 1 is decided later")
    assert _present_and_approve(store, tmp_path, sid=PARENT).ok is True
    assert contour.entry()["user_subplan_approval"]


# --- the approvals are the engine's ---------------------------------------------------------

@pytest.mark.parametrize("field", ["parent_resource_approval", "user_subplan_approval"])
def test_preset_approval_refused_at_submission(contour, field):
    """A child's `submit-plan` and its `replan --plan`, run the way a linked child runs them,
    refuse a plan whose sub-plan record states an approval the engine has not written; the
    parent's entry stays unset and its stage stays refused at dispatch. The same plan without
    the field is taken."""
    refused = contour.submit(contour.text(**{field: "approved"}))
    assert refused["ok"] is False, refused
    assert f"`{field}` is stated" in _text_of(refused), refused
    assert field not in contour.entry()
    spawns = Spawns()
    _not_started(_dispatch(contour.store, PARENT, spawns), spawns, "decided later")

    plain = contour.write(contour.text())
    with contour.as_child():
        assert contour.child.run("submit_plan", session=CHILD, plan=plain)["ok"] is True
    preset = contour.write(contour.text(**{field: "approved"}))
    code, body, err = contour.own("replan", "--plan", preset)
    assert f"`{field}` is stated" in json.dumps(body) + err, (code, body, err)
    assert field not in contour.entry()


def _frame_submission(store, venued_fixtures_dir, tmp_path, **approvals):
    """The frame carrier: stage 1 of a decided-later plan is planned in a pushed frame of the
    same session -- push-subplan, classify, plan, submit-plan of a plan carrying the record."""
    _executing(store, venued_fixtures_dir, PARENT)
    _described(store, PARENT, lazy=True)
    base = (venued_fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    base = base.replace("depends_on = []\n", 'depends_on = []\nplace = "the engine"\n'
                        'binding_conditions = []\n', 1)
    path = tmp_path / "frame.toml"
    path.write_text(base + _subplan_table(**approvals), encoding="utf-8")
    assert cli.cmd_push_subplan(ns(session=PARENT, plan=str(path), task=None,
                                   originating_stage=1), store=store).ok
    cli.cmd_classify(ns(session=PARENT, architectural=True, changed_lines=200,
                        wall_clock_min=60), store=store)
    cli.cmd_plan(ns(session=PARENT), store=store)
    return cli.cmd_submit_plan(ns(session=PARENT, plan=str(path)), store=store)


@pytest.mark.parametrize("field", ["parent_resource_approval", "user_subplan_approval"])
def test_preset_approval_refused_in_a_frame_submission(store, venued_fixtures_dir, tmp_path, field):
    """A frame submission carrying an approval the engine has not written is refused with the
    same reason and writes nothing."""
    preset = _frame_submission(store, venued_fixtures_dir, tmp_path, **{field: "approved"})
    assert preset.ok is False and f"`{field}` is stated" in _text_of(preset), preset
    assert field not in store.load(PARENT).stage_approvals.get("0:1", {})


def test_a_frame_submission_without_the_approval_fields_is_taken(store, venued_fixtures_dir,
                                                                  tmp_path):
    plain = _frame_submission(store, venued_fixtures_dir, tmp_path)
    assert plain.ok is True, _text_of(plain)


def test_rewritten_approval_after_edit_refused(contour, tmp_path):
    """After the user approved the sub-plan, a child that edits it cannot carry the approval
    over by writing the new digest into its own plan: its `replan --plan` is refused with the
    named reason and the parent's copy keeps the approved digest; the edit itself (made in the
    child's own record, which no plan text gates) leaves the stage refused at dispatch and at
    the child's own approval."""
    assert contour.submit()["ok"] is True
    assert _present_and_approve(contour.store, tmp_path, sid=PARENT).ok is True
    approved = contour.entry()["user_subplan_approval"]
    record = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    record.method = "split it differently"
    rewritten = sub_plan.approval_digest(record)
    assert rewritten != approved

    code, body, err = contour.own("replan", "--plan", contour.write(
        contour.text("split it differently", user_subplan_approval=rewritten)))
    assert "`user_subplan_approval` is stated and differs" in json.dumps(body) + err, (code, body, err)
    assert contour.entry()["user_subplan_approval"] == approved

    child = contour.child_state()
    sub_plan.store(child, record)
    contour.store.save(child)
    assert contour.entry()["user_subplan_approval"] == approved
    assert sub_plan.read(contour.store.load(PARENT), 1, contour.store).method == \
        "split it differently"
    _refused(_agent_approves_stage_resources(contour.store, sid=PARENT), "changed since the user approved it")
    spawns = Spawns()
    _not_started(_dispatch(contour.store, PARENT, spawns), spawns, "changed since the user approved it")
    code, body, err = contour.own("approve", "--by", "agent")
    assert code != 0 or body.get("ok") is False, (code, body)
    assert "changed since the user approved it" in json.dumps(body) + err, (code, body, err)


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
        store, venued_fixtures_dir, tmp_path, config_row):
    """In one plan, stage 1 above the line and stage 2 below it (five minutes): before the user's
    approval of the plan stage 1 cannot start (in-thread or spawned) and stage 2 has nothing to
    wait for; the user's `approve` writes the parent approval of stage 1 alone; stage 2 starts on
    the plan's approval with no sub-plan and no parent approval, and the verb refuses it as
    below the line. Moving the configured line to three minutes moves stage 2 above it."""
    _planning(store)
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, in_thread=(2,), minutes={2: 5})
    cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    needs = "the parent has not approved its resources"
    assert needs in " ".join(_start_blockers(store, 1))
    assert _start_blockers(store, 2) == []

    _put(store, _rec(1))
    assert cli.cmd_approve(ns(session=SESSION, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=SESSION, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    assert list(store.load(SESSION).stage_approvals) == ["0:1"]
    assert _start_blockers(store, 1) == [] and _start_blockers(store, 2) == []
    _refused(_agent_approves_stage_resources(store, stage=2), "below the size line")
    assert _next_stage(store).ok is not False
    spawns = Spawns()
    _started(_dispatch(store, spawns=spawns), spawns, in_thread=False)

    config_row("substantive-wall-clock-min", 3)
    assert needs in " ".join(_start_blockers(store, 2))


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
    child.start_parent = False
    tca._linked_submit(child)
    refused = child.run("approve", session="child-1", by="agent")
    assert refused["ok"] is False
    blockers = refused["data"]["blockers"]
    assert any("cannot be read" in b and "report this to your parent" in b for b in blockers), blockers
    child.store.save(state_mod.SessionState(session_id="parent-s", task_id="t",
                                            stages=[so._stage(1, place=ENGINE)]))
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


# --- the parent's approval of resources -------------------------------------------------------

def _later_plan_of_an_approved_order(tmp_path):
    """The user approves a new-form plan of an order; a later plan of the same order is opened
    and the engine's verdict on it is `self_approve`. Returns the harness and the later session."""
    import test_replan_autonomy_boundary as tr

    eng = tr.Eng(tmp_path / "root-order")
    eng.tmp.mkdir()
    text = tr.plan_text()
    for marker in ("depends_on = []\n", "depends_on = [1]\n"):
        text = text.replace(marker, marker + 'place = "the engine"\nbinding_conditions = []\n', 1)
    plan = eng.write(text, "approved.toml")
    eng.approve_as_user("u1", plan)
    opened = eng.open("c1", plan)
    assert tr.action(opened) == "self_approve", opened
    return eng, plan


def test_agent_approval_writes_resource_approval(contour, tmp_path):
    """`approve --by agent` on the engine's `self_approve` verdict -- a later plan of a
    user-approved order at the root, and a linked child's own plan through `agentctl-own.py` --
    writes the parent's approval of a stage's resources, so the stage starts after it and not
    before; a child's request outside the set its parent approved is left unapproved, with the
    stage refused, and a plan that leaves the user's approved resources goes to the user."""
    eng, plan = _later_plan_of_an_approved_order(tmp_path)
    later = eng.state("c1")
    sub_plan.store(later, _rec(1, allow=("Edit",)), level=0)
    sub_plan.write_parent_approval(later, 1, _rec(1, allow=("Edit",)).resource_request)
    later.stage_approvals["0:1"].pop("parent_resource_approval")
    eng.store.save(later)
    before = gates.stage_start_blockers(later, later.stage(1), store=eng.store)
    assert before and "the parent has not approved" in before[0], before
    assert eng.run("approve", session="c1", by="agent")["ok"] is True
    approved = eng.state("c1")
    assert approved.stage_approvals["0:1"]["parent_resource_approval"] \
        == sub_plan.resource_request_digest(_rec(1).resource_request)
    assert gates.stage_start_blockers(approved, approved.stage(1), store=eng.store) == []

    import test_replan_autonomy_boundary as tr

    wider = tr.plan_text(extra_outputs=("another.py",)).replace(
        "depends_on = []\n", 'depends_on = []\nplace = "the engine"\nbinding_conditions = []\n', 1)
    assert tr.action(eng.open("c2", eng.write(wider))) == "await_user_approval"
    assert eng.run("approve", session="c2", by="agent")["ok"] is False

    submitted = contour.submit()
    assert submitted["ok"] is True, submitted
    assert _present_and_approve(contour.store, tmp_path, sid=PARENT).ok is True
    assert _agent_approves_stage_resources(contour.store, sid=PARENT).ok is True
    for allow, written in ((("Edit",), True), (("Edit", "Read"), False)):
        child = contour.child_state()
        child.node = Node.PLAN_READY.value
        child.stage_approvals.pop("0:1", None)
        sub_plan.store(child, _rec(1, allow=allow), level=0)
        contour.store.save(child)
        code, body, err = contour.own("approve", "--by", "agent")
        assert code == 0 and body.get("ok") is True and body["action"] != "no_action", (code, body, err)
        child = contour.child_state()
        assert bool(child.stage_approvals.get("0:1", {}).get("parent_resource_approval")) is written
        blockers = gates.stage_start_blockers(child, child.stage(1), store=contour.store)
        assert (blockers == []) is written, blockers
        if not written:
            assert REPORT_TO_PARENT in blockers[0]


def test_an_agents_approval_with_no_approved_set_leaves_a_request_unapproved_and_names_the_way_on(
        tmp_path):
    """`approve --by agent`, through the verb, on a stage no approved set is on record for -- a
    session approved before approvals were recorded that replans, or a stage a later self-approved
    plan version added -- starts nothing the user never approved: a request that names resources
    or permissions is left unapproved and the stage refused, naming the whole way on (`replan`
    and the user's approval of the plan, or the sub-plan presented and `approve-resources`;
    the legacy-session test walks the verb itself)."""
    eng, _ = _later_plan_of_an_approved_order(tmp_path)
    later = eng.state("c1")
    sub_plan.store(later, _rec(1, allow=("Edit",)), level=0)
    eng.store.save(later)
    assert eng.run("approve", session="c1", by="agent")["ok"] is True
    after = eng.state("c1")
    assert "parent_resource_approval" not in after.stage_approvals.get("0:1", {})
    blockers = " ".join(gates.stage_start_blockers(after, after.stage(1), store=eng.store))
    assert "the parent has not approved its resources and permissions" in blockers
    assert "`replan`, then `approve --by user`" in blockers
    assert "approve-resources --stage 1" in blockers


def test_an_agents_approval_of_a_request_outside_the_approved_set_leaves_it_unapproved(tmp_path):
    """`approve --by agent`, through the verb, on a stage whose request was widened past the set
    the user approved: the approval of the earlier request stands, no approval of the wider one is
    written, the set stays as it was, and the stage is refused with the exact reason."""
    eng, _ = _later_plan_of_an_approved_order(tmp_path)
    later = eng.state("c1")
    sub_plan.write_parent_approval(later, 1, _rec(1, allow=("Edit",)).resource_request)
    sub_plan.store(later, _rec(1, allow=("Edit", "Bash(rm:*)")), level=0)
    eng.store.save(later)
    assert eng.run("approve", session="c1", by="agent")["ok"] is True
    after = eng.state("c1")
    assert after.stage_approvals["0:1"]["parent_resource_approval"] \
        == sub_plan.resource_request_digest(_rec(1, allow=("Edit",)).resource_request)
    assert after.stage_approvals["0:1"]["approved"]["allow"] == ["Edit"]
    assert gates.stage_start_blockers(after, after.stage(1), store=eng.store)[0].startswith(
        "stage 1 cannot start: its resource request changed since the parent approved it:")


def test_agent_approval_writes_resource_approval_for_the_set_the_user_approved(
        store, venued_fixtures_dir):
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
    """With no set the user approved, the agent's approval of a plan leaves a request that names
    something unapproved and seeds no set -- the agent's own approval is never what a later
    request is tested against -- while a request that names nothing is inside any set and is
    approved."""
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    store.save(state)
    assert not _entry(store)
    _refused(_dispatch(store), "approve-resources --stage 1")
    _refused(_agent_approves_stage_resources(store), "approve-resources --stage 1")

    state = _put(store, _rec(1, allow=()))
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    store.save(state)
    entry = _entry(store)
    assert entry["parent_resource_approval"] and "approved" not in entry
    assert _dispatch(store).action == "execute_in_thread"


@pytest.mark.parametrize("allow, written", [(("Edit",), True), (("Edit", "Read"), False)])
def test_a_childs_own_plan_is_approved_inside_the_set_its_parent_approved(
        tmp_path, monkeypatch, allow, written):
    """A linked child's `approve --by agent` of its own new-form plan, through the CLI: the
    child's per-stage approval is written when the request lies inside the resources the parent
    approved for the stage the child was handed, and is not when it lies outside."""
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
    sub_plan.store(state, _rec(1, allow=allow), level=0)
    child.store.save(state)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True
    assert bool(child.state().stage_approvals.get("0:1", {}).get("parent_resource_approval")) is written


@pytest.mark.parametrize("allow, written", [(("Edit",), True), (("Edit", "Read"), False)])
def test_a_plan_approved_under_a_mandate_leaves_its_children_a_set_to_be_approved_inside(
        tmp_path, monkeypatch, venued_fixtures_dir, allow, written):
    """The set a child of a mandate-approved stage is checked against is the request the
    mandate-approved plan states for that stage: the mandate approval is the user's standing
    authority for the item (the mandate checks bound the plan), so it records the set as the user's
    approval does; a child's request inside it is approved, one outside it is not."""
    approver = "mandate:m1"
    import test_child_approval as tca

    monkeypatch.setattr(cli_approval, "_mandate_basis_problems", lambda *a, **k: [])
    child = tca.Child(tmp_path, monkeypatch)
    child.start_parent = False
    root = child.store
    _planning(root, "parent-s")
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, in_thread=(1, 2))
    cli.cmd_submit_plan(ns(session="parent-s", plan=plan), store=root)
    parent = root.load("parent-s")
    sub_plan.store(parent, _rec(1, allow=("Edit",)), level=0)
    sub_plan.store(parent, _rec(2, allow=("Edit",)), level=0)
    root.save(parent)
    approved = cli.cmd_approve(ns(session="parent-s", by=approver), store=root)
    assert approved.ok is True, approved
    entry = root.load("parent-s").stage_approvals["0:1"]
    assert entry["parent_resource_approval"]
    assert entry.get("approved")

    text = tca._spliced(parent_norm=child.digest, final_checks=((tca.PARENT_CHECK, 0),))
    text = text.replace("depends_on = []\n", 'depends_on = []\nplace = "the engine"\n'
                        'binding_conditions = []\n', 1)
    tca._linked_submit(child, plan_text=text)
    state = child.state()
    state.stages[0].place = ENGINE
    state.stages[0].binding_conditions_stated = True
    sub_plan.store(state, _rec(1, allow=allow), level=0)
    child.store.save(state)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True
    got = bool(child.state().stage_approvals.get("0:1", {}).get("parent_resource_approval"))
    assert got is written


def test_user_approval_writes_resource_approval_and_the_set(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit", "Read"), dirs=(("/work/x", "write"),)), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    entry = state.stage_approvals["0:1"]
    assert entry["approved"]["allow"] == ["Edit", "Read"]
    assert entry["approved"]["add_dirs"] == [["/work/x", "write"]]
    assert gates.parent_resource_dispatch_blockers(state, state.stage(1), sub_plan.read(state, 1)) == []


def test_stage_resource_approval_verb(contour, venued_fixtures_dir, tmp_path, config_row):
    """The verb is the agent's (in the own contour, out of the user-authority set). For a
    decided-later stage planned by the planning dispatch it is refused before the user approves
    the sub-plan, writes nothing, and passes after it, on the request recorded in the user's
    approval; a child runs it through `agentctl-own.py` for a stage of its own plan -- inside
    the resources its parent approved for the stage it was handed it passes, outside them it is
    refused -- and not for a stage outside its plan; below the line it is refused, and the line is
    the configured one. The verb asks no method question."""
    assert "approve-stage-resources" in own_contour.OWN_CONTOUR_VERBS
    assert "approve-stage-resources" not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    store = contour.store
    planned = Spawns()
    assert _dispatch(store, PARENT, planned, subplan_only=True).ok is not False
    assert len(planned.argvs) == 1
    assert contour.submit()["ok"] is True
    spawns = Spawns()
    _not_started(_dispatch(store, PARENT, spawns), spawns, "stage 1 is decided later")
    _refused(_agent_approves_stage_resources(store, sid=PARENT), "has not approved a sub-plan")
    assert "parent_resource_approval" not in contour.entry()

    assert _present_and_approve(store, tmp_path, sid=PARENT).ok is True
    approved = _agent_approves_stage_resources(store, sid=PARENT)
    assert approved.ok is True and "method" not in approved.detail.lower(), approved
    recorded = sub_plan.read(store.load(PARENT), 1, store)
    assert contour.entry()["parent_resource_approval"] \
        == sub_plan.resource_request_digest(recorded.resource_request)
    started = Spawns()
    assert _dispatch(store, PARENT, started).ok is not False and started.argvs

    for allow, passes in ((("Edit", "Read"), False), (("Edit",), True)):
        child = contour.child_state()
        child.stage_approvals.pop("0:1", None)
        sub_plan.store(child, _rec(1, allow=allow), level=0)
        contour.store.save(child)
        code, body, err = contour.own("approve-stage-resources", "--stage", "1")
        text = json.dumps(body) + err
        assert (body.get("ok") is True) is passes, (code, text)
        assert "method" not in text.lower()
        written = contour.child_state().stage_approvals.get("0:1", {})
        assert bool(written.get("parent_resource_approval")) is passes
        if not passes:
            assert "outside the approved set" in text, text
    code, body, err = contour.own("approve-stage-resources", "--stage", "9")
    assert body.get("ok") is False and "not a stage of this session's own plan" in json.dumps(body), body

    _executing(store, venued_fixtures_dir)
    _described(store, minutes=5)
    _put(store, _rec(1))
    _refused(_agent_approves_stage_resources(store), "below the size line")
    assert "parent_resource_approval" not in _entry(store)
    config_row("substantive-wall-clock-min", 3)
    _refused(_agent_approves_stage_resources(store), "approve-resources --stage 1")


def test_stage_resource_approval_for_a_decided_later_stage_follows_the_users_approval(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=True)
    _put(store, _rec(1))
    _refused(_agent_approves_stage_resources(store), "has not approved a sub-plan")
    assert "parent_resource_approval" not in _entry(store)
    assert _present_and_approve(store, tmp_path).ok
    assert _agent_approves_stage_resources(store).ok
    assert _entry(store)["parent_resource_approval"]


def test_stage_resource_approval_needs_an_approved_set(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    out = _agent_approves_stage_resources(store)
    _refused(out, "approve-resources --stage 1")
    assert "parent_resource_approval" not in _entry(store)


def _user_approves_resources(store, by="fedor", stage=1, sid=SESSION):
    return cli_approval.cmd_approve_resources(ns(session=sid, stage=stage, by=by), store=store)


def test_a_session_approved_before_approvals_were_recorded_goes_on_by_the_verb_the_refusal_names(
        home_store, venued_fixtures_dir, tmp_path):
    """A plan approved before per-stage approvals were written (or with no order at all, as
    here: an old-form plan driven to EXECUTING) has no approval entry for a stage above the line.
    Every way of going on is refused naming `approve-resources`; the verb itself is refused until
    the stage's sub-plan was presented for its live digest and the delivery stamped (the bar of
    `approve-subplan`), by an agent at any point, and then works and leaves the set the agent's
    later requests are checked against."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store)
    _put(store, _rec(1))
    assert not store.load(SESSION).stage_approvals
    spawns = Spawns()
    for refused in (_dispatch(store, spawns=spawns), _dispatch(store, dry_run=True),
                    _agent_approves_stage_resources(store), _record_passed(store)):
        _refused(refused, "approve-resources --stage 1", "`replan`, then `approve --by user`")
    assert not spawns.argvs
    _refused(_user_approves_resources(store, by="agent"), "approve-stage-resources")
    _refused(_user_approves_resources(store, by=""), "approve-stage-resources")
    _refused(_user_approves_resources(store), "was not presented to the user",
             "present-plan --kind subplan --stage 1")
    _present(store, tmp_path, confirm=False)
    _refused(_user_approves_resources(store), "has no delivery stamp")
    _put(store, _rec(1, allow=("Edit", "Read")))
    _refused(_user_approves_resources(store), "changed since it was presented")
    assert not store.load(SESSION).stage_approvals

    _put(store, _rec(1))
    _present(store, tmp_path)
    _refused(_user_approves_resources(store, by="agent"), "approve-stage-resources")
    assert not store.load(SESSION).stage_approvals
    assert _user_approves_resources(store).ok
    assert _entry(store)["parent_resource_approval"] and _entry(store)["approved"]
    assert _dispatch(store).action == "execute_in_thread"
    _put(store, _rec(1, allow=("Edit", "Read")))
    assert _agent_approves_stage_resources(store).ok is False
    _refused(_dispatch(store), "approve-resources --stage 1")


def test_the_way_to_the_users_plan_approval_names_replan_wherever_approve_is_not_legal():
    """`approve` is legal only at PLAN_READY; from every node the guards run at, the refusal
    names `replan` before it."""
    for node in Node:
        way = gates.user_plan_approval_way(Namespace(node=node.value))
        assert way == ("`approve --by user`" if node is Node.PLAN_READY
                       else "`replan`, then `approve --by user`"), node


def test_approve_resources_refuses_what_it_cannot_approve_and_says_what_to_do(
        store, venued_fixtures_dir, tmp_path):
    _executing(store, venued_fixtures_dir)
    _described(store, minutes=5)
    _put(store, _rec(1))
    _refused(_user_approves_resources(store), "below the size line")
    _refused(_user_approves_resources(store, stage=9), "no stage 9")

    _described(store)
    state = store.load(SESSION)
    state.sub_plan_records.clear()
    store.save(state)
    _refused(_user_approves_resources(store), "no sub-plan on record")

    _described(store, lazy=True)
    _put(store, _rec(1))
    _refused(_user_approves_resources(store), "approve-subplan")

    _described(store)
    state = store.load(SESSION)
    state.parent_link = {"session_id": "the-parent"}
    store.save(state)
    _refused(_user_approves_resources(store), "root session")
    assert not store.load(SESSION).stage_approvals


def test_approve_resources_is_registered_as_the_users_verb():
    assert "approve-resources" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert "approve-resources" in cli_common._SESSION_COMMANDS
    assert "approve-resources" not in own_contour.OWN_CONTOUR_VERBS
    args = cli.build_parser().parse_args(
        ["approve-resources", "--session", "s", "--stage", "1", "--by", "fedor"])
    assert (args.stage, args.by) == (1, "fedor")


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
    _refused(_agent_approves_stage_resources(store), "calling session's own plan")
    assert "parent_resource_approval" not in _entry(store)
    monkeypatch.setenv(own_contour.OWN_SESSION_ENV, SESSION)
    assert _agent_approves_stage_resources(store).ok


def test_resource_request_edit_voids_parent_approval(store, venued_fixtures_dir, tmp_path):
    """A stage of the root plan approved with the whole plan (`approve --by user`) starts. An
    edit of the record's request inside the approved set voids the parent's approval (the start
    is refused with the digest reason) until the agent's `approve-stage-resources` re-approves it;
    an edit widening it past the set leaves that verb refused and the start refused, and the user's
    way on is the plan's own approval: `replan`, then `approve --by user` (which records the wider
    set), after which the stage starts; an edit of the estimate alone leaves the approval in
    force."""
    _planning(store)
    plan = _new_form_plan(venued_fixtures_dir, tmp_path, in_thread=(1, 2))
    cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    _put(store, _rec(1, allow=("Edit", "Read")))
    assert cli.cmd_approve(ns(session=SESSION, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=SESSION, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    digest = _entry(store)["parent_resource_approval"]
    assert digest == sub_plan.resource_request_digest(_rec(1, allow=("Edit", "Read")).resource_request)
    assert _start_blockers(store) == []

    same_request = _rec(1, allow=("Edit", "Read"))
    same_request.estimate = sub_plan.Estimate(minutes=99, usd=9.0, author="a", confirmer="b")
    _put(store, same_request)
    assert _start_blockers(store) == []

    voided = "its resource request changed since the parent approved it"
    _put(store, _rec(1, allow=("Edit",)))
    assert voided in " ".join(_start_blockers(store))
    assert _agent_approves_stage_resources(store).ok is True
    assert _start_blockers(store) == []

    _put(store, _rec(1, allow=("Edit", "Read", "Bash(rm:*)")))
    assert voided in " ".join(_start_blockers(store))
    _refused(_agent_approves_stage_resources(store), "outside the approved set")
    assert voided in " ".join(_start_blockers(store))

    changed = Path(plan).read_text(encoding="utf-8").replace(
        'done_criterion = "both stages PASSED and resolution confirmed"',
        'done_criterion = "both stages PASSED, resolution confirmed, and the delivery announced"', 1)
    replanned = tmp_path / "replanned.toml"
    replanned.write_text(changed, encoding="utf-8")
    assert replanned.read_text(encoding="utf-8") != Path(plan).read_text(encoding="utf-8")
    assert cli.cmd_replan(ns(session=SESSION, plan=str(replanned)), store=store).marker == "PLAN-READY"
    assert voided in " ".join(_start_blockers(store))
    assert cli.cmd_approve(ns(session=SESSION, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=SESSION, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    assert _start_blockers(store) == []
    assert _entry(store)["approved"]["allow"] == sorted(["Edit", "Read", "Bash(rm:*)"])
    assert _next_stage(store).action == "execute_in_thread"


def test_no_verb_an_agent_can_call_widens_the_approved_set_without_the_users_act(
        home_store, venued_fixtures_dir, tmp_path):
    """Every verb a session can run after the record is widened past the set the user approved,
    short of the user's own act, leaves the stage refused and the set as it was: the agent's
    `approve-stage-resources`, an agent approval of the plan, `approve-resources` with no
    presentation, an unstamped one and a stale one, `approve-subplan` by an agent. Only the
    presented, stamped sub-plan and the user's `approve-resources` widen it, and the stage then
    starts."""
    store = home_store
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    store.save(state)
    approved_set = dict(_entry(store)["approved"])
    assert _start_blockers(store) == []

    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)")))
    state = store.load(SESSION)
    cli_approval._write_stage_approvals(state, store, by_agent=True)
    store.save(state)
    attempts = (
        _agent_approves_stage_resources(store),
        _user_approves_resources(store, by="agent"),
        _user_approves_resources(store),
        cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="agent"), store=store),
    )
    assert all(a.ok is False for a in attempts), attempts
    _present(store, tmp_path, confirm=False)
    assert _user_approves_resources(store).ok is False
    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)", "Read")))
    assert _user_approves_resources(store).ok is False
    assert _entry(store)["approved"] == approved_set
    assert _start_blockers(store)[0].startswith(
        "stage 1 cannot start: its resource request changed since the parent approved it:")
    spawns = Spawns()
    _not_started(_dispatch(store, spawns=spawns), spawns)

    _put(store, _rec(1, allow=("Edit", "Bash(rm:*)")))
    _present(store, tmp_path)
    assert _user_approves_resources(store).ok is True
    assert sorted(_entry(store)["approved"]["allow"]) == ["Bash(rm:*)", "Edit"]
    assert _start_blockers(store) == []
    assert _dispatch(store).action == "execute_in_thread"


def test_an_edit_inside_the_approved_set_is_reapproved_by_the_verb(store, venued_fixtures_dir):
    _executing(store, venued_fixtures_dir)
    state = _described(store)
    sub_plan.store(state, _rec(1, allow=("Edit", "Read")), level=0)
    cli_approval._write_stage_approvals(state, store, by_agent=False)
    sub_plan.store(state, _rec(1, allow=("Edit",)), level=0)
    store.save(state)
    _refused(_dispatch(store), "its resource request changed since the parent approved it")
    assert _agent_approves_stage_resources(store).ok
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


# --- a child whose parent stage carries no approved set -------------------------------------------

REPORT_TO_PARENT = "report the request to your parent"
THE_USERS_VERBS = ("approve --by user", "approve-resources")
NO_WAY_FOR_OWN_SUBPLAN = "no verb yet lets the user approve the sub-plan of a stage of your own plan"
OUTSIDE_RULE = "Bash(make deploy:*)"


def _standing_grant(rule="Edit", *, by="fedor", scope="stage") -> dict:
    return {"rule": rule, "provenance": "runtime", "consumed": False, "stage_title": "t",
            "scope": scope, "granted_by": by}


def _contour_under(contour, parent: str, *standing: dict):
    """`contour` with its parent stage 1 made one that records no approved set -- `old-form` (no
    description, as in every plan written before approvals were recorded), `below-the-line` or
    `above-the-line` (described, none approved before the child was handed it) -- holding the
    given runtime grants, and the child linked and submitted."""
    state = contour.store.load(PARENT)
    stage = state.stage(1)
    stage.lazy = False
    if parent == "old-form":
        stage.place = None
    elif parent == "below-the-line":
        stage.expected_minutes = 5
    state.runtime_grants["1"] = list(standing)
    contour.store.save(state)
    assert contour.submit()["ok"] is True
    assert contour.entry() == {}
    return contour


def _childs_request(contour, allow) -> None:
    """The child's stage 1 carries a request naming `allow`, and its plan stands at the node
    `approve` acts from (a later `approve` of an approved plan does nothing)."""
    child = contour.child_state()
    child.node = Node.PLAN_READY.value
    child.stage_approvals.pop("0:1", None)
    sub_plan.store(child, _rec(1, allow=allow), level=0)
    contour.store.save(child)


def _childs_start_refusal(contour) -> str:
    child = contour.child_state()
    return " ".join(gates.stage_start_blockers(child, child.stage(1), store=contour.store))


PARENT_STAGES = pytest.mark.parametrize(
    "parent", ["old-form", "below-the-line", "above-the-line"])


@PARENT_STAGES
def test_a_child_of_a_stage_with_no_approved_set_is_bounded_by_the_permissions_in_force_for_it(
        contour, parent):
    """The child runs `approve --by agent` through its channel. Inside what the parent stage's
    permissions already cover (here a standing grant of `Edit`) its stage starts, a request that
    names nothing included -- and this stage declares no size -- while a request outside them
    is left unapproved and the start refusal names the one way the child can take, the report to
    its parent, never the user's verbs it cannot run."""
    _contour_under(contour, parent, _standing_grant("Edit"))
    for allow, starts in ((("Edit",), True), ((), True), (("Edit", "Read"), False)):
        _childs_request(contour, allow)
        code, body, err = contour.own("approve", "--by", "agent")
        assert code == 0 and body.get("ok") is True, (allow, code, body, err)
        written = contour.child_state().stage_approvals.get("0:1", {})
        assert bool(written.get("parent_resource_approval")) is starts, (allow, body)
        assert "approved" not in written
        refusal = _childs_start_refusal(contour)
        assert (refusal == "") is starts, (allow, refusal)
        if not starts:
            assert REPORT_TO_PARENT in refusal
            assert not any(verb in refusal for verb in THE_USERS_VERBS), refusal
    assert contour.entry() == {}


@PARENT_STAGES
def test_a_child_re_approves_an_edited_request_inside_the_permissions_in_force_for_its_stage(
        contour, parent):
    """`approve-stage-resources`, through the child's channel, for a stage whose parent recorded no
    set: an edit that stays inside the permissions in force is re-approved, one that leaves them
    is refused with the report to the parent -- not the user's verbs."""
    _contour_under(contour, parent, _standing_grant("Edit"))
    for allow, passes in ((("Edit",), True), (("Edit", "Bash(rm:*)"), False)):
        _childs_request(contour, allow)
        code, body, err = contour.own("approve-stage-resources", "--stage", "1")
        text = json.dumps(body) + err
        assert (body.get("ok") is True) is passes, (allow, code, text)
        assert bool(contour.child_state().stage_approvals.get("0:1", {})
                    .get("parent_resource_approval")) is passes
        if not passes:
            assert REPORT_TO_PARENT in text and "outside" in text
            assert not any(verb in text for verb in THE_USERS_VERBS), text
        assert (_childs_start_refusal(contour) == "") is passes


def test_a_childs_edited_request_after_its_approval_names_the_report_to_the_parent(contour):
    """The request changes after the child's own approval: the stage no longer starts, and the
    refusal names `approve-stage-resources` for what stays inside the bound and the report to
    the parent for the rest -- not the user's verbs the child cannot run."""
    _contour_under(contour, "old-form", _standing_grant("Edit"))
    _childs_request(contour, ("Edit",))
    assert contour.own("approve", "--by", "agent")[1].get("ok") is True
    assert _childs_start_refusal(contour) == ""
    child = contour.child_state()
    sub_plan.store(child, _rec(1, allow=("Edit", "Read")), level=0)
    contour.store.save(child)
    refusal = _childs_start_refusal(contour)
    assert "approve-stage-resources --stage 1" in refusal and REPORT_TO_PARENT in refusal
    assert not any(verb in refusal for verb in THE_USERS_VERBS), refusal


@pytest.mark.parametrize("standing", [
    _standing_grant("Edit", by="agent"), _standing_grant("Edit", scope="once"), None],
    ids=["granted-by-the-agent", "once-only", "none"])
def test_nothing_the_agent_alone_can_do_widens_what_a_child_may_approve(contour, standing):
    """The permissions in force that bound a child are the user's: a grant the agent made itself,
    a one-launch grant, or none at all leave `Edit` unapproved for the child."""
    _contour_under(contour, "old-form", *([standing] if standing else []))
    _childs_request(contour, ("Edit",))
    assert contour.own("approve", "--by", "agent")[1].get("ok") is True
    assert "parent_resource_approval" not in contour.child_state().stage_approvals.get("0:1", {})
    assert REPORT_TO_PARENT in _childs_start_refusal(contour)


def test_the_grants_a_plan_declared_for_the_parent_stage_bound_the_child_too(contour, monkeypatch):
    """What the user approved the parent's plan to grant for the stage (a rule, an added
    directory) is in force for it: a request inside is covered, one outside is not."""
    declared = [{"rule": "Bash(git status:*)"}, {"path": "/srv/area", "mode": "write"}]
    monkeypatch.setattr(cli_dispatch, "_stage_grant_entries",
                        lambda state, index: (declared, [], [], None, None))
    parent = contour.store.load(PARENT)

    def outside(allow=(), dirs=()):
        return cli_dispatch.request_outside_stage_grants(parent, 1, _rec(1, allow=allow, dirs=dirs)
                                                         .resource_request)

    assert outside(("Bash(git status:*)",), (("/srv/area/sub", "write"),)) == []
    assert outside(("Bash(git push:*)",)) == ["Bash(git push:*)"]
    assert outside(dirs=(("/srv/area/sub", "read"),)) == []
    assert outside(dirs=(("/srv/other", "write"),)) == ["add_dir /srv/other (write)"]
    assert outside(dirs=(("/srv/other", "read"),)) == ["add_dir /srv/other (read)"]


def test_a_spawned_developer_stage_holds_its_workdir_for_writing(contour, monkeypatch):
    """A spawned developer holds its workdir for writing: the parent stage's in-force
    permissions include it, so a child's request for that directory is covered
    (an in-thread stage holds no such grant). The test's workdir lies under the sandbox home, so
    the protected roots are emptied to let the coverage test see the directory itself."""
    monkeypatch.setattr(widening_targets, "protected_roots", lambda: [])
    parent = contour.store.load(PARENT)
    venue = parent.resolve_check_venue(state_mod.CheckVenue.DELIVERY.value)
    rows = _rec(1, allow=(), dirs=((venue, "write"),)).resource_request
    assert cli_dispatch.request_outside_stage_grants(parent, 1, rows) == []
    parent.stage(1).actor.executor = "in_thread"
    assert cli_dispatch.request_outside_stage_grants(parent, 1, rows) == [
        f"add_dir {venue} (write)"]


def test_a_decided_later_stage_of_a_child_is_not_bounded_by_the_parents_permissions(contour):
    """A decided-later stage is approved through the user's sub-plan approval alone: with none on
    record, the agent's approval has no bound to work inside -- the permissions in force for the
    parent stage do not stand in for it."""
    _contour_under(contour, "old-form", _standing_grant("Edit"))
    child = contour.child_state()
    rows = _rec(1, allow=("Edit",)).resource_request
    assert cli_approval._agent_approval_outside(child, contour.store, child.stage(1), rows) == []
    child.stage(1).lazy = True
    assert cli_approval._agent_approval_outside(child, contour.store, child.stage(1), rows) is None


def test_a_child_whose_parent_stage_is_gone_has_no_bound_to_approve_inside(contour):
    """The stage the child was handed is no longer in its parent's plan: nothing is in force for
    it, so even a request the standing grants of another stage would cover is not approved."""
    _contour_under(contour, "old-form", _standing_grant("Edit"))
    child = contour.child_state()
    rows = _rec(1, allow=("Edit",)).resource_request
    assert cli_approval._outside_parent_permissions(child, contour.store, rows) == []
    child.parent_link = {**child.parent_link, "stage_index": 9}
    assert cli_approval._outside_parent_permissions(child, contour.store, rows) \
        == ["the parent session has no stage 9"]


def _the_own_verbs_a_refusal_names(refusal: str, stage: int = 1) -> list[list[str]]:
    """The calls through the child's channel that `refusal` names as a way on."""
    ways = [["approve-stage-resources", "--stage", str(stage)], ["approve", "--by", "agent"]]
    return [argv for argv in ways if " ".join(argv) in refusal]


def test_a_child_at_executing_follows_the_refusal_to_a_start_after_the_users_grant(contour):
    """The whole loop from the node a started child is in. `approve` acts only from PLAN_READY
    (at EXECUTING it does nothing), so the way the refusal names must be one that acts from
    EXECUTING: a request outside the bound is refused with the report to the parent; the user's
    `resolve-permission` puts the grant on the parent stage; the child takes the verb the refusal
    names, from EXECUTING, the approval is written and the stage starts."""
    _contour_under(contour, "old-form")
    _childs_request(contour, (OUTSIDE_RULE,))
    for argv in (["approve", "--by", "agent"], ["partition"], ["next-stage"]):
        code, body, err = contour.own(*argv)
        assert code == 0 and body.get("ok") is not False, (argv, code, body, err)
    assert contour.child_state().node == Node.EXECUTING.value
    refusal = _childs_start_refusal(contour)
    assert REPORT_TO_PARENT in refusal and "not approved" in refusal, refusal
    assert "`PERMISSION-REQUEST:`" in refusal and "COMPLETED" not in refusal, refusal
    assert contour.own("approve-stage-resources", "--stage", "1")[1].get("ok") is not True
    assert "parent_resource_approval" not in contour.child_state().stage_approvals.get("0:1", {})

    parent = contour.store.load(PARENT)
    parent.permission_request = cli.PermissionRequest(
        action=OUTSIDE_RULE, stage_index=1, raw=OUTSIDE_RULE)
    contour.store.save(parent)
    granted = cli.cmd_resolve_permission(
        ns(session=PARENT, decision="granted", scope="stage", by="fedor", rules=[OUTSIDE_RULE],
           add_dirs=None), store=contour.store)
    assert granted.ok is True, granted

    refusal = _childs_start_refusal(contour)
    assert refusal, "the grant alone approves nothing: the child's own approval is still unwritten"
    ways = _the_own_verbs_a_refusal_names(refusal)
    assert ways, refusal
    for argv in ways:
        contour.own(*argv)
    assert contour.child_state().node == Node.EXECUTING.value
    written = contour.child_state().stage_approvals.get("0:1", {})
    assert written.get("parent_resource_approval"), (refusal, ways)
    assert _childs_start_refusal(contour) == ""


def test_a_linked_child_is_never_sent_to_a_verb_of_the_user_or_the_root(contour):
    """`present-plan`, `approve-subplan`, `approve-resources` and `approve --by user` are run in
    the session that presented the plan: a linked child's channel refuses each. A refusal about
    a sub-plan the user must approve names the return the parent's engine routes to the
    sub-plan's presentation (`PLAN-READY:`), never the permission one, whose end is a grant
    (`resolve-permission`); for a stage of the child's own plan, where no verb lets the user
    approve the sub-plan yet, it promises no way on and says so; a request outside the bound
    keeps the permission report."""
    _contour_under(contour, "above-the-line")
    for argv in (["approve", "--by", "agent"], ["partition"], ["next-stage"]):
        assert contour.own(*argv)[0] == 0
    child = contour.child_state()
    child.stage(1).lazy, child.stage(1).actor.executor = True, "spawn:developer"
    sub_plan.store(child, _rec(1), level=0)
    contour.store.save(child)
    start = _childs_start_refusal(contour)
    assert REPORT_TO_PARENT in start and "PERMISSION-REQUEST:" in start, start
    child = contour.child_state()
    record = sub_plan.read(child, 1, contour.store)
    dead_ends = {"start guard": " ".join(
        gates.decided_later_dispatch_blockers(child, child.stage(1), record))}
    code, body, err = contour.own("approve-stage-resources", "--stage", "1")
    dead_ends["approve-stage-resources"] = json.dumps(body) + err
    planned = _dispatch(contour.store, CHILD, Spawns(), subplan_only=True)
    assert planned.action == "await_subplan_approval", planned
    dead_ends["planning dispatch"] = planned.detail
    child = contour.child_state()
    dead_ends["record-result"] = " ".join(
        gates.recorded_pass_blockers(child, child.stage(1), store=contour.store))
    parent = contour.store.load(PARENT)
    parent.stage(1).lazy = True
    way = " ".join(gates.child_approval_blockers(child, parent))
    for name, text in dead_ends.items():
        assert NO_WAY_FOR_OWN_SUBPLAN in text and "`ESCALATE:`" in text, (name, text)
        assert not any(wrong in text for wrong in (
            *THE_USERS_VERBS, "approve-subplan", "present-plan", "PERMISSION-REQUEST:",
            "PLAN-READY:", "then dispatch the stage")), (name, text)
    assert "`PLAN-READY:`" in way and "PERMISSION-REQUEST" not in way, way
    assert not any(wrong in way for wrong in (*THE_USERS_VERBS, "approve-subplan", "present-plan")), way
    assert "approve-subplan" in sub_plan.subplan_approval_problem({}, None)


@pytest.mark.parametrize("allow", [(), ("Edit",)], ids=["empty-request", "request-in-bound"])
def test_a_child_whose_parent_session_cannot_be_read_gets_nothing_approved(contour, allow):
    """The parent is the only source of what is in force for the child's stage: with its session
    gone the child has no bound to check against, so neither verb of its channel approves, not
    even a request that names nothing."""
    _contour_under(contour, "old-form", _standing_grant("Edit"))
    _childs_request(contour, allow)
    child = contour.child_state()
    child.parent_link = {**child.parent_link, "parent_session": "no-such-parent"}
    contour.store.save(child)
    for argv in (["approve", "--by", "agent"], ["approve-stage-resources", "--stage", "1"]):
        code, body, err = contour.own(*argv)
        assert body.get("ok") is not True, (argv, code, body, err)
        assert contour.child_state().stage_approvals.get("0:1", {}).get(
            "parent_resource_approval") in (None, ""), argv
    assert _childs_start_refusal(contour) != ""


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
    assert _agent_approves_stage_resources(store).ok
    started = Spawns()
    assert _dispatch(store, spawns=started).ok is not False and len(started.argvs) == 1
    assert _entry(store)["planning"] is False
    _put(store, _rec(1, method="split it differently"))
    again = Spawns()
    _not_started(_dispatch(store, spawns=again), again, "changed since the user approved it")


# --- the delivery stamp of a presented sub-plan ----------------------------------------------

def _ask_after_the_rendering(store, *, marker=True, landed=True, digest=None, sid=SESSION):
    """Run the delivery hook on an `AskUserQuestion` over a transcript in which the presented
    rendering reached the user (`landed`) as a completed turn's final text."""
    from test_plan_delivery_gate_presentation import (
        run_hook, text_only_entry, user_prompt_entry, write_transcript)
    home = store.root.parent.parent
    receipt = store.load(sid).plan_presentations[-1]
    entries = [user_prompt_entry(receipt.presented_ts - 5)]
    if landed:
        entries.append(text_only_entry(receipt.presented_ts + 5, receipt.rendering_text))
    entries.append(user_prompt_entry(receipt.presented_ts + 10))
    transcript = write_transcript(home / "t.jsonl", entries)
    anchor = state_mod.subplan_marker(1, digest or receipt.understanding_sha256)
    label = f"Approve this sub-plan {anchor}" if marker else "Approve this sub-plan"
    payload = {"tool_name": "AskUserQuestion", "session_id": sid, "transcript_path": str(transcript),
               "tool_input": {"questions": [{"question": "Approve the sub-plan of stage 1?",
                                             "options": [{"label": label}, {"label": "No"}]}]}}
    return run_hook(payload, home)


@pytest.mark.parametrize("ask, stamped", [
    ({}, True),
    ({"marker": False}, False),
    ({"landed": False}, False),
    ({"digest": "0" * 64}, False),
], ids=["marked-and-delivered", "no-anchor-in-the-ask", "rendering-never-landed",
        "anchor-of-another-sub-plan"])
def test_the_hook_stamps_a_presented_subplan_only_on_its_anchored_ask_after_the_rendering_landed(
        home_store, venued_fixtures_dir, tmp_path, ask, stamped):
    """The sub-plan's delivery stamp is written by the delivery hook as the plan's is: when the
    rendering landed as a completed turn's final text and the ask carries the anchor of this very
    sub-plan. `approve-subplan` then takes the user's approval, and only then."""
    from test_plan_delivery_gate_presentation import _stamp
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    _refused(_present_and_approve(store, tmp_path, confirm=False), "has no delivery stamp")

    proc = _ask_after_the_rendering(store, **ask)
    assert proc.returncode == 0, proc.stderr
    assert (_stamp(store.root.parent.parent, SESSION) is not None) is stamped

    approved = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    if stamped:
        assert approved.ok is True and _entry(store)["user_subplan_approval"]
    else:
        _refused(approved, "has no delivery stamp")
        assert not _entry(store).get("user_subplan_approval")


def test_a_stamp_given_for_one_subplan_is_refused_once_the_subplan_is_edited(
        home_store, venued_fixtures_dir, tmp_path):
    store = home_store
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, _rec(1))
    _present_and_approve(store, tmp_path, confirm=False)
    assert _ask_after_the_rendering(store).returncode == 0
    _put(store, _rec(1, method="split it differently"))
    refused = cli_approval.cmd_approve_subplan(ns(session=SESSION, stage=1, by="fedor"), store=store)
    _refused(refused, "changed since it was presented")
