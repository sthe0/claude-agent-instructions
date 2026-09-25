"""PERMISSION-REQUEST gate: an already-granted action re-spawns without an ask; an
ungranted one parks the request in state and routes to ask_user_permission;
resolve-permission clears the parked request and hands back the continuation."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, permissions
from agentctl.dispatch import RunResult
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="perm-demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _dispatch(store, sid, stdout, perm_checker):
    runner = lambda argv: RunResult(0, stdout=stdout)
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=runner,
                            perm_checker=perm_checker)


# --- check_permission seam --------------------------------------------------

def test_check_permission_granted_on_exit_zero():
    granted = lambda argv: RunResult(0)
    assert permissions.check_permission("push to release", runner=granted) is True


def test_check_permission_denied_on_nonzero_exit():
    denied = lambda argv: RunResult(1)
    assert permissions.check_permission("push to release", runner=denied) is False


# --- dispatch routing -------------------------------------------------------

def test_global_checker_grant_never_skips_the_ask(store, fixtures_dir):
    """A permissions-cli global grant is checked (its own audit row is still
    written) but its answer no longer auto-continues the spawn: that grant never
    reaches the child's --settings/--add-dir, so the old skip just re-spawned into
    the identical denial. Absent any materialized (declared/derived/runtime) grant
    covering the action, dispatch still parks and asks."""
    _to_executing(store, "p1", fixtures_dir)
    d = _dispatch(store, "p1", "PERMISSION-REQUEST: deploy to staging\n",
                  perm_checker=lambda action: True)
    assert d.ok is True
    assert d.action == "ask_user_permission"
    assert d.marker == "PERMISSION-REQUEST"
    assert d.data["action"] == "deploy to staging"
    parked = store.load("p1").permission_request
    assert parked is not None
    assert parked.action == "deploy to staging"
    assert d.node == Node.EXECUTING.value
    misses = store.load("p1").planning_misses
    assert len(misses) == 1
    assert misses[0]["asked_user"] is True
    assert misses[0]["source"] == "permission-request"
    assert misses[0]["stage_index"] == 1


def test_self_reported_rule_line_covered_by_runtime_grant_is_materialization_defect(
        store, fixtures_dir):
    """A PERMISSION-REQUEST whose body carries a `Rule:` line already covered by the
    stage's effective grant set (here: a runtime grant) is a materialization
    defect — the grant existed, the child's own settings just didn't carry it — so
    the stage is recorded FAILED and the session routed straight to DIAGNOSING, no
    re-ask, no re-dispatch."""
    _to_executing(store, "p6", fixtures_dir)
    state = store.load("p6")
    state.runtime_grants["1"] = [
        {"rule": "Bash(git push origin release:*)", "provenance": "runtime",
         "consumed": False, "stage_title": state.stage(1).title},
    ]
    store.save(state)
    d = _dispatch(
        store, "p6",
        "PERMISSION-REQUEST: push to release branch\nRule: Bash(git push origin release:*)\n",
        perm_checker=lambda action: False,
    )
    assert d.ok is False
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"
    after = store.load("p6")
    assert after.node == Node.DIAGNOSING.value
    assert after.permission_request is None
    assert after.stage(1).outcome.status == "FAILED"
    assert len(after.materialization_defects) == 1
    assert after.materialization_defects[0]["evidence"] == "self-reported"
    assert after.materialization_defects[0]["stage_index"] == 1
    assert after.difficulty is not None
    assert after.difficulty.declaration is not None
    assert not after.planning_misses  # covered -> never counted as a miss


def test_ungranted_parks_and_asks(store, fixtures_dir):
    _to_executing(store, "p2", fixtures_dir)
    d = _dispatch(store, "p2", "PERMISSION-REQUEST: push to release branch\n",
                  perm_checker=lambda action: False)
    assert d.ok is True
    assert d.action == "ask_user_permission"
    assert d.marker == "PERMISSION-REQUEST"
    assert d.data["options"] == ["once", "project", "global", "deny"]
    parked = store.load("p2").permission_request
    assert parked is not None
    assert parked.action == "push to release branch"
    assert parked.stage_index == 1
    assert d.node == Node.EXECUTING.value  # node unchanged — still executing


def test_resolve_permission_granted_clears_and_continues(store, fixtures_dir):
    _to_executing(store, "p3", fixtures_dir)
    _dispatch(store, "p3", "PERMISSION-REQUEST: push to release branch\n",
              perm_checker=lambda action: False)
    d = cli.cmd_resolve_permission(
        ns(session="p3", decision="granted", scope="global"), store=store)
    assert d.ok is True
    assert d.action == "continue_spawn"
    assert d.data["decision"] == "granted"
    assert "GRANTED" in d.data["continuation"]
    assert "global grant" in d.data["continuation"]
    assert store.load("p3").permission_request is None


def test_resolve_permission_denied_clears_and_continues(store, fixtures_dir):
    _to_executing(store, "p4", fixtures_dir)
    _dispatch(store, "p4", "PERMISSION-REQUEST: drop the table\n",
              perm_checker=lambda action: False)
    d = cli.cmd_resolve_permission(
        ns(session="p4", decision="denied", scope="once"), store=store)
    assert d.ok is True
    assert d.action == "continue_spawn"
    assert d.data["decision"] == "denied"
    assert "DENIED" in d.data["continuation"]
    assert store.load("p4").permission_request is None


def test_resolve_permission_without_pending_is_noop(store, fixtures_dir):
    _to_executing(store, "p5", fixtures_dir)
    d = cli.cmd_resolve_permission(
        ns(session="p5", decision="granted", scope="once"), store=store)
    assert d.ok is False
    assert d.action == "noop"
