"""The acceptance-review judge gate (gates.acceptance_review_blockers): a
record-result --status passed on an acceptance_review stage is blocked until a
recorded StageReview with a passing (or user-override) verdict, BOUND to the exact
observation bytes, exists. The sibling of the thinker-review gate — same shape,
same override escape, same weight/kill-switch scoping — but over a stage observation
instead of a plan file, and fed by a fail-open cheap judge rather than the thinker.

Deliberately ABSENT from gates.GUARDIANS (an internal record-result precondition,
like plan_review_blockers), so verify-agentctl requires no new hook, and PURE (reads
only the recorded StageReview + hashes the observation's own bytes)."""
from __future__ import annotations

import hashlib

import pytest

from agentctl import gates
from agentctl.state import (
    Actor,
    Criterion,
    JudgeBypass,
    Means,
    Outcome,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
    Subject,
)
from ast_purity import impure_names


def _sha(observation: str) -> str:
    return hashlib.sha256(observation.encode("utf-8")).hexdigest()


def _stage(observation="", result="the expected image"):
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result=result),
        means=Means(means="Read", method="observe"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="acceptance_review",
                            done_criterion="c", observation=observation),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _subst(stage, *, reviews=(), weight="SUBSTANTIVE"):
    return SessionState(session_id="s", task_id="t", weight_class=weight,
                        stages=[stage], stage_reviews=list(reviews))


def _review(observation, verdict, reviewer="judge:haiku", note="", bind=True):
    return StageReview(stage_index=1, verdict=verdict, reviewer=reviewer, note=note,
                       observation_sha256=_sha(observation) if bind else "")


# --- activation scoping (weight + kill switch) -------------------------------

def test_inactive_on_small_change_is_vacuous(monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    s = _subst(_stage("obs"), weight="SMALL_CHANGE")
    assert gates.stage_review_active(s) is False
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


def test_force_off_env_makes_gate_vacuous(monkeypatch):
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    s = _subst(_stage("obs"))  # substantive, no review recorded
    assert gates.stage_review_active(s) is False
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


def test_force_on_env_activates_small_change(monkeypatch):
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "1")
    s = _subst(_stage("obs"), weight="SMALL_CHANGE")
    assert gates.stage_review_active(s) is True
    assert gates.acceptance_review_blockers(s, s.stages[0])  # no review -> blocks


def test_advisor_kill_switch_does_not_disable_gate(monkeypatch):
    # The advisor's own cost knob must not silently defeat the mandatory gate.
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    s = _subst(_stage("obs"))
    assert gates.stage_review_active(s) is True
    assert gates.acceptance_review_blockers(s, s.stages[0])  # blocks despite advisor off


# --- the verdict matrix (gate active) ----------------------------------------

@pytest.fixture(autouse=True)
def _gate_on_by_weight(monkeypatch):
    # Default the matrix to weight-scoped activation (env unset) so a substantive
    # session is active; individual scoping tests set the env explicitly.
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)


def test_missing_review_blocks():
    s = _subst(_stage("obs"))
    b = gates.acceptance_review_blockers(s, s.stages[0])
    assert b and "no acceptance judge verdict" in b[0]


def test_pass_bound_to_observation_clears():
    obs = "I ran the module and it printed 3 rows"
    s = _subst(_stage(obs), reviews=[_review(obs, "pass")])
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


def test_pass_bound_to_a_different_observation_is_stale():
    s = _subst(_stage("the observation now"),
               reviews=[_review("a DIFFERENT observation", "pass")])
    b = gates.acceptance_review_blockers(s, s.stages[0])
    assert b and "stale" in b[0]


def test_empty_stored_sha_degrades_to_verdict_only():
    s = _subst(_stage("obs"), reviews=[_review("obs", "pass", bind=False)])
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


def test_revise_blocks():
    obs = "obs"
    s = _subst(_stage(obs), reviews=[_review(obs, "revise")])
    b = gates.acceptance_review_blockers(s, s.stages[0])
    assert b and "revise" in b[0]


def test_unknown_verdict_blocks():
    obs = "obs"
    s = _subst(_stage(obs), reviews=[_review(obs, "maybe")])
    assert gates.acceptance_review_blockers(s, s.stages[0])


def test_override_with_reviewer_and_note_clears():
    obs = "obs"
    s = _subst(_stage(obs),
               reviews=[_review(obs, "override", reviewer="fedor", note="deadlock")])
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


def test_override_missing_reviewer_blocks():
    obs = "obs"
    s = _subst(_stage(obs), reviews=[_review(obs, "override", reviewer="", note="x")])
    b = gates.acceptance_review_blockers(s, s.stages[0])
    assert b and "requires a non-empty reviewer" in b[0]


def test_override_missing_note_blocks():
    obs = "obs"
    s = _subst(_stage(obs), reviews=[_review(obs, "override", reviewer="fedor", note="")])
    b = gates.acceptance_review_blockers(s, s.stages[0])
    assert b and "requires a non-empty note" in b[0]


def test_last_recorded_review_wins():
    obs = "obs"
    s = _subst(_stage(obs),
               reviews=[_review(obs, "revise"), _review(obs, "pass")])
    assert gates.acceptance_review_blockers(s, s.stages[0]) == []


# --- structural contract: pure, and NOT a registered guardian ----------------

def test_gate_is_pure():
    assert impure_names(gates.acceptance_review_blockers) == set()


def test_gate_absent_from_guardians():
    assert "acceptance_review" not in gates.GUARDIANS
    assert set(gates.GUARDIANS) == {"plan_approval", "resolution"}


def test_gates_module_docstring_states_the_asymmetry():
    assert "fail-open" in gates.__doc__ and "fail-closed" in gates.__doc__
