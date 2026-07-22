"""The review-dispatch plugin — both slots (thinker/plan_review, code-reviewer/
code_review). Proves:

  1. registration: `review_dispatch` is in the REGISTRY, observes
     `submit_plan` AND `dispatch` ONLY (deliberately NOT `replan` — see the
     module docstring), and contributes no gate — enforcement stays in
     gates.plan_review_blockers / gates.code_review_blockers.
  2. `_auto_activate` arms for SUBSTANTIVE alone (both env-override directions).
  3. the real `submit_plan` event (fired through cli.main()'s central
     `_fire_plugins` wiring, driven by the actual `cmd_submit_plan` command)
     appends a blocking directive naming the thinker spawn whenever
     gates.plan_review_blockers is non-empty, and stays silent on a
     small-change session and with the knob off. "Silent once a bound passing
     review exists" is proven by firing the event directly (see the module
     docstring on why a second submit-plan call can't demonstrate this: it
     unconditionally clears the review itself).
  4. the `dispatch` event (fired directly against an in-memory state, mirroring
     the "silent once bound" thinker test) appends a blocking directive naming
     the code-reviewer spawn whenever gates.code_review_blockers is non-empty
     for the active needs_control() stage, and stays silent once a bound
     passing review exists, the active stage is not a developer stage, or the
     session is non-substantive / the knob is off. The submit_plan/thinker
     behaviour from (3) is unchanged by this addition.
"""
from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plugins
from agentctl import plugins_review_dispatch as prd
from agentctl.directive import Directive
from agentctl.state import (
    Actor,
    CodeReview,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)
from agentctl.store import FileStateStore


@pytest.fixture(autouse=True)
def _review_dispatch_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PLAN_REVIEW=0 / AGENTCTL_CODE_REVIEW=0
    force-offs: this module is the one place that exercises the real plan-review AND
    code-review arming predicates the plugin's two observers ride on, so it deletes
    all three knobs and lets the plain weight_class logic decide. A module-local
    autouse fixture runs after the conftest ones, so this delenv wins for every test
    here."""
    monkeypatch.delenv("AGENTCTL_PLAN_REVIEW", raising=False)
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    monkeypatch.delenv("AGENTCTL_REVIEW_DISPATCH", raising=False)


def _dev_stage(index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Edit", method="implement"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _non_dev_stage(index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Read", method="plan"),
        actor=Actor(executor="spawn:thinker"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- registration --------------------------------------------------------

def test_registered_observes_submit_plan_and_dispatch_only_no_gate():
    p = plugins.REGISTRY["review_dispatch"]
    assert set(p.observers) == {"submit_plan", "dispatch"}
    assert "replan" not in p.observers
    assert p.gates == {}


# --- auto-activation: SUBSTANTIVE alone, both env-override directions ----

def test_auto_activates_for_substantive():
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    assert prd._auto_activate(substantive) is True


def test_does_not_auto_activate_for_small_change():
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value)
    assert prd._auto_activate(small) is False


def test_env_override_forces_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "1")
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value)
    assert prd._auto_activate(small) is True


def test_env_override_forces_off(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "0")
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    assert prd._auto_activate(substantive) is False


# --- end-to-end through cli.main() (the real _fire_plugins wiring) -------

def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _drive_to_plan_ready(capsys, root, sid, plan):
    _run(capsys, root, "start", "--session", sid, "--task", "t",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    return _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)


def test_submit_plan_fires_blocking_thinker_directive(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "rd1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    rc, d = _drive_to_plan_ready(capsys, root, sid, plan)
    assert rc == 0
    assert d["node"] == Node.PLAN_READY.value
    pds = d["data"]["plugin_directives"]
    matches = [p for p in pds if p["plugin"] == "review_dispatch"
               and p["action"] == "spawn_thinker_review"]
    assert len(matches) == 1
    m = matches[0]
    assert m["blocking"] is True
    assert m["data"]["specialist"] == "thinker"
    assert m["data"]["slot"] == "plan_review"


def test_submit_plan_silent_once_bound_passing_review_exists(capsys, tmp_path, fixtures_dir):
    # A second submit-plan at PLAN_READY is a RESUBMISSION: cmd_submit_plan
    # unconditionally clears state.plan_review (the plan may have changed), so
    # exercising "silent once bound" through a second submit-plan call would
    # only prove cmd_submit_plan's own reset, not the observer. Fire the event
    # directly against a state that already carries a bound passing review,
    # mirroring plugins_premise's gate-level unit tests.
    root = str(tmp_path / "state")
    sid = "rd2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_plan_ready(capsys, root, sid, plan)
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    rc, d = _run(capsys, root, "plan-review", "--session", sid, "--verdict", "pass",
                 "--reviewer", "thinker", "--plan-digest", digest)
    assert rc == 0

    store = FileStateStore(root)
    state = store.load(sid)
    directive = Directive(True, state.node, "noop")
    fired = plugins.fire("submit_plan", state, directive)
    assert not any(p["plugin"] == "review_dispatch" for p in fired)


def test_submit_plan_silent_for_small_change_session(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "rd3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _run(capsys, root, "start", "--session", sid, "--task", "t",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--changed-lines", "5",
         "--files", "1", "--wall-clock-min", "5")
    rc, d = _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)
    assert "plugin_directives" not in d["data"] or not any(
        p["plugin"] == "review_dispatch" for p in d["data"].get("plugin_directives", [])
    )


def test_submit_plan_silent_when_knob_off(capsys, tmp_path, fixtures_dir, monkeypatch):
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "0")
    root = str(tmp_path / "state")
    sid = "rd4"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    rc, d = _drive_to_plan_ready(capsys, root, sid, plan)
    assert rc == 0
    pds = d["data"].get("plugin_directives", [])
    assert not any(p["plugin"] == "review_dispatch" for p in pds)


# --- dispatch -> code-reviewer slot ---------------------------------------

def _dispatch_state(stage, *, weight=WeightClass.SUBSTANTIVE.value, reviews=()):
    state = SessionState(
        session_id="rd-dispatch", task_id="t", node=Node.EXECUTING.value,
        weight_class=weight, stages=[stage], current_stage=stage.index,
        code_reviews=list(reviews),
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
    )
    plugins.activate(state, "review_dispatch")
    return state


def _fire_dispatch(state):
    directive = Directive(True, state.node, "noop")
    return plugins.fire("dispatch", state, directive)


def test_dispatch_fires_blocking_code_review_directive():
    state = _dispatch_state(_dev_stage())
    fired = _fire_dispatch(state)
    matches = [p for p in fired if p["plugin"] == "review_dispatch"
               and p["action"] == "spawn_code_review"]
    assert len(matches) == 1
    m = matches[0]
    assert m["blocking"] is True
    assert m["data"]["specialist"] == "code-reviewer"
    assert m["data"]["slot"] == "code_review"
    assert m["data"]["stage"] == 1


def test_dispatch_silent_once_bound_passing_review_exists():
    state = _dispatch_state(
        _dev_stage(), reviews=[CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer")]
    )
    fired = _fire_dispatch(state)
    assert not any(p["plugin"] == "review_dispatch" for p in fired)


def test_dispatch_silent_for_non_developer_active_stage():
    state = _dispatch_state(_non_dev_stage())
    fired = _fire_dispatch(state)
    assert not any(p["plugin"] == "review_dispatch" for p in fired)


def test_dispatch_silent_for_small_change_session():
    state = _dispatch_state(_dev_stage(), weight=WeightClass.SMALL_CHANGE.value)
    fired = _fire_dispatch(state)
    assert not any(p["plugin"] == "review_dispatch" for p in fired)


def test_dispatch_silent_when_knob_off(monkeypatch):
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "0")
    state = _dispatch_state(_dev_stage())
    fired = _fire_dispatch(state)
    assert not any(p["plugin"] == "review_dispatch" for p in fired)


def test_submit_plan_thinker_directive_unaffected_by_dispatch_addition():
    """Regression: the dispatch observer added in this stage must not change
    the submit_plan/thinker directive's shape or trigger condition. Fired at
    PLAN_READY — the node the submit_plan observer is guarded to — not the
    EXECUTING node _dispatch_state builds for the dispatch-slot tests."""
    state = _dispatch_state(_dev_stage())
    state.node = Node.PLAN_READY.value
    state.plan_path = "/tmp/some-plan.toml"
    directive = Directive(True, state.node, "noop")
    fired = plugins.fire("submit_plan", state, directive)
    matches = [p for p in fired if p["plugin"] == "review_dispatch"
               and p["action"] == "spawn_thinker_review"]
    assert len(matches) == 1
    assert matches[0]["data"]["specialist"] == "thinker"
