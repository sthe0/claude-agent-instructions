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

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    FinalCheck,
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
        ns(session="v5", status="passed", actual="ok", control=None,
           observation="I ran the module and it executed without errors"),
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


# --- acceptance_review observation gate ---------------------------------------

def test_acceptance_pass_requires_nonempty_observation(store):
    """cmd_record_result refuses an acceptance_review pass with no observation."""
    store.save(_executing("ar1", _stage(1, criterion_type="acceptance_review")))
    d = cli.cmd_record_result(
        ns(session="ar1", status="passed", actual="ok", control=None, observation=""),
        store=store, runner=boom,
    )
    assert d.ok is False
    assert d.action == "attest_observation"
    assert store.load("ar1").stage(1).outcome.status == StageStatus.ACTIVE.value


def test_acceptance_pass_refuses_echoed_expected_image(store):
    """cmd_record_result refuses an observation that echoes the expected image."""
    # _stage has subject.result = "img"
    store.save(_executing("ar2", _stage(1, criterion_type="acceptance_review")))
    d = cli.cmd_record_result(
        ns(session="ar2", status="passed", actual="ok", control=None,
           observation="img"),
        store=store, runner=boom,
    )
    assert d.ok is False
    assert d.action == "attest_observation"
    assert "echoing" in d.detail.lower() or "distinct" in d.detail.lower()


def test_acceptance_pass_refuses_echoed_observation_case_insensitive(store):
    """Echoed observation check is case-insensitive (normalized)."""
    store.save(_executing("ar3", _stage(1, criterion_type="acceptance_review")))
    d = cli.cmd_record_result(
        ns(session="ar3", status="passed", actual="ok", control=None,
           observation="IMG"),
        store=store, runner=boom,
    )
    assert d.ok is False
    assert d.action == "attest_observation"


def test_acceptance_pass_accepts_distinct_observation(store):
    """A distinct non-empty observation passes and is persisted to criterion.observation."""
    store.save(_executing("ar4", _stage(1, criterion_type="acceptance_review")))
    obs = "I navigated to /health and saw status 200 with body {ok: true}"
    d = cli.cmd_record_result(
        ns(session="ar4", status="passed", actual="ok", control=None,
           observation=obs),
        store=store, runner=boom,
    )
    assert d.ok is True
    after = store.load("ar4")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert after.stage(1).criterion.observation == obs


def test_measurable_pass_does_not_require_observation(store):
    """A measurable stage pass proceeds without --observation (unchanged behaviour)."""
    store.save(_executing("ar5", _stage(1, verify_command=None)))
    d = cli.cmd_record_result(
        ns(session="ar5", status="passed", actual="ok", control=None),
        store=store, runner=boom,
    )
    assert d.ok is True
    assert store.load("ar5").stage(1).outcome.status == StageStatus.PASSED.value


def test_acceptance_failed_does_not_require_observation(store):
    """A failed acceptance_review stage never triggers the observation gate."""
    store.save(_executing("ar6", _stage(1, criterion_type="acceptance_review")))
    d = cli.cmd_record_result(
        ns(session="ar6", status="failed", actual="did not work", control=None),
        store=store, runner=boom,
    )
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value  # normal failure path


# --- final_check entries run at verify-final ----------------------------------

def _verifying_with_final_check(sid, stage, final_checks):
    s = _verifying(sid, stage)
    s.final_check = final_checks
    return s


def test_final_check_failure_refuses_resolution(store):
    """A failing final_check refuses the RESOLUTION transition."""
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying_with_final_check(
        "fc1", stage, [FinalCheck(command="false", expected_exit=0, label="suite")]
    ))
    d = cli.cmd_verify_final(
        ns(session="fc1"), store=store, runner=runner_returning(1)
    )
    assert d.ok is False
    assert "failures" in d.data
    assert "suite" in str(d.data["failures"]) or "final_check" in str(d.data["failures"])
    assert store.load("fc1").node == Node.VERIFYING.value


def test_final_check_pass_allows_resolution(store):
    """A passing final_check lets verify-final advance to RESOLUTION."""
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying_with_final_check(
        "fc2", stage, [FinalCheck(command="true", expected_exit=0, label="suite")]
    ))
    d = cli.cmd_verify_final(
        ns(session="fc2"), store=store, runner=runner_returning(0)
    )
    assert d.ok is True
    assert store.load("fc2").node == Node.RESOLUTION.value


def test_no_final_check_behaves_as_before(store):
    """A plan with no final_check reaches RESOLUTION exactly as before (regression guard)."""
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying("fc3", stage))  # final_check defaults to []
    d = cli.cmd_verify_final(
        ns(session="fc3"), store=store, runner=boom  # runner must not be called
    )
    assert d.ok is True
    assert store.load("fc3").node == Node.RESOLUTION.value


def test_final_check_label_in_failure_message(store):
    """The label (or command when label is empty) appears in the failure string."""
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying_with_final_check(
        "fc4", stage,
        [FinalCheck(command="pytest -q tests/", expected_exit=0, label="full suite")]
    ))
    d = cli.cmd_verify_final(
        ns(session="fc4"), store=store, runner=runner_returning(1)
    )
    assert d.ok is False
    assert any("full suite" in f for f in d.data["failures"])


# --- cost rollup at verify-final ----------------------------------------------

def _stage_passed_with_cost(i, cost_usd, duration_ms, spawn_count=1):
    s = _stage(i, verify_command=None, status=StageStatus.PASSED.value)
    s.actor = Actor(executor="spawn:developer")
    s.outcome.cost_usd = cost_usd
    s.outcome.duration_ms = duration_ms
    s.outcome.spawn_count = spawn_count
    return s


def test_verify_final_sets_state_cost_from_stage_outcomes(store):
    """verify-final computes CostRollup from attributed stage outcomes and stores it."""
    stage = _stage_passed_with_cost(1, cost_usd=1.5, duration_ms=3000)
    state = _verifying("cost-vf1", stage)
    store.save(state)

    d = cli.cmd_verify_final(ns(session="cost-vf1"), store=store, runner=boom)
    assert d.ok is True
    after = store.load("cost-vf1")
    assert after.cost is not None
    assert after.cost.total_cost_usd == pytest.approx(1.5)
    assert after.cost.total_duration_ms == 3000
    assert after.cost.spawn_count == 1
    assert after.cost.attributed_stages == 1


def test_verify_final_sums_multiple_stage_costs(store):
    """CostRollup sums costs across all stages."""
    s1 = _stage_passed_with_cost(1, cost_usd=0.5, duration_ms=1000)
    s2 = _stage_passed_with_cost(2, cost_usd=0.8, duration_ms=2000, spawn_count=2)

    state = SessionState(
        session_id="cost-vf2", task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[s1, s2],
    )
    store.save(state)

    d = cli.cmd_verify_final(ns(session="cost-vf2"), store=store, runner=boom)
    assert d.ok is True
    after = store.load("cost-vf2")
    assert after.cost is not None
    assert after.cost.total_cost_usd == pytest.approx(1.3)
    assert after.cost.total_duration_ms == 3000
    assert after.cost.spawn_count == 3
    assert after.cost.attributed_stages == 2
    assert "cost" in d.data
    assert d.data["cost"]["total_cost_usd"] == pytest.approx(1.3)


def test_verify_final_cost_rollup_with_no_attributed_stages(store):
    """A plan with no attributed spawn costs yields a zero/None rollup."""
    stage = _stage(1, verify_command=None, status=StageStatus.PASSED.value)
    store.save(_verifying("cost-vf3", stage))

    d = cli.cmd_verify_final(ns(session="cost-vf3"), store=store, runner=boom)
    assert d.ok is True
    after = store.load("cost-vf3")
    assert after.cost is not None
    assert after.cost.total_cost_usd is None
    assert after.cost.spawn_count == 0
    assert after.cost.attributed_stages == 0
    assert "cost" in d.data
    assert d.data["cost"]["total_cost_usd"] is None
