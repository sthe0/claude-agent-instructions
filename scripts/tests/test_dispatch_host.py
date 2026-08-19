"""Host-isolated spawn: `runtime_host` selects which wrapper script a dispatch
shells out to (spawn_cli_for / build_argv / dispatch_stage / cmd_dispatch), and
cmd_dispatch refuses outright when the session never bound a host.

Done criteria this file pins directly:
  - runtime_host=cursor -> dispatch (dry-run or real) invokes spawn-cursor-
    specialist.py, never spawn-specialist.py (`claude`'s wrapper).
  - runtime_host=claude -> dispatch invokes spawn-specialist.py, never
    spawn-cursor-specialist.py (`agent`'s wrapper).
  - an unbound session's dispatch refuses with an explicit error rather than
    silently defaulting to either host.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import (
    SPAWN_CLI,
    SPAWN_CLI_CURSOR,
    RunResult,
    build_argv,
    dispatch_stage,
    spawn_cli_for,
)
from agentctl.state import Actor, Criterion, Means, Stage, Subject
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR


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


def _capture_runner(seen):
    def runner(argv, cwd=None):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")
    return runner


# --- spawn_cli_for ---------------------------------------------------------------

def test_spawn_cli_for_claude_is_the_claude_wrapper():
    assert spawn_cli_for(HOST_CLAUDE) == SPAWN_CLI
    assert SPAWN_CLI.name == "spawn-specialist.py"


def test_spawn_cli_for_cursor_is_the_cursor_wrapper():
    assert spawn_cli_for(HOST_CURSOR) == SPAWN_CLI_CURSOR
    assert SPAWN_CLI_CURSOR.name == "spawn-cursor-specialist.py"


def test_spawn_cli_for_unknown_host_raises():
    try:
        spawn_cli_for("windows")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown host")


# --- build_argv ------------------------------------------------------------------

def test_build_argv_defaults_to_claude_wrapper():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml")
    assert argv[1] == str(SPAWN_CLI)
    assert "spawn-cursor-specialist.py" not in argv[1]


def test_build_argv_cursor_host_selects_cursor_wrapper():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml", runtime_host=HOST_CURSOR)
    assert argv[1] == str(SPAWN_CLI_CURSOR)
    assert argv[1] != str(SPAWN_CLI)


# --- dispatch_stage ----------------------------------------------------------------

def test_dispatch_stage_claude_host_never_names_the_cursor_wrapper():
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen), runtime_host=HOST_CLAUDE)
    assert seen[0][1] == str(SPAWN_CLI)
    assert "spawn-cursor-specialist.py" not in seen[0][1]


def test_dispatch_stage_cursor_host_never_names_the_claude_wrapper():
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen), runtime_host=HOST_CURSOR)
    assert seen[0][1] == str(SPAWN_CLI_CURSOR)
    assert seen[0][1] != str(SPAWN_CLI)


# --- cmd_dispatch integration: host threads from the bound session ---------------

def _to_executing(store, sid, fixtures_dir, host=None):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0, host=host), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False, host=host), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def test_cmd_dispatch_claude_session_uses_claude_wrapper(store, fixtures_dir):
    sid = "host-claude"
    _to_executing(store, sid, fixtures_dir, host=HOST_CLAUDE)
    seen = []
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium", dry_run=False),
                     store=store, runner=_capture_runner(seen))
    assert seen[0][1] == str(SPAWN_CLI)


def test_cmd_dispatch_cursor_session_uses_cursor_wrapper(store, fixtures_dir):
    sid = "host-cursor"
    _to_executing(store, sid, fixtures_dir, host=HOST_CURSOR)
    seen = []
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium", dry_run=False),
                     store=store, runner=_capture_runner(seen))
    assert seen[0][1] == str(SPAWN_CLI_CURSOR)


def test_cmd_dispatch_refuses_when_session_never_bound_a_host(store, fixtures_dir):
    sid = "host-unbound"
    _to_executing(store, sid, fixtures_dir, host=HOST_CLAUDE)
    state = store.load(sid)
    # Force the pre-schema-25 / never-classified shape: no bound host.
    state.runtime_host = None
    store.save(state)
    seen = []
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium", dry_run=False),
                         store=store, runner=_capture_runner(seen))
    assert d.ok is False
    assert seen == []  # never reached the runner
