"""Risk-shaped planning, phase A: binding, the skeleton checks, the structural digests.

A session binds at its first submit-plan from the `risk-shaped-planning` config row (env
override `AGENTCTL_RISK_PLANNING`); everything new is gated on that binding, so a session that
did not bind behaves as before -- `test_unbound_session_unchanged` is the control for that.
"""
from __future__ import annotations

import pytest

from agentctl import config as agentctl_config
from agentctl import gates, plan
from agentctl.config import RISK_PLANNING_ENV, parse_config_md, risk_planning_enabled
from agentctl.state import PLAN_BOUND_FIELDS, SESSION_SCOPED_FIELDS
from risk_planning_scenario import SESSION, Scenario, skeleton_text

NEW_FIELDS = {"risk_planning", "plan_phase", "skeleton_freeze", "skeleton_returns"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(RISK_PLANNING_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def sc(tmp_path):
    return Scenario(tmp_path)


def load(sc: Scenario, text: str) -> plan.PlanDoc:
    return plan.load_plan(sc.write(text))


def test_new_session_fields_are_session_scoped():
    assert NEW_FIELDS <= SESSION_SCOPED_FIELDS
    assert not NEW_FIELDS & PLAN_BOUND_FIELDS


def test_config_row_reads_off():
    assert parse_config_md()["risk-shaped-planning"] == "off"
    assert risk_planning_enabled() is False


@pytest.mark.parametrize("env, row, expected", [
    ({}, "on", True),
    ({}, "off", False),
    ({}, "maybe", False),
    ({RISK_PLANNING_ENV: "1"}, "off", True),
    ({RISK_PLANNING_ENV: "0"}, "on", False),
    ({RISK_PLANNING_ENV: "yes"}, "on", True),
    ({RISK_PLANNING_ENV: "yes"}, "off", False),
])
def test_resolver_env_overrides_row(env, row, expected):
    assert risk_planning_enabled(env, {"risk-shaped-planning": row}) is expected


def test_resolver_missing_row_is_off():
    assert risk_planning_enabled({}, {}) is False


def test_skeleton_problems_clean_chain(sc):
    assert plan.skeleton_problems(load(sc, skeleton_text())) == []


def test_skeleton_refuses_orphan_stage(sc):
    doc = load(sc, skeleton_text(base_stage=1))
    problems = plan.skeleton_problems(doc)
    assert len(problems) == 1 and "[2]" in problems[0] and "base_stage 1" in problems[0]


def test_skeleton_orphan_is_a_branch_not_on_the_chain(sc):
    three = skeleton_text(stage3=True, base_stage=2)
    problems = plan.skeleton_problems(load(sc, three))
    assert len(problems) == 1 and "[3]" in problems[0]


def test_skeleton_needs_alternatives_or_waiver(sc):
    doc = load(sc, skeleton_text(alternatives=""))
    assert any("alternatives" in p for p in plan.skeleton_problems(doc))
    chains = 'alternatives = ["build the tests first, then the module"]\n'
    assert plan.skeleton_problems(load(sc, skeleton_text(alternatives=chains))) == []


@pytest.mark.parametrize("base", [0, 9])
def test_skeleton_base_stage_must_be_a_stage(sc, base):
    problems = plan.skeleton_problems(load(sc, skeleton_text(base_stage=base)))
    assert any("is not a stage" in p for p in problems)


def test_skeleton_needs_an_integer_base_stage(sc):
    text = skeleton_text().replace("base_stage = 2\n", 'base_stage = "two"\n', 1)
    assert any("base_stage" in p for p in plan.skeleton_problems(load(sc, text)))


def test_digests_ignore_bets_controls_and_grants(sc):
    base = plan.skeleton_digests(load(sc, skeleton_text()))
    bets = (
        ('depends_on = []\n', 'depends_on = []\nverify_command = "true"\nnegative_control = "false"\n'),
        ('done_criterion = "pytest tests/test_mod.py green"',
         'done_criterion = "pytest tests/test_mod.py green and quiet"'),
        ('executor = "spawn:developer"\nexpected_result_image = "pytest green',
         'executor = "spawn:developer"\ncost_tier = "large"\nexpected_result_image = "pytest green'),
    )
    for edit in bets:
        assert plan.skeleton_digests(load(sc, skeleton_text(edits=(edit,)))) == base, edit


@pytest.mark.parametrize("edit, key", [
    (('title = "Add tests"', 'title = "Add more tests"'), "stage:2:title"),
    (('executor = "spawn:developer"\nexpected_result_image = "pytest green',
      'executor = "in_thread"\nexpected_result_image = "pytest green'), "stage:2:executor"),
    (('depends_on = [1]', 'depends_on = []'), "stage:2:depends_on"),
    (('output_artifacts = ["tests/test_mod.py"]', 'output_artifacts = ["tests/test_other.py"]'),
     "stage:2:product"),
    (('base_stage = 2', 'base_stage = 1'), "meta:base_stage"),
])
def test_digests_move_on_structural_edits(sc, edit, key):
    base = plan.skeleton_digests(load(sc, skeleton_text()))
    moved = plan.skeleton_changed(base, plan.skeleton_digests(load(sc, skeleton_text(edits=(edit,)))))
    assert key in moved


def test_digests_move_on_a_goal_edit(sc):
    base = plan.skeleton_digests(load(sc, skeleton_text()))
    edit = ('goal = "Demonstrate the full two-stage coordination cycle"', 'goal = "Another goal"')
    moved = plan.skeleton_changed(base, plan.skeleton_digests(load(sc, skeleton_text(edits=(edit,)))))
    assert moved == ["meta:goal"]


def test_a_stage_added_counts_as_changed(sc):
    base = plan.skeleton_digests(load(sc, skeleton_text()))
    three = plan.skeleton_digests(load(sc, skeleton_text(stage3=True)))
    assert any(k.startswith("stage:3:") for k in plan.skeleton_changed(base, three))


@pytest.mark.parametrize("body, axis", [
    ("axis:R stage 1 feeds nothing", "R"),
    ("the chain is cut. axis:W", "W"),
    ("no tag here", None),
    ("axis:X unknown", None),
    ("preaxis:R glued", None),
])
def test_concern_axis_reads_the_token(body, axis):
    assert plan.concern_axis(body) == axis


def test_binding_is_fixed_at_the_first_submit(sc, monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    path = sc.write(skeleton_text())
    assert sc.submit(path)["marker"] == "PLAN-READY"
    assert sc.state().risk_planning is True
    monkeypatch.setenv(RISK_PLANNING_ENV, "0")
    assert sc.submit(path)["ok"] is True
    assert sc.state().risk_planning is True


def test_an_unbound_binding_is_not_changed_by_a_later_env(sc, monkeypatch):
    path = sc.write(skeleton_text())
    sc.submit(path)
    assert sc.state().risk_planning is False
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    sc.submit(path)
    assert sc.state().risk_planning is False


def test_bound_session_validates_a_skeleton_and_refuses_an_orphan(sc, monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    d = sc.submit(sc.write(skeleton_text(base_stage=1)))
    assert d["ok"] is False and d["action"] == "fix_plan"
    assert any("not on the chain" in p for p in d["data"]["problems"])


def test_bound_skeleton_takes_the_whole_plan_route(sc, monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    sc.submit(sc.write(skeleton_text()))
    state = sc.state()
    assert state.plan_phase == "skeleton"
    assert gates.skeleton_review_active(state) is True
    assert gates.pairwise_review_route(state) is False


def test_unbound_session_unchanged(sc):
    """No binding: the skeleton keys are inert, so a plan that would be refused as a skeleton
    (stage 2 is off the chain of base_stage 1) is submitted and graded as before, nothing
    freezes, and the review route is the one origin/main picks."""
    path = sc.write(skeleton_text(base_stage=1))
    d = sc.submit(path)
    assert d["marker"] == "PLAN-READY", d
    state = sc.state()
    assert (state.risk_planning, state.plan_phase) == (False, "")
    assert gates.skeleton_review_active(state) is False
    assert sc.review(path)["ok"] is True
    state = sc.state()
    assert state.skeleton_freeze is None and state.skeleton_returns == []
    assert gates.skeleton_return_blockers(state) == []


def test_resolver_is_read_through_the_config_module(sc, monkeypatch):
    monkeypatch.setattr(agentctl_config, "risk_planning_enabled", lambda *a, **k: True)
    sc.submit(sc.write(skeleton_text()))
    assert sc.state().risk_planning is True
