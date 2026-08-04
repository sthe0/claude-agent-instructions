"""Schema + validation for `[*.differential]` — Stage 1 of the differential-verify
plan.

A differential-verify declaration lets the engine re-run a FAILED check at the
delivery branch's merge-base against a trunk and report green iff no NEW
violation appears relative to that frozen base (fixing the false-fails a
fast-forward-only land's mandatory rebase produces when it pulls in a
pre-existing, orthogonal red — experience leaf instance 22). This file proves
only the schema/validation surface: DifferentialSpec, the seven hard rules
(D1-D7), presence in the operative surface / carry key / replan diff, and the
SCHEMA_VERSION 25 round-trip. Evaluation (re-running at the base, comparing
violation sets) is Stage 2/3, not exercised here.
"""
import json

import pytest

from agentctl.gates import _operative_surface
from agentctl.plan import PlanError, diff_plans, parse_plan, stage_carry_key, stage_question_key
from agentctl.state import (
    Actor,
    Criterion,
    DifferentialSpec,
    FinalCheck,
    GateRecord,
    Means,
    Outcome,
    Partition,
    Route,
    SCHEMA_VERSION,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def _minimal_stage(index=1, **overrides):
    base = {
        "index": index,
        "title": "Do something",
        "executor": "in_thread",
        "expected_result_image": "thing done",
        "done_criterion": "check passes",
    }
    base.update(overrides)
    return base


def _full_substantive_stage(index=1, **overrides):
    base = {
        **_minimal_stage(index),
        "material": "existing code",
        "means": "Edit tool",
        "method": "add the field",
        "conditions": "EXECUTING node",
        "invariants": "legacy plans unchanged",
        "capability_required": "Python",
        "verify_command": "python3 -m pytest tests/ -q",
        "principle": {
            "statement": "additive-optional keeps backward compat",
            "source": "leaf-schema.md precedent",
            "derivation": "that precedent added an optional field and no loader broke, so the same shape applies here",
            "confidence": "high",
            "refutation": "refuted if existing fixture breaks",
        },
    }
    base.update(overrides)
    return base


def _substantive_meta(**overrides):
    base = {
        "task_id": "t", "weight_class": "substantive",
        "external_research": "checked wiki; none applies",
        "repo_root": "/repo",
    }
    base.update(overrides)
    return base


def _differential_table(**overrides):
    base = {"target": "main", "remote": "origin"}
    base.update(overrides)
    return base


# --- valid differential shapes parse OK -------------------------------------

def test_valid_differential_final_check_parses():
    data = {
        "meta": {"task_id": "t", "repo_root": "/repo"},
        "stage": [_minimal_stage()],
        "final_check": [{"command": "true", "differential": _differential_table()}],
    }
    doc = parse_plan(data)
    fc = doc.meta.final_check[0]
    assert fc.differential == DifferentialSpec(target="main", remote="origin", violation_pattern=None)


def test_valid_differential_stage_criterion_parses():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(violation_pattern=r"^ERROR:")
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    crit = doc.stages[0].criterion
    assert crit.differential == DifferentialSpec(target="main", remote="origin", violation_pattern=r"^ERROR:")


def test_no_differential_table_parses_to_none():
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [_minimal_stage()]})
    assert doc.stages[0].criterion.differential is None
    assert doc.meta.final_check == []


# --- D1: rejected on a kind = "landed" check --------------------------------

def test_d1_landed_final_check_rejects_differential():
    data = {
        "meta": _substantive_meta(),
        "stage": [_minimal_stage()],
        "final_check": [{
            "kind": "landed",
            "landed": {"target": "main", "delivered_stage": 1},
            "differential": _differential_table(),
        }],
    }
    with pytest.raises(PlanError, match="D1"):
        parse_plan(data)


def test_d1_landed_stage_rejects_differential():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = {"target": "main", "delivered_stage": 1}
    stage["differential"] = _differential_table()
    with pytest.raises(PlanError, match="D1"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- D2: rejected unless criterion_type = "measurable" (stage only) --------

def test_d2_acceptance_review_stage_rejected():
    stage = _full_substantive_stage()
    stage["criterion_type"] = "acceptance_review"
    stage["differential"] = _differential_table()
    with pytest.raises(PlanError, match="D2"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- D3: rejected when the check carries no command -------------------------

def test_d3_stage_without_verify_command_rejected():
    # Non-substantive so _validate_substantive_stage's own "has no
    # verify_command" precondition (a different, pre-existing message) never
    # fires first — this isolates D3 to the differential-specific rejection.
    stage = _minimal_stage()
    stage["differential"] = _differential_table()
    with pytest.raises(PlanError, match="D3"):
        parse_plan({"meta": {"task_id": "t"}, "stage": [stage]})


# D3's final_check half is unreachable through parse_plan today: a shell
# final_check already requires a non-empty 'command' unconditionally (a
# pre-existing check that fires before differential parsing is ever reached),
# and a landed final_check hits D1 first. The shared function still enforces
# D3 uniformly for both call sites; only the stage path can exercise it.


# --- D4: target required + ref-shape validated; remote defaults + validated -

def test_d4_missing_target_raises():
    stage = _full_substantive_stage()
    stage["differential"] = {"remote": "origin"}
    with pytest.raises(PlanError, match="D4"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_d4_metacharacter_target_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(target="main; rm -rf /")
    with pytest.raises(PlanError, match="D4"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_d4_metacharacter_remote_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(remote="origin && evil")
    with pytest.raises(PlanError, match="D4"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_d4_remote_defaults_to_origin():
    stage = _full_substantive_stage()
    stage["differential"] = {"target": "main"}
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.differential.remote == "origin"


# --- D5: violation_pattern, when present, must be a valid regex ------------

def test_d5_invalid_regex_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(violation_pattern="[unclosed")
    with pytest.raises(PlanError, match="D5"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_d5_non_string_violation_pattern_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(violation_pattern=123)
    with pytest.raises(PlanError, match="D5"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_d5_valid_regex_accepted():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(violation_pattern=r"^ERROR:\s+\d+")
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.differential.violation_pattern == r"^ERROR:\s+\d+"


# --- D6: rejected when [meta] repo_root is unset ----------------------------

def test_d6_missing_repo_root_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table()
    meta = _substantive_meta()
    del meta["repo_root"]
    with pytest.raises(PlanError, match="D6"):
        parse_plan({"meta": meta, "stage": [stage]})


# --- D7: unknown keys in the table rejected ---------------------------------

def test_d7_unknown_key_rejected():
    stage = _full_substantive_stage()
    stage["differential"] = _differential_table(bogus="nope")
    with pytest.raises(PlanError, match="D7"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- appears in operative surface / carry key / replan diff -----------------

def test_differential_changes_operative_surface():
    base_stage = _full_substantive_stage()
    with_diff_stage = _full_substantive_stage()
    with_diff_stage["differential"] = _differential_table()
    meta = _substantive_meta()
    doc_without = parse_plan({"meta": meta, "stage": [base_stage]})
    doc_with = parse_plan({"meta": meta, "stage": [with_diff_stage]})
    assert _operative_surface(doc_without) != _operative_surface(doc_with)


def test_differential_changes_stage_carry_key():
    base_stage = _full_substantive_stage()
    with_diff_stage = _full_substantive_stage()
    with_diff_stage["differential"] = _differential_table()
    meta = _substantive_meta()
    doc_without = parse_plan({"meta": meta, "stage": [base_stage]})
    doc_with = parse_plan({"meta": meta, "stage": [with_diff_stage]})
    assert stage_carry_key(doc_without.stages[0]) != stage_carry_key(doc_with.stages[0])


def test_differential_changes_stage_question_key():
    base_stage = _full_substantive_stage()
    with_diff_stage = _full_substantive_stage()
    with_diff_stage["differential"] = _differential_table()
    meta = _substantive_meta()
    doc_without = parse_plan({"meta": meta, "stage": [base_stage]})
    doc_with = parse_plan({"meta": meta, "stage": [with_diff_stage]})
    assert stage_question_key(doc_without.stages[0]) != stage_question_key(doc_with.stages[0])


def test_differential_surfaces_in_replan_diff():
    base_stage = _full_substantive_stage()
    with_diff_stage = _full_substantive_stage()
    with_diff_stage["differential"] = _differential_table()
    meta = _substantive_meta()
    doc_without = parse_plan({"meta": meta, "stage": [base_stage]})
    doc_with = parse_plan({"meta": meta, "stage": [with_diff_stage]})
    assert diff_plans(doc_without, doc_with) != "no_change"


def test_differential_absent_leaves_operative_surface_and_keys_unaffected_by_declaration_order():
    """A plan that never declares differential produces the same operative
    surface / carry key / question key regardless of the field's presence in
    the dataclass — the declared-only contribution pattern means two
    otherwise-identical plans without differential compare equal."""
    stage_a = _full_substantive_stage()
    stage_b = _full_substantive_stage()
    meta = _substantive_meta()
    doc_a = parse_plan({"meta": meta, "stage": [stage_a]})
    doc_b = parse_plan({"meta": meta, "stage": [stage_b]})
    assert _operative_surface(doc_a) == _operative_surface(doc_b)
    assert stage_carry_key(doc_a.stages[0]) == stage_carry_key(doc_b.stages[0])
    assert stage_question_key(doc_a.stages[0]) == stage_question_key(doc_b.stages[0])
    assert diff_plans(doc_a, doc_b) == "no_change"


# --- SCHEMA_VERSION 25 round-trip / migration --------------------------------

def test_schema_version_is_25():
    assert SCHEMA_VERSION == 25


def _differential_session(sid="s1"):
    differential = DifferentialSpec(target="main", remote="origin", violation_pattern=None)
    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command="true", differential=differential,
        ),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )
    fc = FinalCheck(command="true", differential=differential)
    return SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage], final_check=[fc],
    )


def test_differential_state_round_trips():
    s = _differential_session()
    reloaded = SessionState.from_json(s.to_json())
    assert reloaded.stages[0].criterion.differential == DifferentialSpec(
        target="main", remote="origin", violation_pattern=None
    )
    assert reloaded.final_check[0].differential == DifferentialSpec(
        target="main", remote="origin", violation_pattern=None
    )
    assert reloaded.schema_version == 25


def test_schema_24_persisted_state_loads_at_25_with_differential_absent():
    """A schema-24 state.json (no `differential` key anywhere, no
    `differential_base` key at all) loads at schema 25 with the new field
    absent everywhere and behaviour unchanged — the grandfather migration."""
    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c", verify_command="true"),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )
    fc = FinalCheck(command="true")
    legacy_state = SessionState(
        session_id="legacy", task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        stages=[stage], final_check=[fc],
    )
    legacy_json = legacy_state.to_json()
    data = __import__("json").loads(legacy_json)
    # Simulate a genuine schema-24 persisted file: no differential/differential_base keys.
    data.pop("differential_base", None)
    data["schema_version"] = 24
    del legacy_json
    reloaded = SessionState.from_json(__import__("json").dumps(data))
    assert reloaded.stages[0].criterion.differential is None
    assert reloaded.final_check[0].differential is None
    assert reloaded.differential_base == {}


def test_legacy_plan_with_no_differential_round_trips_unchanged():
    """A plan/state with no `differential` anywhere loads and round-trips
    exactly as before: differential stays None, differential_base stays {}."""
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [_minimal_stage()]})
    assert doc.stages[0].criterion.differential is None
    assert doc.meta.final_check == []

    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.PENDING.value),
    )
    s = SessionState(session_id="legacy", task_id="t", stages=[stage])
    reloaded = SessionState.from_json(s.to_json())
    assert reloaded.stages[0].criterion.differential is None
    assert reloaded.final_check == []
    assert reloaded.differential_base == {}
