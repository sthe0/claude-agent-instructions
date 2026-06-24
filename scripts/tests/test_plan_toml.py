import pytest

from agentctl.plan import PlanError, diff_plans, load_plan, parse_plan


def test_load_two_stage_plan(fixtures_dir):
    doc = load_plan(fixtures_dir / "plan_two_stage.toml")
    assert doc.meta.task_id == "demo-two-stage"
    assert [s.index for s in doc.stages] == [1, 2]
    assert doc.stages[0].is_spawn()
    assert doc.stages[0].spawn_kind() == "developer"
    assert doc.stages[1].depends_on == [1]


def test_missing_meta_raises():
    with pytest.raises(PlanError):
        parse_plan({"stage": [{"title": "x"}]})


def test_no_stages_raises():
    with pytest.raises(PlanError):
        parse_plan({"meta": {"task_id": "t"}})


def test_missing_required_stage_field_raises():
    with pytest.raises(PlanError):
        parse_plan({"meta": {"task_id": "t"}, "stage": [{"index": 1, "title": "x"}]})


def test_duplicate_indices_raise():
    data = {
        "meta": {"task_id": "t"},
        "stage": [
            {"index": 1, "title": "a", "executor": "in_thread",
             "expected_result_image": "i", "done_criterion": "c"},
            {"index": 1, "title": "b", "executor": "in_thread",
             "expected_result_image": "i", "done_criterion": "c"},
        ],
    }
    with pytest.raises(PlanError):
        parse_plan(data)


def test_diff_no_change(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage.toml")
    assert diff_plans(a, b) == "no_change"


def test_diff_refinement(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage_refined.toml")
    assert diff_plans(a, b) == "refinement"


def test_diff_substantive(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage_substantive.toml")
    assert diff_plans(a, b) == "substantive"


# --- 8-element activity-structure (weight_class = "substantive") ---

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


def _full_substantive_stage(index=1):
    return {
        **_minimal_stage(index),
        "material": "existing code",
        "means": "Edit tool",
        "method": "add the field",
        "conditions": "EXECUTING node",
        "invariants": "legacy plans unchanged",
        "capability_required": "Python",
        "principle": {
            "statement": "additive-optional keeps backward compat",
            "source": "leaf-schema.md precedent",
            "confidence": "high",
            "refutation": "refuted if existing fixture breaks",
        },
    }


def _substantive_meta():
    return {"task_id": "t", "weight_class": "substantive"}


def test_substantive_full_parses_ok():
    data = {"meta": _substantive_meta(), "stage": [_full_substantive_stage()]}
    doc = parse_plan(data)
    assert doc.meta.weight_class == "substantive"
    assert doc.stages[0].index == 1


def test_substantive_missing_principle_raises():
    stage = _full_substantive_stage()
    del stage["principle"]
    with pytest.raises(PlanError, match="principle"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_missing_principle_subfield_raises():
    stage = _full_substantive_stage()
    del stage["principle"]["source"]
    with pytest.raises(PlanError, match="source"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_missing_material_raises():
    stage = _full_substantive_stage()
    del stage["material"]
    with pytest.raises(PlanError, match="material"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_non_substantive_without_new_fields_ok():
    """Non-substantive plans with only the legacy required fields still parse."""
    data = {
        "meta": {"task_id": "t", "weight_class": "small_change"},
        "stage": [_minimal_stage()],
    }
    doc = parse_plan(data)
    assert doc.meta.weight_class == "small_change"


def test_absent_weight_class_without_new_fields_ok():
    """Plans without weight_class (legacy) parse with only the legacy required fields."""
    data = {"meta": {"task_id": "t"}, "stage": [_minimal_stage()]}
    doc = parse_plan(data)
    assert doc.meta.weight_class is None


def test_diff_weight_class_change_is_substantive(fixtures_dir):
    """Changing weight_class triggers a substantive diff."""
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    import copy
    b_doc = copy.deepcopy(a)
    b_doc.meta.weight_class = "substantive"
    from agentctl.plan import diff_plans as _diff
    assert _diff(a, b_doc) == "substantive"
