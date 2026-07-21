"""Sub-plan frame stack: push-subplan / pop-subplan lifecycle tests.

Covers:
  - full push -> (child resolved) -> pop cycle with parent restoration
  - originating stage marked PASSED on pop with the sub-plan control note
  - round-trip: from_json(to_json(s)) == s with a non-empty plan_stack
  - _MAX_PLAN_STACK enforcement (push beyond cap raises InvariantError)
  - "no auto-pop across unresolved child": pop requires node=RESOLVED
  - push_subplan / pop_subplan machine transitions wired correctly
"""
import argparse

import pytest

from agentctl import cli
from agentctl.machine import transition
from agentctl.state import (
    _MAX_PLAN_STACK,
    Actor,
    Criterion,
    FinalCheck,
    GateRecord,
    InvariantError,
    Means,
    Node,
    Outcome,
    Partition,
    PlanFrame,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return argparse.Namespace(**kw)


def _stage(i, status=StageStatus.PENDING.value):
    return Stage(
        index=i,
        title=f"stage {i}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do it"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
        outcome=Outcome(status=status),
    )


def _executing_state(store, sid, n_stages=2, current=1):
    """Build a parent substantive state at EXECUTING with n_stages and current_stage=current."""
    state = SessionState(
        session_id=sid,
        task_id="parent-task",
        goal="parent goal",
        overall_done_criterion="parent done",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="tester"),
        partition=Partition(verdict="single"),
        repo_root="/tmp/repo",
        delivery_worktree="/tmp/repo/.wt",
        final_check=[FinalCheck(command="echo ok", expected_exit=0, label="smoke")],
        stages=[_stage(i) for i in range(1, n_stages + 1)],
        current_stage=current,
    )
    state.stages[current - 1].outcome.status = StageStatus.ACTIVE.value
    store.save(state)
    return state


def _resolve_child(store, sid):
    """Fast-path: directly set the live state to RESOLVED (all stages passed)."""
    state = store.load(sid)
    # Give the child one synthetic passed stage so all_stages_passed() is True.
    child_stage = Stage(
        index=1,
        title="child stage",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="done"),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )
    state.stages = [child_stage]
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.node = Node.RESOLVED.value
    state.current_stage = None
    store.save(state)


# --- full push -> child-resolved -> pop cycle ----------------------------------

def test_push_subplan_transitions_to_classified(store):
    _executing_state(store, "s1")
    d = cli.cmd_push_subplan(
        ns(session="s1", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    assert d.ok is True
    assert d.node == Node.CLASSIFIED.value
    assert d.action == "classify"
    assert d.data["stack_depth"] == 1
    assert d.data["originating_stage"] == 1

    state = store.load("s1")
    assert state.node == Node.CLASSIFIED.value
    assert len(state.plan_stack) == 1
    assert state.task_id == "child-task"
    assert state.plan_path == "/tmp/child.toml"
    assert state.stages == []
    assert state.current_stage is None
    assert state.approval.passed is False
    assert state.resolution.passed is False
    assert state.weight_class is None
    assert state.route is None
    assert state.partition is None
    assert state.delivery_worktree is None  # reset on push, like repo_root


def test_push_preserves_parent_in_frame(store):
    parent = _executing_state(store, "s2")
    cli.cmd_push_subplan(
        ns(session="s2", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    state = store.load("s2")
    frame = state.plan_stack[0]
    assert frame.task_id == "parent-task"
    assert frame.node == Node.EXECUTING.value
    assert frame.goal == "parent goal"
    assert frame.overall_done_criterion == "parent done"
    assert frame.weight_class == WeightClass.SUBSTANTIVE.value
    assert frame.route == Route.SPAWN.value
    assert frame.repo_root == "/tmp/repo"
    assert frame.delivery_worktree == "/tmp/repo/.wt"
    assert len(frame.final_check) == 1
    assert frame.final_check[0].command == "echo ok"
    assert frame.partition is not None
    assert frame.approval.passed is True
    assert len(frame.stages) == 2
    assert frame.current_stage == 1
    assert frame.originating_stage == 1


def test_pop_subplan_restores_parent(store):
    _executing_state(store, "s3")
    cli.cmd_push_subplan(
        ns(session="s3", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    _resolve_child(store, "s3")

    d = cli.cmd_pop_subplan(ns(session="s3"), store=store)
    assert d.ok is True
    assert d.node == Node.EXECUTING.value
    assert d.action == "next_stage"
    assert d.data["originating_stage"] == 1
    assert d.data["child_task_id"] == "child-task"
    assert d.data["stack_depth"] == 0

    state = store.load("s3")
    # Parent fields restored.
    assert state.node == Node.EXECUTING.value
    assert state.task_id == "parent-task"
    assert state.goal == "parent goal"
    assert state.overall_done_criterion == "parent done"
    assert state.weight_class == WeightClass.SUBSTANTIVE.value
    assert state.route == Route.SPAWN.value
    assert state.repo_root == "/tmp/repo"
    assert state.delivery_worktree == "/tmp/repo/.wt"
    assert len(state.final_check) == 1
    assert state.final_check[0].command == "echo ok"
    assert state.partition is not None
    assert state.approval.passed is True
    assert len(state.stages) == 2
    assert len(state.plan_stack) == 0
    # Originating stage is PASSED with the sub-plan control note.
    orig = state.stage(1)
    assert orig.outcome.status == StageStatus.PASSED.value
    assert "child-task" in (orig.control or "")
    assert "sub-plan" in (orig.control or "")
    # No active stage pointer — caller runs next-stage.
    assert state.current_stage is None


# --- round-trip with non-empty plan_stack --------------------------------------

def test_roundtrip_with_plan_stack(store):
    _executing_state(store, "rt")
    cli.cmd_push_subplan(
        ns(session="rt", plan="/tmp/child.toml", task="child-task", originating_stage=2),
        store=store,
    )
    state = store.load("rt")
    assert len(state.plan_stack) == 1
    back = SessionState.from_json(state.to_json())
    assert back == state
    frame = back.plan_stack[0]
    assert frame.task_id == "parent-task"
    assert frame.originating_stage == 2
    assert frame.partition is not None
    assert frame.approval.passed is True
    assert len(frame.stages) == 2
    assert len(frame.final_check) == 1


# --- _MAX_PLAN_STACK enforcement -----------------------------------------------

def _make_frame(i):
    return PlanFrame(
        plan_path=f"/tmp/plan{i}.toml",
        node=Node.EXECUTING.value,
        task_id=f"task{i}",
        goal="g",
        overall_done_criterion="dc",
        overall_criterion_type="measurable",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        repo_root=None,
        delivery_worktree=None,
        final_check=[],
        partition=Partition(verdict="single"),
        approval=GateRecord("plan_approval", armed=True, passed=True),
        resolution=GateRecord("resolution"),
        stages=[_stage(1)],
        current_stage=1,
        originating_stage=1,
    )


def test_max_plan_stack_invariant_at_construction():
    """_MAX_PLAN_STACK frames is the maximum; one more raises InvariantError."""
    frames = [_make_frame(i) for i in range(_MAX_PLAN_STACK)]
    # Exactly at max: construction must succeed.
    state = SessionState(
        session_id="cap",
        task_id="t",
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(verdict="single"),
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        plan_stack=frames,
    )
    assert len(state.plan_stack) == _MAX_PLAN_STACK

    # One over: must raise.
    with pytest.raises(InvariantError, match="_MAX_PLAN_STACK"):
        SessionState(
            session_id="cap",
            task_id="t",
            node=Node.EXECUTING.value,
            approval=GateRecord("plan_approval", armed=True, passed=True),
            partition=Partition(verdict="single"),
            weight_class=WeightClass.SUBSTANTIVE.value,
            route=Route.SPAWN.value,
            plan_stack=frames + [_make_frame(_MAX_PLAN_STACK)],
        )


def test_push_beyond_max_raises_invariant_error(store):
    """Pushing to a session already at _MAX_PLAN_STACK raises InvariantError from store.save."""
    frames = [_make_frame(i) for i in range(_MAX_PLAN_STACK)]
    state = SessionState(
        session_id="cap2",
        task_id="parent-task",
        goal="g",
        overall_done_criterion="dc",
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(verdict="single"),
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        stages=[_stage(1, StageStatus.ACTIVE.value)],
        current_stage=1,
        plan_stack=frames,
    )
    store.save(state)  # must pass: exactly at max

    with pytest.raises(InvariantError, match="_MAX_PLAN_STACK"):
        cli.cmd_push_subplan(
            ns(session="cap2", plan="/tmp/overflow.toml", task="overflow", originating_stage=1),
            store=store,
        )


# --- no auto-pop across unresolved child ---------------------------------------

def test_pop_requires_resolved_node(store):
    _executing_state(store, "unresolved")
    cli.cmd_push_subplan(
        ns(session="unresolved", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    # Child is at CLASSIFIED — not RESOLVED.
    d = cli.cmd_pop_subplan(ns(session="unresolved"), store=store)
    assert d.ok is False
    assert "RESOLVED" in d.detail


def test_pop_empty_stack_is_noop(store):
    _executing_state(store, "empty")
    d = cli.cmd_pop_subplan(ns(session="empty"), store=store)
    assert d.ok is False
    assert "empty" in d.detail


# --- push requires EXECUTING node ----------------------------------------------

def test_push_requires_executing_node(store):
    state = SessionState(session_id="notex", task_id="t")
    store.save(state)
    d = cli.cmd_push_subplan(
        ns(session="notex", plan="/tmp/p.toml", task="x", originating_stage=1),
        store=store,
    )
    assert d.ok is False
    assert "EXECUTING" in d.detail


# --- machine transitions -------------------------------------------------------

def test_push_subplan_transition():
    assert transition(Node.EXECUTING.value, "push_subplan") == Node.CLASSIFIED.value


def test_pop_subplan_transition():
    assert transition(Node.RESOLVED.value, "pop_subplan") == Node.EXECUTING.value


# --- legacy state without plan_stack loads with [] ----------------------------

def test_legacy_state_no_plan_stack_loads_with_default():
    import json
    s = SessionState(session_id="legacy", task_id="t")
    raw = json.loads(s.to_json())
    del raw["plan_stack"]
    loaded = SessionState.from_dict(raw)
    assert loaded.plan_stack == []


# --- originating_stage defaults to current_stage ------------------------------

def test_push_defaults_originating_stage_to_current_stage(store):
    _executing_state(store, "default-orig", current=2)
    cli.cmd_push_subplan(
        ns(session="default-orig", plan="/tmp/c.toml", task="child", originating_stage=None),
        store=store,
    )
    state = store.load("default-orig")
    assert state.plan_stack[0].originating_stage == 2
