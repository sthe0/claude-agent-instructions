"""Schema + validation for `kind = "landed"` — Stage 1 of the landed-check plan.

A landed check replaces ad-hoc shell landed-ness assertions (SHA equality, a
live-resolved `rev-parse`, a literal commit range — the shapes behind 20
accumulated false-fail contexts, experience leaf
2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md) with a
typed, validated discriminator whose payload the engine — not the author —
turns into the one durable check (Stage 2, not exercised here). This file
proves only the schema/validation surface: CheckKind, LandedSpec, the seven
hard rules (R1-R7), the SCHEMA_VERSION 23 round-trip, and that a landed
stage/final_check needs no author verify_command to parse as substantive.
"""
import pytest

from agentctl.plan import PlanError, load_plan, parse_plan
from agentctl.state import (
    Actor,
    CheckKind,
    Criterion,
    FinalCheck,
    GateRecord,
    LANDED_GIT_ERROR_EXIT,
    LandedSpec,
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


def _substantive_meta():
    return {"task_id": "t", "weight_class": "substantive", "external_research": "checked wiki; none applies"}


def _landed_table(**overrides):
    base = {"target": "main", "remote": "origin", "delivered_stage": 1}
    base.update(overrides)
    return base


# --- valid landed shapes parse OK -------------------------------------------

def test_valid_landed_final_check_parses():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": _landed_table()}],
    }
    doc = parse_plan(data)
    fc = doc.meta.final_check[0]
    assert fc.kind == CheckKind.LANDED.value
    assert fc.command == ""
    assert fc.landed == LandedSpec(target="main", remote="origin", delivered_stage=1)
    assert fc.venue == "repo_root"


def test_valid_landed_stage_criterion_parses():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table()
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    crit = doc.stages[0].criterion
    assert crit.verify_kind == CheckKind.LANDED.value
    assert crit.verify_command is None
    assert crit.landed == LandedSpec(target="main", remote="origin", delivered_stage=1)
    assert crit.verify_venue == "repo_root"


# --- R1: a landed check must not carry command/verify_command/expected_exit -

def test_r1_landed_final_check_rejects_command():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "command": "true", "landed": _landed_table()}],
    }
    with pytest.raises(PlanError, match="R1"):
        parse_plan(data)


def test_r1_landed_final_check_rejects_expected_exit():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "expected_exit": 1, "landed": _landed_table()}],
    }
    with pytest.raises(PlanError, match="R1"):
        parse_plan(data)


def test_r1_landed_stage_rejects_verify_command():
    stage = _full_substantive_stage()
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table()
    with pytest.raises(PlanError, match="R1"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_r1_landed_stage_rejects_expected_exit():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table()
    stage["expected_exit"] = 1
    with pytest.raises(PlanError, match="R1"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- R2: target/remote required, non-empty, ref-name-shaped -----------------

def test_r2_missing_target_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": {"delivered_stage": 1}}],
    }
    with pytest.raises(PlanError, match="R2"):
        parse_plan(data)


def test_r2_metacharacter_target_rejected():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": _landed_table(target="main; rm -rf /")}],
    }
    with pytest.raises(PlanError, match="R2"):
        parse_plan(data)


def test_r2_metacharacter_remote_rejected():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": _landed_table(remote="origin && evil")}],
    }
    with pytest.raises(PlanError, match="R2"):
        parse_plan(data)


def test_landed_kind_without_table_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed"}],
    }
    with pytest.raises(PlanError, match="requires a"):
        parse_plan(data)


# --- R3: a landed check's venue must be repo_root ----------------------------

def test_r3_landed_defaults_venue_to_repo_root():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": _landed_table()}],
    }
    doc = parse_plan(data)
    assert doc.meta.final_check[0].venue == "repo_root"


def test_r3_landed_rejects_explicit_delivery_venue():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "venue": "delivery", "landed": _landed_table()}],
    }
    with pytest.raises(PlanError, match="repo_root"):
        parse_plan(data)


# --- R4: delivered_stage required and must name an existing stage -----------

def test_r4_missing_delivered_stage_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": {"target": "main"}}],
    }
    with pytest.raises(PlanError, match="R4"):
        parse_plan(data)


def test_r4_nonexistent_delivered_stage_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "landed", "landed": _landed_table(delivered_stage=99)}],
    }
    with pytest.raises(PlanError, match="R4"):
        parse_plan(data)


# --- R5: ordering — self-reference OK on a stage, forward reference rejected

def test_r5_stage_self_reference_accepted():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table(delivered_stage=1)
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.landed.delivered_stage == 1


def test_r5_stage_forward_reference_rejected():
    stage1 = _full_substantive_stage(1)
    stage2 = _full_substantive_stage(2)
    del stage1["verify_command"]
    stage1["verify_kind"] = "landed"
    stage1["landed"] = _landed_table(delivered_stage=2)  # forward: stage 1 -> stage 2
    with pytest.raises(PlanError, match="R5"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage1, stage2]})


def test_r5_final_check_may_reference_any_existing_stage():
    """A [[final_check]] runs after every stage, so it may name any existing
    index — including a later one — unlike a stage criterion's self-or-earlier
    restriction."""
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage(1), _minimal_stage(2)],
        "final_check": [{"kind": "landed", "landed": _landed_table(delivered_stage=2)}],
    }
    doc = parse_plan(data)
    assert doc.meta.final_check[0].landed.delivered_stage == 2


# --- R6: a landed stage criterion must be measurable -------------------------

def test_r6_acceptance_review_landed_stage_rejected():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["criterion_type"] = "acceptance_review"
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table()
    with pytest.raises(PlanError, match="R6"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- R7: a shell check must not carry target/remote/delivered_stage ---------

def test_r7_shell_final_check_rejects_landed_table():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"command": "true", "landed": _landed_table()}],
    }
    with pytest.raises(PlanError, match="R7"):
        parse_plan(data)


def test_r7_shell_stage_rejects_landed_table():
    stage = _full_substantive_stage()
    stage["landed"] = _landed_table()
    with pytest.raises(PlanError, match="R7"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- a free-text kind is rejected --------------------------------------------

def test_free_text_kind_rejected_on_final_check():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
        "final_check": [{"kind": "bogus", "command": "true"}],
    }
    with pytest.raises(PlanError, match="bogus"):
        parse_plan(data)


def test_free_text_kind_rejected_on_stage():
    stage = _full_substantive_stage()
    stage["verify_kind"] = "bogus"
    with pytest.raises(PlanError, match="bogus"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- step 8: a landed substantive stage needs no verify_command -------------

def test_substantive_landed_stage_needs_no_verify_command_hand_built():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = _landed_table()
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.verify_command is None
    assert doc.stages[0].criterion.verify_kind == CheckKind.LANDED.value


def test_substantive_landed_fixture_parses_strict(fixtures_dir):
    doc = load_plan(fixtures_dir / "plan_landed_example.toml", strict=True)
    assert doc.meta.weight_class == "substantive"
    landed_stages = [s for s in doc.stages if s.criterion.verify_kind == CheckKind.LANDED.value]
    assert landed_stages
    for s in landed_stages:
        assert s.criterion.verify_command is None
    landed_fcs = [fc for fc in doc.meta.final_check if fc.kind == CheckKind.LANDED.value]
    assert landed_fcs
    for fc in landed_fcs:
        assert fc.command == ""


def test_substantive_measurable_neither_command_nor_landed_still_raises():
    """Regression control for the step-8 amendment: a substantive measurable
    stage that is neither commanded nor landed keeps failing with its
    unchanged message."""
    stage = _full_substantive_stage()
    del stage["verify_command"]
    with pytest.raises(PlanError, match="has no verify_command"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- SCHEMA_VERSION 23 round-trip --------------------------------------------

def _landed_session(sid="s1"):
    landed = LandedSpec(target="main", remote="origin", delivered_stage=1)
    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_kind="landed", landed=landed, verify_venue="repo_root",
        ),
        outcome=Outcome(status=StageStatus.PASSED.value, delivered_head="deadbeef"),
    )
    fc = FinalCheck(command="", kind="landed", landed=landed, venue="repo_root")
    return SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage], final_check=[fc],
    )


def test_schema_version_covers_the_landed_check():
    """The landed check's fields arrived at schema 23 and were never withdrawn.

    Asserted as a floor, not an equality: a later feature's bump is not this
    file's business, and an equality pin makes every unrelated bump land here as
    a spurious failure (this one silently drifted to a literal 25 that way)."""
    assert SCHEMA_VERSION >= 23


def test_landed_state_round_trips():
    s = _landed_session()
    reloaded = SessionState.from_json(s.to_json())
    assert reloaded.stages[0].criterion.verify_kind == "landed"
    assert reloaded.stages[0].criterion.landed == LandedSpec(target="main", remote="origin", delivered_stage=1)
    assert reloaded.stages[0].outcome.delivered_head == "deadbeef"
    assert reloaded.final_check[0].kind == "landed"
    assert reloaded.final_check[0].landed == LandedSpec(target="main", remote="origin", delivered_stage=1)
    assert reloaded.schema_version == SCHEMA_VERSION


def test_landed_final_check_empty_command_not_none_round_trips():
    s = _landed_session()
    reloaded = SessionState.from_json(s.to_json())
    assert reloaded.final_check[0].command == ""
    assert reloaded.final_check[0].command is not None


def test_legacy_plan_with_no_kind_round_trips_unchanged():
    """A plan/state with no `kind` anywhere loads and round-trips exactly as
    before: verify_kind defaults to shell, landed stays None."""
    doc = parse_plan({
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage()],
    })
    assert doc.stages[0].criterion.verify_kind == CheckKind.SHELL.value
    assert doc.stages[0].criterion.landed is None
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
    assert reloaded.stages[0].criterion.verify_kind == "shell"
    assert reloaded.stages[0].criterion.landed is None
    assert reloaded.final_check == []


def test_landed_git_error_exit_constant():
    assert LANDED_GIT_ERROR_EXIT == 97
