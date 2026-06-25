import pytest

from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    InvariantError,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    Supply,
    WeightClass,
)


def _stage(i, status=StageStatus.PENDING.value, supplies=None):
    return Stage(
        index=i,
        title=f"stage {i}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do it"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
        supplies=supplies or [],
        outcome=Outcome(status=status),
    )


def test_json_roundtrip_equality():
    s = SessionState(
        session_id="sess",
        task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        stages=[_stage(1), _stage(2)],
    )
    s.log("hello", n=1)
    back = SessionState.from_json(s.to_json())
    assert back == s


def test_executing_requires_approval():
    with pytest.raises(InvariantError):
        SessionState(session_id="s", task_id="t", node=Node.EXECUTING.value)


def test_executing_ok_with_approval():
    s = SessionState(
        session_id="s", task_id="t", node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
    )
    assert s.node == Node.EXECUTING.value


def test_resolved_requires_all_stages_passed():
    with pytest.raises(InvariantError):
        SessionState(
            session_id="s", task_id="t", node=Node.RESOLVED.value,
            resolution=GateRecord("resolution", armed=True, passed=True),
            stages=[_stage(1, StageStatus.PASSED.value), _stage(2, StageStatus.FAILED.value)],
        )


def test_spawn_route_requires_substantive():
    with pytest.raises(InvariantError):
        SessionState(
            session_id="s", task_id="t",
            weight_class=WeightClass.SMALL_CHANGE.value, route=Route.SPAWN.value,
        )


def test_chat_is_terminal_at_routed():
    with pytest.raises(InvariantError):
        SessionState(
            session_id="s", task_id="t",
            weight_class=WeightClass.CHAT.value, node=Node.PLANNING.value,
        )


def test_ready_stages_respects_dependencies():
    s = SessionState(session_id="s", task_id="t")
    s.stages = [
        _stage(1),
        _stage(2, supplies=[Supply(on=1)]),
    ]
    ready = [s_.index for s_ in s.ready_stages()]
    assert ready == [1]
    s.stage(1).outcome.status = StageStatus.PASSED.value
    assert [s_.index for s_ in s.ready_stages()] == [2]
