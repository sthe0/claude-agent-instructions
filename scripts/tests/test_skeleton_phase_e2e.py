"""Risk-shaped planning, phase A, through `cli.main`: freeze, counted return, and its clearing.

A counted whole-plan pass freezes the skeleton's structural digests. A resubmission that moves
one of them, or a blocking `axis:R` concern after the freeze, opens a counted return; the
return stays open (a plan-review blocker) until a skeleton pass whose note names its id.
"""
from __future__ import annotations

import pytest

from agentctl import gates, plan
from agentctl.config import RISK_PLANNING_ENV
from risk_planning_scenario import Scenario, skeleton_text

RETITLE = ('title = "Add tests"', 'title = "Add more tests"')
BET = ('depends_on = []\n', 'depends_on = []\nverify_command = "true"\n')
AXIS_R = "blocking: axis:R stage 1 feeds nothing the goal needs"


@pytest.fixture(autouse=True)
def _bound(monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture
def sc(tmp_path):
    return Scenario(tmp_path)


def frozen(sc: Scenario, text: str | None = None) -> str:
    path = sc.write(text or skeleton_text(), "skeleton.toml")
    assert sc.submit(path)["marker"] == "PLAN-READY"
    assert sc.review(path)["ok"] is True
    assert sc.state().skeleton_freeze is not None
    return path


def blockers(sc: Scenario, path: str) -> list[str]:
    return gates.plan_review_blockers(sc.state(), path)


def test_a_counted_pass_freezes_the_digests(sc):
    path = frozen(sc)
    freeze = sc.state().skeleton_freeze
    assert freeze["digests"] == plan.skeleton_digests(plan.load_plan(path))
    assert len(freeze["plan_sha256"]) == 64
    assert sc.state().skeleton_returns == []
    assert blockers(sc, path) == []


def test_a_revise_does_not_freeze(sc):
    path = sc.write(skeleton_text())
    sc.submit(path)
    sc.review(path, "revise", concerns=("blocking: axis:V the controls are weak",))
    assert sc.state().skeleton_freeze is None


def test_an_unattested_pass_does_not_freeze(sc):
    path = sc.write(skeleton_text())
    sc.submit(path)
    d = sc.eng.run("plan_review", session="sk1", verdict="pass", reviewer="thinker", target=path)
    state = sc.state()
    assert state.skeleton_freeze is None, d


def test_a_bet_edit_after_the_freeze_keeps_it(sc):
    frozen(sc)
    edited = sc.write(skeleton_text(edits=(BET,)), "edited.toml")
    d = sc.submit(edited)
    assert "skeleton_return" not in d["data"]
    state = sc.state()
    assert state.skeleton_freeze is not None and state.skeleton_returns == []


def test_a_structural_edit_after_the_freeze_opens_a_return(sc):
    frozen(sc)
    edited = sc.write(skeleton_text(edits=(RETITLE,)), "edited.toml")
    d = sc.submit(edited)
    record = d["data"]["skeleton_return"]
    assert record["id"] == "R1" and record["changed"] == ["stage:2:title"]
    state = sc.state()
    assert state.skeleton_freeze is None
    assert [r["id"] for r in state.skeleton_returns] == ["R1"]
    assert any("skeleton return R1 is open" in b for b in blockers(sc, edited))


def test_a_pass_that_does_not_name_the_return_leaves_it_open(sc):
    frozen(sc)
    edited = sc.write(skeleton_text(edits=(RETITLE,)), "edited.toml")
    sc.submit(edited)
    d = sc.review(edited, note="the chain still reaches the goal")
    state = sc.state()
    assert state.skeleton_freeze is None
    assert state.skeleton_returns[0]["cleared"] is False
    assert d["ok"] is False
    assert any("R1" in b for b in d["data"]["blockers"])


def test_a_pass_naming_the_return_clears_it_and_refreezes(sc):
    frozen(sc)
    edited = sc.write(skeleton_text(edits=(RETITLE,)), "edited.toml")
    sc.submit(edited)
    d = sc.review(edited, note="R1: the retitle changes nothing the chain relies on")
    state = sc.state()
    assert d["ok"] is True, d
    assert state.skeleton_returns[0]["cleared"] is True
    assert state.skeleton_freeze["digests"] == plan.skeleton_digests(plan.load_plan(edited))
    assert blockers(sc, edited) == []


def test_a_note_naming_another_return_does_not_clear_this_one(sc):
    frozen(sc)
    edited = sc.write(skeleton_text(edits=(RETITLE,)), "edited.toml")
    sc.submit(edited)
    sc.review(edited, note="R10 is not it")
    assert sc.state().skeleton_returns[0]["cleared"] is False


def test_a_second_return_gets_the_next_id(sc):
    frozen(sc)
    first = sc.write(skeleton_text(edits=(RETITLE,)), "first.toml")
    sc.submit(first)
    sc.review(first, note="clears R1")
    second = sc.write(skeleton_text(edits=(('title = "Scaffold module"', 'title = "Scaffold the module"'),)),
                      "second.toml")
    d = sc.submit(second)
    assert d["data"]["skeleton_return"]["id"] == "R2"
    assert [r["cleared"] for r in sc.state().skeleton_returns] == [True, False]


def test_a_blocking_axis_r_concern_after_the_freeze_opens_a_return(sc):
    path = frozen(sc)
    d = sc.review(path, "revise", concerns=(AXIS_R,))
    state = sc.state()
    assert d["data"]["skeleton_return"]["id"] == "R1"
    assert state.skeleton_freeze is None
    assert state.skeleton_returns[0]["cause"].startswith("blocking axis:R")


@pytest.mark.parametrize("concern", [
    "blocking: axis:V the controls are weak",
    "note: axis:R a note-severity remark",
    "blocking: a concern with no axis tag",
])
def test_other_concerns_after_the_freeze_open_no_return(sc, concern):
    path = frozen(sc)
    sc.review(path, "revise", concerns=(concern,))
    state = sc.state()
    assert state.skeleton_freeze is not None and state.skeleton_returns == []


def test_a_return_cleared_after_an_axis_r_concern(sc):
    path = frozen(sc)
    sc.review(path, "revise", concerns=(AXIS_R,))
    d = sc.review(path, note="R1 addressed: stage 1 now feeds stage 2")
    assert d["ok"] is True, d
    state = sc.state()
    assert state.skeleton_returns[0]["cleared"] is True and state.skeleton_freeze is not None


def test_an_orphan_skeleton_is_refused_before_review(sc):
    d = sc.submit(sc.write(skeleton_text(base_stage=1)))
    assert d["action"] == "fix_plan" and sc.state().skeleton_freeze is None


def test_plan_render_skeleton_shows_the_frozen_fields(sc):
    path = sc.write(skeleton_text())
    d = sc.eng.run("plan_render", plan=path, skeleton=True)
    md = d["detail"]
    assert d["ok"] is True
    for needle in ("axis R", "Base stage", "alternatives".capitalize(), "Stage 1: Scaffold module",
                   "Stage 2: Add tests", "depends_on: 1", "tests/test_mod.py"):
        assert needle in md, needle


def test_plan_render_skeleton_refuses_a_plan_that_is_not_one(sc):
    path = sc.write(skeleton_text().replace('phase = "skeleton"\n', "", 1))
    d = sc.eng.run("plan_render", plan=path, skeleton=True)
    assert d["ok"] is False and "phase" in d["detail"]


def test_plan_render_without_the_flag_is_unchanged(sc):
    path = sc.write(skeleton_text())
    d = sc.eng.run("plan_render", plan=path)
    assert d["ok"] is True and "Skeleton review" not in d["detail"]
