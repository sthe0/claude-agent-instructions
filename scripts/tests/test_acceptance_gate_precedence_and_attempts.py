"""Behavioral tests for two `cmd_record_result` correctness properties that sit
alongside the judge-after-check ordering (see test_convergence_r4_judge_after_check.py):

  - A standing `revise` StageReview must outrank a later `fail_open` JudgeBypass
    bound to the same observation -- a judge CALL failure is not evidence FOR the
    observation, so it must never override a verdict that is already evidence
    AGAINST it. cli.cmd_record_result never even records such a bypass in the
    first place (gates.standing_revise_bound_to); gates.acceptance_review_blockers
    independently refuses to be fooled by one recorded some other way.
  - Outcome.record_attempts and the record_result_attempt history event must
    persist through every early-gate return in cmd_record_result (attest_control,
    attest_observation missing, attest_observation echoing the target verbatim),
    not just through a call that reaches the judge.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    JudgeBypass,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


def _measurable_session(store, sid, *, verify_command=None, expected_exit=0,
                         executor="in_thread"):
    """Same shape as the sibling R4 test files' helper of the same name -- kept
    as a local copy per this suite's one-file-one-fixture-set convention."""
    state = SessionState(
        session_id=sid,
        task_id="gate-precedence-test",
        goal="fix the bug",
        overall_done_criterion="the test suite passes",
        overall_criterion_type=CriterionType.MEASURABLE.value,
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="test-setup"),
        stages=[
            Stage(
                index=1,
                title="Fix the bug",
                subject=Subject(material="the module", result="tests pass with no failures"),
                means=Means(means="pytest", method="run the test suite"),
                actor=Actor(executor=executor),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="pytest exits 0",
                    verify_command=verify_command,
                    expected_exit=expected_exit,
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state


class _Runner:
    """Routes a call by argv shape: a git-flavored call (plain or `env
    GIT_INDEX_FILE=... git ...`), the verify_command's `bash -c <cmd>`, or a
    judge call."""

    def __init__(self, *, verify_exit=0, judge_stdout="YES\nlooks right", judge_exit=0):
        self.verify_calls = 0
        self.judge_calls = 0
        self.judge_exit = judge_exit
        self.judge_stdout = judge_stdout
        self.verify_exit = verify_exit

    def __call__(self, argv, *, timeout=None, stdin=""):
        git_argv = argv[2:] if argv[:1] == ["env"] else argv
        if git_argv[:1] == ["git"]:
            if "rev-parse" in git_argv:
                if "--git-path" in git_argv:
                    return RunResult(0, stdout=".git/index", stderr="")
                return RunResult(0, stdout="deadbeef", stderr="")
            if "write-tree" in git_argv:
                return RunResult(0, stdout="treesha", stderr="")
            return RunResult(0, stdout="", stderr="")
        if argv[:2] == ["bash", "-c"]:
            self.verify_calls += 1
            return RunResult(self.verify_exit, stdout="", stderr="")
        self.judge_calls += 1
        return RunResult(self.judge_exit, stdout=self.judge_stdout, stderr="")


# --- a standing revise outranks a later fail_open bypass --------------------

def test_gate_standing_revise_blocks_despite_later_fail_open_bypass(store, monkeypatch):
    """Unit-level: a `revise` StageReview and a `fail_open` JudgeBypass both
    bound to the SAME observation coexist (the bypass recorded strictly after
    the revise) -- the pure gate must still block, since the bypass is not
    evidence the observation is good, only that a later judge CALL could not
    be reached."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    state = _measurable_session(store, "b1")
    stage = state.stages[0]
    observation = "pytest printed 12 passed, 0 failed"
    stage.criterion.observation = observation
    obs_sha = cli._observation_sha256(observation)

    state.stage_reviews.append(StageReview(
        stage_index=stage.index, verdict="revise", reviewer="judge:haiku",
        note="not enough detail", observation_sha256=obs_sha,
    ))
    state.judge_bypassed.append(JudgeBypass(
        stage_index=stage.index, kind="fail_open", note="judge timed out",
        observation_sha256=obs_sha,
    ))

    blockers = gates.acceptance_review_blockers(state, stage)

    assert blockers  # the revise stands; the bypass must not clear it


def test_fail_open_bypass_still_authorizes_when_no_review_exists(store, monkeypatch):
    """Negative control for the check above: with NO standing review at all, a
    fail_open bypass bound to the current observation must still authorize the
    pass exactly as before -- the precedence rule narrows the bypass, it does
    not disable it."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    state = _measurable_session(store, "b2")
    stage = state.stages[0]
    observation = "pytest printed 12 passed, 0 failed"
    stage.criterion.observation = observation
    obs_sha = cli._observation_sha256(observation)

    state.judge_bypassed.append(JudgeBypass(
        stage_index=stage.index, kind="fail_open", note="judge timed out",
        observation_sha256=obs_sha,
    ))

    assert gates.acceptance_review_blockers(state, stage) == []


def test_standing_revise_blocks_end_to_end_after_later_judge_call_failure(store, monkeypatch):
    """Functional (via cmd_record_result): the judge genuinely says NO once,
    then a second record-result attempt hits a judge CALL failure (outage)
    rather than a second real verdict. The pass must stay blocked -- a mere
    inability to re-judge must never wave through a "no" the judge already
    gave -- and cmd_record_result must not even record a fail_open bypass for
    an observation a standing revise already covers."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "b3", verify_command="true")
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(verify_exit=0, judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="b3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )
    assert d1.ok is False  # genuine revise

    runner.judge_exit = 1  # the judge CALL itself now fails
    d2 = cli.cmd_record_result(
        ns(session="b3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d2.ok is False
    reloaded = store.load("b3")
    obs_sha = cli._observation_sha256(observation)
    assert not any(
        b.kind == "fail_open" and b.observation_sha256 == obs_sha
        for b in reloaded.judge_bypassed
    )


# --- record_attempts / record_result_attempt survive an early gate ---------

def test_record_attempt_persists_through_attest_control_early_return(store):
    state = _measurable_session(store, "p1", verify_command="true",
                                 executor="spawn:developer")

    d = cli.cmd_record_result(
        ns(session="p1", status="passed", actual="did the thing", control=None,
           observation="whatever"),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_control"

    reloaded = store.load("p1")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempt_persists_through_attest_observation_missing(store):
    _measurable_session(store, "p2", verify_command="true")

    d = cli.cmd_record_result(
        ns(session="p2", status="passed", actual="did the thing", control=None,
           observation=""),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_observation"

    reloaded = store.load("p2")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempt_persists_through_attest_observation_echo(store):
    state = _measurable_session(store, "p3", verify_command="true")
    target = state.stages[0].subject.result

    d = cli.cmd_record_result(
        ns(session="p3", status="passed", actual="did the thing", control=None,
           observation=target),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_observation"

    reloaded = store.load("p3")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempts_accumulate_across_repeated_blocked_calls(store):
    """Two blocked calls in a row must leave record_attempts == 2, not 1 --
    proves the persistence holds across repeated early-gate hits, not just the
    first."""
    _measurable_session(store, "p4", verify_command="true")

    for _ in range(2):
        cli.cmd_record_result(
            ns(session="p4", status="passed", actual="did the thing", control=None,
               observation=""),
            store=store, runner=_Runner(),
        )

    reloaded = store.load("p4")
    assert reloaded.stages[0].outcome.record_attempts == 2


# --- a fail-open judge CALL failure also writes a `decided` ledger line -----

def test_fail_open_judge_call_writes_decided_ledger_line(store, monkeypatch):
    """test_acceptance_judge_writes_decided_ledger_line (in the sibling R4 test
    file) only covers a genuine pass verdict; a judge CALL failure that fails
    open must be logged to the ledger the same way, or a diagnosing session can
    never tell a fail-open pass apart from an unlogged one."""
    from lib import judge_ledger

    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "fo1", verify_command="true")
    runner = _Runner(verify_exit=0, judge_exit=1)  # the judge CALL itself fails

    d = cli.cmd_record_result(
        ns(session="fo1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d.ok is True  # fails open

    records = judge_ledger.read_records()
    assert any(
        r.get("kind") == "decided" and r.get("judge") == "acceptance_judge"
        and r.get("stage") == "call"
        for r in records
    )
