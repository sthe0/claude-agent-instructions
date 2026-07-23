"""Cluster B — dispatch semantics (#7 executor vocabulary, #10 pure dry-run,
#13 explicit spawn wording, #43 dependent-stage worktree continuity).

#7:  parse_plan rejects executors outside {in_thread, spawn:<kind>} at submission
     (a typo silently defaulting to in-thread degraded whole plans); the OLD /
     approved-snapshot side of cmd_replan loads with strict_executor=False so
     plans approved before the vocabulary existed stay diffable.
#10: cmd_dispatch --dry-run is a pure preview — no event log, no state save, no
     marker routing.
#13: cmd_next_stage's dispatch directive says the spawn happens via `agentctl
     dispatch` itself (synchronous, blocking) — never manually.
#43: a stage that depends on a prior SPAWN stage gets --continue-worktree threaded
     through build_argv/cmd_dispatch, naming the prior stage's shared worktree so
     the next developer builds on it instead of forking fresh off origin/main; an
     independent stage (or one with no spawn dependency) never receives the flag.
"""
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult, build_argv
from agentctl.plan import PlanError, load_plan, parse_plan
from agentctl.state import Actor, Criterion, Means, Node, Stage, StageStatus, Subject


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


def _stage_dict(executor: str) -> dict:
    return {
        "index": 1,
        "title": "x",
        "executor": executor,
        "expected_result_image": "i",
        "done_criterion": "c",
    }


def _plan_data(executor: str) -> dict:
    return {"meta": {"task_id": "t"}, "stage": [_stage_dict(executor)]}


# --- #7: executor vocabulary ---------------------------------------------------

@pytest.mark.parametrize("executor", ["in_thread", "spawn:developer", "spawn:code-reviewer"])
def test_vocabulary_executor_accepted(executor):
    doc = parse_plan(_plan_data(executor))
    assert doc.stages[0].actor.executor == executor


@pytest.mark.parametrize("executor", [
    "inthread",
    "spawn developer",
    "spawn:Agent (sonnet explorer); root synthesizes",
    "the coordinator edits in place",
    "spawn:",
])
def test_out_of_vocabulary_executor_rejected(executor):
    with pytest.raises(PlanError, match="outside the vocabulary"):
        parse_plan(_plan_data(executor))


def test_strict_executor_false_tolerates_legacy():
    doc = parse_plan(_plan_data("prose description of who does it"),
                     strict_executor=False)
    assert doc.stages[0].actor.executor == "prose description of who does it"


def test_load_plan_strict_by_default(tmp_path):
    p = tmp_path / "legacy.toml"
    p.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\nindex = 1\ntitle = "x"\n'
        'executor = "free text executor"\n'
        'expected_result_image = "i"\ndone_criterion = "c"\n',
        encoding="utf-8",
    )
    with pytest.raises(PlanError):
        load_plan(p)
    doc = load_plan(p, strict_executor=False)
    assert doc.stages[0].actor.executor == "free text executor"


def _to_planning(store, sid):
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)


def test_submit_plan_rejects_out_of_vocabulary_executor(store, tmp_path):
    sid = "voc1"
    _to_planning(store, sid)
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\nindex = 1\ntitle = "x"\n'
        'executor = "developer, spawned manually"\n'
        'expected_result_image = "i"\ndone_criterion = "c"\n',
        encoding="utf-8",
    )
    with pytest.raises(PlanError, match="outside the vocabulary"):
        cli.cmd_submit_plan(ns(session=sid, plan=str(bad)), store=store)
    assert store.load(sid).node == Node.PLANNING.value  # nothing recorded


def test_replan_old_side_tolerates_legacy_executor(store, fixtures_dir, tmp_path):
    """A plan approved before the vocabulary existed (free-text executor) must not
    brick its session's replan: the OLD side loads with strict_executor=False."""
    sid = "voc2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)

    legacy = tmp_path / "legacy_approved.toml"
    legacy.write_text(
        '[meta]\ntask_id = "t"\n'
        'goal = "g"\ndone_criterion = "dc"\ncriterion_type = "measurable"\n'
        '[[stage]]\nindex = 1\ntitle = "Scaffold module"\n'
        'executor = "spawn:Agent (sonnet explorer); root synthesizes"\n'
        'expected_result_image = "i"\ndone_criterion = "c"\n',
        encoding="utf-8",
    )
    state = store.load(sid)
    state.plan_path = str(legacy)
    state.plan_snapshot_path = None  # simulate a pre-#8 session: no snapshot
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=plan, coverage_waiver=None), store=store)
    # substantive diff (executor/stage set changed) re-arms the approval gate —
    # the point is that the legacy OLD side parsed instead of raising PlanError
    assert d.ok is True


# --- #10: pure dry-run ----------------------------------------------------------

def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def test_dry_run_is_pure_preview(store, fixtures_dir):
    sid = "dry1"
    _to_executing(store, sid, fixtures_dir)
    before = store.path(sid).read_bytes()
    events_before = len(store.load(sid).history)

    seen_argv = []
    def runner(argv):
        seen_argv.append(argv)
        return RunResult(0, stdout="python3 spawn-specialist.py --kind developer ...\n")

    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=True), store=store, runner=runner)
    assert d.ok is True
    assert d.action == "preview"
    assert d.marker is None  # no marker routing
    assert "no state change" in d.detail
    assert d.data["stdout"].startswith("python3 spawn-specialist.py")
    assert "--dry-run" in seen_argv[0]

    after = store.load(sid)
    assert len(after.history) == events_before  # no event logged
    assert after.node == Node.EXECUTING.value
    assert after.stage(1).outcome.status == StageStatus.ACTIVE.value
    assert store.path(sid).read_bytes() == before  # byte-identical state file


def test_dry_run_never_routes_completed_marker(store, fixtures_dir):
    """Even if the previewed command's stdout resembles a marker, a dry-run must
    not route it — the stage stays ACTIVE and no record_result is suggested."""
    sid = "dry2"
    _to_executing(store, sid, fixtures_dir)
    runner = lambda argv: RunResult(0, stdout="COMPLETED: looks like a marker\n")
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=True), store=store, runner=runner)
    assert d.action == "preview"
    assert d.marker is None
    assert store.load(sid).stage(1).outcome.status == StageStatus.ACTIVE.value


# --- #13: explicit spawn wording -------------------------------------------------

def test_next_stage_directive_names_dispatch_as_the_spawn(store, fixtures_dir):
    sid = "word1"
    d = _to_executing(store, sid, fixtures_dir)
    assert d.action == "dispatch"
    assert "via agentctl dispatch" in d.detail
    assert "synchronous, blocking" in d.detail
    assert "do NOT spawn manually" in d.detail
    assert "spawn:developer" in d.detail


def test_next_stage_in_thread_detail_unchanged(store, tmp_path):
    sid = "word2"
    plan = tmp_path / "inthread.toml"
    plan.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\nindex = 1\ntitle = "x"\nexecutor = "in_thread"\n'
        'expected_result_image = "i"\ndone_criterion = "c"\n',
        encoding="utf-8",
    )
    _to_planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.action == "execute_in_thread"
    assert "spawn" not in d.detail.lower()


# --- #43: dependent-stage worktree continuity -----------------------------------

def test_build_argv_threads_continue_worktree_when_given():
    stage = _make_spawn_stage(index=2)
    argv = build_argv(stage, "/tmp/plan.toml", continue_worktree="/repo/.claude/worktrees/t")
    assert "--continue-worktree" in argv
    idx = argv.index("--continue-worktree")
    assert argv[idx + 1] == "/repo/.claude/worktrees/t"


def test_build_argv_omits_continue_worktree_when_unset():
    stage = _make_spawn_stage(index=2)
    argv_default = build_argv(stage, "/tmp/plan.toml")
    argv_explicit_none = build_argv(stage, "/tmp/plan.toml", continue_worktree=None)
    assert "--continue-worktree" not in argv_default
    assert argv_default == argv_explicit_none  # byte-identical to pre-flag behaviour


def test_cmd_dispatch_continues_worktree_for_dependent_spawn_stage(store, fixtures_dir):
    """Stage 2 of plan_two_stage.toml depends_on [1] and both stages are
    spawn:developer — cmd_dispatch must thread --continue-worktree for stage 2
    (naming the shared delivery worktree) but never for independent stage 1."""
    sid = "cont1"
    _to_executing(store, sid, fixtures_dir)  # stage 1 now ACTIVE
    state = store.load(sid)
    state.delivery_worktree = "/repo/.claude/worktrees/demo-two-stage"
    store.save(state)

    seen_argv = []

    def runner(argv, cwd=None):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert "--continue-worktree" not in seen_argv[0]  # stage 1 has no dependency

    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                            control="reviewed: ok"), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)  # activates stage 2

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    argv2 = seen_argv[1]
    assert "--continue-worktree" in argv2
    idx = argv2.index("--continue-worktree")
    assert argv2[idx + 1] == "/repo/.claude/worktrees/demo-two-stage"


def test_cmd_dispatch_omits_continue_worktree_without_delivery_worktree_or_repo_root(store, fixtures_dir):
    """Even a dependent spawn stage gets no continuation when the session carries
    neither delivery_worktree nor repo_root (nothing to anchor a default path to)."""
    sid = "cont2"
    _to_executing(store, sid, fixtures_dir)
    assert store.load(sid).delivery_worktree is None
    assert store.load(sid).repo_root is None

    seen_argv = []

    def runner(argv):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                            control="reviewed: ok"), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert "--continue-worktree" not in seen_argv[1]
