"""cmd_dispatch: a spawn refused because its venue is in no project routes to BLOCKED + ESCALATE."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import Node
from test_dispatch_recursion_cap import _start_substantive, ns

REFUSAL_STDOUT = "Refusal: child-outside-project\n"
REFUSAL_STDERR = "error: the child would start in /venue, which is in no project ...\n"


def _dispatch(store, venued_fixtures_dir, runner):
    sid = _start_substantive(store, venued_fixtures_dir)
    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False), store=store, runner=runner,
    )
    return sid, directive


def test_child_outside_project_refusal_parks_the_stage_and_escalates(store, venued_fixtures_dir):
    runner = lambda argv, cwd=None: RunResult(2, stdout=REFUSAL_STDOUT, stderr=REFUSAL_STDERR)
    sid, directive = _dispatch(store, venued_fixtures_dir, runner)

    assert not directive.ok
    assert directive.node == Node.BLOCKED.value
    assert directive.action == "escalate"
    assert directive.marker == "ESCALATE"
    assert ".claude/settings.local.json" in directive.detail
    assert directive.data["stdout"] == REFUSAL_STDOUT

    state = store.load(sid)
    assert state.node == Node.BLOCKED
    refused = [e for e in state.history if e.get("event") == "dispatch_refused"]
    assert [e["reason"] for e in refused] == ["child-outside-project"]


def test_exit_2_without_the_refusal_line_is_not_a_child_outside_project_refusal():
    assert cli._is_child_outside_project_refusal(RunResult(2, stderr="error: --workdir does not exist\n")) is False


def test_refusal_line_with_another_exit_code_is_not_a_child_outside_project_refusal():
    assert cli._is_child_outside_project_refusal(RunResult(0, stdout=REFUSAL_STDOUT)) is False


def test_refusal_line_is_recognised_after_other_stdout():
    assert cli._is_child_outside_project_refusal(RunResult(2, stdout="note\n" + REFUSAL_STDOUT)) is True


def test_refusal_message_does_not_look_like_a_recursion_refusal():
    assert cli._is_recursion_refusal(RunResult(2, stdout=REFUSAL_STDOUT, stderr=REFUSAL_STDERR)) is False
