"""Grouped-Stage model: round-trip, derived depends_on, and the flat->nested
migration shim that lets legacy live-state JSON load unchanged."""
import json

from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Outcome,
    Principle,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    Supply,
)


def _grouped_stage(index=1, supplies=None, principle=None):
    return Stage(
        index=index,
        title=f"stage {index}",
        subject=Subject(material="existing code", result="field added", invariants="legacy ok"),
        means=Means(means="Edit tool", method="add the field"),
        actor=Actor(executor="spawn:developer", capability_required="Python"),
        criterion=Criterion(criterion_type="measurable", done_criterion="pytest green"),
        principle=principle,
        conditions="EXECUTING node",
        supplies=supplies or [],
        outcome=Outcome(status=StageStatus.PENDING.value),
    )


def test_grouped_stage_roundtrip():
    s = SessionState(
        session_id="s", task_id="t",
        stages=[
            _grouped_stage(
                1,
                principle=Principle(
                    statement="additive keeps compat",
                    source="leaf-schema precedent",
                    confidence="high",
                    refutation="refuted if fixture breaks",
                ),
            ),
            _grouped_stage(2, supplies=[Supply(on=1, element="result", artifact="state.py")]),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage(2).supplies[0].element == "result"
    assert back.stage(1).principle.confidence == "high"


def test_depends_on_is_derived_from_supplies():
    s = _grouped_stage(3, supplies=[Supply(on=2), Supply(on=1), Supply(on=2, element="means")])
    # derived: sorted, de-duplicated set of supply sources — never stored
    assert s.depends_on == [1, 2]


def test_to_dict_drops_derived_depends_on():
    s = SessionState(session_id="s", task_id="t", stages=[_grouped_stage(1, supplies=[Supply(on=1)])])
    raw = json.loads(s.to_json())
    assert "depends_on" not in raw["stages"][0]
    assert raw["stages"][0]["supplies"] == [{"on": 1, "element": None, "artifact": None}]


def test_is_spawn_reads_actor_executor():
    assert _grouped_stage(1).is_spawn() is True
    assert _grouped_stage(1).spawn_kind() == "developer"
    s = _grouped_stage(1)
    s.actor.executor = "in_thread"
    assert s.is_spawn() is False
    assert s.spawn_kind() is None


def test_flat_to_nested_migration_shim():
    """A legacy FLAT stage dict (top-level executor/status/depends_on/...) loads
    into the grouped model via Stage.from_dict."""
    flat = {
        "index": 2,
        "title": "legacy stage",
        "executor": "spawn:developer",
        "expected_result_image": "img",
        "criterion_type": "measurable",
        "done_criterion": "dc",
        "material": "old material",
        "depends_on": [1],
        "status": "ACTIVE",
        "actual": "ran",
        "fail_digests": ["abc"],
    }
    st = Stage.from_dict(flat)
    assert st.actor.executor == "spawn:developer"
    assert st.subject.result == "img"
    assert st.subject.material == "old material"
    assert st.criterion.done_criterion == "dc"
    assert st.outcome.status == "ACTIVE"
    assert st.outcome.actual == "ran"
    assert st.outcome.fail_digests == ["abc"]
    # depends_on lifted into element-less supply edges, then derived back
    assert [sup.on for sup in st.supplies] == [1]
    assert st.depends_on == [1]
    assert st.principle is None


def test_migration_shim_via_session_from_dict():
    """A whole session JSON with flat stages loads through SessionState.from_dict."""
    payload = {
        "session_id": "s",
        "task_id": "t",
        "approval": {"name": "plan_approval"},
        "resolution": {"name": "resolution"},
        "stages": [
            {"index": 1, "title": "a", "executor": "in_thread",
             "expected_result_image": "i", "criterion_type": "measurable",
             "done_criterion": "c", "status": "PASSED"},
        ],
    }
    st = SessionState.from_dict(payload)
    assert st.stage(1).outcome.status == "PASSED"
    assert st.stage(1).actor.executor == "in_thread"
