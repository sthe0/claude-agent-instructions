"""Stage 1 of the venue-lifecycle plan: the optional `verify_venue_at_final`
field on a stage `Criterion` (schema 24) — a SECOND check venue, read only at
verify-final via `SessionState.resolve_final_check_venue`, that lets a stage
whose delivery venue disappears once the change lands (a rebase/land stage)
still declare where its OWN check runs without perturbing `verify_venue` (the
venue `cmd_dispatch`/`cmd_record_result` always use during execution).

Proves only the schema/validation surface: V1 (CheckVenue vocabulary), V2 (not
on a `verify_kind = "landed"` criterion — that venue is already fixed at
repo_root by R3), V3 (must not differ from `verify_venue` when no
`delivery_worktree` names a second tree), V4 (absent -> resolves to
`verify_venue`, not an error) — plus the field's presence in every
engine-consumed comparison surface (`stage_carry_key`, `stage_question_key`,
`diff_plans`, `gates._operative_surface`) and a schema-23 -> 24 persisted-state
migration proving a pre-existing session loads unchanged."""
import pytest

from agentctl import gates
from agentctl.plan import PlanError, diff_plans, parse_plan, stage_carry_key, stage_question_key
from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Outcome,
    SCHEMA_VERSION,
    SessionState,
    Stage,
    StageStatus,
    Subject,
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
    base = {"task_id": "t", "weight_class": "substantive", "external_research": "checked wiki; none applies"}
    base.update(overrides)
    return base


# --- SCHEMA_VERSION 24 --------------------------------------------------

def test_schema_version_is_24():
    assert SCHEMA_VERSION == 24


# --- V1: free-text value rejected with the CheckVenue vocabulary ---------

def test_v1_free_text_verify_venue_at_final_rejected():
    stage = _full_substantive_stage(verify_venue_at_final="orbit")
    with pytest.raises(PlanError, match="verify_venue_at_final 'orbit' is not one of"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- V2: must not appear on a verify_kind = "landed" criterion -----------

def test_v2_landed_criterion_rejects_verify_venue_at_final():
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["verify_kind"] = "landed"
    stage["landed"] = {"target": "main", "remote": "origin", "delivered_stage": 1}
    stage["verify_venue_at_final"] = "repo_root"
    with pytest.raises(PlanError, match="V2"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


# --- V3: must not differ from verify_venue with no delivery_worktree ----

def test_v3_differing_value_without_delivery_worktree_rejected():
    stage = _full_substantive_stage(verify_venue_at_final="repo_root")
    with pytest.raises(PlanError, match="V3"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_v3_differing_value_with_delivery_worktree_accepted():
    stage = _full_substantive_stage(verify_venue_at_final="repo_root")
    doc = parse_plan({
        "meta": _substantive_meta(delivery_worktree="/tmp/some-worktree"),
        "stage": [stage],
    })
    assert doc.stages[0].criterion.verify_venue_at_final == "repo_root"


def test_v3_equal_value_accepted_without_delivery_worktree():
    stage = _full_substantive_stage(verify_venue_at_final="delivery")
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.verify_venue_at_final == "delivery"


# --- V4: absent -> resolves to verify_venue, not an error ----------------

def test_v4_absent_field_parses_to_none():
    stage = _full_substantive_stage()
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.verify_venue_at_final is None


def test_v4_resolve_final_check_venue_falls_back_to_verify_venue():
    criterion = Criterion(criterion_type="measurable", done_criterion="c", verify_venue="delivery")
    state = SessionState(session_id="s1", task_id="t")
    assert state.resolve_final_check_venue(criterion) == state.resolve_check_venue("delivery")


def test_resolve_final_check_venue_uses_declared_value_when_present():
    criterion = Criterion(
        criterion_type="measurable", done_criterion="c",
        verify_venue="delivery", verify_venue_at_final="repo_root",
    )
    state = SessionState(session_id="s1", task_id="t")
    assert state.resolve_final_check_venue(criterion) == state.resolve_check_venue("repo_root")


# --- operative-surface / replan-diff wiring ------------------------------

def _two_docs_differing_only_in_verify_venue_at_final():
    stage_a = _full_substantive_stage()
    stage_b = _full_substantive_stage(verify_venue_at_final="repo_root")
    meta = _substantive_meta(delivery_worktree="/tmp/some-worktree")
    doc_a = parse_plan({"meta": meta, "stage": [stage_a]})
    doc_b = parse_plan({"meta": meta, "stage": [stage_b]})
    return doc_a, doc_b


def test_stage_carry_key_notices_verify_venue_at_final_change():
    doc_a, doc_b = _two_docs_differing_only_in_verify_venue_at_final()
    assert stage_carry_key(doc_a.stages[0]) != stage_carry_key(doc_b.stages[0])


def test_stage_question_key_notices_verify_venue_at_final_change():
    doc_a, doc_b = _two_docs_differing_only_in_verify_venue_at_final()
    assert stage_question_key(doc_a.stages[0]) != stage_question_key(doc_b.stages[0])


def test_diff_plans_surfaces_verify_venue_at_final_only_change():
    doc_a, doc_b = _two_docs_differing_only_in_verify_venue_at_final()
    assert diff_plans(doc_a, doc_a) == "no_change"
    assert diff_plans(doc_a, doc_b) == "refinement"


def test_operative_surface_notices_verify_venue_at_final_change():
    doc_a, doc_b = _two_docs_differing_only_in_verify_venue_at_final()
    assert gates._operative_surface(doc_a) != gates._operative_surface(doc_b)


# --- schema-23 -> 24 persisted-state migration ---------------------------

def test_legacy_schema23_state_loads_with_none_and_resolves_unchanged():
    """A schema-23 persisted state.json has no `verify_venue_at_final` key
    anywhere in its criterion dicts. Loading it under schema-24 code must
    parse the field to None (V4) and resolve identically to how it always
    resolved verify_venue — a plan/state authored before this field existed
    stays byte-identical in behaviour."""
    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c", verify_venue="delivery"),
        outcome=Outcome(status=StageStatus.PENDING.value),
    )
    state = SessionState(session_id="s1", task_id="t", stages=[stage])
    data = state.to_dict()
    data["schema_version"] = 23
    del data["stages"][0]["criterion"]["verify_venue_at_final"]

    reloaded = SessionState.from_dict(data)

    crit = reloaded.stages[0].criterion
    assert crit.verify_venue_at_final is None
    assert reloaded.resolve_final_check_venue(crit) == reloaded.resolve_check_venue("delivery")


def test_plan_with_no_verify_venue_at_final_round_trips_byte_identical_modulo_schema_version():
    """Stage 1's invariant: a plan that does not declare the new field parses
    to a byte-identical PlanDoc (verify_venue_at_final stays None throughout)
    and its state round-trips with the same criterion shape modulo the bumped
    SCHEMA_VERSION."""
    stage = _full_substantive_stage()
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.verify_venue_at_final is None

    session_stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.PENDING.value),
    )
    state = SessionState(session_id="legacy", task_id="t", stages=[session_stage])
    reloaded = SessionState.from_json(state.to_json())
    assert reloaded.stages[0].criterion.verify_venue_at_final is None
    assert reloaded.schema_version == SCHEMA_VERSION


# --- back-compat: an ABSENT field must not perturb any persisted digest ----

# stage_question_key of a plan that does not declare verify_venue_at_final,
# pinned under schema 24. `stage_question_key` is persisted in
# Question.disposed_at_key and compared across processes at the plan_approval
# gate, so this digest MUST equal the schema-23 value — an absent field has to
# contribute nothing to the tuple. A `... or ""` member (the stage-1 review's
# blocking finding) would append an empty string and change this hash, flipping
# every disposed question of every unrelated live session to a spurious "stage
# definition changed" blocker. If an intentional later change to the stage tuple
# alters this golden, re-pin it AND re-confirm the absent-field identity still
# holds (a schema-N plan and its schema-(N+1) reparse hash equally).
_PRE_FIELD_QUESTION_KEY = "35a6dba386aa51a269c65f672e1604a7fbe272fb2d03a0a122781bac8dfc3d8e"


def test_absent_verify_venue_at_final_reproduces_pinned_question_key():
    stage = _full_substantive_stage()
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.verify_venue_at_final is None
    assert stage_question_key(doc.stages[0]) == _PRE_FIELD_QUESTION_KEY


def test_declared_verify_venue_at_final_changes_question_key_off_the_pin():
    # The pin is specifically the ABSENT-field value: declaring the field must
    # move the digest away from it (the member is contributed when present).
    stage = _full_substantive_stage(verify_venue_at_final="repo_root")
    doc = parse_plan({
        "meta": _substantive_meta(delivery_worktree="/tmp/some-worktree"),
        "stage": [stage],
    })
    assert stage_question_key(doc.stages[0]) != _PRE_FIELD_QUESTION_KEY
