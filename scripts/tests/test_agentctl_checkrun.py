"""observe_stage_checks/format_observations (task judge-import-blindness, C.2):
actually RUNS each stage's verify_command in its declared venue at submit-plan
time and reports what happened, rather than statically classifying the target
script's source — the static-lint predecessor (v2) could not structurally
separate a legitimate `os.environ.get(...)` read from one that silently
swallows a missing project root, since both have identical syntax. Running the
command sidesteps that classification: whatever the command itself prints is
the discriminator.

The first three cases deliberately exercise REAL repo scripts (not a hermetic
fixture) — the flaw that got the static-lint approach (v2) rejected was that its
test suite could self-check without ever touching the motivating case; wiring
these tests to actual repo commands means a regression in either the mechanism
or the underlying scripts breaks this suite.
"""
from __future__ import annotations

import subprocess
import time

from agentctl.checkrun import (
    GREEN_AT_SUBMIT,
    NOT_JUDGED,
    RED,
    format_observations,
    observe_stage_checks,
)
from agentctl.dispatch import REPO_ROOT, RunResult
from agentctl.state import Actor, Criterion, Means, Outcome, Stage, StageStatus, Subject


def _stage(verify_command, expected_exit=0, verify_venue="repo_root", index=1, title="s1"):
    return Stage(
        index=index, title=title,
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, expected_exit=expected_exit,
            verify_venue=verify_venue,
        ),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


class _CaptureAll:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv):
        self.calls.append(argv)
        return RunResult(self.code, stdout="", stderr="")


def _resolve_repo_root(_venue):
    return str(REPO_ROOT)


# --- case 1: the motivating C.2 case — a real probe missing its ambient env ----

def test_motivating_case_missing_project_dir_is_red(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stage = _stage("python3 scripts/hook-canon-guard-wired-check.py --check-timeouts",
                    expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root, head_chars=10000)
    assert obs.label == RED
    assert "CLAUDE_PROJECT_DIR" in obs.head


# --- case 2: same probe, WITH the env set — the certification-refusal must not
# reappear, and the absence check must not be vacuous (head-truncation) --------

def test_motivating_case_with_project_dir_set_clears_the_refusal(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(REPO_ROOT))
    stage = _stage("python3 scripts/hook-canon-guard-wired-check.py --check-timeouts",
                    expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root, head_chars=10000)
    # Positive control FIRST: the absence check below is only meaningful if
    # `head` holds the WHOLE combined output rather than a truncated prefix —
    # otherwise "absent" could just mean "cut off before we got there". Since
    # head = combined[:head_chars], `len(head) < head_chars` proves nothing was
    # cut (the full output fit inside the window), so the absence check below
    # examines the complete output, not a fragment of it.
    assert len(obs.head) < 10000
    assert "named no project root" not in obs.head


# --- case 3: defect-S1 fixture — a real witness invoked bare ------------------

def test_bare_dispatch_witness_is_red_with_argparse_usage(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stage = _stage("python3 scripts/check-dispatch-witness.py", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root, head_chars=10000)
    assert obs.label == RED
    assert obs.returncode == 2
    assert "the following arguments are required" in obs.head


# --- case 4: a check that is already green before any work happened ----------

def test_already_passing_command_is_green_at_submit():
    stage = _stage("true", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root)
    assert obs.label == GREEN_AT_SUBMIT
    assert obs.returncode == 0


# --- case 5: a declared venue that does not exist on disk yet ----------------

def test_missing_venue_is_not_judged_and_never_runs(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    stage = _stage("true", verify_venue="delivery")
    cap = _CaptureAll()
    [obs] = observe_stage_checks([stage], lambda _v: missing, runner=cap)
    assert obs.label == NOT_JUDGED
    assert "does not exist" in obs.reason
    assert cap.calls == []


# --- case 6: a command that exceeds its timeout -------------------------------

def test_timeout_is_not_judged_and_does_not_wait_out_the_sleep(tmp_path):
    stage = _stage("sleep 5", verify_venue="repo_root")
    started = time.monotonic()
    [obs] = observe_stage_checks([stage], lambda _v: str(tmp_path), timeout_s=1)
    elapsed = time.monotonic() - started
    assert obs.label == NOT_JUDGED
    assert "timeout" in obs.reason
    assert elapsed < 4


# --- defect 1 (blocking): non-UTF-8 command output must not raise -------------

def test_non_utf8_output_is_observed_not_raised():
    # The coordinator's own repro: a byte sequence that is invalid on its own
    # in UTF-8. Before the fix, decoding this inside communicate() raised
    # UnicodeDecodeError straight out of observe_stage_checks.
    stage = _stage("printf '\\xff\\xfe'", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root)
    assert obs.label == GREEN_AT_SUBMIT
    assert obs.returncode == 0
    assert "�" in obs.head


# --- defect 2: a command that never launches is not-judged, not red-127 -------

def test_process_launch_failure_is_not_judged_not_red(monkeypatch):
    def raise_oserror(*_args, **_kwargs):
        raise OSError("[Errno 2] No such file or directory: 'bash'")

    monkeypatch.setattr(subprocess, "Popen", raise_oserror)
    stage = _stage("true", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root)
    assert obs.label == NOT_JUDGED
    assert obs.returncode is None
    assert "OSError" in obs.reason


# --- defect 1 (structural): an injected runner that raises must not raise -----

def test_injected_runner_that_raises_is_not_judged_and_does_not_raise():
    def boom(_argv):
        raise RuntimeError("simulated injected runner failure")

    stage = _stage("true", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root, runner=boom)
    assert obs.label == NOT_JUDGED
    assert "RuntimeError" in obs.reason
    assert "simulated injected runner failure" in obs.reason


# --- defect 3: green-at-submit names both readings, verdicts no longer ------

def test_green_at_submit_message_names_both_readings_not_a_verdict():
    stage = _stage("true", expected_exit=0)
    [obs] = observe_stage_checks([stage], _resolve_repo_root)
    [line] = format_observations([obs])
    assert "already done" in line or "already is" in line or "rc reflects" in line
    assert "cannot go red" in line or "cannot discriminate" in line
    assert "worth reconsidering" not in line
