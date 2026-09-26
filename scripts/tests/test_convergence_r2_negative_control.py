"""R2: a measurable shell stage's verify_command is trusted only after being shown
it CAN fail. A `negative_control` (the same command family, fed known-bad input, run
in the same venue) is required at the SUBMISSION seam for every substantive plan's
measurable/shell stage carrying a verify_command -- or a `negative_control_waiver`
naming why one cannot be built. `cmd_record_result` runs the control after the
positive check goes green and refuses the pass if the control does not discriminate
(matches the same expected_exit as the positive check). `observe_stage_checks`
reports DISCRIMINATES/NOT_DISCRIMINATING at submit time (advisory only), and
`render_plan_md`/`render_stage_brief` surface the control or its waiver.

Seven tests, one per bullet of the stage's own verify_command. Deliberately imports
only symbols already present on the pre-lever baseline (commit 54744ff) at module
level -- new fields (Criterion.negative_control/negative_control_waiver) are set via
plain dict keys or constructor kwargs INSIDE test bodies, and new checkrun constants
(DISCRIMINATES/NOT_DISCRIMINATING) are imported inside the one test body that needs
them -- so this module collects cleanly there too: the negative-control run must fail
each test via AssertionError/TypeError (pytest FAILED), not an ImportError/collection
error.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import REPO_ROOT, RunResult
from agentctl.plan import parse_plan
from agentctl.render import render_plan_md, render_stage_brief
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)
from agentctl.submission import submission_violations


def ns(**kw):
    return Namespace(**kw)


def _measurable_session(store, sid, *, verify_command=None, negative_control=None,
                         negative_control_waiver=None, expected_exit=0):
    """A MEASURABLE-criterion, SUBSTANTIVE stage, optionally carrying a
    verify_command and a negative_control/waiver. Local copy, not a shared
    fixture import -- same reason as the sibling R4 test file: a freshly-added
    shared helper would not exist on the pre-lever baseline, an ImportError
    there rather than the required AssertionError/TypeError."""
    state = SessionState(
        session_id=sid,
        task_id="test",
        goal="fix the bug",
        overall_done_criterion="the test suite passes",
        overall_criterion_type=CriterionType.MEASURABLE.value,
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="test-setup"),
        stages=[
            Stage(
                index=1,
                title="Fix the bug",
                subject=Subject(material="the module", result="tests pass with no failures"),
                means=Means(means="pytest", method="run the test suite"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="the check passes",
                    verify_command=verify_command,
                    expected_exit=expected_exit,
                    negative_control=negative_control,
                    negative_control_waiver=negative_control_waiver,
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state


class _Runner:
    """Routes a call by its argv shape, mirroring the sibling R4 test file's
    _Runner: a plain or env-wrapped git identity probe, and a `bash -c` call for
    either the positive verify_command or the negative_control -- distinguished
    from each other by which literal command text they carry (every scenario
    below uses a distinct command string for whichever ones must be told
    apart). Any command text present as a key in `exit_codes` returns that exit
    code; anything else defaults to 0."""

    def __init__(self, *, head="deadbeef", exit_codes=None):
        self.calls = []
        self.identity_calls = 0
        self.bash_calls = []
        self.head = head
        self.exit_codes = exit_codes or {}

    def __call__(self, argv, *, timeout=None, stdin=""):
        if argv[:1] == ["env"] and "git" in argv:
            git_argv = argv[argv.index("git"):]
        else:
            git_argv = argv
        if git_argv[:1] == ["git"]:
            self.identity_calls += 1
            if "rev-parse" in git_argv:
                if "--git-path" in git_argv:
                    return RunResult(0, stdout=".git/index", stderr="")
                return RunResult(0, stdout=self.head, stderr="")
            if "write-tree" in git_argv:
                return RunResult(0, stdout=f"tree-of-{self.head}", stderr="")
            return RunResult(0, stdout="", stderr="")  # add -A, ls-files -s
        if argv[:2] == ["bash", "-c"]:
            command = argv[2]
            self.bash_calls.append(command)
            for needle, code in self.exit_codes.items():
                if needle in command:
                    return RunResult(code, stdout="", stderr="")
            return RunResult(0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {argv}")


def _plan_data(*, negative_control=None, negative_control_waiver=None):
    """A one-stage SUBSTANTIVE plan dict, parse_plan-ready (à la test_render.py's
    `_doc`), with a measurable/shell stage carrying a verify_command -- the
    shape submission_violations and the renderer both need."""
    stage = {
        "index": 1,
        "title": "Fix the bug",
        "executor": "in_thread",
        "expected_result_image": "tests pass with no failures",
        "criterion_type": "measurable",
        "done_criterion": "the check passes",
        "verify_command": "true",
        "material": "the module",
        "means": "pytest",
        "method": "run the test suite",
        "conditions": "c",
        "invariants": "inv",
        "capability_required": "cap",
        "principle": {
            "statement": "statement 1",
            "source": "src",
            "derivation": "der follows from src",
            "confidence": "high",
            "refutation": "ref",
        },
    }
    if negative_control is not None:
        stage["negative_control"] = negative_control
    if negative_control_waiver is not None:
        stage["negative_control_waiver"] = negative_control_waiver
    return {
        "meta": {
            "task_id": "r2-test",
            "goal": "g",
            "done_criterion": "d",
            "criterion_type": "measurable",
            "weight_class": "substantive",
            "external_research": "n/a",
        },
        "stage": [stage],
    }


# --- 1: submit refuses a measurable shell stage with no negative_control -------

def test_submit_refuses_measurable_shell_stage_without_negative_control():
    doc = parse_plan(_plan_data())
    problems = submission_violations(doc)
    assert any("negative_control" in p for p in problems), problems


# --- 2: a waiver with a reason is accepted, and the record-time waiver is logged

def test_waiver_with_reason_is_accepted_and_logged(store):
    doc = parse_plan(_plan_data(negative_control_waiver="cannot be automated: destructive"))
    problems = submission_violations(doc)
    assert not any("negative_control" in p for p in problems), problems

    _measurable_session(
        store, "waiver1", verify_command="true",
        negative_control_waiver="cannot be automated: destructive",
    )
    d = cli.cmd_record_result(
        ns(session="waiver1", status="passed", actual="ran the suite", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=_Runner(),
    )
    assert d.ok is True
    state = store.load("waiver1")
    assert any(h.get("event") == "negative_control_waived" for h in state.history), state.history


# --- 3: record-result refuses the pass when the negative control ALSO passes --

def test_record_result_refuses_when_negative_control_passes(store):
    _measurable_session(store, "t3", verify_command="verify-cmd", negative_control="verify-cmd-bad")
    runner = _Runner(exit_codes={})  # both commands default to exit 0 -- non-discriminating

    d = cli.cmd_record_result(
        ns(session="t3", status="passed", actual="ran the suite", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )

    assert d.ok is False
    state = store.load("t3")
    assert state.node == Node.DIAGNOSING.value
    assert "did not fail on bad input" in state.stages[0].outcome.actual
    assert any(h.get("event") == "control_not_discriminating" for h in state.history), state.history


# --- 4: record-result passes when the negative control correctly fails --------

def test_record_result_passes_when_negative_control_fails(store):
    _measurable_session(store, "t4", verify_command="verify-cmd", negative_control="verify-cmd-bad")
    runner = _Runner(exit_codes={"verify-cmd-bad": 1})

    d = cli.cmd_record_result(
        ns(session="t4", status="passed", actual="ran the suite", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )

    assert d.ok is True
    state = store.load("t4")
    assert any(h.get("event") == "negative_control_discriminates" for h in state.history), state.history


# --- 5: the negative control is never run when the positive check is red ------

def test_negative_control_not_run_when_positive_check_red(store):
    _measurable_session(store, "t5", verify_command="verify-cmd-fails", negative_control="verify-cmd-neg")
    runner = _Runner(exit_codes={"verify-cmd-fails": 1})

    d = cli.cmd_record_result(
        ns(session="t5", status="passed", actual="ran the suite", control=None,
           observation="pytest printed 2 failed, 0 passed"),
        store=store, runner=runner,
    )

    assert d.ok is False
    assert runner.bash_calls == ["verify-cmd-fails"]


# --- 6: checkrun's submit-time advisory reports DISCRIMINATES/NOT_DISCRIMINATING

def test_checkrun_reports_negative_control_at_submit():
    from agentctl.checkrun import (
        DISCRIMINATES,
        GREEN_AT_SUBMIT,
        NOT_DISCRIMINATING,
        format_observations,
        observe_stage_checks,
    )

    def _resolve_repo_root(_venue):
        return str(REPO_ROOT)

    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command="true", expected_exit=0, verify_venue="repo_root",
            negative_control="false",
        ),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )
    [obs] = observe_stage_checks([stage], _resolve_repo_root)
    assert obs.label == GREEN_AT_SUBMIT
    assert obs.negative_control_label == DISCRIMINATES
    lines = format_observations([obs])
    assert any("discriminates" in line for line in lines), lines

    stage_stuck = Stage(
        index=2, title="s2",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command="true", expected_exit=0, verify_venue="repo_root",
            negative_control="true",
        ),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )
    [obs2] = observe_stage_checks([stage_stuck], _resolve_repo_root)
    assert obs2.negative_control_label == NOT_DISCRIMINATING
    lines2 = format_observations([obs2])
    assert any("not-discriminating" in line for line in lines2), lines2


# --- 7: plan/stage rendering shows the negative control or its waiver ---------

def test_plan_render_shows_negative_control():
    data = _plan_data(negative_control="verify-cmd-bad")
    data["stage"].append({
        "index": 2,
        "title": "Second stage",
        "executor": "in_thread",
        "expected_result_image": "img2",
        "criterion_type": "measurable",
        "done_criterion": "done 2",
        "verify_command": "true",
        "negative_control_waiver": "cannot be automated: destructive",
        "material": "m",
        "means": "e",
        "method": "meth",
        "conditions": "c",
        "invariants": "inv",
        "capability_required": "cap",
        "principle": {
            "statement": "statement 2",
            "source": "src",
            "derivation": "der follows from src",
            "confidence": "high",
            "refutation": "ref",
        },
    })
    doc = parse_plan(data)

    md = render_plan_md(doc)
    assert "**Negative control:**" in md
    assert "**Negative control waived:**" in md

    brief1 = render_stage_brief(doc, 1)
    assert "**Negative control:**" in brief1
    brief2 = render_stage_brief(doc, 2)
    assert "**Negative control waived:**" in brief2
