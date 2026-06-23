"""cmd_dispatch: recursion-cap refusal routes to BLOCKED + ESCALATE marker."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _start_substantive(store, fixtures_dir):
    """Bring the session to EXECUTING with an active spawn stage."""
    sid = "rc-test"
    plan = str(fixtures_dir / "plan_two_stage.toml")

    cli.cmd_start(ns(session=sid, task="rc-task", goal="g",
                     done_criterion="dc", criterion_type="measurable",
                     recursion_depth=4), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    return sid


def test_recursion_cap_refusal_yields_blocked_escalate(store, fixtures_dir):
    sid = _start_substantive(store, fixtures_dir)

    refused_runner = lambda argv: RunResult(3, stderr="above max-recursion-depth=5\n")
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=False), store=store, runner=refused_runner)

    assert not d.ok
    assert d.node == Node.BLOCKED.value
    assert d.marker == "ESCALATE"


def test_successful_dispatch_routes_to_record_result(store, fixtures_dir):
    sid = _start_substantive(store, fixtures_dir)

    ok_runner = lambda argv: RunResult(0, stdout="COMPLETED: done\n")
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=False), store=store, runner=ok_runner)

    assert d.ok
    assert d.action == "record_result"
    assert d.marker is None


def test_is_recursion_refusal_on_returncode_3():
    result = RunResult(3, stderr="above max-recursion-depth=5\n")
    assert cli._is_recursion_refusal(result) is True


def test_is_recursion_refusal_on_stderr_only():
    result = RunResult(1, stderr="max-recursion-depth exceeded\n")
    assert cli._is_recursion_refusal(result) is True


def test_is_recursion_refusal_false_on_clean_success():
    result = RunResult(0, stdout="COMPLETED: ok\n")
    assert cli._is_recursion_refusal(result) is False
