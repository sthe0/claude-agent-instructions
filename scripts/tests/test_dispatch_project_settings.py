"""agentctl dispatch forwards the delivery venue's own `.claude/settings.local.json`
to a spawned `spawn:developer` via `--project-settings`, so a project that has
already, deliberately, allow-listed its own build/test commands (e.g.
`Bash(python3 scripts/test_render_site.py:*)`) doesn't have to fight the spawn
sandbox on every stage. Before this fix `dispatch_stage` only ever forwarded
the fleet-wide KIND_BASELINES["developer"] list, scoped to this repo's own
verifiers, regardless of the target project.

Covers both layers: `dispatch_stage`/`build_argv`'s injectable
`project_settings` threading, and `cmd_dispatch` resolving the concrete path
from `state.resolve_check_venue(DELIVERY)` and forwarding it only when the
file actually exists.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult, build_argv, dispatch_stage
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


# --- unit: build_argv / dispatch_stage threading --------------------------------

def test_build_argv_forwards_project_settings_when_given():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml", project_settings="/repo/.claude/settings.local.json")
    assert "--project-settings" in argv
    idx = argv.index("--project-settings")
    assert argv[idx + 1] == "/repo/.claude/settings.local.json"


def test_build_argv_omits_project_settings_when_unset():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml")
    assert "--project-settings" not in argv


def test_dispatch_stage_threads_project_settings_to_build_argv():
    stage = _make_spawn_stage()
    seen = []

    def runner(argv, cwd=None):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(
        stage, "/tmp/plan.toml", runner=runner, cwd="/repo",
        project_settings="/repo/.claude/settings.local.json",
    )
    assert "--project-settings" in seen[0]


# --- integration: cmd_dispatch resolving the path from session state -----------

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


def test_cmd_dispatch_forwards_project_settings_when_delivery_venue_has_one(store, fixtures_dir, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}', encoding="utf-8")

    sid = "project-settings-present"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = str(tmp_path)
    state.repo_root = str(tmp_path)
    store.save(state)

    seen_argv = []

    def runner(argv, cwd=None):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert "--project-settings" in seen_argv[0]
    idx = seen_argv[0].index("--project-settings")
    assert seen_argv[0][idx + 1] == str(settings_path)


def test_cmd_dispatch_omits_project_settings_when_delivery_venue_has_none(store, fixtures_dir, tmp_path):
    sid = "project-settings-absent"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = str(tmp_path)
    state.repo_root = str(tmp_path)
    store.save(state)

    seen_argv = []

    def runner(argv, cwd=None):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert "--project-settings" not in seen_argv[0]


def test_cmd_dispatch_omits_project_settings_when_no_venue_resolves(store, fixtures_dir):
    sid = "project-settings-no-venue"
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
    assert "--project-settings" not in calls[0]
