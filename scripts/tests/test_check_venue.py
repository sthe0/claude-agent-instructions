"""Venue symmetry: cmd_dispatch, cmd_record_result, and cmd_verify_final (both its
per-stage re-verify and its [[final_check]] loop) resolve a stage's check venue
through the ONE shared resolver (SessionState.resolve_check_venue), so dispatch
and verification always observe the same tree instead of the venue-asymmetry
defect where dispatch wrote to delivery_worktree while verification silently
checked repo_root.

Two layers of proof, mirroring test_verify_cwd.py:
  - unit (argv capture): the bash -c string carries a `cd <venue> &&` prefix
    matching the declared venue at each of the three verify sites;
  - refusal: a declared delivery_worktree that does not exist on disk refuses
    the check (distinct from a check failure) without failing the stage or
    entering DIAGNOSING.
"""
import shlex
from argparse import Namespace

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


def _stage(verify_command, expected_exit=0, verify_venue="delivery",
           status=StageStatus.ACTIVE.value, index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, expected_exit=expected_exit,
            verify_venue=verify_venue,
        ),
        outcome=Outcome(status=status),
    )


def _executing(sid, stage, repo_root=None, delivery_worktree=None):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value, repo_root=repo_root,
        delivery_worktree=delivery_worktree,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = 1
    return s


def _verifying(sid, stages, repo_root=None, delivery_worktree=None, final_check=None):
    return SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value, repo_root=repo_root,
        delivery_worktree=delivery_worktree,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=stages,
        final_check=final_check or [],
    )


class _Capture:
    """Captures the single argv of the one call expected in a cmd_record_result test."""

    def __init__(self, code=0):
        self.code, self.argv = code, None

    def __call__(self, argv):
        self.argv = argv
        return RunResult(self.code, stdout="", stderr="")


class _CaptureAll:
    """Captures every argv across cmd_verify_final's stage + final_check loops."""

    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv):
        self.calls.append(argv)
        return RunResult(self.code, stdout="", stderr="")


# --- cmd_record_result resolves the declared venue, symmetric with dispatch ----

def test_record_result_verifies_in_delivery_worktree(store, tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    cap = _Capture()
    s = _executing("v1", _stage("pytest -q"), repo_root=str(repo_root),
                    delivery_worktree=str(worktree))
    store.save(s)
    cli.cmd_record_result(
        ns(session="v1", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    assert cap.argv[:2] == ["bash", "-c"]
    assert cap.argv[2] == f"cd {shlex.quote(str(worktree))} && pytest -q"


def test_no_delivery_worktree_is_byte_identical_to_repo_root_cwd(store):
    """Regression control: with delivery_worktree unset (the common case — 152/171
    existing plans), adding verify_venue (default "delivery") must not change a
    single byte of the executed command versus the pre-fix, repo_root-only path."""
    cap = _Capture()
    s = _executing("v2", _stage("pytest -q"), repo_root="/abs/the repo")
    store.save(s)
    cli.cmd_record_result(
        ns(session="v2", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    assert cap.argv == ["bash", "-c", "cd '/abs/the repo' && pytest -q"]


# --- cmd_verify_final: per-stage re-verify and [[final_check]] loop -------------

def test_verify_final_stage_reverify_uses_delivery_worktree(store, tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "w3"
    worktree.mkdir(parents=True)
    cap = _CaptureAll()
    stage = _stage("pytest -q", status=StageStatus.PASSED.value)
    s = _verifying("v3", [stage], repo_root=str(repo_root),
                    delivery_worktree=str(worktree))
    store.save(s)
    d = cli.cmd_verify_final(ns(session="v3"), store=store, runner=cap)
    assert d.ok is True
    assert cap.calls == [["bash", "-c", f"cd {shlex.quote(str(worktree))} && pytest -q"]]


def test_final_check_defaults_to_delivery_worktree(store, tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "w4"
    worktree.mkdir(parents=True)
    cap = _CaptureAll()
    stage = _stage("true", status=StageStatus.PASSED.value)
    fc = FinalCheck(command="pytest -q")  # venue unset -> default "delivery"
    s = _verifying("v4", [stage], repo_root=str(repo_root),
                    delivery_worktree=str(worktree), final_check=[fc])
    store.save(s)
    d = cli.cmd_verify_final(ns(session="v4"), store=store, runner=cap)
    assert d.ok is True
    # last call is the final_check (the stage re-verify runs first).
    assert cap.calls[-1] == ["bash", "-c", f"cd {shlex.quote(str(worktree))} && pytest -q"]


def test_final_check_repo_root_venue_opt_in_uses_canon(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w5"
    worktree.mkdir(parents=True)
    cap = _CaptureAll()
    stage = _stage("true", status=StageStatus.PASSED.value)
    fc = FinalCheck(command="pytest -q", venue="repo_root")
    s = _verifying("v5", [stage], repo_root=str(repo_root),
                    delivery_worktree=str(worktree), final_check=[fc])
    store.save(s)
    d = cli.cmd_verify_final(ns(session="v5"), store=store, runner=cap)
    assert d.ok is True
    assert cap.calls[-1] == ["bash", "-c", f"cd {shlex.quote(str(repo_root))} && pytest -q"]


# --- refusal: a declared but missing venue refuses without failing the stage ----

def test_missing_delivery_worktree_refuses_without_failing_stage(store, tmp_path):
    cap = _Capture()
    missing = str(tmp_path / "does-not-exist")
    s = _executing("v6", _stage("pytest -q"), repo_root="/repo",
                    delivery_worktree=missing)
    store.save(s)
    d = cli.cmd_record_result(
        ns(session="v6", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    assert d.ok is False
    assert d.action == "fix_venue"
    assert cap.argv is None  # refusal short-circuits before the check ever runs
    reloaded = store.load("v6")
    assert reloaded.stage(1).outcome.status != StageStatus.FAILED.value
    assert reloaded.node != Node.DIAGNOSING.value
