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

from agentctl import cli, effort
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


def test_subplan_custody_preserves_nondefault_effort_fields(store):
    """The five effort-custody seams (effort.py's SUB-PLAN CUSTODY) round-trip a
    NON-DEFAULT parent — unlike test_roundtrip_with_plan_stack's all-default parent,
    a dropped seam here (e.g. a forgotten reset-list or restore-list line) fails an
    equality assertion instead of silently passing because everything was already
    None/{}/[] on both sides."""
    parent = _executing_state(store, "custody")
    effort.arm(parent)
    parent.effort_actuals[effort.ACTUAL_SPEND_KEY] = 12.5
    parent.effort_actuals[effort.ACTUAL_MINUTES_KEY] = 30.0
    parent.effort_spend_seen["/tmp/parent.toml"] = 12.5
    parent.effort_fires = [{
        "scale": "spend", "kind": "ratio", "actual": 12.5, "estimate": 3.0,
        "multiple": 4.17, "history_len": 0, "ts": 1.0,
    }]
    pre_estimate = dict(parent.effort_estimate)
    pre_baseline = dict(parent.effort_baseline)
    pre_actuals = dict(parent.effort_actuals)
    pre_fires = list(parent.effort_fires)
    pre_spend_seen = dict(parent.effort_spend_seen)
    store.save(parent)

    cli.cmd_push_subplan(
        ns(session="custody", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    state = store.load("custody")
    frame = state.plan_stack[0]
    # seams (b)/(e) — PlanFrame dataclass + cmd_push_subplan's construction: the frame
    # snapshotted the parent's non-default values, not defaults.
    assert frame.effort_estimate == pre_estimate
    assert frame.effort_baseline == pre_baseline
    assert frame.effort_actuals == pre_actuals
    assert frame.effort_fires == pre_fires
    assert frame.effort_spend_seen == pre_spend_seen
    # seam (a) — cmd_push_subplan's reset list: the child starts a fresh, unarmed window.
    assert state.effort_estimate is None
    assert state.effort_baseline is None
    assert state.effort_actuals == {}
    assert state.effort_fires == []
    assert state.effort_spend_seen == {}

    # The child accumulates its own effort before resolving.
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 4.0
    state.effort_actuals[effort.ACTUAL_MINUTES_KEY] = 6.0
    store.save(state)
    _resolve_child(store, "custody")

    d = cli.cmd_pop_subplan(ns(session="custody"), store=store)
    assert d.ok is True
    restored = store.load("custody")
    # seam (c)/(d) — SessionState.from_dict's rebuild + cmd_pop_subplan's restore list:
    # the parent's own custody fields come back verbatim...
    assert restored.effort_estimate == pre_estimate
    assert restored.effort_baseline == pre_baseline
    assert restored.effort_fires == pre_fires
    assert restored.effort_spend_seen == pre_spend_seen
    # ...except effort_actuals, which ADDS the child's consumption onto the parent's
    # (effort.merge_actuals) rather than overwriting it.
    assert restored.effort_actuals == {
        effort.ACTUAL_SPEND_KEY: pytest.approx(12.5 + 4.0),
        effort.ACTUAL_MINUTES_KEY: pytest.approx(30.0 + 6.0),
    }


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
        effort_estimate=None,
        effort_baseline=None,
        effort_actuals={},
        effort_fires=[],
        effort_spend_seen={},
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


# --- pop re-derives the parent venue from the parent plan file ----------------

def _parent_plan(fixtures_dir, tmp_path, *, repo_root, delivery_worktree):
    inject = f'repo_root = "{repo_root}"\ndelivery_worktree = "{delivery_worktree}"\n'
    path = tmp_path / "parent.toml"
    path.write_text(
        (fixtures_dir / "plan_two_stage.toml").read_text().replace("[meta]\n", "[meta]\n" + inject, 1)
    )
    return path


def test_pop_subplan_rederives_venue_from_plan(store, fixtures_dir, tmp_path):
    """The parent plan FILE is authoritative for the parent's venue, so pop must
    re-derive it rather than restore whatever the frame happened to snapshot: a
    frame captured after the value was already lost keeps it lost forever, which
    is how the parent stays dispatching into the canonical checkout. The frame
    values remain the fallback for a parent plan that cannot be read."""
    plan = _parent_plan(fixtures_dir, tmp_path,
                        repo_root="/tmp/canon", delivery_worktree="/tmp/canon-wt")
    state = _executing_state(store, "pv1")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/canon"
    state.delivery_worktree = None  # the value the defect loses before the push
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="pv1", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    _resolve_child(store, "pv1")
    d = cli.cmd_pop_subplan(ns(session="pv1"), store=store)

    assert d.ok is True
    state = store.load("pv1")
    assert state.delivery_worktree == "/tmp/canon-wt"
    assert state.repo_root == "/tmp/canon"

    # Fail-safe half of the same contract: an absent or unparseable parent plan
    # leaves the frame's snapshotted venue in place and never raises out of pop.
    state = _executing_state(store, "pv2")
    state.plan_path = str(tmp_path / "never-written.toml")
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="pv2", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    _resolve_child(store, "pv2")
    d = cli.cmd_pop_subplan(ns(session="pv2"), store=store)

    assert d.ok is True
    state = store.load("pv2")
    assert state.repo_root == "/tmp/repo"
    assert state.delivery_worktree == "/tmp/repo/.wt"


# --- pop restores the active-stage pointer from stage status, not the frame ----

def test_pop_subplan_restores_pointer_to_active_stage(store):
    """Reproduces the live wedge: a stage is left ACTIVE by a prior push while
    current_stage has already gone missing (the exact shape a previous buggy pop
    leaves behind), and the sub-plan being popped now satisfies a DIFFERENT
    stage. Restoring frame.current_stage verbatim would restore None and leave
    the ACTIVE stage unreachable by every ordinary verb; the pointer must
    instead be derived from which stage is still ACTIVE."""
    state = _executing_state(store, "ptr1", n_stages=2, current=2)  # stage 2 ACTIVE
    state.current_stage = None  # pointer already lost, as the defect leaves it
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="ptr1", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    frame = store.load("ptr1").plan_stack[0]
    assert frame.current_stage is None  # confirms the reproduced shape

    _resolve_child(store, "ptr1")
    cli.cmd_pop_subplan(ns(session="ptr1"), store=store)

    state = store.load("ptr1")
    # Originating stage 1 satisfied by the sub-plan.
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    # Stage 2 was left ACTIVE by the push and must remain re-enterable.
    assert state.stage(2).outcome.status == StageStatus.ACTIVE.value
    assert state.current_stage == 2
    assert state.active_stage() is state.stage(2)


def _write_parent_plan(fixtures_dir, tmp_path, name, *, repo_root=None, delivery_worktree=None):
    """A plan_two_stage.toml derivative with a controllable [meta] venue pair --
    repo_root/delivery_worktree are injected only when given, so a caller can build
    the majority plan shape (repo_root declared, delivery_worktree absent) as well
    as the fully-declared one."""
    inject = ""
    if repo_root is not None:
        inject += f'repo_root = "{repo_root}"\n'
    if delivery_worktree is not None:
        inject += f'delivery_worktree = "{delivery_worktree}"\n'
    path = tmp_path / name
    text = (fixtures_dir / "plan_two_stage.toml").read_text()
    if inject:
        text = text.replace("[meta]\n", "[meta]\n" + inject, 1)
    path.write_text(text)
    return path


# --- pop-subplan venue-substitution guard (stage 7) -----------------------------
# _sync_venue_from_plan's unconditional re-derive trusts the parent plan FILE even
# when it moved out from under a pushed child -- the one other post-approval route
# from an edited plan file to live state. These tests exercise the guard: it
# compares the exact (repo_root, delivery_worktree) pair captured at push against a
# fresh read at pop, and keeps the frame's restored venue only when that pair moved.

def test_pop_keeps_frame_venue_when_parent_repo_root_moved_while_pushed(store, fixtures_dir, tmp_path):
    """(a) Editing the parent plan's repo_root between push and pop must not be
    silently adopted at pop -- the frame's already-restored venue is kept, and the
    Directive reports why."""
    plan = _write_parent_plan(fixtures_dir, tmp_path, "parent.toml",
                               repo_root="/tmp/orig", delivery_worktree="/tmp/orig-wt")
    state = _executing_state(store, "guard-a")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/orig"
    state.delivery_worktree = "/tmp/orig-wt"
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="guard-a", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    frame = store.load("guard-a").plan_stack[0]
    assert frame.parent_venue_captured is True
    assert (frame.parent_repo_root, frame.parent_delivery_worktree) == ("/tmp/orig", "/tmp/orig-wt")

    # The parent plan FILE's venue moves out from under the pushed child.
    plan.write_text(plan.read_text().replace('repo_root = "/tmp/orig"', 'repo_root = "/tmp/moved"'))

    _resolve_child(store, "guard-a")
    d = cli.cmd_pop_subplan(ns(session="guard-a"), store=store)
    assert d.ok is True
    assert d.data["venue_source"] == "frame (parent plan venue changed while pushed)"
    assert "frame" in d.detail

    state = store.load("guard-a")
    # The frame's ORIGINAL venue is kept -- neither the file's moved value nor a
    # refusal; pop never refuses.
    assert state.repo_root == "/tmp/orig"
    assert state.delivery_worktree == "/tmp/orig-wt"


def test_pop_rederives_from_plan_file_when_parent_untouched(store, fixtures_dir, tmp_path):
    """(b) An untouched parent still re-derives from the file exactly as before this
    guard existed -- the guard only changes behaviour on an actual venue move."""
    plan = _write_parent_plan(fixtures_dir, tmp_path, "parent.toml",
                               repo_root="/tmp/canon", delivery_worktree="/tmp/canon-wt")
    state = _executing_state(store, "guard-b")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/canon"
    state.delivery_worktree = "/tmp/canon-wt"
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="guard-b", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    _resolve_child(store, "guard-b")
    d = cli.cmd_pop_subplan(ns(session="guard-b"), store=store)
    assert d.ok is True
    assert d.data["venue_source"] == "plan-file"

    state = store.load("guard-b")
    assert state.repo_root == "/tmp/canon"
    assert state.delivery_worktree == "/tmp/canon-wt"


def test_pop_rederives_despite_parent_edit_outside_venue_fields(store, fixtures_dir, tmp_path):
    """(c) The regression a whole-file hash would have caused: a parent edited only
    OUTSIDE the two venue fields (an `Actual effort:` stamp on an already-passed
    stage -- the canonical in-thread refinement CLAUDE.md sanctions) must not
    suppress the repair of a frame whose delivery_worktree was already lost before
    push. A coarse whole-file-hash guard would treat this edit as a venue move and
    keep the stale frame value forever; the two-field comparison must not."""
    plan = _write_parent_plan(fixtures_dir, tmp_path, "parent.toml", repo_root="/tmp/canon")
    # delivery_worktree is NOT declared in the parent file at all.
    state = _executing_state(store, "guard-c")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/canon"
    state.delivery_worktree = "/tmp/lost"  # stale value the defect leaves before push
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="guard-c", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    frame = store.load("guard-c").plan_stack[0]
    assert frame.parent_venue_captured is True
    assert frame.parent_delivery_worktree == ""

    # Edit the parent plan OUTSIDE the venue fields -- an Actual-effort stamp on a
    # completed stage, as a comment (no schema key changes; parses unconditionally).
    text = plan.read_text().replace(
        'title = "Scaffold module"',
        'title = "Scaffold module"\n# Actual effort: as estimated',
    )
    plan.write_text(text)

    _resolve_child(store, "guard-c")
    d = cli.cmd_pop_subplan(ns(session="guard-c"), store=store)
    assert d.ok is True
    assert d.data["venue_source"] == "plan-file"

    state = store.load("guard-c")
    assert state.repo_root == "/tmp/canon"
    # Repaired: matches the file's undeclared value, not the stale frame value.
    assert state.delivery_worktree is None


def test_pop_legacy_frame_without_venue_capture_uses_rederive_path(store, fixtures_dir, tmp_path):
    """(d) A PlanFrame persisted before schema 34 carries none of the three new
    keys; SessionState.from_dict must default parent_venue_captured to False so pop
    takes the unconditional re-derive path exactly as it always has, rather than
    raising or misreading the absent capture as 'nothing changed'."""
    plan = _write_parent_plan(fixtures_dir, tmp_path, "parent.toml",
                               repo_root="/tmp/canon", delivery_worktree="/tmp/canon-wt")
    state = _executing_state(store, "guard-d")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/canon"
    state.delivery_worktree = "/tmp/canon-wt"
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="guard-d", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )

    # Simulate a pre-schema-34 persisted frame: strip the three new keys.
    import json
    raw = json.loads(store.load("guard-d").to_json())
    frame_raw = raw["plan_stack"][0]
    del frame_raw["parent_repo_root"]
    del frame_raw["parent_delivery_worktree"]
    del frame_raw["parent_venue_captured"]
    store.save(SessionState.from_dict(raw))

    loaded_frame = store.load("guard-d").plan_stack[0]
    assert loaded_frame.parent_venue_captured is False

    _resolve_child(store, "guard-d")
    d = cli.cmd_pop_subplan(ns(session="guard-d"), store=store)
    assert d.ok is True
    assert d.data["venue_source"] == "plan-file"

    state = store.load("guard-d")
    assert state.repo_root == "/tmp/canon"
    assert state.delivery_worktree == "/tmp/canon-wt"


def test_pop_catches_delivery_worktree_added_to_bare_parent(store, fixtures_dir, tmp_path):
    """(e) THE EMPTY-FIELD BLIND SPOT. The majority plan shape declares repo_root and
    no delivery_worktree at all -- captured pair ('/tmp/a', ''). A delivery_worktree
    later ADDED to that parent while the child is pushed must be caught like any
    other move: an empty captured value is an OBSERVATION (nothing declared), not a
    hole a non-empty precondition on either side of the comparison would step over."""
    plan = _write_parent_plan(fixtures_dir, tmp_path, "parent.toml", repo_root="/tmp/a")
    state = _executing_state(store, "guard-e")
    state.plan_path = str(plan)
    state.repo_root = "/tmp/a"
    state.delivery_worktree = None
    store.save(state)

    cli.cmd_push_subplan(
        ns(session="guard-e", plan="/tmp/child.toml", task="child", originating_stage=1),
        store=store,
    )
    frame = store.load("guard-e").plan_stack[0]
    assert frame.parent_venue_captured is True
    assert (frame.parent_repo_root, frame.parent_delivery_worktree) == ("/tmp/a", "")

    # A delivery_worktree pointing somewhere else is ADDED to the parent.
    plan.write_text(plan.read_text().replace(
        'repo_root = "/tmp/a"', 'repo_root = "/tmp/a"\ndelivery_worktree = "/tmp/added-wt"'
    ))

    _resolve_child(store, "guard-e")
    d = cli.cmd_pop_subplan(ns(session="guard-e"), store=store)
    assert d.ok is True
    assert d.data["venue_source"] == "frame (parent plan venue changed while pushed)"

    state = store.load("guard-e")
    assert state.repo_root == "/tmp/a"
    # NOT adopted -- the frame's original (empty) delivery_worktree is kept.
    assert state.delivery_worktree is None


def test_pop_subplan_clears_pointer_when_originating_stage_was_active(store):
    """The other direction: when the ACTIVE stage IS the one the sub-plan
    satisfies, the pop marks it PASSED and no stage is left ACTIVE — the
    derivation must run AFTER the PASSED marking, or it would re-point at an
    already-satisfied stage instead of correctly leaving no pointer."""
    _executing_state(store, "ptr2", n_stages=2, current=1)  # stage 1 ACTIVE

    cli.cmd_push_subplan(
        ns(session="ptr2", plan="/tmp/child.toml", task="child-task", originating_stage=1),
        store=store,
    )
    _resolve_child(store, "ptr2")
    cli.cmd_pop_subplan(ns(session="ptr2"), store=store)

    state = store.load("ptr2")
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(2).outcome.status == StageStatus.PENDING.value
    assert state.current_stage is None
    assert state.active_stage() is None
