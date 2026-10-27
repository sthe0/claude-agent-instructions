"""record-result keeps a failing stage verify_command's output: the full stdout/stderr go
to a log file under the stage's evidence dir, and the stage's `actual` names the log and
shows the last lines, so a rare red run stays diagnosable. A command that prints nothing
keeps its old note."""
from __future__ import annotations

import os
from pathlib import Path

from agentctl.dispatch import RunResult
from agentctl.state import Node, StageStatus
from test_verify_execution import _executing, _record_passed, _stage

COMMAND = "pytest -q"
FAIL_EXIT = 1
STDOUT = "collected 2 items\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed, 1 passed"
STDERR = "DeprecationWarning: something noisy"
LAST_STDOUT_LINE = STDOUT.splitlines()[-1]
OLD_NOTE = f"verify_command exit {FAIL_EXIT} != expected 0: {COMMAND}"
LOG_NOTE = "; log: "


def _runner(stdout=STDOUT, stderr=STDERR):
    return lambda argv, *, timeout=None: RunResult(FAIL_EXIT, stdout=stdout, stderr=stderr)


def _logs_written() -> list[Path]:
    return sorted(Path(os.environ["AGENTCTL_EVIDENCE_ROOT"]).rglob("*.log"))


def _failed_actual(store, sid, runner) -> str:
    store.save(_executing(sid, _stage(1, verify_command=COMMAND)))
    d = _record_passed(store, sid, actual="claim ok", runner=runner)
    assert d.node == Node.DIAGNOSING.value
    stage = store.load(sid).stage(1)
    assert stage.outcome.status == StageStatus.FAILED.value
    return stage.outcome.actual


def test_failing_stage_output_is_logged_and_tailed(store):
    actual = _failed_actual(store, "so-fail", _runner())
    assert OLD_NOTE + LOG_NOTE in actual
    _, _, rest = actual.partition(LOG_NOTE)
    log = Path(rest.split("\n", 1)[0])
    assert log.parent.name == "stage-1" and log.parent.parent.name == "so-fail"
    body = log.read_text(encoding="utf-8")
    assert COMMAND in body and STDOUT in body and STDERR in body
    assert actual.rstrip().endswith(LAST_STDOUT_LINE)
    assert actual.index(STDERR) < actual.index(LAST_STDOUT_LINE)


def test_failing_stage_without_output_keeps_old_note(store):
    actual = _failed_actual(store, "so-silent", _runner(stdout="", stderr=""))
    assert actual.endswith(OLD_NOTE)
    assert LOG_NOTE not in actual
    assert _logs_written() == []
