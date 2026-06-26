"""F1: a stage's verify_command runs in the plan's repo_root, not the invoker cwd.

Two layers of proof:
  - unit (argv capture): cwd set -> the bash -c string carries a `cd <root> &&`
    prefix; cwd None -> the string is byte-identical to the bare verify_command;
  - behavioural (real subprocess): a REPO-RELATIVE verify path resolves only when
    repo_root points at the dir holding it — the discriminating real-world case.
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


def _stage(verify_command, expected_exit=0):
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, expected_exit=expected_exit,
        ),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _executing(sid, stage, repo_root):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value, repo_root=repo_root,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = 1
    return s


# --- unit: argv capture proves the cd-prefix is applied iff repo_root is set ---

class _Capture:
    def __init__(self, code=0):
        self.code, self.argv = code, None

    def __call__(self, argv):
        self.argv = argv
        return RunResult(self.code, stdout="", stderr="")


def test_cd_prefix_applied_when_repo_root_set(store):
    cap = _Capture()
    store.save(_executing("c1", _stage("pytest -q"), repo_root="/abs/the repo"))
    cli.cmd_record_result(
        ns(session="c1", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    # cwd applied INSIDE the bash -c string; Runner protocol (argv) is unchanged.
    assert cap.argv[:2] == ["bash", "-c"]
    assert cap.argv[2] == "cd '/abs/the repo' && pytest -q"  # shlex-quoted path


def test_no_prefix_when_repo_root_none(store):
    cap = _Capture()
    store.save(_executing("c2", _stage("pytest -q"), repo_root=None))
    cli.cmd_record_result(
        ns(session="c2", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    # byte-identical to the pre-repo_root behaviour: bare command, no cd.
    assert cap.argv == ["bash", "-c", "pytest -q"]


# --- behavioural: a repo-relative verify path resolves only in repo_root --------

SENTINEL = "sentinel_f1_verify_cwd"


def test_relative_path_resolves_when_repo_root_points_at_it(store, tmp_path):
    (tmp_path / SENTINEL).write_text("x", encoding="utf-8")
    store.save(_executing("b1", _stage(f"test -f {SENTINEL}"), repo_root=str(tmp_path)))
    # real subprocess (runner=None): cd tmp_path && test -f sentinel -> exit 0
    d = cli.cmd_record_result(
        ns(session="b1", status="passed", actual="ok", control=None), store=store,
    )
    assert d.ok is True
    assert store.load("b1").stage(1).outcome.status == StageStatus.PASSED.value


def test_relative_path_fails_without_repo_root(store, tmp_path):
    # same sentinel exists in tmp_path, but repo_root is None -> command runs in the
    # invoker cwd (not tmp_path) -> relative path misses -> the passed claim is
    # OVERRIDDEN to a failure. This is exactly the F1 bug, now made observable.
    (tmp_path / SENTINEL).write_text("x", encoding="utf-8")
    store.save(_executing("b2", _stage(f"test -f {SENTINEL}"), repo_root=None))
    d = cli.cmd_record_result(
        ns(session="b2", status="passed", actual="ok", control=None), store=store,
    )
    assert d.ok is False
    assert store.load("b2").stage(1).outcome.status == StageStatus.FAILED.value
