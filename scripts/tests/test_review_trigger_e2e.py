"""Review-cost triggers through `cli.main`: the route into DIAGNOSING and the two exits.

A counted review act crossing `review-cycle-acts` while the plan-review gate still has
blockers moves a PLAN_READY session into the overcome-difficulty cycle through the
`review_trigger` edge. The cycle leaves by `replan` in one of two ways: an agent override on
the complete record followed by a same-bytes replan (approve is then admitted on the
override), or a changed plan (approve then owes a fresh counted PASS).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agentctl import cli, cli_common, config, task_accumulator
from agentctl.config import Thresholds
from agentctl.state import Node, PlanReview, SessionState
from test_replan_autonomy_boundary import Eng, plan_text, thinker_review

ACTS_ROW = 2
ROUNDS_AXIS = "review_rounds_since_user_decision"


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_harness_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture(autouse=True)
def low_acts_row(tmp_path, monkeypatch):
    table = (tmp_path / "rows" / "config.md")
    table.parent.mkdir()
    table.write_text(
        config.CONFIG_MD.read_text(encoding="utf-8")
        + f"\n| `review-cycle-acts` | `{ACTS_ROW}` | x |\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_MD", table)


def reflect(eng: Eng, sid: str, tag: str = "r") -> None:
    for cmd, flags in (
        ("investigate", dict(localized_expectation=f"le-{tag}", localized_actual=f"la-{tag}",
                             hypothesis=[f"h1-{tag}", f"h2-{tag}"])),
        ("critique", dict(functional_ground=f"fg-{tag}", replanning_task=f"rt-{tag}",
                          failure_address="not_applicable", difference_to_remove=[f"d-{tag}"])),
        ("normalize", dict(factor=f"factor-{tag}", level="note")),
    ):
        d = eng.run(cmd, session=sid, **flags)
        assert d["ok"] is True, (cmd, d)


def agent_override(eng: Eng, sid: str, plan: str) -> dict:
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    return eng.run("plan_review", session=sid, verdict="override", reviewer="agent", target=plan,
                   plan_digest=digest, note="the review loop did not converge; reflection done")


def fired_session(eng: Eng, sid: str = "t1"):
    """A PLAN_READY session driven to the acts row by revise verdicts; returns (plan, directive)."""
    plan = eng.write(plan_text(), "draft.toml")
    assert eng.open(sid, plan)["marker"] == "PLAN-READY"
    first = thinker_review(eng, sid, plan, "revise")
    assert "review_trigger" not in (first.get("data") or {}), first
    assert eng.state(sid).node == Node.PLAN_READY.value
    return plan, thinker_review(eng, sid, plan, "revise")


def test_act_threshold_routes_to_diagnosing(eng):
    plan, d = fired_session(eng)
    state = eng.state("t1")
    assert state.node == Node.DIAGNOSING.value
    assert d["action"] == "declare"
    assert d["data"]["review_trigger"] == {
        "trigger": "cycle_acts", "value": ACTS_ROW, "threshold": ACTS_ROW, "stage": None}
    assert state.difficulty is not None
    assert state.difficulty.investigation is None and state.difficulty.critique is None
    assert state.effort_fires == []
    assert [e for e in state.history if e.get("event") == "review_trigger"]


def test_no_fire_below_the_row(eng):
    plan = eng.write(plan_text(), "draft.toml")
    eng.open("t1", plan)
    d = thinker_review(eng, "t1", plan, "revise")
    assert "review_trigger" not in (d.get("data") or {})
    assert eng.state("t1").node == Node.PLAN_READY.value


def test_a_passing_review_never_fires_the_trigger(eng):
    plan = eng.write(plan_text(), "draft.toml")
    eng.open("t1", plan)
    for _ in range(ACTS_ROW + 1):
        d = thinker_review(eng, "t1", plan, "pass")
        assert "review_trigger" not in (d.get("data") or {})
    assert eng.state("t1").node == Node.PLAN_READY.value


def test_override_on_an_incomplete_trigger_record_is_refused(eng):
    plan, _ = fired_session(eng)
    d = agent_override(eng, "t1", plan)
    assert d["ok"] is False
    assert eng.state("t1").plan_review.verdict == "revise"


def test_override_branch_admits_approve_on_the_recorded_override(eng):
    plan, _ = fired_session(eng)
    eng.run("declare", session="t1", expected="e", actual="a", mismatch="m")
    reflect(eng, "t1")
    override = agent_override(eng, "t1", plan)
    assert override["ok"] is True, override
    before = eng.state("t1")
    replanned = eng.run("replan", session="t1", plan=plan)
    assert replanned["ok"] is True, replanned
    assert replanned["data"]["review_trigger_exit"] == "approve is admitted on the recorded override"
    after = eng.state("t1")
    assert after.node == Node.PLAN_READY.value
    assert after.review_trigger_fire is None
    assert after.review_cycle_acts == before.review_cycle_acts
    assert after.plan_review.verdict == "override"
    assert eng.run("approve", session="t1", by="user")["ok"] is True
    closed = eng.state("t1")
    assert (closed.review_cycle_open, closed.review_cycle_acts, closed.review_stage_streaks) == (False, 0, {})


def test_changed_plan_branch_owes_a_fresh_counted_pass(eng):
    plan, _ = fired_session(eng)
    eng.run("declare", session="t1", expected="e", actual="a", mismatch="m")
    reflect(eng, "t1")
    changed = eng.write(plan_text(stage3=True, revised=True), "changed.toml")
    replanned = eng.run("replan", session="t1", plan=changed)
    assert replanned["ok"] is True, replanned
    assert replanned["data"]["review_trigger_exit"] == cli._TRIGGER_EXIT_FRESH_PASS
    state = eng.state("t1")
    assert state.node == Node.PLAN_READY.value
    assert state.review_trigger_fire is None
    assert state.review_cycle_acts == 0
    refused = eng.run("approve", session="t1", by="user")
    assert refused["ok"] is False
    assert thinker_review(eng, "t1", changed, "pass")["ok"] is True
    assert eng.run("approve", session="t1", by="user")["ok"] is True


def test_trigger_cycle_override_is_refused_at_the_review_round_backstop(eng):
    plan, _ = fired_session(eng)
    eng.run("declare", session="t1", expected="e", actual="a", mismatch="m")
    reflect(eng, "t1")
    backstop = Thresholds().review_round_backstop()
    current = task_accumulator.get("task-t1")["per_axis_totals"].get(ROUNDS_AXIS, 0)
    task_accumulator.add("task-t1", ROUNDS_AXIS, backstop - current, session_id="seed", now=None)
    refused = agent_override(eng, "t1", plan)
    assert refused["ok"] is False
    assert eng.state("t1").plan_review.verdict == "revise"


def test_a_new_sha_submission_resets_acts_and_a_same_sha_resubmit_does_not(eng):
    plan = eng.write(plan_text(), "draft.toml")
    eng.open("t1", plan)
    thinker_review(eng, "t1", plan, "revise")
    assert eng.state("t1").review_cycle_acts == 1
    eng.run("submit_plan", session="t1", plan=plan)
    assert eng.state("t1").review_cycle_acts == 1
    other = eng.write(plan_text(stage3=True), "other.toml")
    eng.run("submit_plan", session="t1", plan=other)
    state = eng.state("t1")
    assert state.node == Node.PLAN_READY.value
    assert state.review_cycle_acts == 0


def test_no_refire_on_the_same_evidence_after_the_override_exit(eng):
    plan, _ = fired_session(eng)
    eng.run("declare", session="t1", expected="e", actual="a", mismatch="m")
    reflect(eng, "t1")
    agent_override(eng, "t1", plan)
    eng.run("replan", session="t1", plan=plan)
    assert eng.state("t1").node == Node.PLAN_READY.value
    d = eng.run("approve", session="t1", by="user")
    assert d["ok"] is True
    assert eng.state("t1").node == Node.APPROVED.value


def reflected_session(eng: Eng, sid: str = "t1") -> str:
    plan, _ = fired_session(eng, sid)
    eng.run("declare", session=sid, expected="e", actual="a", mismatch="m")
    reflect(eng, sid)
    return plan


def test_a_fire_at_the_difficulty_depth_cap_keeps_the_review_and_spends_the_evidence(eng, monkeypatch):
    monkeypatch.setattr(cli_common, "_difficulty_level_cap", lambda: 0)
    plan = eng.write(plan_text(), "draft.toml")
    eng.open("t1", plan)
    thinker_review(eng, "t1", plan, "revise")
    capped = thinker_review(eng, "t1", plan, "revise")
    assert capped["ok"] is False and capped["action"] == "escalate"
    assert capped["data"]["difficulty_level_cap"] == 0
    state = eng.state("t1")
    assert state.node == Node.PLAN_READY.value
    assert state.review_trigger_fire is None and state.difficulty is None
    assert state.review_cycle_acts == ACTS_ROW
    assert state.review_trigger_spent == {"cycle_acts": ACTS_ROW}
    again = thinker_review(eng, "t1", plan, "revise")
    assert "review_trigger" not in (again.get("data") or {})
    assert eng.state("t1").review_cycle_acts == ACTS_ROW + 1


def test_the_changed_plan_exit_drops_the_agent_override(eng):
    plan = reflected_session(eng)
    assert agent_override(eng, "t1", plan)["ok"] is True
    assert eng.state("t1").plan_review.verdict == "override"
    changed = eng.write(plan_text(stage3=True, revised=True), "changed.toml")
    replanned = eng.run("replan", session="t1", plan=changed)
    assert replanned["ok"] is True, replanned
    assert replanned["data"]["review_trigger_exit"] == cli._TRIGGER_EXIT_FRESH_PASS
    assert "agent_review_override" not in replanned["data"]
    state = eng.state("t1")
    assert state.plan_review is None or state.plan_review.verdict != "override"
    assert eng.run("approve", session="t1", by="user")["ok"] is False
    assert thinker_review(eng, "t1", changed, "pass")["ok"] is True
    assert eng.run("approve", session="t1", by="user")["ok"] is True


def test_drop_agent_overrides_removes_every_agent_override_and_keeps_the_rest():
    def record(verdict, reviewer):
        return PlanReview(plan_path="p.toml", verdict=verdict, reviewer=reviewer, note="n")

    state = SessionState(session_id="unit", task_id="task-unit")
    state.plan_review = record("override", "agent")
    state.plan_stage_reviews = {"stage:1": record("override", " Agent "), "stage:2": record("override", "user"),
                                "stage:3": record("pass", "thinker")}
    state.plan_pair_reviews = {"unit:a": record("override", "agent"), "unit:b": record("pass", "thinker")}
    cli._drop_agent_overrides(state)
    assert state.plan_review is None
    assert sorted(state.plan_stage_reviews) == ["stage:2", "stage:3"]
    assert sorted(state.plan_pair_reviews) == ["unit:b"]


def test_the_trigger_record_admits_the_override_while_the_round_release_is_live(eng):
    plan = reflected_session(eng)
    state = eng.state("t1")
    state.plan_review_rounds = 99
    eng.store.save(state)
    assert cli._plan_review_release_active(eng.state("t1")) is True
    admitted = agent_override(eng, "t1", plan)
    assert admitted["ok"] is True, admitted
    assert eng.state("t1").plan_review.verdict == "override"
