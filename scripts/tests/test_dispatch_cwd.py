"""Dispatched-spawn cwd pinning: a spawned `claude -p` child is hard-sandboxed to
the git tree it is launched in (its cwd at spawn time), so a dispatch that leaves
cwd unset lets the child inherit whatever tree the coordinator happens to be
running in rather than the plan's delivery worktree. Covers both layers of the
fix: `dispatch_stage`'s injectable cwd threading, and `cmd_dispatch` computing
`state.delivery_worktree or state.repo_root` as that cwd.
"""
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult, dispatch_stage
from agentctl.state import Actor, Criterion, Means, Stage, Subject


def ns(**kw):
    return Namespace(**kw)


def _make_spawn_stage(index: int = 1) -> Stage:
    return Stage(
        index=index,
        title="test stage",
        subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="tests green"),
    )


# --- unit: dispatch_stage's cwd threading ---------------------------------------

def test_dispatch_stage_threads_cwd_to_runner_when_given():
    stage = _make_spawn_stage()
    seen = []

    def runner(argv, cwd=None):
        seen.append(cwd)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner, cwd="/repo/.claude/worktrees/t")
    assert seen == ["/repo/.claude/worktrees/t"]


def test_dispatch_stage_omits_cwd_kwarg_when_unset():
    """Back-compat: with cwd unset, the runner is called exactly as before
    (single positional arg, no cwd kwarg) — every pre-existing fake runner
    defined as `def runner(argv):` keeps working untouched."""
    stage = _make_spawn_stage()
    calls = []

    def runner(argv):
        calls.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner)
    assert len(calls) == 1


# --- integration: cmd_dispatch computing cwd from session state ----------------

def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def test_cmd_dispatch_uses_delivery_worktree_as_cwd(store, fixtures_dir):
    sid = "cwd-delivery"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = "/repo/.claude/worktrees/demo-two-stage"
    state.repo_root = "/repo"
    store.save(state)

    seen_cwd = []

    def runner(argv, cwd=None):
        seen_cwd.append(cwd)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert seen_cwd == ["/repo/.claude/worktrees/demo-two-stage"]


def test_cmd_dispatch_falls_back_to_repo_root_without_delivery_worktree(store, fixtures_dir):
    sid = "cwd-repo-root"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = None
    state.repo_root = "/repo"
    store.save(state)

    seen_cwd = []

    def runner(argv, cwd=None):
        seen_cwd.append(cwd)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert seen_cwd == ["/repo"]


def test_cmd_dispatch_cwd_none_when_neither_set(store, fixtures_dir):
    """Back-compat: neither delivery_worktree nor repo_root set => cwd stays
    None => the runner is invoked exactly as pre-fix (single positional arg,
    the spawned child inherits the invoker's cwd)."""
    sid = "cwd-none"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    assert state.delivery_worktree is None
    assert state.repo_root is None

    calls = []

    def runner(argv):
        calls.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert len(calls) == 1
