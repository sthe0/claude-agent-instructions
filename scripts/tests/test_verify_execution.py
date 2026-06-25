"""Machine-executed stage verification (stage 2 of determinize-coordination-phase2).

When a measurable stage carries a `verify_command`, the engine runs it via the
injected runner and gates on the real exit code:
  - record-result --status passed is OVERRIDDEN to a failure when the command
    contradicts the claim (exit != expected_exit) -> DIAGNOSING;
  - verify-final re-runs every measurable command as defense in depth and refuses
    RESOLUTION on any non-match.
The runner is faked so these tests never shell out.
"""
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


def runner_returning(code):
    return lambda argv: RunResult(code, stdout="", stderr="")


def boom(argv):
    raise AssertionError(f"runner must not be called (argv={argv})")


def _stage(i, *, verify_command=None, expected_exit=0,
           criterion_type="measurable", status=StageStatus.ACTIVE.value):
    return Stage(
        index=i,
        title=f"s{i}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=criterion_type,
            done_criterion="c",
            verify_command=verify_command,
            expected_exit=expected_exit,
        ),
        outcome=Outcome(status=status),
    )


def _executing(sid, stage):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = stage.index
    return s


def _verifying(sid, stage):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    return s


# --- record-result executes the command and gates on the exit code -----------

def test_passed_with_matching_command_accepts(store):
    store.save(_executing("v1", _stage(1, verify_command="true", expected_exit=0)))
    d = cli.cmd_record_result(
        ns(session="v1", status="passed", actual="claim ok", control=None),
        store=store, runner=runner_returning(0),
    )
    assert d.ok is True
    assert store.load("v1").stage(1).outcome.status == StageStatus.PASSED.value


def test_passed_claim_contradicted_by_command_becomes_failure(store):
    store.save(_executing("v2", _stage(1, verify_command="false", expected_exit=0)))
    d = cli.cmd_record_result(
        ns(session="v2", status="passed", actual="claim ok", control=None),
        store=store, runner=runner_returning(1),
    )
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.marker == "OVERCOME-DIFFICULTY"
    after = store.load("v2")
    assert after.stage(1).outcome.status == StageStatus.FAILED.value
    assert "verify_command exit 1" in (after.stage(1).outcome.actual or "")


def test_nonzero_expected_exit_accepts_when_matched(store):
    store.save(_executing("v3", _stage(1, verify_command="exit 2", expected_exit=2)))
    d = cli.cmd_record_result(
        ns(session="v3", status="passed", actual="ok", control=None),
        store=store, runner=runner_returning(2),
    )
    assert d.ok is True
    assert store.load("v3").stage(1).outcome.status == StageStatus.PASSED.value


def test_no_command_keeps_flag_only_behaviour(store):
    store.save(_executing("v4", _stage(1, verify_command=None)))
    d = cli.cmd_record_result(
        ns(session="v4", status="passed", actual="ok", control=None),
        store=store, runner=boom,  # proves the runner is not invoked
    )
    assert d.ok is True
    assert store.load("v4").stage(1).outcome.status == StageStatus.PASSED.value


def test_acceptance_review_stage_skips_command(store):
    store.save(_executing(
        "v5", _stage(1, verify_command="false", criterion_type="acceptance_review")))
    d = cli.cmd_record_result(
        ns(session="v5", status="passed", actual="ok", control=None),
        store=store, runner=boom,  # acceptance-review is not machine-checked
    )
    assert d.ok is True
    assert store.load("v5").stage(1).outcome.status == StageStatus.PASSED.value


# --- verify-final re-runs commands as the final gate -------------------------

def test_verify_final_blocks_on_failing_command(store):
    stage = _stage(1, verify_command="false", expected_exit=0,
                   status=StageStatus.PASSED.value)
    store.save(_verifying("vf1", stage))
    d = cli.cmd_verify_final(ns(session="vf1"), store=store, runner=runner_returning(1))
    assert d.ok is False
    assert "failures" in d.data
    # RESOLUTION refused — stays at VERIFYING rather than trusting the PASSED flag
    assert store.load("vf1").node == Node.VERIFYING.value


def test_verify_final_passes_when_command_matches(store):
    stage = _stage(1, verify_command="true", expected_exit=0,
                   status=StageStatus.PASSED.value)
    store.save(_verifying("vf2", stage))
    d = cli.cmd_verify_final(ns(session="vf2"), store=store, runner=runner_returning(0))
    assert d.ok is True
    assert store.load("vf2").node == Node.RESOLUTION.value


def test_verify_final_no_commands_passes_without_runner(store):
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying("vf3", stage))
    d = cli.cmd_verify_final(ns(session="vf3"), store=store, runner=boom)
    assert d.ok is True
    assert store.load("vf3").node == Node.RESOLUTION.value
