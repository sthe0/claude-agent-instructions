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
