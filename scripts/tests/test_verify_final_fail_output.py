"""verify-final keeps a failing check's output: the full stdout/stderr go to a log file,
and the failure text names the log and shows the last lines, so the cause is readable
without a manual re-run. A check that prints nothing keeps its old failure text."""
from __future__ import annotations

import os
from pathlib import Path

from agentctl.dispatch import RunResult
from agentctl.state import FinalCheck
from test_verify_final_speedup import _Runner, _verify, _verifying

COMMAND = "pytest -q"
FAIL_EXIT = 3
STDOUT = "collected 2 items\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed, 1 passed"
STDERR = "DeprecationWarning: something noisy"
LAST_STDOUT_LINE = STDOUT.splitlines()[-1]
LOG_NOTE = "; log: "


class _OutputRunner(_Runner):
    """_Runner whose bash checks print STDOUT/STDERR (or nothing, when `silent`)."""

    def __init__(self, silent=False, **kw):
        super().__init__(**kw)
        self.silent = silent

    def __call__(self, argv, *, timeout=None, stdin=""):
        result = super().__call__(argv, timeout=timeout, stdin=stdin)
        if argv[:2] != ["bash", "-c"] or self.silent:
            return result
        return RunResult(result.returncode, stdout=STDOUT, stderr=STDERR)


def _logs_written() -> list[Path]:
    return sorted(Path(os.environ["AGENTCTL_EVIDENCE_ROOT"]).rglob("*.log"))


def _log_path(failure: str) -> Path:
    _, found, rest = failure.partition(LOG_NOTE)
    assert found, f"no log path in failure text: {failure!r}"
    return Path(rest.split("\n", 1)[0])


def test_failing_check_output_is_logged_and_tailed(store):
    _verifying(store, "fo-fail", [COMMAND])
    d = _verify(store, "fo-fail", _OutputRunner(exits={COMMAND: FAIL_EXIT}))
    assert d.ok is False
    [failure] = d.data["failures"]
    assert failure.startswith(f"stage 1: exit {FAIL_EXIT} != 0 ({COMMAND})")
    log = _log_path(failure)
    assert log.is_file()
    body = log.read_text(encoding="utf-8")
    assert COMMAND in body and STDOUT in body and STDERR in body
    assert failure.rstrip().endswith(LAST_STDOUT_LINE)
    assert failure.index(STDERR) < failure.index(LAST_STDOUT_LINE)


def test_passing_check_writes_no_log(store):
    _verifying(store, "fo-pass", [COMMAND])
    d = _verify(store, "fo-pass", _OutputRunner())
    assert d.ok is True, d.detail
    assert _logs_written() == []


def test_duplicate_of_failing_check_reports_the_same_log(store):
    _verifying(store, "fo-dup", [COMMAND], [FinalCheck(command=COMMAND, label="again")])
    runner = _OutputRunner(exits={COMMAND: FAIL_EXIT})
    d = _verify(store, "fo-dup", runner)
    assert d.ok is False
    assert runner.ran(COMMAND) == 1
    stage_failure, dup_failure = d.data["failures"]
    assert dup_failure.startswith(f"final_check 'again': exit {FAIL_EXIT} != 0")
    assert _log_path(dup_failure) == _log_path(stage_failure)


def test_failing_check_without_output_keeps_old_text(store):
    _verifying(store, "fo-silent", [COMMAND])
    d = _verify(store, "fo-silent", _OutputRunner(silent=True, exits={COMMAND: FAIL_EXIT}))
    assert d.ok is False
    assert d.data["failures"] == [f"stage 1: exit {FAIL_EXIT} != 0 ({COMMAND})"]
    assert _logs_written() == []
