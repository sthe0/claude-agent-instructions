"""Stage 6: the opt-in re-attest route (cmd_dispatch --re-attest / _try_reattest).

A stage recorded PASSED, then re-armed by a substantive replan, may skip a full
specialist (re-)spawn IFF all three hold: (1) a ReattestStash exists for it, (2)
the replan did not touch its operative surface (method/control criterion/expected
result image/executor/done criterion) AND the stage was not edited again since,
(3) its own control (verify_command, in its declared venue) passes when RE-RUN
NOW. Any single failure refuses and falls through to the existing, unmodified
dispatch path — never carrying a stale PASS forward. The control-fails-now case
comes first per the brief: this route must never trust the stashed record over
what actually runs today.

Direct SessionState/Stage/ReattestStash construction (test_code_review.py's
style) rather than a full plan-submission cycle, since a fixture plan is not
needed to exercise cmd_dispatch once a session is already EXECUTING with a
bound runtime_host."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import stage_reattest_digest
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    ReattestStash,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)
from lib.runtime_models import HOST_CLAUDE


def _dev_stage(index=1, verify_command="true", control=None,
               method="implement widget", done_criterion="tests green",
               result="widget works"):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result=result),
        means=Means(means="Edit", method=method),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion=done_criterion,
                             verify_command=verify_command),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
        control=control,
    )


def _reattest_stash(stage, *, matched=True, prior_control="reviewed: prior pass",
                     prior_actual="prior pass", digest_override=None):
    return ReattestStash(
        stage_index=stage.index,
        operative_surface_matched=matched,
        prior_outcome=Outcome(status=StageStatus.PASSED.value, actual=prior_actual,
                               cost_usd=1.0, duration_ms=1000, spawn_count=1),
        prior_control=prior_control,
        reattest_digest=digest_override if digest_override is not None else stage_reattest_digest(stage),
    )


def _state(stage, *, reattest_stash=(), code_reviews=(), weight="SUBSTANTIVE"):
    return SessionState(
        session_id="ra", task_id="ra-task",
        node=Node.EXECUTING.value,
        weight_class=weight,
        route="SPAWN" if weight == "SUBSTANTIVE" else "INLINE",
        runtime_host=HOST_CLAUDE,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(verdict="not-recommended"),
        stages=[stage],
        current_stage=stage.index,
        plan_path="/tmp/plan.toml",
        reattest_stash=list(reattest_stash),
        code_reviews=list(code_reviews),
    )


class _Mem:
    def __init__(self, state):
        self.s = state

    def load(self, _):
        return self.s

    def save(self, s):
        self.s = s


def _runner(*, control_returncode=0, control_stdout="", control_stderr="",
            spawn_returncode=0, spawn_stdout="INCOMPLETE: still working\n"):
    """A fake runner distinguishing the control re-run (`bash -c ...`) from a
    real specialist (re-)spawn (`python3 spawn-specialist.py ...`) by argv[0],
    so a test can assert exactly which of the two actually ran."""
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        if argv[0] == "bash":
            return RunResult(control_returncode, control_stdout, control_stderr)
        return RunResult(spawn_returncode, spawn_stdout, "")

    runner.calls = calls
    return runner


def _dispatch(store, runner):
    return cli.cmd_dispatch(Namespace(session="ra", re_attest=True), store=store, runner=runner)


def _declined_reason(store):
    events = [e for e in store.s.history if e["event"] == "reattest_declined"]
    assert events, "expected a reattest_declined log event"
    return events[-1]["reason"]


# --- (c) the control fails on re-run: never carry a stale PASS forward ------

def test_reattest_refuses_when_control_fails_on_rerun():
    stage = _dev_stage()
    stash = _reattest_stash(stage)
    store = _Mem(_state(stage, reattest_stash=[stash]))
    runner = _runner(control_returncode=1, control_stderr="boom")

    d = _dispatch(store, runner)

    # falls through to the existing, unmodified dispatch path — the INCOMPLETE
    # routing below only happens if dispatch_stage's real spawn call ran
    assert d.marker == "INCOMPLETE"
    assert d.action == "decide_incomplete"
    assert store.s.stages[0].outcome.status == StageStatus.ACTIVE.value
    assert "control failed on re-run" in _declined_reason(store)
    assert [c[0] for c in runner.calls] == ["bash", "python3"]


# --- (a) all three conditions hold: re-attests without a real spawn --------

def test_reattest_succeeds_when_all_three_conditions_hold():
    stage = _dev_stage()
    stash = _reattest_stash(stage, prior_control="reviewed: prior pass",
                             prior_actual="prior work done")
    store = _Mem(_state(stage, reattest_stash=[stash]))
    runner = _runner(control_returncode=0)

    d = _dispatch(store, runner)

    assert d.ok is True
    assert d.action == "verify_final"  # the only stage; all_stages_passed() now true
    s = store.s.stages[0]
    assert s.outcome.status == StageStatus.PASSED.value
    assert "[re_attested]" in s.outcome.actual
    assert "prior work done" in s.outcome.actual
    assert s.control == "reviewed: prior pass"
    assert store.s.node == Node.VERIFYING.value
    assert store.s.current_stage is None
    assert any(e["event"] == "reattest" for e in store.s.history)
    # the control re-run is the ONLY thing that ran — no full specialist re-spawn
    assert [c[0] for c in runner.calls] == ["bash"]


# --- (b) replan touched the stage's operative surface -----------------------

def test_reattest_refuses_when_replan_touched_operative_surface():
    stage = _dev_stage()
    stash = _reattest_stash(stage, matched=False)
    store = _Mem(_state(stage, reattest_stash=[stash]))
    runner = _runner()

    d = _dispatch(store, runner)

    assert d.marker == "INCOMPLETE"
    assert store.s.stages[0].outcome.status == StageStatus.ACTIVE.value
    assert "operative surface" in _declined_reason(store)
    # refused before the control was even re-run
    assert [c[0] for c in runner.calls] == ["python3"]


# --- (d) no prior PASSED outcome recorded for this stage --------------------

def test_reattest_refuses_when_no_prior_passed_outcome_is_stashed():
    stage = _dev_stage()
    store = _Mem(_state(stage, reattest_stash=[]))
    runner = _runner()

    d = _dispatch(store, runner)

    assert d.marker == "INCOMPLETE"
    assert "no prior PASSED outcome" in _declined_reason(store)
    assert [c[0] for c in runner.calls] == ["python3"]


# --- (e) a code-review-gated stage keeps its bound review requirement ------

def test_reattest_does_not_bypass_the_code_review_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "1")
    stage = _dev_stage()
    stash = _reattest_stash(stage, prior_control="reviewed: prior pass")
    store = _Mem(_state(stage, reattest_stash=[stash], code_reviews=[]))
    runner = _runner(control_returncode=0)

    d = _dispatch(store, runner)

    assert d.marker == "INCOMPLETE"
    assert store.s.stages[0].outcome.status == StageStatus.ACTIVE.value
    assert "code review" in _declined_reason(store)
    # the control re-run DID pass — only the review gate blocked it, proving
    # the same review requirement cmd_record_result enforces binds here too
    assert [c[0] for c in runner.calls] == ["bash", "python3"]
