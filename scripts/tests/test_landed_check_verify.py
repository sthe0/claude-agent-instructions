"""Verify-site wiring for `kind = "landed"` — Stage 2 of the landed-check plan.

Stage 1 (test_landed_check_schema.py) proved the schema/validation surface only;
nothing yet executed a landed check. This file proves the runtime half: ONE
synthesizer (`SessionState.render_landed_command`) feeding THREE verify sites
(`cmd_record_result`'s machine-executed verification, `cmd_verify_final`'s
per-stage re-run loop, `cmd_verify_final`'s final_check loop), plus the
delivered-head freeze `cmd_record_result` performs before any of them dispatch.

Hermetic: every repo is a real local `git init`/`clone` under tmp_path, exercised
via the REAL subprocess runner (runner=None -> agentctl.dispatch.subprocess_runner)
— no fake runner, because the whole point is proving genuine `git merge-base
--is-ancestor` behaviour, not a scripted stand-in for it. No network touched;
"origin" is a local bare clone.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.plan import load_plan
from agentctl.state import (
    Actor,
    CheckKind,
    Criterion,
    CriterionType,
    FinalCheck,
    GateRecord,
    LANDED_GIT_ERROR_EXIT,
    LandedSpec,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


# The session fixtures below carry task_id "t" and no plan file, so the landed check's
# task-proof reads the session id: every fixture commit is written with its trailer.
TRAILER = "Task: t"


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
        check=check, capture_output=True, text=True,
    )


def rev_parse(cwd, ref="HEAD") -> str:
    return git("rev-parse", ref, cwd=cwd).stdout.strip()


def make_repo_with_remote(tmp_path: Path, name: str = "work") -> Path:
    """A bare "origin" plus a real clone `work` tracking it, one seed commit on
    `main`, pushed — `git push` updates `work`'s local `refs/remotes/origin/main`
    too, so no fetch is needed to keep the remote-tracking ref current."""
    origin = tmp_path / f"{name}-origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / name
    git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", f"seed\n\n{TRAILER}", cwd=work)
    git("push", "--quiet", "-u", "origin", "main", cwd=work)
    return work


def commit(work: Path, msg: str) -> str:
    git("commit", "--quiet", "--allow-empty", "-m", f"{msg}\n\n{TRAILER}", cwd=work)
    return rev_parse(work)


def push_main(work: Path) -> None:
    git("push", "--quiet", "origin", "main", cwd=work)


def ns(**kw):
    return Namespace(**kw)


def _record_delivered(store, sid):
    """Record the delivering stage passed, then let the landed machinery decide.

    A substantive stage's pass also needs an --observation (Defect 2); it is fixed
    here because this file's subject is the freeze/verify machinery, not the
    attestation. Worded as what a controller looked AT, so it stays true of the
    case below where the engine then refuses the pass for an unfrozen head.
    """
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="delivered", control=None,
           observation="checked the target branch's head in the declared venue against the commit this stage delivered"),
        store=store, runner=None,
    )


def _landed_stage(index, *, target="main", remote="origin", delivered_stage,
                   status=StageStatus.ACTIVE.value):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="c",
            verify_kind=CheckKind.LANDED.value,
            landed=LandedSpec(target=target, remote=remote, delivered_stage=delivered_stage),
        ),
        outcome=Outcome(status=status),
    )


def _shell_stage(index, *, verify_command=None, status=StageStatus.ACTIVE.value):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="c",
            verify_command=verify_command,
        ),
        outcome=Outcome(status=status),
    )


def _executing(sid, stages, *, repo_root=None, final_check=None):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=stages, repo_root=repo_root, final_check=final_check or [],
    )
    s.current_stage = stages[0].index
    return s


def _verifying(sid, stages, *, repo_root=None, final_check=None):
    return SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=stages, repo_root=repo_root, final_check=final_check or [],
    )


# --- SessionState.render_landed_command: the ONE synthesizer, in isolation ---

def test_render_refuses_with_no_repo_root():
    stage = _landed_stage(1, delivered_stage=1)
    stage.outcome.delivered_head = "deadbeef"
    state = _verifying("u1", [stage], repo_root=None)
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert command is None
    assert "repo_root" in refusal


def test_render_refuses_on_unknown_delivered_stage():
    stage = _landed_stage(1, delivered_stage=99)
    state = _verifying("u2", [stage], repo_root="/tmp/whatever")
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert command is None
    assert "99" in refusal
    assert "does not name an existing stage" in refusal


def test_render_refuses_when_delivered_head_not_yet_frozen():
    stage = _landed_stage(1, delivered_stage=1)  # outcome.delivered_head still None
    state = _verifying("u3", [stage], repo_root="/tmp/whatever")
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert command is None
    assert "not yet frozen" in refusal


def test_render_command_shape(tmp_path):
    stage = _landed_stage(1, target="main", remote="origin", delivered_stage=1)
    stage.outcome.delivered_head = "deadbeefcafe"
    state = _verifying("u4", [stage], repo_root=str(tmp_path))
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert refusal is None
    assert command.rstrip().endswith("exit 0")
    assert "merge-base --is-ancestor" in command
    assert "fetch" not in command
    assert "rev-parse" not in command
    assert "deadbeefcafe" in command
    assert "for R in main origin/main" in command


# --- self-reference: freeze precedes verification in cmd_record_result ------

def test_cherry_fallback_command_shape(tmp_path):
    stage = _landed_stage(1, target="main", remote="origin", delivered_stage=1)
    stage.outcome.delivered_head = "deadbeefcafe"
    state = _verifying("u4c", [stage], repo_root=str(tmp_path))
    command, _ = state.render_landed_command(stage.criterion.landed)
    assert "rev-list --merges" in command
    assert "cherry" in command
    assert "fetch" not in command


def test_self_referencing_stage_finds_frozen_head_present(tmp_path):
    """A stage whose OWN criterion is landed, self-referencing its own index,
    must find its own delivered head already frozen — proving freeze runs
    before dispatch in the SAME record-result call, not after."""
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    delivered_sha = commit(work, "feature work")  # ahead of main: not landed
    stage = _landed_stage(1, delivered_stage=1)
    state = _executing("sr1", [stage])
    state.repo_root = str(work)
    d = _record_delivered(_MemStore(state), "sr1")
    assert stage.outcome.delivered_head == delivered_sha
    assert d.action != "fix_venue" and "not yet frozen" not in d.detail, d.detail
    assert d.ok is False  # a genuine red verdict on the frozen commit, not a refusal


# --- never-landed commit stays red -------------------------------------------

def test_never_landed_commit_becomes_a_real_failure(tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    commit(work, "feature work")  # HEAD now ahead of main/origin-main, unmerged

    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                     landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
                     venue="repo_root")
    stage = _shell_stage(1, verify_command=None, status=StageStatus.ACTIVE.value)
    state = _executing("nl1", [stage], repo_root=str(work), final_check=[fc])
    d = _record_delivered(store := _MemStore(state), "nl1")
    assert d.ok is True  # stage 1 itself is a plain shell-less measurable pass

    d2 = cli.cmd_verify_final(ns(session="nl1"), store=store, runner=None)
    assert d2.ok is False
    assert d2.node == Node.DIAGNOSING.value
    assert any("landed check" in f for f in d2.data["failures"])


# --- headline regression: fail before landing, pass after, stays green ------

def test_headline_landing_regression(tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    delivered_sha = commit(work, "feature work")

    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                     landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
                     venue="repo_root")
    stage = _shell_stage(1, status=StageStatus.ACTIVE.value)
    state = _executing("hl1", [stage], repo_root=str(work), final_check=[fc])
    store = _MemStore(state)
    d = _record_delivered(store, "hl1")
    assert d.ok is True
    assert stage.outcome.delivered_head == delivered_sha

    # Not yet merged: verify-final's landed final_check must fail (genuine red).
    d_fail = cli.cmd_verify_final(ns(session="hl1"), store=store, runner=None)
    assert d_fail.ok is False
    assert d_fail.node == Node.DIAGNOSING.value

    # Land it: merge feature into main, push (updates local origin/main too).
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)

    # Reset back to VERIFYING by hand (DIAGNOSING is a dead end without a full
    # overcome-difficulty cycle) — the same stage/state object, now landed.
    state.node = Node.VERIFYING.value
    d_pass = cli.cmd_verify_final(ns(session="hl1"), store=store, runner=None)
    assert d_pass.ok is True
    assert store.load("hl1").node == Node.RESOLUTION.value

    # Trunk advances further AND the delivery venue gets further commits —
    # the frozen delivered_head never changes, so the check must stay green.
    commit(work, "unrelated trunk work")
    push_main(work)
    git("checkout", "--quiet", "-b", "next-feature", cwd=work)
    commit(work, "further unrelated work in the delivery venue")

    state.node = Node.VERIFYING.value
    d_still_pass = cli.cmd_verify_final(ns(session="hl1"), store=store, runner=None)
    assert d_still_pass.ok is True


# --- refusals: engine cannot evaluate, never a stage failure — but DOES route
# into DIAGNOSING (the venue-lifecycle plan's stage 2): a refusal at verify-final
# is a difficulty the coordinator must be able to declare/investigate/critique,
# not a dead end reachable only via `reset --force`. ------------------------

def test_nonexistent_target_ref_refuses(tmp_path):
    work = make_repo_with_remote(tmp_path)
    delivered_sha = rev_parse(work)
    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                     landed=LandedSpec(target="totally-bogus-ref-xyz", remote="origin",
                                        delivered_stage=1),
                     venue="repo_root")
    stage = _shell_stage(1, status=StageStatus.PASSED.value)
    stage.outcome.delivered_head = delivered_sha
    state = _verifying("nr1", [stage], repo_root=str(work), final_check=[fc])
    d = cli.cmd_verify_final(ns(session="nr1"), store=_MemStore(state), runner=None)
    assert d.ok is False
    assert d.action == "declare"
    assert d.node == Node.DIAGNOSING.value
    assert state.stage(1).outcome.status != StageStatus.FAILED.value


def test_option_shaped_target_refuses(tmp_path):
    """A target string shaped like a flag (leading '-') makes git treat "$R" as
    an option even though it is a single shell-quoted word — git's usage error
    (not 0/1) is correctly classified as a refusal, never a false pass/fail."""
    work = make_repo_with_remote(tmp_path)
    delivered_sha = rev_parse(work)
    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                     landed=LandedSpec(target="-not-a-real-flag", remote="origin",
                                        delivered_stage=1),
                     venue="repo_root")
    stage = _shell_stage(1, status=StageStatus.PASSED.value)
    stage.outcome.delivered_head = delivered_sha
    state = _verifying("os1", [stage], repo_root=str(work), final_check=[fc])
    d = cli.cmd_verify_final(ns(session="os1"), store=_MemStore(state), runner=None)
    assert d.ok is False
    assert d.action == "declare"


def test_unknown_delivered_commit_refuses(tmp_path):
    work = make_repo_with_remote(tmp_path)
    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                     landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
                     venue="repo_root")
    stage = _shell_stage(1, status=StageStatus.PASSED.value)
    stage.outcome.delivered_head = "0123456789abcdef0123456789abcdef01234567"  # never existed
    state = _verifying("uc1", [stage], repo_root=str(work), final_check=[fc])
    d = cli.cmd_verify_final(ns(session="uc1"), store=_MemStore(state), runner=None)
    assert d.ok is False
    assert d.action == "declare"


def test_delivered_stage_with_no_frozen_head_refuses(tmp_path):
    work = make_repo_with_remote(tmp_path)
    landed = LandedSpec(target="main", remote="origin", delivered_stage=1)
    stage1 = _shell_stage(1, status=StageStatus.PENDING.value)  # never recorded
    stage2 = _landed_stage(2, delivered_stage=1)
    state = _executing("nf1", [stage1, stage2], repo_root=str(work))
    state.current_stage = 2
    d = _record_delivered(_MemStore(state), "nf1")
    assert d.ok is False
    assert d.action == "fix_venue"
    assert "not yet frozen" in d.detail


def test_resolved_venue_missing_refuses_landed_but_fails_shell_control(tmp_path):
    """Same broken repo_root (a path that does not exist on disk), two checks:
    a landed final_check must REFUSE (exit 97, routed to declare/DIAGNOSING) while an ordinary
    shell final_check genuinely FAILS (a `cd`-into-nowhere shell failure) —
    proving the two are distinguished, not both collapsed into one behaviour."""
    missing = str(tmp_path / "does-not-exist")
    landed_fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                            landed=LandedSpec(target="main", remote="origin",
                                                delivered_stage=1),
                            venue="repo_root")
    stage = _shell_stage(1, status=StageStatus.PASSED.value)
    stage.outcome.delivered_head = "deadbeef"
    state = _verifying("mv1", [stage], repo_root=missing, final_check=[landed_fc])
    d = cli.cmd_verify_final(ns(session="mv1"), store=_MemStore(state), runner=None)
    assert d.ok is False
    assert d.action == "declare"

    shell_fc = FinalCheck(command="true", expected_exit=0, label="control", venue="repo_root")
    state2 = _verifying("mv2", [_shell_stage(1, status=StageStatus.PASSED.value)],
                         repo_root=missing, final_check=[shell_fc])
    d2 = cli.cmd_verify_final(ns(session="mv2"), store=_MemStore(state2), runner=None)
    assert d2.ok is False
    assert d2.node == Node.DIAGNOSING.value  # a real failure, not a refusal
    assert "failures" in d2.data


def test_no_delivery_worktree_freezes_repo_root_head_runs_green(tmp_path):
    """No [meta] delivery_worktree declared: freeze reads repo_root's own HEAD
    (resolve_check_venue's documented delivery-defaults-to-repo_root fallback),
    and the delivered commit passes the landed final_check once it is on `main`."""
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    sha = commit(work, "feature work")
    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                    landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
                    venue="repo_root")
    stage = _shell_stage(1)
    state = _executing("dw1", [stage], final_check=[fc])
    state.repo_root = str(work)
    assert state.delivery_worktree is None
    store = _MemStore(state)
    assert _record_delivered(store, "dw1").ok is True
    assert stage.outcome.delivered_head == sha
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    assert cli.cmd_verify_final(ns(session="dw1"), store=store, runner=None).ok is True


# --- the dogfood fixture loads and synthesizes end-to-end --------------------

def test_fixture_plan_landed_example_synthesizes_end_to_end(tmp_path, fixtures_dir):
    doc = load_plan(fixtures_dir / "plan_landed_example.toml", strict=True)
    stage_landed = next(
        s.criterion.landed for s in doc.stages if s.criterion.landed is not None
    )
    fc_landed = next(
        fc.landed for fc in doc.meta.final_check if fc.landed is not None
    )

    work = make_repo_with_remote(tmp_path)
    sha = rev_parse(work)  # HEAD already == main == origin/main

    stage = _landed_stage(1, target=stage_landed.target, remote=stage_landed.remote,
                           delivered_stage=stage_landed.delivered_stage)
    stage.outcome.delivered_head = sha
    state = _verifying("fx1", [stage], repo_root=str(work))

    command, refusal = state.render_landed_command(stage_landed)
    assert refusal is None
    result = cli.subprocess_runner(["bash", "-c", command])
    assert result.returncode != 0  # stage_landed targets a ticket branch that doesn't exist here

    # fc_landed targets "main", which the fixture's own stage.landed does not —
    # confirm IT synthesizes and runs green against the same delivered commit.
    command2, refusal2 = state.render_landed_command(
        LandedSpec(target="main", remote=fc_landed.remote, delivered_stage=fc_landed.delivered_stage)
    )
    assert refusal2 is None
    result2 = cli.subprocess_runner(["bash", "-c", command2])
    assert result2.returncode == 0


# --- patch-equivalence fallback: landings that do not preserve the SHA -------
# Every fixture commit carries a real diff: empty commits share git's null
# patch-id and would match each other vacuously.

def commit_file(work: Path, name: str, text: str) -> str:
    (work / name).write_text(text)
    git("add", name, cwd=work)
    git("commit", "--quiet", "-m", f"add {name}\n\n{TRAILER}", cwd=work)
    return rev_parse(work)


def landed_exit(work: Path, delivered: str, env=None) -> int:
    stage = _landed_stage(1, delivered_stage=1)
    stage.outcome.delivered_head = delivered
    state = _verifying("pe", [stage], repo_root=str(work))
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert refusal is None
    return subprocess.run(
        ["bash", "-c", command], env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    ).returncode


def deliver_on_feature_then_advance_trunk(work: Path) -> str:
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    delivered = commit_file(work, "f.txt", "feature\n")
    git("checkout", "--quiet", "main", cwd=work)
    commit_file(work, "m.txt", "trunk moved\n")
    push_main(work)
    return delivered


def land_by_rebase(work: Path, push: bool = True) -> None:
    git("checkout", "--quiet", "feature", cwd=work)
    git("rebase", "--quiet", "main", cwd=work)
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    if push:
        push_main(work)


def test_rebase_landing_is_green_on_both_refs(tmp_path):
    work = make_repo_with_remote(tmp_path)
    delivered = deliver_on_feature_then_advance_trunk(work)
    land_by_rebase(work)
    assert git("merge-base", "--is-ancestor", delivered, "main", cwd=work, check=False).returncode == 1
    assert landed_exit(work, delivered) == 0


def _git_shim(tmp_path, fail_on: str) -> dict:
    """PATH shim that records every sub-command it saw in `seen` and exits 2 on `fail_on`."""
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    seen = shim_dir / "seen"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do echo "$a" >> {seen}; [ "$a" = {fail_on} ] && exit 2; done\n'
        f'exec {real_git} "$@"\n'
    )
    shim.chmod(0o755)
    return {"env": {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}, "seen": seen}


def _rebase_landed(tmp_path) -> tuple[Path, str]:
    work = make_repo_with_remote(tmp_path)
    delivered = deliver_on_feature_then_advance_trunk(work)
    land_by_rebase(work)
    assert landed_exit(work, delivered) == 0  # green without the shim
    return work, delivered


def test_cherry_failure_is_a_git_error_not_a_stage_failure(tmp_path):
    work, delivered = _rebase_landed(tmp_path)
    shim = _git_shim(tmp_path, "cherry")
    assert landed_exit(work, delivered, env=shim["env"]) == LANDED_GIT_ERROR_EXIT
    assert "cherry" in shim["seen"].read_text().split()  # the fallback was actually reached


def test_cherry_path_merges_listing_failure_is_a_git_error(tmp_path):
    work, delivered = _rebase_landed(tmp_path)
    shim = _git_shim(tmp_path, "--merges")
    assert landed_exit(work, delivered, env=shim["env"]) == LANDED_GIT_ERROR_EXIT
    assert "--merges" in shim["seen"].read_text().split()


def test_squash_of_two_commits_is_green(tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    commit_file(work, "a.txt", "a\n")
    delivered = commit_file(work, "b.txt", "b\n")
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--squash", "feature", cwd=work)
    git("commit", "--quiet", "-m", "squashed", cwd=work)
    push_main(work)
    assert landed_exit(work, delivered) == 0


def test_merge_in_range_stays_red_though_every_patch_is_on_trunk(tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    a = commit_file(work, "a.txt", "a\n")
    git("checkout", "--quiet", "-b", "side", "main", cwd=work)
    b = commit_file(work, "b.txt", "b\n")
    git("checkout", "--quiet", "feature", cwd=work)
    git("merge", "--quiet", "--no-ff", "-m", "merge side", "side", cwd=work)
    delivered = rev_parse(work)
    git("checkout", "--quiet", "main", cwd=work)
    git("cherry-pick", a, b, cwd=work)
    push_main(work)
    out = git("cherry", "main", delivered, cwd=work).stdout
    assert "+" not in out
    assert landed_exit(work, delivered) == 1


def test_unmerged_delivery_stays_red_with_trunk_advanced(tmp_path):
    work = make_repo_with_remote(tmp_path)
    delivered = deliver_on_feature_then_advance_trunk(work)
    assert landed_exit(work, delivered) == 1


def test_local_target_has_delivery_but_remote_tracking_ref_lacks_it(tmp_path):
    work = make_repo_with_remote(tmp_path)
    delivered = deliver_on_feature_then_advance_trunk(work)
    land_by_rebase(work, push=False)
    assert landed_exit(work, delivered) == 1


# --- minimal in-memory StateStore (avoids FileStateStore/tmp_path plumbing) --

class _MemStore:
    def __init__(self, state):
        self._state = state

    def load(self, session_id):
        return self._state if self._state.session_id == session_id else None

    def save(self, state):
        self._state = state
