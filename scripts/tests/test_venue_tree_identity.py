"""Behavioral tests for cli._venue_tree_identity: a digest of a git venue's HEAD
sha plus the tree `git write-tree` would produce from a full `git add -A` of the
working tree, computed through a disposable `GIT_INDEX_FILE` copy so the venue's
real index and working tree are never touched.
"""
from __future__ import annotations

import os
import subprocess
from argparse import Namespace
from pathlib import Path

from agentctl import cli
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


def _measurable_session(store, sid, *, verify_command=None, expected_exit=0):
    """Same shape as the sibling R4 test files' helper of the same name -- kept
    as a local copy per this suite's one-file-one-fixture-set convention."""
    state = SessionState(
        session_id=sid,
        task_id="venue-identity-test",
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


# --- content edits the identity must see ------------------------------------

def test_changes_on_staged_content_edit(tmp_path):
    """Same staged path, different staged bytes across the two calls -- a key
    built only from unstaged `git diff` or from `git status --porcelain`'s
    status code cannot tell these apart; a real `add -A` + `write-tree` can,
    because it stages the actual bytes."""
    work = make_repo(tmp_path)

    (work / "README.md").write_text("staged v1\n")
    git("add", "-A", cwd=work)
    v1 = cli._venue_tree_identity(str(work), None)

    (work / "README.md").write_text("staged v2\n")
    git("add", "-A", cwd=work)
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


def test_changes_on_untracked_content_edit(tmp_path):
    """Same untracked path, different bytes -- `git status --porcelain` prints
    the identical "?? scratch.txt" line either way."""
    work = make_repo(tmp_path)

    (work / "scratch.txt").write_text("v1\n")
    v1 = cli._venue_tree_identity(str(work), None)

    (work / "scratch.txt").write_text("v2\n")
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


def test_stable_when_nothing_changed(tmp_path):
    """Negative control for the two tests above: an unchanged tree must
    produce the SAME identity across calls, or the record-result cache could
    never hit at all."""
    work = make_repo(tmp_path)
    (work / "scratch.txt").write_text("v1\n")

    a = cli._venue_tree_identity(str(work), None)
    b = cli._venue_tree_identity(str(work), None)

    assert a == b


def test_changes_on_non_ascii_untracked_filename_content_edit(tmp_path):
    """A Cyrillic filename is treated the same as any other path: git's own
    `add -A` already normalizes non-ASCII path bytes the way a real commit
    would, so the identity must move on a content edit exactly as it does for
    an ASCII untracked file."""
    work = make_repo(tmp_path)
    name = "черновик.txt"

    (work / name).write_text("v1\n")
    v1 = cli._venue_tree_identity(str(work), None)

    (work / name).write_text("v2\n")
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


def test_changes_on_leading_trailing_space_filename_content_edit(tmp_path):
    """A filename with leading/trailing spaces is legal to git and to most
    filesystems but easy for a hand-rolled path-splitting parser to mishandle;
    routing through `add -A` sidesteps that entirely."""
    work = make_repo(tmp_path)
    name = "  padded name  .txt"

    (work / name).write_text("v1\n")
    v1 = cli._venue_tree_identity(str(work), None)

    (work / name).write_text("v2\n")
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 != v2


# --- documented limitations --------------------------------------------------

def test_ignored_file_edit_does_not_move_identity(tmp_path):
    """Documented limitation: a `.gitignore`d file's content is invisible to
    `add -A` exactly as it is to a real commit, so an edit confined to it must
    not move the identity."""
    work = make_repo(tmp_path)
    (work / ".gitignore").write_text("ignored.txt\n")
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", "ignore ignored.txt", cwd=work)

    (work / "ignored.txt").write_text("v1\n")
    v1 = cli._venue_tree_identity(str(work), None)

    (work / "ignored.txt").write_text("v2\n")
    v2 = cli._venue_tree_identity(str(work), None)

    assert v1 == v2


def test_embedded_repo_returns_none(tmp_path):
    """A gitlink (mode 160000, an embedded repo) makes git blind to that
    repo's own working-tree state without recursing into it, so the identity
    must refuse to resolve at all whenever the temp index contains one --
    present or not -- rather than risk a false cache hit."""
    work = make_repo(tmp_path)
    inner = work / "inner"
    inner.mkdir()
    git("init", "--quiet", "-b", "main", str(inner), cwd=work)
    (inner / "f.txt").write_text("x\n")
    git("add", "-A", cwd=inner)
    git("commit", "--quiet", "-m", "inner seed", cwd=inner)
    git("add", "inner", cwd=work)  # recorded as a gitlink; no .gitmodules needed
    git("commit", "--quiet", "-m", "embed inner", cwd=work)

    assert cli._venue_tree_identity(str(work), None) is None


def test_failing_git_call_returns_none(tmp_path):
    """`rev-parse HEAD` failing (not a git repo, or no commits yet) must fail
    SAFE -- no caching, always re-run -- rather than raise or hash an
    empty/garbage value."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()

    assert cli._venue_tree_identity(str(empty), None) is None


def test_real_index_is_byte_identical_before_and_after(tmp_path):
    """The real index must never be written to -- only a disposable
    GIT_INDEX_FILE copy is."""
    work = make_repo(tmp_path)
    (work / "scratch.txt").write_text("v1\n")
    git("add", "-A", cwd=work)
    git_path = git("rev-parse", "--git-path", "index", cwd=work).stdout.strip()
    index_path = Path(git_path)
    if not index_path.is_absolute():
        index_path = work / index_path
    before = index_path.read_bytes()

    cli._venue_tree_identity(str(work), None)

    assert index_path.read_bytes() == before


# --- end to end against a real repo: cache staleness on a genuine edit ------

class _RealGitFakeJudgeRunner:
    """Routes every git-flavored call (a plain `git ...` probe, or the
    disposable-index half issued as `env GIT_INDEX_FILE=... git ...`) to the
    real subprocess_runner, so the venue-tree identity reflects genuine tree
    state; the verify_command and the judge are faked."""

    def __init__(self, *, verify_exit=0, judge_stdout="NO\nnot enough detail", judge_exit=0):
        self.verify_calls = 0
        self.judge_calls = 0
        self.verify_exit = verify_exit
        self.judge_stdout = judge_stdout
        self.judge_exit = judge_exit

    def __call__(self, argv, *, timeout=None, stdin=""):
        if argv[:1] in (["git"], ["env"]):
            return cli.subprocess_runner(argv)
        if argv[:2] == ["bash", "-c"]:
            self.verify_calls += 1
            return RunResult(self.verify_exit, stdout="", stderr="")
        self.judge_calls += 1
        return RunResult(self.judge_exit, stdout=self.judge_stdout, stderr="")


def test_record_result_cache_not_served_stale_after_staged_edit(store, tmp_path, monkeypatch):
    """A record-result cache hit must not survive a staged edit landing in the
    venue between two calls: the verify_command must re-run rather than trust
    the (now stale) cached green identity. The judge is faked to REVISE so the
    stage stays active for a second record-result call; the mechanical check
    still runs and caches on its own green result independently of the judge.

    Both calls see the SAME `git status --porcelain` status code ("M  README.md"
    -- the file is staged before d1 too), so only a same-status DIFFERENT-content
    edit between the two calls isolates staged-content visibility specifically."""
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

    (work / "README.md").write_text("staged v2\n")
    git("add", "-A", cwd=work)

    d2 = cli.cmd_record_result(
        ns(session="id1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d2.ok is False
    assert runner.verify_calls == 2  # re-ran; a stale hit would leave this at 1


def test_record_result_end_to_end_non_ascii_untracked_edit_forces_recheck(store, tmp_path, monkeypatch):
    """End to end with a real repo: an untracked file with a Cyrillic name is
    added, and the mechanical check runs green against it (real verify_calls
    count, judge faked to REVISE so the stage stays active for a second call --
    same reason as test_record_result_cache_not_served_stale_after_staged_edit
    above). The file's content is then edited to something the (still faked,
    but now flipped) check rejects -- the identity must move on this genuine,
    non-ASCII, untracked edit exactly as the unit-level tests above prove, so
    the second call must NOT serve the first call's cached green result: it
    must re-run for real and the stage must NOT pass."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    work = make_repo(tmp_path)
    name = "черновик.txt"
    (work / name).write_text("good\n")
    state = _measurable_session(store, "cyr1", verify_command="true")
    state.repo_root = str(work)
    store.save(state)
    runner = _RealGitFakeJudgeRunner(verify_exit=0, judge_stdout="NO\nnot enough detail")

    d1 = cli.cmd_record_result(
        ns(session="cyr1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d1.ok is False  # judge revise -- stage stays active; check itself was green
    assert runner.verify_calls == 1

    (work / name).write_text("bad\n")
    runner.verify_exit = 1  # the check now genuinely rejects the edited content

    d2 = cli.cmd_record_result(
        ns(session="cyr1", status="passed", actual="ran", control=None,
           observation="pytest printed 12 passed, 0 failed"),
        store=store, runner=runner,
    )
    assert d2.ok is False  # re-ran (identity moved) and the check now genuinely fails
    assert runner.verify_calls == 2  # a stale hit would leave this at 1
