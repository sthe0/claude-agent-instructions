"""The landed check proves THIS task's commits reached trunk (R8, R9).

Two additions to `SessionState.render_landed_command`, both exercised against real
temp git repos (a local bare "origin" plus a clone) through the real `bash -c` the
engine runs:

* R9 — a `Task: <plan task id>` trailer must appear in the frozen range
  `delivered_base..delivered_head` before any containment test, and a landed check may
  only name a delivered stage verified in the delivery venue (submission seam).
* R8 — a squash landing of 2+ commits is recognised by patch-id equality of the
  combined diff, with the frozen `delivered_base` as the merge base so the verdict is
  monotone as trunk gains commits.

New symbols (`delivered_base`, `plan_task_id`, the venue rule) are referenced inside
test bodies only, so the pre-change tree fails these tests on assertions, not at
collection.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.plan import parse_plan
from agentctl.state import (
    Actor,
    CheckKind,
    Criterion,
    CriterionType,
    FinalCheck,
    LANDED_GIT_ERROR_EXIT,
    LandedSpec,
    Means,
    Outcome,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)
from agentctl.submission import submission_violations

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
TASK = "proof-task"


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
        check=check, capture_output=True, text=True,
    )


def rev(cwd, ref="HEAD") -> str:
    return git("rev-parse", ref, cwd=cwd).stdout.strip()


def commit_file(work: Path, name: str, text: str, trailer: str | None = f"Task: {TASK}") -> str:
    (work / name).write_text(text)
    git("add", name, cwd=work)
    msg = f"add {name}" + (f"\n\n{trailer}" if trailer else "")
    git("commit", "--quiet", "-m", msg, cwd=work)
    return rev(work)


def make_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    commit_file(work, "README.md", "seed\n", trailer=None)
    git("push", "--quiet", "-u", "origin", "main", cwd=work)
    return work


def push_main(work: Path) -> None:
    git("push", "--quiet", "origin", "main", cwd=work)


def feature_branch(work: Path, *files: str, trailer: str | None = f"Task: {TASK}") -> tuple[str, str]:
    """Commit one file per name on a new `feature` branch; return (base, head)."""
    base = rev(work)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    for name in files:
        commit_file(work, name, f"{name}\n", trailer=trailer)
    head = rev(work)
    git("checkout", "--quiet", "main", cwd=work)
    return base, head


def land_by_squash(work: Path) -> None:
    git("merge", "--quiet", "--squash", "feature", cwd=work)
    git("commit", "--quiet", "-m", "squashed", cwd=work)
    push_main(work)


def _stage(index=1, *, venue="delivery"):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="c",
            verify_command="true", verify_venue=venue,
        ),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )


def landed_run(work, head, base, *, session_task="t", plan_task_id=TASK, plan_path=None, env=None):
    stage = _stage()
    stage.outcome.delivered_head = head
    stage.outcome.delivered_base = base
    state = SessionState(session_id="tp", task_id=session_task, stages=[stage],
                         repo_root=str(work))
    state.plan_task_id = plan_task_id
    if plan_path:
        state.plan_path = plan_path
    command, refusal = state.render_landed_command(LandedSpec(target="main", delivered_stage=1))
    assert refusal is None, refusal
    return subprocess.run(
        ["bash", "-c", command], env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    )


def _git_shim(tmp_path, fail_on):
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do [ "$a" = {fail_on} ] && exit 2; done\n'
        f'exec {real_git} "$@"\n'
    )
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


# --- R8: squash landings ------------------------------------------------------

def test_squash_of_three_commits_is_green(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", "b.txt", "c.txt")
    land_by_squash(work)
    assert git("merge-base", "--is-ancestor", head, "main", cwd=work, check=False).returncode == 1
    assert landed_run(work, head, base).returncode == 0


def test_unrelated_trunk_change_is_red(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", "b.txt")
    commit_file(work, "other.txt", "unrelated\n")
    push_main(work)
    assert landed_run(work, head, base).returncode == 1


def test_squash_green_stays_green_when_trunk_gains_commits(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", "b.txt")
    land_by_squash(work)
    assert landed_run(work, head, base).returncode == 0
    commit_file(work, "later1.txt", "one\n")
    commit_file(work, "later2.txt", "two\n")
    push_main(work)
    assert landed_run(work, head, base).returncode == 0


def test_squash_green_with_legacy_state_lacking_a_frozen_base(tmp_path):
    work = make_repo(tmp_path)
    _, head = feature_branch(work, "a.txt", "b.txt")
    land_by_squash(work)
    assert landed_run(work, head, None).returncode == 0


def test_rebase_landing_still_green(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "f.txt")
    commit_file(work, "m.txt", "trunk moved\n")
    push_main(work)
    git("checkout", "--quiet", "feature", cwd=work)
    git("rebase", "--quiet", "main", cwd=work)
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    assert git("merge-base", "--is-ancestor", head, "main", cwd=work, check=False).returncode == 1
    assert landed_run(work, head, base).returncode == 0


def test_patch_id_failure_is_a_git_error(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", "b.txt")
    land_by_squash(work)
    result = landed_run(work, head, base, env=_git_shim(tmp_path, "patch-id"))
    assert result.returncode == LANDED_GIT_ERROR_EXIT


def test_unknown_delivered_commit_is_a_git_error(tmp_path):
    work = make_repo(tmp_path)
    result = landed_run(work, "0123456789abcdef0123456789abcdef01234567", rev(work))
    assert result.returncode == LANDED_GIT_ERROR_EXIT


# --- R9: the commits are THIS task's ---------------------------------------------

def test_range_without_task_trailer_is_red_even_when_ancestor_of_trunk(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", trailer=None)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    result = landed_run(work, head, base)
    assert result.returncode == 1
    assert f"Task: {TASK}" in result.stderr


def test_empty_range_is_red(tmp_path):
    work = make_repo(tmp_path)
    head = rev(work)
    assert landed_run(work, head, head).returncode == 1


def test_legacy_state_requires_trailer_on_the_delivered_commit_itself(tmp_path):
    work = make_repo(tmp_path)
    _, with_trailer = feature_branch(work, "a.txt")
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    assert landed_run(work, with_trailer, None).returncode == 0

    git("checkout", "--quiet", "-b", "bare", cwd=work)
    without = commit_file(work, "z.txt", "z\n", trailer=None)
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "bare", cwd=work)
    push_main(work)
    assert landed_run(work, without, None).returncode == 1


def test_identity_is_the_plans_task_id_not_the_sessions(tmp_path):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt")
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    assert landed_run(work, head, base, session_task="named-by-start", plan_task_id=TASK).returncode == 0
    assert landed_run(work, head, base, session_task=TASK, plan_task_id="another-plan").returncode == 1


def test_legacy_state_without_plan_task_id_reads_the_plan_file(tmp_path, fixtures_dir):
    work = make_repo(tmp_path)
    base, head = feature_branch(work, "a.txt", trailer="Task: landed-example")
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    plan = str(fixtures_dir / "plan_landed_example.toml")
    result = landed_run(work, head, base, session_task="named-by-start", plan_task_id="", plan_path=plan)
    assert result.returncode == 0


def test_submit_plan_stamps_plan_task_id_from_meta(store, fixtures_dir):
    sid = "stamp1"
    cli.cmd_start(Namespace(session=sid, task="named-by-start", goal="", done_criterion="",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    cli.cmd_submit_plan(Namespace(session=sid, plan=str(fixtures_dir / "plan_two_stage_refined.toml")),
                        store=store)
    state = store.load(sid)
    assert state.task_id == "named-by-start"
    assert state.plan_task_id == "demo-two-stage"


# --- delivered_base is frozen with the head ------------------------------------------

def test_freeze_stamps_delivered_base_and_keeps_it_after_landing(tmp_path):
    work = make_repo(tmp_path)
    seed = rev(work)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    head = commit_file(work, "a.txt", "a\n")
    fc = FinalCheck(command="", kind=CheckKind.LANDED.value,
                    landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
                    venue="repo_root")
    stage = _stage()
    state = SessionState(session_id="fz", task_id="t", stages=[stage], repo_root=str(work),
                         final_check=[fc])
    cli._freeze_delivered_head(state, stage, None)
    assert stage.outcome.delivered_head == head
    assert stage.outcome.delivered_base == seed

    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push_main(work)
    git("checkout", "--quiet", "feature", cwd=work)
    cli._freeze_delivered_head(state, stage, None)
    assert stage.outcome.delivered_base == seed


def test_plan_without_landed_check_writes_no_base(tmp_path):
    work = make_repo(tmp_path)
    stage = _stage()
    state = SessionState(session_id="nf", task_id="t", stages=[stage], repo_root=str(work))
    assert cli._needs_delivered_head_freeze(state, 1) is False
    assert stage.outcome.delivered_base is None


# --- submission: the delivered stage must be verified in the delivery venue -----------

def _plan_data(venue):
    return {
        "meta": {
            "task_id": "venue-test", "goal": "g", "done_criterion": "d",
            "criterion_type": "measurable", "weight_class": "substantive",
            "external_research": "n/a",
        },
        "stage": [{
            "index": 1, "title": "Deliver", "executor": "in_thread",
            "expected_result_image": "the change exists", "criterion_type": "measurable",
            "done_criterion": "the check passes", "verify_command": "true",
            "verify_venue": venue, "negative_control_waiver": "n/a",
            "material": "the module", "means": "edit", "method": "do it",
            "conditions": "c", "invariants": "inv", "capability_required": "cap",
            "principle": {"statement": "s", "source": "src", "derivation": "d follows from src",
                          "confidence": "high", "refutation": "ref"},
        }],
        "final_check": [{"kind": "landed", "label": "landed",
                         "landed": {"target": "main", "remote": "origin", "delivered_stage": 1}}],
    }


def test_repo_root_venue_delivered_stage_is_a_submission_violation():
    problems = submission_violations(parse_plan(_plan_data("repo_root")))
    assert any("delivered_stage 1" in p and "repo_root" in p for p in problems), problems


def test_delivery_venue_delivered_stage_passes_the_venue_rule():
    problems = submission_violations(parse_plan(_plan_data("delivery")))
    assert not any("delivered_stage" in p for p in problems), problems
