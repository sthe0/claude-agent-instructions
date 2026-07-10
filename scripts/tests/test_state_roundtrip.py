import pytest

from agentctl.state import (
    Actor,
    CostRollup,
    Criterion,
    FinalCheck,
    GateRecord,
    InvariantError,
    Means,
    Node,
    Outcome,
    Partition,
    PartitionUnit,
    PlanFrame,
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


def test_json_roundtrip_preserves_verify_command():
    s = SessionState(
        session_id="sess",
        task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        stages=[
            Stage(
                index=1,
                title="stage 1",
                subject=Subject(material="m", result="img"),
                means=Means(means="Edit", method="do it"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type="measurable",
                    done_criterion="crit",
                    verify_command="pytest -q tests/test_x.py",
                    expected_exit=0,
                ),
            ),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage(1).criterion.verify_command == "pytest -q tests/test_x.py"
    assert back.stage(1).criterion.expected_exit == 0


def test_legacy_flat_stage_without_verify_command_loads():
    """A pre-existing FLAT stage dict (no verify_command) rebuilds with defaults."""
    flat = {
        "index": 1,
        "title": "legacy",
        "executor": "in_thread",
        "expected_result_image": "img",
        "done_criterion": "crit",
        "criterion_type": "measurable",
    }
    stage = Stage.from_dict(flat)
    assert stage.criterion.verify_command is None
    assert stage.criterion.expected_exit == 0


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


def test_json_roundtrip_preserves_observation():
    """observation persists through to_json / from_json."""
    s = SessionState(
        session_id="sess", task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        stages=[
            Stage(
                index=1,
                title="stage 1",
                subject=Subject(material="m", result="img"),
                means=Means(means="Edit", method="do it"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type="acceptance_review",
                    done_criterion="crit",
                    observation="I saw the feature working end-to-end",
                ),
            ),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage(1).criterion.observation == "I saw the feature working end-to-end"


def test_legacy_criterion_without_observation_loads_with_default():
    """A criterion dict without 'observation' (legacy/grouped shape) loads with '' default."""
    flat_with_grouped_criterion = {
        "index": 1,
        "title": "legacy",
        "subject": {"material": "m", "result": "img", "invariants": None},
        "means": {"means": "Edit", "method": "do"},
        "actor": {"executor": "in_thread", "capability_required": None},
        "criterion": {"criterion_type": "measurable", "done_criterion": "c",
                      "verify_command": None, "expected_exit": 0},
        # no "observation" key
        "principle": None,
        "conditions": None,
        "supplies": [],
        "outcome": {"status": "PENDING", "actual": None, "fail_digests": []},
        "control": None,
    }
    stage = Stage.from_dict(flat_with_grouped_criterion)
    assert stage.criterion.observation == ""


def test_json_roundtrip_preserves_final_check():
    """FinalCheck entries round-trip through to_json / from_json."""
    s = SessionState(
        session_id="sess", task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        final_check=[FinalCheck(command="pytest -q", expected_exit=0, label="suite")],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert len(back.final_check) == 1
    assert back.final_check[0].command == "pytest -q"
    assert back.final_check[0].label == "suite"


def test_legacy_state_without_final_check_loads_with_default():
    """A state dict without 'final_check' (schema_version <= 7) loads with []."""
    import json
    s = SessionState(session_id="s", task_id="t")
    raw = json.loads(s.to_json())
    del raw["final_check"]  # simulate a pre-schema-8 state
    loaded = SessionState.from_dict(raw)
    assert loaded.final_check == []


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


def test_json_roundtrip_preserves_cost_fields():
    """Outcome cost fields and SessionState.cost round-trip through to_json/from_json."""
    stage_with_cost = Stage(
        index=1,
        title="stage 1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do it"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
        outcome=Outcome(
            status=StageStatus.PASSED.value,
            actual="done",
            cost_usd=1.23,
            duration_ms=45000,
            spawn_count=1,
        ),
    )
    s = SessionState(
        session_id="cost-rt",
        task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        stages=[stage_with_cost],
        cost=CostRollup(
            total_cost_usd=1.23,
            total_duration_ms=45000,
            spawn_count=1,
            attributed_stages=1,
            note="spawn costs attributed; main-session tokens not split per stage",
        ),
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage(1).outcome.cost_usd == 1.23
    assert back.stage(1).outcome.duration_ms == 45000
    assert back.stage(1).outcome.spawn_count == 1
    assert back.cost.total_cost_usd == 1.23
    assert back.cost.attributed_stages == 1


def test_json_roundtrip_preserves_partition_units():
    """Partition.units round-trip through to_json / from_json as typed
    PartitionUnit objects (not raw dicts), via Partition.from_dict."""
    s = SessionState(
        session_id="pu-rt",
        task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        node=Node.APPROVED.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        stages=[_stage(1), _stage(2, supplies=[Supply(on=1)])],
        partition=Partition(
            m1=True, m2=True, verdict="recommended",
            units=[
                PartitionUnit(title="core", stages=[1], mode="inline"),
                PartitionUnit(title="tests", stages=[2], mode="subtask", ref="ABC-42"),
            ],
        ),
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert all(isinstance(u, PartitionUnit) for u in back.partition.units)
    assert back.partition.units[1].mode == "subtask"
    assert back.partition.units[1].ref == "ABC-42"


def test_legacy_partition_without_units_loads_with_default():
    """A pre-units partition dict (no 'units' key) loads with [] via
    Partition.from_dict, so old state.json remains loadable."""
    import json
    s = SessionState(
        session_id="s", task_id="t",
        node=Node.APPROVED.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="possible"),
    )
    raw = json.loads(s.to_json())
    raw["partition"].pop("units", None)  # simulate a pre-units state
    loaded = SessionState.from_dict(raw)
    assert loaded.partition.units == []


def test_plan_stack_frame_preserves_partition_units():
    """A PlanFrame on plan_stack carrying a partition with units round-trips its
    units as typed PartitionUnit objects (the second Partition.from_dict site)."""
    frame = PlanFrame(
        plan_path="/tmp/parent.toml",
        node=Node.EXECUTING.value,
        task_id="parent",
        goal="g",
        overall_done_criterion="c",
        overall_criterion_type="measurable",
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value,
        repo_root=None,
        final_check=[],
        partition=Partition(
            m1=True, verdict="recommended",
            units=[PartitionUnit(title="u1", stages=[1], mode="spawn", ref="child-sess")],
        ),
        approval=GateRecord("plan_approval", armed=True, passed=True),
        resolution=GateRecord("resolution"),
        stages=[_stage(1)],
        current_stage=1,
        originating_stage=1,
    )
    s = SessionState(
        session_id="ps-rt", task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        plan_stack=[frame],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    restored = back.plan_stack[0].partition
    assert all(isinstance(u, PartitionUnit) for u in restored.units)
    assert restored.units[0].mode == "spawn"
    assert restored.units[0].ref == "child-sess"


def test_legacy_state_without_cost_fields_loads_with_defaults():
    """A pre-existing state dict without cost fields (Outcome + SessionState) loads with defaults."""
    import json
    s = SessionState(session_id="s", task_id="t", stages=[_stage(1)])
    raw = json.loads(s.to_json())
    # Simulate a pre-schema-10 state: strip cost fields from Outcome and SessionState
    for stg in raw.get("stages", []):
        outcome = stg.get("outcome", {})
        outcome.pop("cost_usd", None)
        outcome.pop("duration_ms", None)
        outcome.pop("spawn_count", None)
    raw.pop("cost", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.cost is None
    assert loaded.stage(1).outcome.cost_usd is None
    assert loaded.stage(1).outcome.duration_ms is None
    assert loaded.stage(1).outcome.spawn_count == 0


def test_legacy_state_without_plan_snapshot_fields_loads_with_defaults():
    """A pre-schema-12 state dict without plan_snapshot_path/plan_snapshot_hash
    (#8) loads with both defaulting to None."""
    import json
    s = SessionState(
        session_id="s", task_id="t",
        plan_snapshot_path="/home/u/.claude/plans/snap/plan-approved-abc.toml",
        plan_snapshot_hash="abc123",
    )
    raw = json.loads(s.to_json())
    raw.pop("plan_snapshot_path", None)
    raw.pop("plan_snapshot_hash", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.plan_snapshot_path is None
    assert loaded.plan_snapshot_hash is None


def test_json_roundtrip_preserves_tracker_key():
    s = SessionState(session_id="s", task_id="t", tracker_key="ABC-9")
    back = SessionState.from_json(s.to_json())
    assert back.tracker_key == "ABC-9"


def test_legacy_state_without_tracker_key_field_loads_with_default():
    """A pre-#11 state dict with no tracker_key key (absent, not even null) loads
    with tracker_key defaulting to None."""
    import json
    s = SessionState(session_id="s", task_id="t", tracker_key="ABC-9")
    raw = json.loads(s.to_json())
    raw.pop("tracker_key", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.tracker_key is None


def test_json_roundtrip_preserves_stage_reviews_and_bypasses():
    """The schema-14 acceptance-judge fields survive a full to_json/from_json cycle,
    including the observation_sha256 binding and the never-cleared bypass ledger."""
    from agentctl.state import JudgeBypass, StageReview
    s = SessionState(
        session_id="s", task_id="t",
        stage_reviews=[
            StageReview(stage_index=1, verdict="pass", reviewer="judge:haiku",
                        concerns=["c1"], note="looks concrete", observation_sha256="deadbeef"),
            StageReview(stage_index=2, verdict="override", reviewer="fedor",
                        note="deadlock escape", observation_sha256="cafe"),
        ],
        judge_bypassed=[
            JudgeBypass(stage_index=2, kind="override", reviewer="fedor", note="deadlock escape"),
            JudgeBypass(stage_index=3, kind="killswitch", note="AGENTCTL_STAGE_REVIEW=0"),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage_reviews[0].observation_sha256 == "deadbeef"
    assert back.judge_bypassed[1].kind == "killswitch"


def test_legacy_state_without_judge_fields_loads_with_defaults():
    """A pre-schema-14 state dict (no stage_reviews / judge_bypassed keys) loads with
    empty lists, so old state.json remains readable."""
    import json
    from agentctl.state import StageReview
    s = SessionState(session_id="s", task_id="t",
                     stage_reviews=[StageReview(stage_index=1, verdict="pass", reviewer="judge:haiku")])
    raw = json.loads(s.to_json())
    raw.pop("stage_reviews", None)
    raw.pop("judge_bypassed", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.stage_reviews == []
    assert loaded.judge_bypassed == []
