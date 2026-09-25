"""cmd_stage_review's escape scope now matches gates.stage_review_active — the SAME
predicate the acceptance-judge gate itself consumes (weight_class == SUBSTANTIVE, or
the AGENTCTL_STAGE_REVIEW override) — instead of refusing every stage whose
criterion_type isn't acceptance_review (GitHub issue #145).

The gate (gates.acceptance_review_blockers) was already broadened past
acceptance_review-only stages by Defect 2 (control compares result with goal at every
stage of a SUBSTANTIVE session, see cmd_record_result's observation gate), so a
MEASURABLE-criterion SUBSTANTIVE stage can judge-deadlock exactly like an
acceptance_review one. Before this change its only escape was the session-wide
AGENTCTL_STAGE_REVIEW=0 kill switch — a strictly weaker, unattributed
JudgeBypass(kind="killswitch") instead of this command's reviewer+note-bound
JudgeBypass(kind="override"). This module proves the escape hatch followed the gate.

It also proves the acceptance_judge fail-open reason reaches the session log and
(R4) that a judge CALL failure (disabled/errored/timed out) no longer blocks the
pass at all: it auto-records a `fail_open` JudgeBypass bound to the observation
and the pass proceeds, so a caller (or a later session review) can tell "the judge
called and said revise" (still blocks; needs a genuine override) apart from "the
judge call itself failed" (fails open automatically, no override needed)."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


def _measurable_session(store, sid, *, weight=WeightClass.SUBSTANTIVE.value):
    """A MEASURABLE-criterion stage on a session of the given weight class, active
    and ready for record-result — the counterpart to test_enumerate_detach.py's
    `_make_acceptance_session`, but over the criterion type the old refusal treated
    as out of scope for stage-review."""
    state = SessionState(
        session_id=sid,
        task_id="measurable-test",
        goal="fix the bug",
        overall_done_criterion="the test suite passes",
        overall_criterion_type=CriterionType.MEASURABLE.value,
        weight_class=weight,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="test-setup"),
        stages=[
            Stage(
                index=1,
                title="Fix the bug",
                subject=Subject(material="the module", result="tests pass with no failures"),
                means=Means(means="pytest", method="run the test suite"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="pytest exits 0",
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state


def _fail_open_runner(argv, *, timeout=None, stdin=""):
    """A stub judge runner that always fails open with a non-zero exit -- the
    judge CALL itself fails, so no verdict is produced at all."""
    return RunResult(returncode=1, stdout="", stderr="boom")


def _revise_runner(argv, *, timeout=None, stdin=""):
    """A stub judge runner that returns a genuine (non-fail-open) revise verdict --
    contrasted with `_fail_open_runner`, whose non-zero exit means the judge never
    produced a verdict at all."""
    return RunResult(returncode=0, stdout="NO\nnot enough detail", stderr="")


# --- (a) the escape now widens to the gate's own scope ------------------------

def test_measurable_substantive_stage_is_now_in_scope(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "s-a")

    d = cli.cmd_stage_review(
        ns(session="s-a", verdict="override", reviewer="fedor", note="escaping a deadlock",
           concerns=None, observation=None),
        store=store,
    )

    assert d.ok is True
    assert "stage-review applies only to" not in d.detail
    state = store.load("s-a")
    assert state.stage_reviews[-1].verdict == "override"
    assert state.stage_reviews[-1].reviewer == "fedor"


# --- (c) still refused when the gate itself is inactive ------------------------

def test_stage_review_refuses_when_gate_inactive(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "s-c", weight=WeightClass.SMALL_CHANGE.value)

    d = cli.cmd_stage_review(
        ns(session="s-c", verdict="override", reviewer="fedor", note="n",
           concerns=None, observation=None),
        store=store,
    )

    assert d.ok is False
    assert gates.stage_review_active(store.load("s-c")) is False
    assert "not active" in d.detail
    assert "SMALL_CHANGE" in d.detail
    assert "AGENTCTL_STAGE_REVIEW" in d.detail


# --- (b) a judge CALL failure (R4) auto-bypasses and unblocks the pass --------

def test_fail_open_judge_call_auto_bypasses_and_unblocks_pass(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "s-b")
    observation = "pytest printed 12 passed, 0 failed"

    d1 = cli.cmd_record_result(
        ns(session="s-b", status="passed", actual="ran the suite",
           control=None, observation=observation),
        store=store, runner=_fail_open_runner,
    )

    # R4: the judge CALL itself failed (non-zero exit) -- that is not evidence
    # against the observation, so the pass proceeds via an automatically recorded
    # fail_open bypass, with no manual override needed.
    assert d1.ok is True
    state = store.load("s-b")
    assert any(
        b.kind == "fail_open" and b.observation_sha256 == cli._observation_sha256(observation)
        for b in state.judge_bypassed
    )


# --- (d) an empty reviewer/note override records but still blocks a genuine
#         revise verdict (fail_open auto-bypass does not apply here) -----------

def test_empty_reviewer_override_recorded_but_still_blocks_pass(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "s-d")
    observation = "pytest printed 12 passed, 0 failed"

    d1 = cli.cmd_stage_review(
        ns(session="s-d", verdict="override", reviewer="", note="",
           concerns=None, observation=observation),
        store=store,
    )
    assert d1.ok is True  # the bare record always succeeds once the gate is active

    # A GENUINE revise verdict (not a call failure) -- the human override already
    # on record (reviewer="") protects itself from being clobbered by the judge's
    # verdict (_record_stage_review), so the empty-reviewer override is still what
    # the gate evaluates, and it still blocks.
    d2 = cli.cmd_record_result(
        ns(session="s-d", status="passed", actual="ran",
           control=None, observation=observation),
        store=store, runner=_revise_runner,
    )

    assert d2.ok is False
    assert any("non-empty reviewer" in b for b in d2.data["blockers"])


# --- (e) the fail-open reason is logged and the pass proceeds (R4) ------------

def test_fail_open_reason_reaches_the_log_and_pass_proceeds(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "s-e")
    observation = "pytest printed 12 passed, 0 failed"

    def no_output_runner(argv, *, timeout=None, stdin=""):
        return RunResult(returncode=0, stdout="", stderr="")

    d = cli.cmd_record_result(
        ns(session="s-e", status="passed", actual="ran",
           control=None, observation=observation),
        store=store, runner=no_output_runner,
    )

    assert d.ok is True

    state = store.load("s-e")
    assert any(
        entry.get("event") == "acceptance_judge_fail_open"
        and entry.get("reason") == "judge returned no output (fail-open)"
        for entry in state.history
    )
    assert any(
        b.kind == "fail_open" and b.note == "judge returned no output (fail-open)"
        for b in state.judge_bypassed
    )
