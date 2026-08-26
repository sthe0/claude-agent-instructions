"""agentctl dispatch must always hand spawn-specialist.py an --effort value.

spawn-specialist.py now hard-requires --effort (see test_spawn_specialist_effort.py):
an omitted flag is an argparse-level SystemExit(2), "the following arguments are
required: --effort". Before this file, every dispatch_stage/build_argv test used a
MOCKED Runner, so a missing --effort in the assembled argv was invisible — the mock
never runs the real parser that would refuse it. This is exactly the class of miss a
code review caught (dispatch.py's build_argv never forwarded --effort, and the
`dispatch` subcommand had no --effort flag to accept one from the caller).

Tests cover:
- build_argv/dispatch_stage always include "--effort <value>" in the assembled argv
- cmd_dispatch derives the value from the stage's cost_tier when --effort is omitted
  (small->low, medium->medium, large->high; absent cost_tier->medium), the same
  resolution shape --budget already uses for its own cost_tier fallback
- an explicit --effort on the dispatch subcommand overrides the cost_tier derivation
- the REAL (non-mocked) spawn-specialist.py argparse parser accepts build_argv's
  assembled argv without complaint -- the check that would have caught the original
  miss, since a mocked Runner never exercises the real parser at all
"""
from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult, build_argv, dispatch_stage
from agentctl.state import Actor, Criterion, Means, Stage, Subject

SCRIPTS = Path(__file__).resolve().parent.parent
SPAWN_SPECIALIST = SCRIPTS / "spawn-specialist.py"


def ns(**kw):
    return Namespace(**kw)


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location("spawn_specialist_for_dispatch_effort", SPAWN_SPECIALIST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN_SPECIALIST_MOD = _load_spawn_specialist()


def _make_spawn_stage(index: int = 1) -> Stage:
    return Stage(
        index=index,
        title="test stage",
        subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="tests green"),
    )


def _to_planning(store, sid):
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)


def _to_executing(store, sid, plan_path):
    _to_planning(store, sid)
    cli.cmd_submit_plan(ns(session=sid, plan=plan_path), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _plan_with_cost_tier(tmp_path, cost_tier: str | None) -> str:
    tier_line = f'cost_tier = "{cost_tier}"\n' if cost_tier else ""
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[meta]\nweight_class = "small_change"\ntask_id = "t"\n'
        'goal = "g"\ndone_criterion = "dc"\ncriterion_type = "measurable"\n'
        '[[stage]]\nindex = 1\ntitle = "x"\nexecutor = "spawn:developer"\n'
        'expected_result_image = "i"\ndone_criterion = "c"\n'
        f'{tier_line}',
        encoding="utf-8",
    )
    return str(plan)


# ---------------------------------------------------------------------------
# build_argv / dispatch_stage: --effort always present
# ---------------------------------------------------------------------------

def test_build_argv_includes_effort_default():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml")
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "medium"


def test_build_argv_includes_effort_explicit():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml", effort="xhigh")
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_dispatch_stage_forwards_effort_to_runner():
    stage = _make_spawn_stage()
    seen_argv = []

    def runner(argv):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner, effort="high", dry_run=True)
    assert "--effort" in seen_argv[0]
    assert seen_argv[0][seen_argv[0].index("--effort") + 1] == "high"


# ---------------------------------------------------------------------------
# cmd_dispatch: cost_tier-derived default, explicit override
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cost_tier,expected_effort", [
    ("small", "low"),
    ("medium", "medium"),
    ("large", "high"),
    (None, "medium"),
])
def test_cmd_dispatch_derives_effort_from_cost_tier(store, tmp_path, cost_tier, expected_effort):
    sid = f"effort-{cost_tier}"
    plan_path = _plan_with_cost_tier(tmp_path, cost_tier)
    _to_executing(store, sid, plan_path)

    seen_argv = []

    def runner(argv):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=True), store=store, runner=runner)
    assert d.ok is True
    argv = seen_argv[0]
    assert argv[argv.index("--effort") + 1] == expected_effort


def test_cmd_dispatch_explicit_effort_overrides_cost_tier(store, tmp_path):
    sid = "effort-explicit"
    plan_path = _plan_with_cost_tier(tmp_path, "small")  # would derive "low"
    _to_executing(store, sid, plan_path)

    seen_argv = []

    def runner(argv):
        seen_argv.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            effort="max", dry_run=True), store=store, runner=runner)
    assert d.ok is True
    argv = seen_argv[0]
    assert argv[argv.index("--effort") + 1] == "max"


# ---------------------------------------------------------------------------
# Integration: build_argv's output actually parses under spawn-specialist.py's
# REAL parser -- the check a mocked Runner can never provide.
# ---------------------------------------------------------------------------

def test_build_argv_output_parses_under_real_spawn_specialist_parser(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("a plan\n", encoding="utf-8")
    stage = _make_spawn_stage()
    argv = build_argv(stage, str(plan), effort="high", dry_run=True)
    # argv[0:2] is ["python3", "<path to spawn-specialist.py>"] -- not parser input.
    real_args = SPAWN_SPECIALIST_MOD.build_parser().parse_args(argv[2:])
    assert real_args.effort == "high"
