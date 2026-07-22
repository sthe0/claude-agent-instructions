"""The conformance-obligations ledger plugin. Proves:

  1. registration: `obligations` contributes a `resolution` gate and NO
     observers — it rides `plugins.fire()`'s generic post-observe `mint()`
     delegate call, not an observer of its own.
  2. `_auto_activate` arms for SUBSTANTIVE alone (both env-override
     directions), mirroring plugins_premise / plugins_review_dispatch.
  3. `mint` records a blocking directive whose action is in `_DISCHARGE`,
     ignores a non-blocking directive, ignores a blocking directive whose
     action is NOT in `_DISCHARGE` (the premise/experience deadlock-avoidance
     cases), dedups a re-fire of the same obligation, and no-ops entirely
     when the plugin is inactive.
  4. the `resolution` guardian returns [] on an empty ledger, blocks an open
     `spawn_thinker_review` obligation until a passing PlanReview is bound and
     clears once it is, and likewise for `spawn_code_review` / CodeReview.
  5. the decisive DEADLOCK-REGRESSION test: with premise + experience +
     obligations all active on a SUBSTANTIVE session, firing `approve`
     (premise's blocking `close_questions`) and `resolve` (experience's
     blocking `record_experience`) through `plugins.fire` never populates the
     obligations ledger and never contributes an obligations-sourced blocker.
  6. round-trip: a SessionState carrying an obligations bag survives
     `from_dict(to_dict())`.
"""
from __future__ import annotations

from dataclasses import asdict

import pytest

from agentctl import plugins
from agentctl import plugins_obligations as ob
from agentctl.directive import Directive
from agentctl.state import (
    CodeReview,
    GateRecord,
    PlanReview,
    SessionState,
    Stage,
    Actor,
    Criterion,
    Means,
    Outcome,
    StageStatus,
    Subject,
    WeightClass,
)


@pytest.fixture(autouse=True)
def _obligations_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PLAN_REVIEW=0 / AGENTCTL_CODE_REVIEW=0
    force-offs: this module exercises the real discharge oracles the ledger's
    guardian calls, so it deletes all the relevant knobs and lets weight_class
    decide, mirroring test_plugins_review_dispatch.py's _review_dispatch_armed."""
    monkeypatch.delenv("AGENTCTL_PLAN_REVIEW", raising=False)
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    monkeypatch.delenv("AGENTCTL_REVIEW_DISPATCH", raising=False)
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    monkeypatch.delenv("AGENTCTL_OBLIGATIONS", raising=False)


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


def _dev_stage(index=1, status=StageStatus.ACTIVE.value):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Edit", method="implement"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=status),
    )


def _fire(state, event):
    directive = Directive(True, state.node, "noop")
    return plugins.fire(event, state, directive)


def _thinker_pd(blockers=("no thinker review recorded",)):
    return {
        "plugin": "review_dispatch", "action": "spawn_thinker_review",
        "detail": "spawn thinker", "blocking": True,
        "data": {"slot": "plan_review", "specialist": "thinker", "blockers": list(blockers)},
    }


def _code_review_pd(stage=1, blockers=("no code-reviewer verdict recorded",)):
    return {
        "plugin": "review_dispatch", "action": "spawn_code_review",
        "detail": "spawn code-reviewer", "blocking": True,
        "data": {"slot": "code_review", "specialist": "code-reviewer", "stage": stage,
                 "blockers": list(blockers)},
    }


# --- registration ----------------------------------------------------------

def test_registered_no_observers_one_gate():
    p = plugins.REGISTRY["obligations"]
    assert p.observers == {}
    assert set(p.gates) == {"resolution"}


# --- auto-activation: SUBSTANTIVE alone, both env-override directions ------

def test_auto_activates_for_substantive():
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    assert ob._auto_activate(substantive) is True


def test_does_not_auto_activate_for_small_change():
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value)
    assert ob._auto_activate(small) is False


def test_env_override_forces_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_OBLIGATIONS", "1")
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value)
    assert ob._auto_activate(small) is True


def test_env_override_forces_off(monkeypatch):
    monkeypatch.setenv("AGENTCTL_OBLIGATIONS", "0")
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    assert ob._auto_activate(substantive) is False


# --- mint() ------------------------------------------------------------------

def _active_state(**kw):
    state = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, **kw)
    plugins.activate(state, "obligations")
    return state


def test_mint_noops_when_inactive():
    state = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    assert "obligations" not in state.plugins
    ob.mint(state, [_thinker_pd()])
    assert "obligations" not in state.plugins


def test_mint_records_discharge_action():
    state = _active_state()
    ob.mint(state, [_thinker_pd()])
    open_ = state.plugins["obligations"]["open"]
    assert len(open_) == 1
    entry = next(iter(open_.values()))
    assert entry["action"] == "spawn_thinker_review"


def test_mint_ignores_non_blocking():
    state = _active_state()
    pd = dict(_thinker_pd())
    pd["blocking"] = False
    ob.mint(state, [pd])
    assert state.plugins["obligations"]["open"] == {}


def test_mint_ignores_action_not_in_discharge():
    state = _active_state()
    close_questions = {"plugin": "premise", "action": "close_questions",
                        "detail": "d", "blocking": True, "data": {}}
    record_experience = {"plugin": "experience", "action": "record_experience",
                          "detail": "d", "blocking": True, "data": {}}
    ob.mint(state, [close_questions, record_experience])
    assert state.plugins["obligations"]["open"] == {}


def test_mint_dedups_on_refire():
    state = _active_state()
    ob.mint(state, [_thinker_pd()])
    ob.mint(state, [_thinker_pd(blockers=("still stale",))])
    open_ = state.plugins["obligations"]["open"]
    assert len(open_) == 1
    entry = next(iter(open_.values()))
    assert entry["data"]["blockers"] == ["still stale"]


# --- resolution guardian ----------------------------------------------------

def test_guardian_empty_ledger_no_blockers():
    state = _active_state()
    bag = state.plugins["obligations"]
    assert ob._resolution_guardian(state, bag) == []


def test_guardian_blocks_open_plan_review_until_passing_review_bound(tmp_path):
    import hashlib
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    state = _active_state(plan_path=str(plan))
    ob.mint(state, [_thinker_pd()])
    bag = state.plugins["obligations"]
    blockers = ob._resolution_guardian(state, bag)
    assert len(blockers) == 1
    assert "spawn_thinker_review" in blockers[0]

    # A pass discharges only when reviewer-attested (non-empty plan_sha256 matching
    # the live bytes); a digest-less pass would leave the obligation open.
    state.plan_review = PlanReview(
        plan_path=str(plan), verdict="pass", reviewer="thinker",
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
    )
    assert ob._resolution_guardian(state, bag) == []


def test_guardian_blocks_open_code_review_until_passing_review_bound():
    stage = _dev_stage(index=1)
    state = _active_state(stages=[stage], current_stage=1)
    ob.mint(state, [_code_review_pd(stage=1)])
    bag = state.plugins["obligations"]
    blockers = ob._resolution_guardian(state, bag)
    assert len(blockers) == 1
    assert "spawn_code_review" in blockers[0]

    state.code_reviews = [CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer")]
    assert ob._resolution_guardian(state, bag) == []


def test_guardian_discharges_obligation_for_stage_no_longer_present():
    state = _active_state(stages=[], current_stage=None)
    ob.mint(state, [_code_review_pd(stage=1)])
    bag = state.plugins["obligations"]
    assert ob._resolution_guardian(state, bag) == []


# --- DEADLOCK REGRESSION -----------------------------------------------------

def test_premise_and_experience_obligations_never_populate_ledger():
    """premise's close_questions and experience's record_experience are both
    blocking directives that fire OUTSIDE _DISCHARGE — premise on `approve`,
    experience on `resolve` itself. If mint recorded either, the ledger would
    carry an obligation resolution can never discharge (record_experience
    fires blocking on the very event that would need to clear it). Proves the
    ledger stays empty and contributes no resolution-gate blocker through the
    real plugins.fire() seam, mirroring the actual approve/resolve sequence."""
    state = _new_state(
        weight_class=WeightClass.SUBSTANTIVE.value,
        approval=GateRecord("plan_approval", armed=True, passed=False, by=None),
    )
    plugins.activate(state, "premise")
    plugins.activate(state, "experience")
    plugins.activate(state, "obligations")

    fired_approve = _fire(state, "approve")
    assert any(p["plugin"] == "premise" and p["action"] == "close_questions" for p in fired_approve)

    fired_resolve = _fire(state, "resolve")
    assert any(p["plugin"] == "experience" and p["action"] == "record_experience" for p in fired_resolve)

    assert state.plugins["obligations"]["open"] == {}
    resolution_blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert not any(b.startswith("[obligations]") for b in resolution_blockers)


# --- round-trip --------------------------------------------------------------

def test_state_with_obligations_bag_roundtrips():
    state = _active_state(plan_path="/tmp/plan.toml")
    ob.mint(state, [_thinker_pd()])
    data = asdict(state)
    restored = SessionState(**data)
    assert restored.plugins["obligations"]["open"] == state.plugins["obligations"]["open"]
