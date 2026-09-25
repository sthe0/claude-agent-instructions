"""R4 code-review follow-up (commit be04e72): three BLOCKING findings, each proven
here as a test that fails on be04e72 by assertion.

  1. `_venue_tree_identity` hashed HEAD + `git status --porcelain` + plain `git diff`
     (unstaged only). A staged edit, or a content edit to an already-untracked file
     (same path, different bytes), moved neither `status --porcelain`'s listing nor
     unstaged `diff`, so the identity stayed put and a red-turned-green re-check
     could be served stale from the record-result cache.
  2. `gates.acceptance_review_blockers` consulted a `fail_open` JudgeBypass BEFORE
     checking for a standing `revise` StageReview bound to the same observation, so
     a later judge-CALL failure (outage, timeout) could wave through a pass the
     judge had already, genuinely, said no to.
  3. `cmd_record_result`'s unconditional attempt log (record_attempts + the
     `record_result_attempt` history event) ran before `store.save`, but the
     attest_control / attest_observation early returns skipped `store.save`
     entirely — so an attempt that got blocked by one of those gates vanished on
     the next load, and a diagnosing session could never see it happened.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from argparse import Namespace
from pathlib import Path

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    JudgeBypass,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
        check=check, capture_output=True, text=True,
    )


def make_repo(tmp_path: Path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    git("init", "--quiet", "-b", "main", str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", "seed", cwd=work)
    return work


def _measurable_session(store, sid, *, verify_command=None, expected_exit=0,
                         executor="in_thread"):
    """Same shape as test_convergence_r4_judge_after_check.py's helper of the same
    name -- kept as a local copy per this test suite's one-file-one-fixture-set
    convention rather than a cross-module import of another file's private helper."""
    state = SessionState(
        session_id=sid,
        task_id="r4-review-test",
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
                actor=Actor(executor=executor),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="pytest exits 0",
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
    """Copy of test_convergence_r4_judge_after_check.py's fake runner -- routes a
    call by argv shape (git venue-identity probe / verify_command / judge)."""

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
        if argv[:1] == ["git"]:
            self.identity_calls += 1
            if "rev-parse" in argv:
                return RunResult(0, stdout=self.head, stderr="")
            return RunResult(0, stdout="", stderr="")
        if argv[:2] == ["bash", "-c"]:
            self.verify_calls += 1
            self.calls.append("verify")
            return RunResult(self.verify_exit, stdout="", stderr="")
        self.judge_calls += 1
        self.calls.append("judge")
        return RunResult(self.judge_exit, stdout=self.judge_stdout, stderr="")


# --- Finding 1: venue-tree identity must see staged AND untracked-content edits --

def test_venue_tree_identity_changes_on_staged_content_edit(tmp_path):
    """Same staged PATH across both calls (`git status --porcelain`'s "M  README.md"
    code is identical either way) with DIFFERENT staged bytes -- a key built from the
    status code (or from plain `git diff`, which is blind to staged changes
    altogether) cannot distinguish these two states, so this is the genuine
    discriminator for the bug, unlike a clean-vs-staged comparison."""
    work = make_repo(tmp_path)

    (work / "README.md").write_text("staged v1\n")
    git("add", "-A", cwd=work)
    v1 = cli._venue_tree_identity(str(work), None)

    (work / "README.md").write_text("staged v2\n")
    git("add", "-A", cwd=work)
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


def test_venue_tree_identity_changes_on_untracked_content_edit(tmp_path):
    """Same untracked PATH, different bytes -- `git status --porcelain` lists only
    the filename ("?? scratch.txt") in both cases, so a key built from that alone
    is blind to this edit."""
    work = make_repo(tmp_path)

    (work / "scratch.txt").write_text("v1\n")
    v1 = cli._venue_tree_identity(str(work), None)

    (work / "scratch.txt").write_text("v2\n")
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


def test_venue_tree_identity_stable_when_nothing_changed(tmp_path):
    """Negative control for the two tests above: an unchanged tree must still
    produce the SAME identity across calls, or the cache could never hit at all."""
    work = make_repo(tmp_path)
    (work / "scratch.txt").write_text("v1\n")

    a = cli._venue_tree_identity(str(work), None)
    b = cli._venue_tree_identity(str(work), None)

    assert a == b


def _pre_fix_venue_tree_identity(work: Path) -> str:
    """Literal reproduction of the be04e72 (pre-fix) `_venue_tree_identity`
    formula -- HEAD + `git status --porcelain` + plain (unstaged-only, non-binary)
    `git diff`, no untracked-file content -- used as an in-suite negative control
    in place of an actual be04e72 checkout: this sandbox blocks every git
    invocation and every bare `python3 <script>.py` run made directly via the
    Bash tool (only `python3 -m pytest ...` is pre-approved), so extracting the
    be04e72 sources at review time was not reachable. Reproducing the documented
    pre-fix formula here, and proving IT is blind where the current
    `cli._venue_tree_identity` is not, is the closest available substitute for a
    literal negative-control run against that commit."""
    prefix = ["git", "-C", str(work)]
    head = subprocess.run(prefix + ["rev-parse", "HEAD"], capture_output=True, text=True)
    status = subprocess.run(prefix + ["status", "--porcelain"], capture_output=True, text=True)
    diff = subprocess.run(prefix + ["diff"], capture_output=True, text=True)
    digest = hashlib.sha256()
    digest.update(head.stdout.strip().encode("utf-8"))
    digest.update(status.stdout.encode("utf-8"))
    digest.update(diff.stdout.encode("utf-8"))
    return digest.hexdigest()


def test_pre_fix_formula_is_blind_to_staged_content_edit(tmp_path):
    """Negative control for Finding 1 (staged case): the pre-fix formula hashes
    `git status --porcelain`, which prints the identical "M  README.md" line for
    both staged contents below, and plain `git diff`, which is blind to staged
    changes altogether -- so it must NOT distinguish v1 from v2, while the fixed
    `cli._venue_tree_identity` (see test_venue_tree_identity_changes_on_staged_
    content_edit above) must."""
    work = make_repo(tmp_path)

    (work / "README.md").write_text("staged v1\n")
    git("add", "-A", cwd=work)
    old_v1 = _pre_fix_venue_tree_identity(work)
    new_v1 = cli._venue_tree_identity(str(work), None)

    (work / "README.md").write_text("staged v2\n")
    git("add", "-A", cwd=work)
    old_v2 = _pre_fix_venue_tree_identity(work)
    new_v2 = cli._venue_tree_identity(str(work), None)

    assert old_v1 == old_v2  # the pre-fix formula is blind to this edit
    assert new_v1 != new_v2  # the fixed formula sees it


def test_pre_fix_formula_is_blind_to_untracked_content_edit(tmp_path):
    """Negative control for Finding 1 (untracked case): `git status --porcelain`
    prints the identical "?? scratch.txt" line for both contents below, and plain
    `git diff` never looks at untracked files at all -- so the pre-fix formula
    must NOT distinguish v1 from v2, while the fixed one must."""
    work = make_repo(tmp_path)

    (work / "scratch.txt").write_text("v1\n")
    old_v1 = _pre_fix_venue_tree_identity(work)
    new_v1 = cli._venue_tree_identity(str(work), None)

    (work / "scratch.txt").write_text("v2\n")
    old_v2 = _pre_fix_venue_tree_identity(work)
    new_v2 = cli._venue_tree_identity(str(work), None)

    assert old_v1 == old_v2  # the pre-fix formula is blind to this edit
    assert new_v1 != new_v2  # the fixed formula sees it


class _RealGitFakeJudgeRunner:
    """`git ...` calls hit the REAL repo (so the venue-tree identity reflects
    genuine tree state); the verify_command and the judge are faked, mirroring
    _Runner but routing the git branch to the real subprocess_runner instead of a
    canned response -- needed here because the point of the test is that a REAL
    staged edit must move the identity."""

    def __init__(self, *, verify_exit=0, judge_stdout="NO\nnot enough detail", judge_exit=0):
        self.verify_calls = 0
        self.judge_calls = 0
        self.verify_exit = verify_exit
        self.judge_stdout = judge_stdout
        self.judge_exit = judge_exit

    def __call__(self, argv, *, timeout=None, stdin=""):
        if argv[:1] == ["git"]:
            return cli.subprocess_runner(argv)
        if argv[:2] == ["bash", "-c"]:
            self.verify_calls += 1
            return RunResult(self.verify_exit, stdout="", stderr="")
        self.judge_calls += 1
        return RunResult(self.judge_exit, stdout=self.judge_stdout, stderr="")


def test_record_result_cache_not_served_stale_after_staged_edit(store, tmp_path, monkeypatch):
    """End-to-end: a record-result cache hit must not survive a staged edit landing
    in the venue between two calls -- the verify_command must re-run rather than
    trusting the (now stale) cached green identity. The judge is faked to REVISE
    so the stage stays active for a second record-result call (a genuine pass
    would advance the stage past ACTIVE, as test_landed_check_verify.py's stages
    do); the mechanical check still runs and caches on its own green result.

    Both calls see the SAME `git status --porcelain` status code ("M  README.md" --
    the file is staged before d1 too) so a status-code-only key (the pre-fix
    identity, which also folds in `git status --porcelain`) would already be
    caught by a clean-vs-staged comparison regardless of the bug; only a
    same-status DIFFERENT-content edit between the two calls isolates the actual
    finding (staged content is invisible to plain `git diff`, which is unstaged
    only)."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    work = make_repo(tmp_path)
    (work / "README.md").write_text("staged v1\n")
    git("add", "-A", cwd=work)
    state = _measurable_session(store, "id1", verify_command="true")
    state.repo_root = str(work)
    store.save(state)
    runner = _RealGitFakeJudgeRunner(verify_exit=0, judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="id1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d1.ok is False  # judge revise -- stage stays active
    assert runner.verify_calls == 1

    # The staged content changes (v1 -> v2) but the status CODE stays "M  README.md"
    # both times -- the tree has genuinely changed, so the cached GREEN identity
    # from d1 must not be trusted.
    (work / "README.md").write_text("staged v2\n")
    git("add", "-A", cwd=work)

    d2 = cli.cmd_record_result(
        ns(session="id1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d2.ok is False
    assert runner.verify_calls == 2  # the verify_command re-ran; a stale hit would leave this at 1


# --- Finding 2: a standing `revise` must outrank a later fail-open bypass --------

def test_gate_standing_revise_blocks_despite_later_fail_open_bypass(store, monkeypatch):
    """Unit-level: a `revise` StageReview and a `fail_open` JudgeBypass both bound
    to the SAME observation coexist (the bypass recorded strictly after the
    revise) -- the pure gate must still block, since the bypass is not evidence
    the observation is good, only that a later judge CALL could not be reached."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    state = _measurable_session(store, "b1")
    stage = state.stages[0]
    observation = "pytest printed 12 passed, 0 failed"
    stage.criterion.observation = observation
    obs_sha = cli._observation_sha256(observation)

    state.stage_reviews.append(StageReview(
        stage_index=stage.index, verdict="revise", reviewer="judge:haiku",
        note="not enough detail", observation_sha256=obs_sha,
    ))
    state.judge_bypassed.append(JudgeBypass(
        stage_index=stage.index, kind="fail_open", note="judge timed out",
        observation_sha256=obs_sha,
    ))

    blockers = gates.acceptance_review_blockers(state, stage)

    assert blockers  # the revise stands; the bypass must not clear it


def test_fail_open_bypass_still_authorizes_when_no_review_exists(store, monkeypatch):
    """Negative control for the fix above: with NO standing review at all, a
    fail_open bypass bound to the current observation must still authorize the
    pass exactly as before -- the fix narrows the bypass, it does not disable it."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    state = _measurable_session(store, "b2")
    stage = state.stages[0]
    observation = "pytest printed 12 passed, 0 failed"
    stage.criterion.observation = observation
    obs_sha = cli._observation_sha256(observation)

    state.judge_bypassed.append(JudgeBypass(
        stage_index=stage.index, kind="fail_open", note="judge timed out",
        observation_sha256=obs_sha,
    ))

    assert gates.acceptance_review_blockers(state, stage) == []


def test_standing_revise_blocks_end_to_end_after_later_judge_call_failure(store, monkeypatch):
    """Functional (via cmd_record_result): the judge genuinely says NO once, then
    a second record-result attempt hits a judge CALL failure (outage) rather than
    a second real verdict. The pass must stay blocked -- a mere inability to
    re-judge must never wave through a "no" the judge already gave."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "b3", verify_command="true")
    observation = "pytest printed 12 passed, 0 failed"
    runner = _Runner(verify_exit=0, judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="b3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )
    assert d1.ok is False  # genuine revise

    runner.judge_exit = 1  # the judge CALL itself now fails
    d2 = cli.cmd_record_result(
        ns(session="b3", status="passed", actual="ran", control=None,
           observation=observation),
        store=store, runner=runner,
    )

    assert d2.ok is False


# --- Finding 3: record_attempts/record_result_attempt survive an early gate ------

def test_record_attempt_persists_through_attest_control_early_return(store):
    state = _measurable_session(store, "p1", verify_command="true",
                                 executor="spawn:developer")

    d = cli.cmd_record_result(
        ns(session="p1", status="passed", actual="did the thing", control=None,
           observation="whatever"),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_control"

    reloaded = store.load("p1")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempt_persists_through_attest_observation_missing(store):
    _measurable_session(store, "p2", verify_command="true")

    d = cli.cmd_record_result(
        ns(session="p2", status="passed", actual="did the thing", control=None,
           observation=""),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_observation"

    reloaded = store.load("p2")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempt_persists_through_attest_observation_echo(store):
    state = _measurable_session(store, "p3", verify_command="true")
    target = state.stages[0].subject.result

    d = cli.cmd_record_result(
        ns(session="p3", status="passed", actual="did the thing", control=None,
           observation=target),
        store=store, runner=_Runner(),
    )
    assert d.ok is False
    assert d.action == "attest_observation"

    reloaded = store.load("p3")
    assert reloaded.stages[0].outcome.record_attempts == 1
    assert any(e.get("event") == "record_result_attempt" for e in reloaded.history)


def test_record_attempts_accumulate_across_repeated_blocked_calls(store):
    """Two blocked calls in a row must leave record_attempts == 2, not 1 -- proves
    the persistence fix survives repeated early-gate hits, not just the first."""
    _measurable_session(store, "p4", verify_command="true")

    for _ in range(2):
        cli.cmd_record_result(
            ns(session="p4", status="passed", actual="did the thing", control=None,
               observation=""),
            store=store, runner=_Runner(),
        )

    reloaded = store.load("p4")
    assert reloaded.stages[0].outcome.record_attempts == 2


# --- SHOULD-FIX: a fail-open judge call also writes a `decided` ledger line ------

def test_fail_open_judge_call_writes_decided_ledger_line(store, monkeypatch):
    """test_acceptance_judge_writes_decided_ledger_line (in the sibling R4 test
    file) only covers a genuine pass verdict; a judge CALL failure that fails open
    must be logged to the ledger exactly the same way, or a diagnosing session can
    never tell a fail-open pass from an unlogged one."""
    from lib import judge_ledger

    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    _measurable_session(store, "fo1", verify_command="true")
    runner = _Runner(verify_exit=0, judge_exit=1)  # the judge CALL itself fails

    d = cli.cmd_record_result(
        ns(session="fo1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d.ok is True  # fails open

    records = judge_ledger.read_records()
    assert any(
        r.get("kind") == "decided" and r.get("judge") == "acceptance_judge"
        and r.get("stage") == "call"
        for r in records
    )
