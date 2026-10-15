"""Review-budget exhaustion: the reflection exit, the shared self-decision predicate and the
two since-user-decision backstops.

When the plan-review round budget is exhausted the release message names the reflection exit
(an overcome-difficulty cycle) first, a fresh whole-plan thinker pass second and the override
last. An agent-authored override is admitted only with a complete, fresh, normalized difficulty
record declared after the release event, and that record is then consumed. Fire-acknowledge,
renegotiation-continue and review-override share one predicate; two since-user-decision
backstops stop the agent from taking any of the three once it has already spent that many
rounds or replans without the user's decision.

Every test whose name appears in rx_mutation_control.py is killed by one named wrong version
of the production line (`python3 scripts/tests/rx_mutation_control.py --list`).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl import cli, effort, gates, task_accumulator
from agentctl.config import Thresholds
from agentctl.state import (
    Critique, Declaration, Difficulty, Investigation, Normalization, PlanReview, SessionState,
)
from test_replan_autonomy_boundary import (
    OBSERVATIONS, Eng, action, ack, agent_session, approved_order, at_ceiling, ceiling_refusal,
    plan_text, release_review_rounds, renegotiate, replans_fire, thinker_review,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
RX_SCRIPT = SCRIPTS_DIR / "tests" / "rx_mutation_control.py"
ARMING = REPO_ROOT / "docs" / "operations" / "review-backstop-arming.json"

REPLANS_AXIS = "replans_since_user_decision"
ROUNDS_AXIS = "review_rounds_since_user_decision"
BOTH_AXES = (REPLANS_AXIS, ROUNDS_AXIS)


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_harness_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _subst(**kw) -> SessionState:
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_path="/plan.toml", plan_verified=True, **kw)


def _revise() -> PlanReview:
    return PlanReview("/plan.toml", "revise", "thinker")


def total(sid: str, axis: str) -> int:
    return task_accumulator.get(f"task-{sid}")["per_axis_totals"].get(axis, 0)


def set_total(sid: str, axis: str, n: int) -> None:
    current = total(sid, axis)
    assert current <= n, (axis, current, n)
    if n > current:
        task_accumulator.add(f"task-{sid}", axis, n - current, session_id="seed", now=None)


def reflect(eng: Eng, sid: str, tag: str = "r", *, difference: bool = True,
            normalize: bool = True) -> None:
    """A planning-difficulty record run through the engine's own commands."""
    steps = [
        ("declare", dict(expected=f"e-{tag}", actual=f"a-{tag}", mismatch=f"m-{tag}")),
        ("investigate", dict(localized_expectation=f"le-{tag}", localized_actual=f"la-{tag}",
                             hypothesis=[f"h1-{tag}", f"h2-{tag}"])),
        ("critique", dict(functional_ground=f"fg-{tag}", replanning_task=f"rt-{tag}",
                          failure_address="not_applicable",
                          difference_to_remove=[f"d-{tag}"] if difference else None)),
    ]
    if normalize:
        steps.append(("normalize", dict(factor=f"factor-{tag}", level="note")))
    for cmd, flags in steps:
        d = eng.run(cmd, session=sid, **flags)
        assert d["ok"] is True, (cmd, d)


def released_order(eng: Eng, sid: str = "o1") -> str:
    plan = approved_order(eng)
    eng.open(sid, plan)
    release_review_rounds(eng, sid)
    return plan


def agent_override(eng: Eng, sid: str, plan: str) -> dict:
    return thinker_review(eng, sid, plan, "override", "agent")


def complete_record(tag: str = "pre") -> Difficulty:
    return Difficulty(
        declaration=Declaration(expected=f"e-{tag}", actual=f"a-{tag}", mismatch=f"m-{tag}"),
        investigation=Investigation(localized_expectation=f"le-{tag}", localized_actual=f"la-{tag}",
                                    hypotheses=[f"h1-{tag}", f"h2-{tag}"]),
        critique=Critique(functional_ground=f"fg-{tag}", replanning_task=f"rt-{tag}",
                          differences_to_remove=[f"d-{tag}"], failure_address="not_applicable"),
        normalization=Normalization(factor=f"factor-{tag}", level="note"),
    )


# --- the release message names the reflection exit first ---------------------------------


def test_whole_plan_release_message_orders_reflection_then_fresh_pass_then_override(gate_on):
    s = _subst(plan_review_rounds=5, plan_review=_revise())
    message = gates.plan_review_blockers(s, s.plan_path)[0]
    assert (message.index("the reflection exit")
            < message.index("fresh whole-plan thinker review")
            < message.index("plan-review --verdict override"))
    assert "the reflection exit's record" in message


def test_post_pass_release_message_orders_reflection_before_override(gate_on):
    s = _subst(plan_review_rounds=5, plan_review=_revise(),
               plan_review_passes={"": PlanReview("/plan.toml", "pass", "thinker",
                                                  plan_sha256="deadbeef")})
    message = gates.plan_review_blockers(s, s.plan_path)[0]
    assert message.index("(1) the reflection exit") < message.index("plan-review --verdict override")
    assert "the reflection exit's record" in message


def test_cross_axis_release_message_orders_reflection_before_override(gate_on):
    s = _subst(plan_review_rounds=3, code_review_rounds=2, plan_review=_revise())
    message = gates.plan_review_blockers(s, s.plan_path)[0]
    assert "COMBINED cross-axis friction ceiling" in message
    assert message.index("the reflection exit") < message.index("plan-review --verdict override")


# --- an agent-authored override needs the reflection exit's record ------------------------


def test_agent_override_refused_without_any_difficulty_record(eng):
    plan = released_order(eng)
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "no complete planning-difficulty record" in d["detail"]
    assert eng.state("o1").plan_review is None


def test_agent_override_refused_for_a_record_declared_before_the_release(eng):
    plan = approved_order(eng)
    eng.open("o1", plan)
    state = eng.state("o1")
    state.difficulty = complete_record()
    eng.store.save(state)
    release_review_rounds(eng, "o1")
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "predates the release" in d["detail"]
    assert eng.state("o1").plan_review is None


def test_agent_override_consumes_its_difficulty_record(eng):
    plan = released_order(eng)
    reflect(eng, "o1", "r1")
    first = cli._difficulty_id(eng.state("o1").difficulty)
    assert agent_override(eng, "o1", plan)["ok"] is True
    assert first in eng.state("o1").agent_ack_difficulty_ids
    again = agent_override(eng, "o1", plan)
    assert again["ok"] is False
    assert "already discharged" in again["detail"]
    reflect(eng, "o1", "r2")
    second = cli._difficulty_id(eng.state("o1").difficulty)
    assert second != first
    assert agent_override(eng, "o1", plan)["ok"] is True
    assert eng.state("o1").agent_ack_difficulty_ids[-2:] == [first, second]


def test_agent_override_needs_a_real_normalization(eng):
    plan = released_order(eng)
    reflect(eng, "o1", normalize=False)
    refused = agent_override(eng, "o1", plan)
    assert refused["ok"] is False
    assert "normalization" in refused["detail"]

    state = eng.state("o1")
    state.difficulty.normalization = None
    state.difficulty.normalization_waiver = "one-off"
    eng.store.save(state)
    waived = agent_override(eng, "o1", plan)
    assert waived["ok"] is False
    assert "a waiver does not discharge" in waived["detail"]

    state = eng.state("o1")
    state.difficulty.normalization_waiver = None
    state.difficulty.normalization = Normalization(factor="   ")
    eng.store.save(state)
    blank = agent_override(eng, "o1", plan)
    assert blank["ok"] is False
    assert "normalization" in blank["detail"]
    assert eng.state("o1").plan_review is None

    assert eng.run("normalize", session="o1", factor="real-factor", level="note")["ok"] is True
    assert agent_override(eng, "o1", plan)["ok"] is True


def test_agent_override_needs_a_difference_to_remove(eng):
    plan = released_order(eng)
    reflect(eng, "o1", difference=False)
    refused = agent_override(eng, "o1", plan)
    assert refused["ok"] is False
    assert "differences_to_remove" in refused["detail"]


def test_override_difficulty_id_is_disclosed_on_the_presented_plan(eng, tmp_path):
    plan = approved_order(eng)
    eng.open("o1", plan)
    rendering = tmp_path / "essence.md"
    rendering.write_text("Summary of the plan.", encoding="utf-8")
    refusal = eng.run("present_plan", session="o1", kind="essence", rendering_file=str(rendering))
    rendering.write_text("Summary of the plan.\n" + refusal["data"]["grants_block"], encoding="utf-8")
    assert eng.run("present_plan", session="o1", kind="essence",
                   rendering_file=str(rendering))["ok"] is True
    release_review_rounds(eng, "o1")
    reflect(eng, "o1")
    expected = cli._difficulty_id(eng.state("o1").difficulty)
    assert agent_override(eng, "o1", plan)["ok"] is True
    after = eng.run("present_plan", session="o1", kind="essence", rendering_file=str(rendering))
    assert after["data"]["agent_review_override"]["difficulty_id"] == expected


def test_declaring_under_the_release_supersedes_the_previous_record(eng):
    released_order(eng)
    reflect(eng, "o1", "r1")
    assert eng.run("declare", session="o1", expected="e-r2", actual="a-r2", mismatch="m-r2")["ok"] is True
    state = eng.state("o1")
    assert state.difficulty.declaration.expected == "e-r2"
    assert state.difficulty.investigation is None
    assert any(e.get("event") == "difficulty_superseded" for e in state.history)


# --- the since-user-decision backstops -----------------------------------------------------


CROSSED = "agent_backstop_crossed"
WIDENING = ("brand_new_module.py",)
SPEND_ESTIMATE = 10.0


def arm(eng: Eng, sid: str, *, spent: float = 0.0) -> None:
    """Open the session's effort window as approval would: an estimate on both ratio scales,
    a zero baseline and `spent` dollars already booked. A session that has not been
    approved yet has none, so nothing bounds a backstop crossing there."""
    state = eng.state(sid)
    state.effort_estimate = {effort.SCALE_SPEND: SPEND_ESTIMATE, effort.SCALE_WALL_CLOCK: 1000.0}
    state.effort_baseline = {scale: 0.0 for scale in effort.SCALE_ORDER}
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = spent
    eng.store.save(state)


def crossings(eng: Eng, sid: str) -> list[dict]:
    return [e for e in eng.state(sid).history if e.get("event") == CROSSED]


def assert_not_crossed(eng: Eng, sid: str, directive: dict) -> None:
    assert not crossings(eng, sid)
    assert CROSSED not in (directive.get("data") or {})


def widened_order(eng: Eng, sid: str = "o1") -> str:
    """An order session whose plan adds a file the user never approved, past the round release."""
    approved_order(eng)
    plan = eng.write(plan_text(extra_outputs=WIDENING), "widened.toml")
    eng.open(sid, plan)
    release_review_rounds(eng, sid)
    return plan


def test_agent_override_refused_at_the_review_round_backstop(eng):
    plan = widened_order(eng)
    arm(eng, "o1")
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "backstop" in d["detail"]
    assert eng.state("o1").plan_review is None
    assert_not_crossed(eng, "o1", d)


def test_agent_override_admitted_just_below_the_review_round_backstop(eng):
    plan = released_order(eng)
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop() - 1)
    assert agent_override(eng, "o1", plan)["ok"] is True


def test_agent_fire_acknowledge_refused_at_the_replan_backstop(eng):
    replans_fire(eng)
    state = eng.state("a1")
    state.plan_path = eng.write(plan_text(extra_outputs=WIDENING), "widened.toml")
    eng.store.save(state)
    eng.diagnose("a1")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    refused = ack(eng)
    assert refused["ok"] is False
    assert "backstop" in refused["detail"]
    assert eng.state("a1").effort_fires[-1].get("ack") is None
    assert_not_crossed(eng, "a1", refused)


def test_agent_fire_acknowledge_admitted_just_below_the_replan_backstop(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop() - 1)
    assert ack(eng)["ok"] is True


def test_agent_renegotiation_continue_refused_at_the_replan_backstop(eng):
    at_ceiling(eng)
    widened = eng.write(plan_text(stage3=True, revised=True, extra_outputs=WIDENING))
    ceiling_refusal(eng, "a1", widened)
    eng.diagnose("a1", "b")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    refused = renegotiate(eng, "a1", widened)
    assert refused["ok"] is False
    assert "backstop" in refused["detail"]
    assert not eng.state("a1").renegotiations
    assert_not_crossed(eng, "a1", refused)


def test_agent_renegotiation_continue_admitted_just_below_the_replan_backstop(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop() - 1)
    assert renegotiate(eng, "a1", replanned)["ok"] is True


# --- past the backstop, inside the autonomy boundary ----------------------------------------


def cross_by_fire_acknowledge(eng: Eng) -> dict:
    replans_fire(eng)
    eng.diagnose("a1")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    return ack(eng)


def cross_by_renegotiation(eng: Eng) -> dict:
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    return renegotiate(eng, "a1", replanned)


def test_agent_override_admitted_past_the_review_round_backstop_inside_the_boundary(eng):
    plan = released_order(eng)
    arm(eng, "o1")
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is True, (
        f"BEHAVIOUR: an in-boundary agent override at the review-round backstop was refused: "
        f"{d['detail']}")
    assert eng.state("o1").plan_review.verdict == "override"
    assert [c["decision"] for c in crossings(eng, "o1")] == ["plan_review_override"]
    disclosed = d["data"][CROSSED]
    assert (disclosed["decision"], disclosed["axis"]) == (
        "plan_review_override", ROUNDS_AXIS), f"BEHAVIOUR: {d['data']}"


def test_agent_fire_acknowledge_admitted_past_the_replan_backstop_inside_the_boundary(eng):
    d = cross_by_fire_acknowledge(eng)
    assert d["ok"] is True, (
        f"BEHAVIOUR: an in-boundary agent fire acknowledgement at the replan backstop was "
        f"refused: {d['detail']}")
    assert eng.state("a1").effort_fires[-1]["ack"]["by"] == "agent"
    assert [c["decision"] for c in crossings(eng, "a1")] == ["fire_acknowledge"]


def test_agent_renegotiation_continue_admitted_past_the_replan_backstop_inside_the_boundary(eng):
    d = cross_by_renegotiation(eng)
    assert d["ok"] is True, (
        f"BEHAVIOUR: an in-boundary agent renegotiation at the replan backstop was refused: "
        f"{d['detail']}")
    assert eng.state("a1").renegotiations[-1]["by"] == "agent"
    assert [c["decision"] for c in crossings(eng, "a1")] == ["renegotiation_continue"]
    disclosed = d["data"][CROSSED]
    assert (disclosed["decision"], disclosed["axis"]) == (
        "renegotiation_continue", REPLANS_AXIS), f"BEHAVIOUR: {d['data']}"


def test_backstop_crossing_is_recorded_once_with_its_axis_count_and_backstop(eng):
    d = cross_by_fire_acknowledge(eng)
    backstop = Thresholds().effort_replan_backstop()
    recorded = crossings(eng, "a1")
    assert len(recorded) == 1, (
        f"BEHAVIOUR: expected exactly one {CROSSED} event, found {len(recorded)}: {d['detail']}")
    event = recorded[0]
    assert (event["decision"], event["axis"], event["spent"], event["backstop"]) == (
        "fire_acknowledge", REPLANS_AXIS, backstop, backstop), f"BEHAVIOUR: {event}"
    assert d["data"][CROSSED]["decision"] == "fire_acknowledge", f"BEHAVIOUR: {d['data']}"


def test_backstop_crossing_is_disclosed_at_verify_final_and_resolve(eng):
    d = cross_by_fire_acknowledge(eng)
    assert d["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {d['detail']}"
    replanned = eng.run("replan", session="a1", plan=eng.write(plan_text(stage3=True, revised=True)))
    assert replanned["ok"] is True, replanned
    approved = eng.run("approve", session="a1", by="agent")
    assert approved["ok"] is True, approved
    eng.execute("a1")
    observations = (*OBSERVATIONS, "the CI config is on disk and its lint step ran and exited zero")
    for index, observation in enumerate(observations):
        recorded = eng.run("record_result", session="a1", status="passed", actual="ok",
                           control="reviewed: ok", observation=observation)
        assert recorded["ok"] is True, recorded
        if index + 1 < len(observations):
            advanced = eng.run("next_stage", session="a1")
            assert advanced["ok"] is True, advanced
    final = eng.run("verify_final", session="a1")
    assert final["ok"] is True, final
    assert [c["decision"] for c in final["data"]["agent_backstop_crossings"]] == [
        "fire_acknowledge"], f"BEHAVIOUR: {final['data']}"
    assert "crossed a since-user backstop" in final["detail"], f"BEHAVIOUR: {final['detail']}"
    for phase in ("searched", "skipped"):
        eng.run("plugin_record", session="a1", plugin="experience", phase=phase,
                note="test fixture, nothing to record")
    resolved = eng.run("resolve", session="a1", by="user", quality=5, quality_by="user-confirmed")
    assert resolved["ok"] is True, resolved
    assert [c["decision"] for c in resolved["data"]["agent_backstop_crossings"]] == [
        "fire_acknowledge"], f"BEHAVIOUR: {resolved['data']}"
    assert "crossed a since-user backstop" in resolved["detail"], f"BEHAVIOUR: {resolved['detail']}"


def reach_resolution(eng: Eng, sid: str) -> dict:
    """Carry an executing session through every stage and its final verification, to the gate
    `resolve` is asked at; returns the verify-final Directive."""
    for index, observation in enumerate(OBSERVATIONS):
        recorded = eng.run("record_result", session=sid, status="passed", actual="ok",
                           control="reviewed: ok", observation=observation)
        assert recorded["ok"] is True, recorded
        if index + 1 < len(OBSERVATIONS):
            assert eng.run("next_stage", session=sid)["ok"] is True
    final = eng.run("verify_final", session=sid)
    assert final["ok"] is True, final
    for phase in ("searched", "skipped"):
        eng.run("plugin_record", session=sid, plugin="experience", phase=phase,
                note="test fixture, nothing to record")
    return final


def finish_session(eng: Eng, sid: str) -> tuple[dict, dict]:
    """Carry an executing session through every stage, its final verification and resolution."""
    final = reach_resolution(eng, sid)
    resolved = eng.run("resolve", session=sid, by="user", quality=5, quality_by="user-confirmed")
    assert resolved["ok"] is True, resolved
    return final, resolved


def assert_disclosed_once(final: dict, resolved: dict) -> None:
    for step in (final, resolved):
        assert [c["decision"] for c in step["data"]["agent_backstop_crossings"]] == [
            "fire_acknowledge"], f"BEHAVIOUR: {step['data']}"
        assert "crossed a since-user backstop" in step["detail"], f"BEHAVIOUR: {step['detail']}"


def test_backstop_crossing_is_disclosed_by_a_later_session_of_the_same_task(eng):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    assert action(eng.open("b1", plan, task="task-a1")) == "self_approve"
    assert eng.run("approve", session="b1", by="agent")["ok"] is True
    eng.execute("b1")
    assert not crossings(eng, "b1")
    final, resolved = finish_session(eng, "b1")
    assert_disclosed_once(final, resolved)


def test_backstop_crossing_is_disclosed_after_the_session_is_reset_and_a_user_decision(eng):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    task_accumulator.clear_since_user_decision("task-a1")
    reset = eng.run("reset", session="a1", task="task-a1", goal="g", done_criterion="dc", force=True)
    assert reset["ok"] is True, reset
    assert not crossings(eng, "a1")
    eng.run("classify", session="a1", architectural=True, files=5, changed_lines=200,
            wall_clock_min=60)
    eng.run("plan", session="a1")
    assert action(eng.run("submit_plan", session="a1", plan=plan)) == "self_approve"
    assert eng.run("approve", session="a1", by="agent")["ok"] is True
    eng.execute("a1")
    final, resolved = finish_session(eng, "a1")
    assert_disclosed_once(final, resolved)


def test_backstop_crossing_record_survives_the_accumulator_reset(eng):
    d = cross_by_renegotiation(eng)
    assert d["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {d['detail']}"
    task = eng.state("a1").task_id
    task_accumulator.reset(task)
    kept = task_accumulator.get(task)["agent_backstop_crossings"]
    assert [c["decision"] for c in kept] == ["renegotiation_continue"], (
        f"BEHAVIOUR: the accumulator reset dropped the crossing record: {kept}")


def test_resolve_clears_the_crossings_it_disclosed_so_a_later_session_of_the_task_starts_clean(eng):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    task = eng.state("a1").task_id
    assert [c["decision"] for c in task_accumulator.get(task)["agent_backstop_crossings"]] == [
        "fire_acknowledge"]
    for sid in ("b1", "c1"):
        assert action(eng.open(sid, plan, task=task)) == "self_approve"
        assert eng.run("approve", session=sid, by="agent")["ok"] is True
        eng.execute(sid)
        final, resolved = finish_session(eng, sid)
        if sid == "b1":
            assert_disclosed_once(final, resolved)
            continue
        for step in (final, resolved):
            assert step["data"]["agent_backstop_crossings"] == [], (
                f"BEHAVIOUR: an accepted crossing was disclosed again: {step['data']}")
            assert "crossed a since-user backstop" not in step["detail"], f"BEHAVIOUR: {step['detail']}"
    assert task_accumulator.get(task)["agent_backstop_crossings"] == []


def test_resolve_keeps_a_crossing_recorded_after_the_disclosure_snapshot(eng, monkeypatch):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    task = eng.state("a1").task_id
    assert action(eng.open("b1", plan, task=task)) == "self_approve"
    assert eng.run("approve", session="b1", by="agent")["ok"] is True
    eng.execute("b1")
    reach_resolution(eng, "b1")
    late = {"event": CROSSED, "decision": "renegotiation_continue", "axis": REPLANS_AXIS,
            "spent": 9, "backstop": 6, "session": "late-session", "ts": "late-moment"}
    disclose = cli._disclose_backstop_crossings

    def disclose_then_let_another_session_cross(state, data, detail):
        detail = disclose(state, data, detail)
        task_accumulator.record_crossing(task, late, session_id="late-session", now="late-moment")
        return detail

    monkeypatch.setattr(cli, "_disclose_backstop_crossings", disclose_then_let_another_session_cross)
    resolved = eng.run("resolve", session="b1", by="user", quality=5, quality_by="user-confirmed")
    assert resolved["ok"] is True, resolved
    assert [c["decision"] for c in resolved["data"]["agent_backstop_crossings"]] == [
        "fire_acknowledge"]
    kept = task_accumulator.get(task)["agent_backstop_crossings"]
    assert [(c["session"], c["decision"]) for c in kept] == [
        ("late-session", "renegotiation_continue")], (
        f"BEHAVIOUR: resolve cleared a crossing it never disclosed, or kept one it did: {kept}")


def test_a_refused_resolve_leaves_the_crossing_record_in_place(eng):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    task = eng.state("a1").task_id
    assert action(eng.open("b1", plan, task=task)) == "self_approve"
    assert eng.run("approve", session="b1", by="agent")["ok"] is True
    eng.execute("b1")
    reach_resolution(eng, "b1")
    refused = eng.run("resolve", session="b1", by="user")
    assert refused["ok"] is False, f"BEHAVIOUR: resolve without a rating was not refused: {refused}"
    kept = task_accumulator.get(task)["agent_backstop_crossings"]
    assert [c["decision"] for c in kept] == ["fire_acknowledge"], (
        f"BEHAVIOUR: a refused resolve cleared the crossing record: {kept}")
    resolved = eng.run("resolve", session="b1", by="user", quality=5, quality_by="user-confirmed")
    assert resolved["ok"] is True, resolved
    assert [c["decision"] for c in resolved["data"]["agent_backstop_crossings"]] == [
        "fire_acknowledge"], f"BEHAVIOUR: {resolved['data']}"
    assert task_accumulator.get(task)["agent_backstop_crossings"] == []


def test_resolve_survives_a_fault_clearing_the_crossing_record(eng, monkeypatch):
    crossed = cross_by_fire_acknowledge(eng)
    assert crossed["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {crossed['detail']}"
    plan = eng.state("a1").plan_path
    task = eng.state("a1").task_id
    assert action(eng.open("b1", plan, task=task)) == "self_approve"
    assert eng.run("approve", session="b1", by="agent")["ok"] is True
    eng.execute("b1")
    reach_resolution(eng, "b1")

    def failing_discard(*args, **kwargs):
        raise OSError("accumulator volume is read-only")

    monkeypatch.setattr(task_accumulator, "discard_crossings", failing_discard)
    resolved = eng.run("resolve", session="b1", by="user", quality=5, quality_by="user-confirmed")
    assert resolved["ok"] is True and resolved["marker"] == "COMPLETED", (
        f"BEHAVIOUR: a fault clearing the crossing record lost the resolution: {resolved}")
    assert eng.state("b1").node == "RESOLVED"
    data = resolved["data"]
    assert [c["decision"] for c in data["agent_backstop_crossings"]] == ["fire_acknowledge"]
    assert "crossed a since-user backstop" in resolved["detail"], f"BEHAVIOUR: {resolved['detail']}"
    assert data["agent_backstop_crossings_cleared"] is False, f"BEHAVIOUR: {data}"
    assert "read-only" in data["agent_backstop_crossings_clear_error"], f"BEHAVIOUR: {data}"
    assert "agent_backstop_crossings_clear_error" in resolved["detail"], f"BEHAVIOUR: {resolved['detail']}"


def test_backstop_crossing_is_disclosed_to_a_session_with_no_history_of_its_own(eng):
    d = cross_by_fire_acknowledge(eng)
    assert d["ok"] is True, f"BEHAVIOUR: the crossing was not admitted: {d['detail']}"
    session = eng.state("a1")
    assert [c["decision"] for c in cli._backstop_crossings(session)] == ["fire_acknowledge"]
    session.history = []
    assert [c["decision"] for c in cli._backstop_crossings(session)] == ["fire_acknowledge"], (
        "BEHAVIOUR: the task's own record was not read")


def test_backstop_refusal_outside_the_boundary_names_the_boundary_reasons(eng):
    plan = widened_order(eng)
    arm(eng, "o1")
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "outside the autonomy boundary" in d["detail"], f"BEHAVIOUR: {d['detail']}"
    assert "the effort bound does not hold" not in d["detail"], f"BEHAVIOUR: {d['detail']}"
    assert_not_crossed(eng, "o1", d)


def test_backstop_crossings_stop_once_spend_reaches_the_divergence_multiple(eng):
    plan = released_order(eng, "o1")
    multiple = Thresholds().effort_divergence_multiple()
    arm(eng, "o1", spent=SPEND_ESTIMATE * (multiple - 0.1))
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    below = agent_override(eng, "o1", plan)
    assert below["ok"] is True, (
        f"BEHAVIOUR: an override below the divergence multiple was refused: {below['detail']}")
    eng.open("o2", plan)
    release_review_rounds(eng, "o2")
    arm(eng, "o2", spent=SPEND_ESTIMATE * multiple)
    reflect(eng, "o2")
    set_total("o2", ROUNDS_AXIS, Thresholds().review_round_backstop())
    at = agent_override(eng, "o2", plan)
    assert at["ok"] is False
    assert "the effort bound does not hold" in at["detail"], f"BEHAVIOUR: {at['detail']}"
    assert "effort-divergence-multiple" in at["detail"], f"BEHAVIOUR: {at['detail']}"
    assert "outside the autonomy boundary" not in at["detail"], f"BEHAVIOUR: {at['detail']}"
    assert_not_crossed(eng, "o2", at)


def test_backstop_crossing_refused_when_no_estimate_is_armed(eng):
    plan = released_order(eng)
    state = eng.state("o1")
    state.effort_estimate = {}
    eng.store.save(state)
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "no armed spend or wall-clock estimate bounds the loop" in d["detail"], (
        f"BEHAVIOUR: {d['detail']}")
    assert eng.state("o1").plan_review is None
    assert_not_crossed(eng, "o1", d)


def test_backstop_crossing_refused_for_a_session_with_no_user_approved_version(eng, monkeypatch):
    plan = released_order(eng)
    arm(eng, "o1")
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    # The only real session that is inside the boundary yet has no ledgered version is a
    # `parent_norm` order, which needs an adopted parent handoff; the ledger lookup is what
    # that session differs by.
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: None)
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "no user-approved version of the order" in d["detail"], f"BEHAVIOUR: {d['detail']}"
    assert eng.state("o1").plan_review is None
    assert_not_crossed(eng, "o1", d)


def test_renegotiation_crossing_is_kept_when_a_later_gate_refuses_the_replan(eng, monkeypatch):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    with monkeypatch.context() as owed:
        owed.setattr(gates, "plan_review_blockers", lambda state, target: ["a review is owed"])
        refused = renegotiate(eng, "a1", replanned)
    assert refused["ok"] is False and refused["action"] == "plan_review", f"BEHAVIOUR: {refused}"
    assert refused["data"][CROSSED]["decision"] == "renegotiation_continue", f"BEHAVIOUR: {refused}"
    assert [c["decision"] for c in crossings(eng, "a1")] == ["renegotiation_continue"], (
        f"BEHAVIOUR: the crossing the refused replan earned was not saved: {refused}")
    assert eng.state("a1").renegotiations[-1]["by"] == "agent"
    plain = eng.run("replan", session="a1", plan=replanned)
    assert plain["ok"] is True, plain
    assert [c["decision"] for c in crossings(eng, "a1")] == ["renegotiation_continue"]


def test_agent_override_refused_past_the_backstop_with_an_open_spend_fire(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    eng.spend_fire("a1", plan)
    eng.open("o1", plan)
    release_review_rounds(eng, "o1")
    arm(eng, "o1")
    reflect(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert "backstop" in d["detail"]
    assert eng.state("o1").plan_review is None
    assert_not_crossed(eng, "o1", d)


def test_agent_fire_acknowledge_refused_past_the_backstop_when_the_plan_does_not_load(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop())
    Path(eng.state("a1").plan_path).unlink()
    refused = ack(eng)
    assert refused["ok"] is False
    assert "backstop" in refused["detail"]
    assert eng.state("a1").effort_fires[-1].get("ack") is None
    assert_not_crossed(eng, "a1", refused)


def test_crossing_inside_the_boundary_still_needs_the_reflection_exit(eng):
    plan = released_order(eng)
    arm(eng, "o1")
    set_total("o1", ROUNDS_AXIS, Thresholds().review_round_backstop())
    d = agent_override(eng, "o1", plan)
    assert d["ok"] is False
    assert eng.state("o1").plan_review is None
    assert_not_crossed(eng, "o1", d)


@pytest.mark.parametrize("case", [
    "override-without-release",
    "override-by-the-reviewer-it-overrides",
    "fire-acknowledge-continue",
    "fire-acknowledge-spend-scale",
    "fire-acknowledge-record-predates-fire",
    "renegotiation-without-ceiling-refusal",
    "renegotiation-record-predates-ceiling",
])
def test_crossing_inside_the_boundary_keeps_every_other_condition(eng, case):
    rounds, replans = Thresholds().review_round_backstop(), Thresholds().effort_replan_backstop()
    if case.startswith("override"):
        sid = "o1"
        if case == "override-without-release":
            plan = approved_order(eng)
            eng.open(sid, plan)
        else:
            plan = released_order(eng)
        arm(eng, sid)
        if case != "override-without-release":
            reflect(eng, sid)
        if case == "override-by-the-reviewer-it-overrides":
            assert thinker_review(eng, sid, plan, "revise", "agent")["ok"] is True
        set_total(sid, ROUNDS_AXIS, rounds)
        d = agent_override(eng, sid, plan)
    elif case.startswith("fire-acknowledge"):
        sid = "a1"
        if case == "fire-acknowledge-spend-scale":
            plan = approved_order(eng)
            agent_session(eng, sid, plan)
            eng.spend_fire(sid, plan)
        else:
            replans_fire(eng)
        eng.diagnose(sid)
        if case == "fire-acknowledge-record-predates-fire":
            state = eng.state(sid)
            state.effort_fires[-1]["difficulty_id_at_fire"] = cli._difficulty_id(state.difficulty)
            eng.store.save(state)
        set_total(sid, REPLANS_AXIS, replans)
        d = ack(eng, decision="continue" if case == "fire-acknowledge-continue" else "revise")
    else:
        sid = "a1"
        at_ceiling(eng)
        replanned = eng.write(plan_text(stage3=True, revised=True))
        if case == "renegotiation-record-predates-ceiling":
            ceiling_refusal(eng, sid, replanned)
        else:
            eng.diagnose(sid, "b")
        set_total(sid, REPLANS_AXIS, replans)
        d = renegotiate(eng, sid, replanned)
    assert d["ok"] is False, d
    assert_not_crossed(eng, sid, d)


def test_agent_decisions_neither_reset_the_order_effort_window_nor_raise_its_estimate(eng):
    replans_fire(eng)
    plan = str(eng.tmp / "approved.toml")
    eng.diagnose("a1")
    backstop = Thresholds().effort_replan_backstop()
    set_total("a1", REPLANS_AXIS, backstop)
    before = eng.ledger(plan)
    estimate = dict(eng.state("a1").effort_estimate)
    ack(eng)
    after = eng.ledger(plan)
    assert after["effort_since_user_approval"] == before["effort_since_user_approval"]
    assert [(r["by"], r.get("effort_estimate")) for r in after["records"]] == [
        (r["by"], r.get("effort_estimate")) for r in before["records"]]
    assert eng.state("a1").effort_estimate == estimate
    assert total("a1", REPLANS_AXIS) == backstop


def test_no_backstop_crossing_recorded_below_the_backstop(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    set_total("a1", REPLANS_AXIS, Thresholds().effort_replan_backstop() - 1)
    d = ack(eng)
    assert d["ok"] is True
    assert_not_crossed(eng, "a1", d)


def test_spend_and_wall_clock_fires_stay_the_users_with_a_complete_record(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    eng.spend_fire("a1", plan)
    eng.diagnose("a1")
    refused = ack(eng)
    assert refused["ok"] is False
    assert "owed to the user" in refused["detail"]


def test_backstops_sit_above_the_replan_ceiling_and_match_the_arming_record():
    thr = Thresholds()
    assert thr.effort_replan_backstop() > thr.effort_replan_absolute()
    assert thr.review_round_backstop() > thr.effort_replan_absolute()
    populations = json.loads(ARMING.read_text(encoding="utf-8"))["populations"]
    assert thr.effort_replan_backstop() == populations["replans"]["backstop"]
    assert thr.review_round_backstop() == populations["review_rounds"]["backstop"]


@pytest.mark.parametrize("rows", [
    {"effort-replan-absolute": "5"},
    {"effort-replan-absolute": "5", "effort-replan-backstop": "abc", "review-round-backstop": ""},
])
def test_a_missing_or_unparsable_backstop_falls_back_above_the_ceiling(rows):
    thr = Thresholds(rows)
    assert thr.effort_replan_backstop() == 6
    assert thr.review_round_backstop() == 6


# --- one predicate for the three agent decisions ---------------------------------------------


@pytest.fixture
def sentinel_predicate(monkeypatch):
    calls = []

    def refuse(state, anchor_id, **kw):
        calls.append(kw.get("event"))
        return "SENTINEL-REFUSAL"

    monkeypatch.setattr(cli, "_agent_self_decision_refusal", refuse)
    return calls


def test_fire_acknowledge_goes_through_the_shared_predicate(eng, sentinel_predicate):
    replans_fire(eng)
    eng.diagnose("a1")
    refused = ack(eng)
    assert refused["ok"] is False
    assert "SENTINEL-REFUSAL" in refused["detail"]
    assert sentinel_predicate == ["the fire"]


def test_renegotiation_goes_through_the_shared_predicate(eng, sentinel_predicate):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    refused = renegotiate(eng, "a1", replanned)
    assert refused["ok"] is False
    assert "SENTINEL-REFUSAL" in refused["detail"]
    assert sentinel_predicate and set(sentinel_predicate) == {"the ceiling event"}


def test_override_goes_through_the_shared_predicate(eng, sentinel_predicate):
    plan = released_order(eng)
    reflect(eng, "o1")
    refused = agent_override(eng, "o1", plan)
    assert refused["ok"] is False
    assert "SENTINEL-REFUSAL" in refused["detail"]
    assert sentinel_predicate == ["the release"]


# --- agent decisions leave the since-user counters; user decisions zero them -------------------


def test_agent_fire_acknowledge_leaves_the_since_user_counters(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    for axis in BOTH_AXES:
        set_total("a1", axis, 3)
    assert ack(eng)["ok"] is True
    assert [total("a1", axis) for axis in BOTH_AXES] == [3, 3]


def test_agent_renegotiation_continue_leaves_the_since_user_counters(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    for axis in BOTH_AXES:
        set_total("a1", axis, 3)
    assert renegotiate(eng, "a1", replanned)["ok"] is True
    assert total("a1", ROUNDS_AXIS) == 3
    assert total("a1", REPLANS_AXIS) >= 3


def test_user_approval_zeroes_the_since_user_counters(eng):
    plan = eng.write(plan_text(), "approved.toml")
    assert eng.open("u1", plan)["marker"] == "PLAN-READY"
    for axis in BOTH_AXES:
        set_total("u1", axis, 3)
    assert eng.run("approve", session="u1", by="user")["ok"] is True
    assert [total("u1", axis) for axis in BOTH_AXES] == [0, 0]


def test_user_fire_acknowledgement_zeroes_the_since_user_counters(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    for axis in BOTH_AXES:
        set_total("a1", axis, 3)
    d = eng.run("fire_acknowledge", session="a1", decision="revise", by="user")
    assert d["ok"] is True, d
    assert [total("a1", axis) for axis in BOTH_AXES] == [0, 0]


def test_user_renegotiation_zeroes_the_since_user_counters(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True, revised=True))
    ceiling_refusal(eng, "a1", replanned)
    for axis in BOTH_AXES:
        set_total("a1", axis, 3)
    d = eng.run("replan", session="a1", plan=replanned, renegotiation_decision="continue",
                renegotiated_by="user", renegotiation_note="the user decided")
    assert d["ok"] is True, d
    assert total("a1", ROUNDS_AXIS) == 0
    assert total("a1", REPLANS_AXIS) < 3


def test_user_plan_review_override_zeroes_the_since_user_counters(eng):
    plan = released_order(eng)
    for axis in BOTH_AXES:
        set_total("o1", axis, 3)
    assert thinker_review(eng, "o1", plan, "override", "user")["ok"] is True
    assert [total("o1", axis) for axis in BOTH_AXES] == [0, 0]


def test_task_reset_clears_the_since_user_counters_only_for_a_non_agent(eng):
    for axis in BOTH_AXES:
        task_accumulator.add("t-since", axis, 4, session_id="x", now=None)
    refused = eng.run("task_reset", task="t-since", reason="loop broke", by="agent")
    assert refused["ok"] is False
    totals = task_accumulator.get("t-since")["per_axis_totals"]
    assert [totals.get(axis, 0) for axis in BOTH_AXES] == [4, 4]
    assert eng.run("task_reset", task="t-since", reason="the user decided", by="user")["ok"] is True
    totals = task_accumulator.get("t-since")["per_axis_totals"]
    assert [totals.get(axis, 0) for axis in BOTH_AXES] == [0, 0]


# --- what the counters count -------------------------------------------------------------------


def test_a_replan_advances_the_since_user_replan_counter_with_the_task_total(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan, execute=False)
    eng.fail_stage("a1")
    eng.diagnose("a1")
    d = eng.run("replan", session="a1", plan=eng.write(plan_text(stage3=True, revised=True)))
    assert action(d) == "self_approve", d
    assert eng.run("approve", session="a1", by="agent")["ok"] is True
    replans = total("a1", "replan_count")
    assert replans > 0
    assert total("a1", REPLANS_AXIS) == replans


def test_counted_thinker_reviews_advance_the_since_user_round_counter(eng):
    plan = approved_order(eng)
    eng.open("o1", plan)
    assert total("o1", ROUNDS_AXIS) == 0
    digest = __import__("hashlib").sha256(Path(plan).read_bytes()).hexdigest()
    for n, note in enumerate(("first", "second")):
        d = eng.run("plan_review", session="o1", verdict="revise", reviewer="thinker",
                    target=plan, plan_digest=digest,
                    concern=[f"blocking: [re:c{n}] {note} concern"])
        assert d["ok"] is True, d
    rounds = total("o1", "review_rounds")
    assert rounds > 0
    assert total("o1", ROUNDS_AXIS) == rounds


# --- an exhaustion-time record is not the diagnosing cycle's record ------------------------------


def test_exhaustion_time_record_is_not_the_diagnosing_cycles_record(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan, execute=False)
    release_review_rounds(eng, "a1")
    reflect(eng, "a1")
    eng.fail_stage("a1")
    state = eng.state("a1")
    assert state.difficulty is None or state.difficulty.declaration is None
    assert len(state.difficulty_stack) == 1
    assert gates.difficulty_blockers(state)


# --- state shape -----------------------------------------------------------------------------------


def test_release_anchor_round_trips_through_the_state_store():
    s = _subst(review_release_difficulty_id="abc123", agent_ack_difficulty_ids=["x", "y"])
    assert SessionState.from_json(s.to_json()) == s


# --- the mutation catalogue ------------------------------------------------------------------------


def _load_rx():
    spec = importlib.util.spec_from_file_location("rx_mutation_control", RX_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its own module by name
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_rx_catalogue_ids_are_single_plain_test_nodes():
    rx = _load_rx()
    rx.validate_catalogue()
    names = {line.split()[0] for line in rx_list_lines()}
    assert names == set(rx.CATALOGUE)
    with pytest.raises(ValueError):
        rx.validate_selector("tests/test_review_exhaustion_reflection.py")
    with pytest.raises(ValueError):
        rx.validate_selector("tests/test_review_exhaustion_reflection.py::test_a[param]")
    with pytest.raises(ValueError):
        rx.validate_selector("tests/test_review_exhaustion_reflection.py::" + rx.CATALOGUE_TEST)


def rx_list_lines() -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "RX_MUTATION_CHILD"}
    out = subprocess.run([sys.executable, str(RX_SCRIPT), "--list"], capture_output=True, text=True,
                         env=env, check=True)
    return out.stdout.strip().splitlines()


def test_rx_script_refuses_to_run_inside_a_mutation_child():
    env = dict(os.environ, RX_MUTATION_CHILD="1")
    run = subprocess.run([sys.executable, str(RX_SCRIPT), "--list"], capture_output=True, text=True,
                         env=env)
    assert run.returncode == 6


@pytest.mark.skipif(bool(os.environ.get("RX_MUTATION_CHILD")),
                    reason="a mutation child never runs the catalogue itself")
def test_rx_mutation_catalogue_kills_every_mutant():
    rx = _load_rx()
    env = {k: v for k, v in os.environ.items() if k not in ("RX_MUTATION_CHILD", "PYTEST_ADDOPTS")}
    survivors = {}
    for name in rx.CATALOGUE:
        run = subprocess.run([sys.executable, str(RX_SCRIPT), "--mutant", name], capture_output=True,
                             text=True, env=env)
        if run.returncode != rx.KILLED:
            survivors[name] = (run.returncode, run.stderr.strip()[-300:])
    assert survivors == {}
