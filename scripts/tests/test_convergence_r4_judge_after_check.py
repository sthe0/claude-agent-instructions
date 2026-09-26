"""R4: the acceptance-judge gate now runs AFTER the mechanical verify_command check,
caches a green check against venue-tree identity, and fails genuinely OPEN (the pass
proceeds via a recorded bypass) rather than fail-closed-in-practice when the judge
CALL itself fails. Every decision point logs.

Four spec items from the plan (convergence-levers-r1-r2-r4.toml, stage 2), one test
group each:
  (a) verify_command runs BEFORE the judge; a failing check never spends a judge call.
  (c) a green check is cached by venue-tree identity; a repeated record-result on an
      unchanged tree skips re-running the command but still re-queries the judge, and
      a changed tree always re-runs the command.
  (e) a judge CALL failure (disabled/errored/timed out/unparseable) auto-records a
      `fail_open` JudgeBypass and the pass proceeds -- fail-open in effect, not just
      in name.
  (f) every judge decision writes to the judge_ledger, and every record-result call
      (blocked or not) increments and logs Outcome.record_attempts.

Deliberately imports only symbols already present on the pre-lever baseline
(commit 54744ff) so this module collects cleanly there too: the negative-control run
must fail each test via AssertionError, not an ImportError/collection error.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, gates
from agentctl.dispatch import RunResult
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
from lib import judge_ledger


def ns(**kw):
    return Namespace(**kw)


def _measurable_session(store, sid, *, verify_command=None, expected_exit=0):
    """A MEASURABLE-criterion, SUBSTANTIVE stage, optionally carrying a
    verify_command. Kept as a local copy rather than importing the shared
    scripts/tests/session_fixtures.py helper the other two R4 test files use:
    this module's own docstring above commits it to collecting cleanly against
    the pre-lever baseline via a single-file copy (no sibling modules), and a
    freshly-added shared helper would not exist on that baseline -- an
    ImportError there, not the required AssertionError."""
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
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state


class _Runner:
    """Routes a call by its argv shape -- the shapes cmd_record_result's verify+judge
    path actually issues: a plain `git -C <cwd> rev-parse ...` call (HEAD, then
    --git-path index), an `env GIT_INDEX_FILE=... GIT_OBJECT_DIRECTORY=... \
    GIT_ALTERNATE_OBJECT_DIRECTORIES=... git -C <cwd> {add -A,ls-files -s,write-tree}`
    call (the venue-identity probe's disposable-index+objects half -- an arbitrary
    number of env assignments ahead of the trailing `git` token, so the split below
    locates that token by value rather than by a fixed offset), the verify_command's
    `bash -c <cmd>`, and a judge's model-launch argv (starting with "claude"). Each
    git-flavored call (plain or env-wrapped) counts as an identity call; verify and
    judge are independently counted so a test can assert exactly which of them ran.
    `head`/`judge_stdout`/`judge_exit` are mutable between calls so a test can
    simulate a tree change or a change in judge verdict mid-scenario."""

    def __init__(self, *, verify_exit=0, judge_stdout="YES\nlooks right", judge_exit=0,
                 head="deadbeef"):
        self.verify_calls = 0
        self.judge_calls = 0
        self.identity_calls = 0
        self.calls = []
        self.verify_exit = verify_exit
        self.judge_stdout = judge_stdout
        self.judge_exit = judge_exit
        self.head = head

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
            self.verify_calls += 1
            self.calls.append("verify")
            return RunResult(self.verify_exit, stdout="", stderr="")
        self.judge_calls += 1
        self.calls.append("judge")
        return RunResult(self.judge_exit, stdout=self.judge_stdout, stderr="")


# --- (a) a failing verify_command never spends a judge call --------------------

def test_judge_not_called_when_verify_command_fails(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t1", verify_command="false")
    runner = _Runner(verify_exit=1)

    d = cli.cmd_record_result(
        ns(session="t1", status="passed", actual="ran the suite", control=None,
           observation="pytest failed with 2 errors"),
        store=store, runner=runner,
    )

    assert d.ok is False
    assert runner.verify_calls == 1
    assert runner.judge_calls == 0


def test_judge_called_after_green_verify(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t2", verify_command="true")
    runner = _Runner(verify_exit=0, judge_stdout="YES\nlooks right")

    d = cli.cmd_record_result(
        ns(session="t2", status="passed", actual="ran the suite", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )

    assert d.ok is True
    assert runner.calls == ["verify", "judge"]
    state = store.load("t2")
    assert state.stage_reviews[-1].verdict == "pass"
    assert state.stage_reviews[-1].reviewer == advisor_module().JUDGE_REVIEWER


def advisor_module():
    """Import indirection so a `from agentctl import advisor` isn't required at
    module scope -- kept local to the one assertion that needs the JUDGE_REVIEWER
    constant, since every other test only exercises advisor indirectly via cli."""
    from agentctl import advisor
    return advisor


# --- (c) a green check is cached by venue-tree identity -------------------------

def test_rerun_on_unchanged_tree_skips_verify_and_requeries_judge(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t3", verify_command="true")
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(verify_exit=0, judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="t3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )
    assert d1.ok is False  # genuine revise verdict blocks
    assert runner.verify_calls == 1
    assert runner.judge_calls == 1

    # Same venue tree (the fake runner's git responses are unchanged) -- the cache
    # must skip the verify_command re-run but still re-query the judge every time.
    runner.judge_stdout = "YES\nlooks right now"
    d2 = cli.cmd_record_result(
        ns(session="t3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d2.ok is True
    assert runner.verify_calls == 1  # cache hit: no second bash -c call
    assert runner.judge_calls == 2  # judge re-queried regardless of the cache


def test_changed_tree_reruns_verify(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t4", verify_command="true")
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(verify_exit=0, judge_stdout="NO\nnot enough detail", head="aaa111")

    d1 = cli.cmd_record_result(
        ns(session="t4", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )
    assert d1.ok is False
    assert runner.verify_calls == 1

    # The venue tree moved (a real edit landed) -- a red-or-not, the cache must never
    # trust a recorded identity that no longer matches, so the check re-runs.
    runner.head = "bbb222"
    runner.judge_stdout = "YES\nlooks right now"
    d2 = cli.cmd_record_result(
        ns(session="t4", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d2.ok is True
    assert runner.verify_calls == 2  # cache miss: the identity changed


# --- (e) a judge CALL failure fails open, recording a surfaced bypass -----------

def test_judge_none_verdict_fails_open_with_surfaced_bypass(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t5", verify_command="true")
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(verify_exit=0, judge_exit=1)  # the judge CALL itself fails

    d = cli.cmd_record_result(
        ns(session="t5", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d.ok is True  # fails OPEN: the pass proceeds despite no genuine verdict
    state = store.load("t5")
    assert any(
        b.kind == "fail_open" and b.observation_sha256 == cli._observation_sha256(observation)
        for b in state.judge_bypassed
    )


# --- (f) every judge decision and every record-result attempt is logged --------

def test_acceptance_judge_writes_decided_ledger_line(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t6", verify_command="true")
    runner = _Runner(verify_exit=0, judge_stdout="YES\nlooks right")

    d = cli.cmd_record_result(
        ns(session="t6", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d.ok is True

    records = judge_ledger.read_records()
    assert any(
        r.get("kind") == "decided" and r.get("judge") == "acceptance_judge"
        and r.get("stage") == "call"
        for r in records
    )


def test_revise_verdict_and_attempts_are_logged(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "t7")  # no verify_command: judge is the only gate
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="t7", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )
    d2 = cli.cmd_record_result(
        ns(session="t7", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d1.ok is False
    assert d2.ok is False
    state = store.load("t7")
    verdict_events = [
        e for e in state.history
        if e.get("event") == "acceptance_judge_verdict" and e.get("verdict") == "revise"
    ]
    assert len(verdict_events) == 2
    assert state.stages[0].outcome.record_attempts == 2
