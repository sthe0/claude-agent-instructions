"""The landed check proves a rebased delivery from the landing `record-landing` recorded
(Core issue #292).

A delivery rebased onto a moved trunk — with or without conflict resolution — lands as
new commits whose patch-ids differ from the frozen ones, so the frozen-head check is red
on work that did land. `record-landing` stores the landing's (base, head) on the stage,
and `SessionState.render_landed_command` first tries to prove the delivery from that
range: the range is on trunk, every range commit carries this task's trailer, every
delivered message reappears whole, and every delivered added or removed line reappears
per path (unless the user waived that content fact). Anything else falls through to the
unchanged frozen-head check.

Every case runs against real temp git repos through the real `bash -c` the engine runs.
New symbols are referenced inside test bodies only, so the pre-change tree fails these
tests on assertions, not at collection.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli
from agentctl.state import (
    Actor, CheckKind, Criterion, CriterionType, FinalCheck, LANDED_GIT_ERROR_EXIT,
    LandedSpec, Means, Node, Outcome, SessionState, Stage, StageStatus, Subject,
)
from agentctl.store import FileStateStore
from conftest import STAGE_OBSERVATIONS

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_EDITOR": "true",
}
TASK = "landing-task"
TRAILER = f"Task: {TASK}"
MSG = f"change f\n\n{TRAILER}\nStage: 1"
SEED = "a\nb\nc\nd\n"
TRUNK_NEAR_HUNK = {"f.txt": "a\nb\nc\nD\n"}
LANDED_NEAR_HUNK = {"f.txt": "a\nB\nc\nD\n"}
WAIVER_MARKER = "[content-waiver-ask]"
SID = "rl"


def git(*args, cwd, check=True, env=None):
    return subprocess.run(["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV, **(env or {})},
                          check=check, capture_output=True, text=True)


def rev(cwd, ref="HEAD") -> str:
    return git("rev-parse", ref, cwd=cwd).stdout.strip()


def write(work: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit(work: Path, files: dict[str, str], message: str = "", reuse: str | None = None) -> str:
    write(work, files)
    git("add", "-A", cwd=work)
    if reuse:
        git("commit", "--quiet", "--allow-empty", "-C", reuse, cwd=work)
    else:
        git("commit", "--quiet", "--allow-empty", "-m", message, cwd=work)
    return rev(work)


def make_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    commit(work, files or {"f.txt": SEED}, "seed")
    git("push", "--quiet", "-u", "origin", "main", cwd=work)
    return work


def push(work: Path) -> None:
    git("push", "--quiet", "origin", "main", cwd=work)


def deliver(work: Path, *changes: dict[str, str], message: str = MSG) -> tuple[str, str]:
    """One commit per `changes` on a new `feature` branch off main; return (base, head)."""
    base = rev(work)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    for i, files in enumerate(changes):
        commit(work, files, message if i == 0 else f"{message}\n\npart {i}")
    head = rev(work)
    git("checkout", "--quiet", "main", cwd=work)
    return base, head


def move_trunk(work: Path, files: dict[str, str]) -> str:
    sha = commit(work, files, "trunk edit")
    push(work)
    return sha


def land(work: Path, *commits: tuple[dict[str, str], str]) -> tuple[str, str]:
    """Commit each (files, delivered sha whose message to reuse) on main and push;
    return the recorded (base, head)."""
    base = rev(work)
    for files, reuse in commits:
        commit(work, files, reuse=reuse)
    push(work)
    return base, rev(work)


def _stage(index=1):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type=CriterionType.MEASURABLE.value, done_criterion="c",
                            verify_command="true", verify_venue="delivery"),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )


def landed_state(work, dh, db, lb=None, lh=None, *, waiver=None, with_check=True) -> SessionState:
    stage = _stage()
    stage.outcome.delivered_head = dh
    stage.outcome.delivered_base = db
    stage.outcome.landed_head = lh
    stage.outcome.landed_base = lb
    stage.outcome.landed_content_waiver = waiver
    final_check = [FinalCheck(command="", kind=CheckKind.LANDED.value,
                              landed=LandedSpec(target="main", delivered_stage=1))] if with_check else []
    state = SessionState(session_id=SID, task_id="t", stages=[stage], repo_root=str(work),
                         final_check=final_check)
    state.plan_task_id = TASK
    return state


def check(work, dh, db, lb=None, lh=None, *, waiver=None, env=None):
    state = landed_state(work, dh, db, lb, lh, waiver=waiver)
    command, refusal = state.render_landed_command(LandedSpec(target="main", delivered_stage=1))
    assert refusal is None, refusal
    return subprocess.run(["bash", "-c", command], env={**os.environ, **(env or {})},
                          capture_output=True, text=True)


def _git_shim(tmp_path, fail_on):
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text("#!/bin/sh\n"
                    f'for a in "$@"; do [ "$a" = {fail_on} ] && exit 2; done\n'
                    f'exec {real_git} "$@"\n')
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


# --- the issue reproducer ----------------------------------------------------------

def test_rebase_with_resolved_adjacent_conflict_is_green_only_when_recorded(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, {"f.txt": "a\nb\nC\nd\n"})
    git("checkout", "--quiet", "feature", cwd=work)
    assert git("rebase", "main", cwd=work, check=False).returncode != 0
    write(work, {"f.txt": "a\nB\nC\nd\n"})
    git("add", "f.txt", cwd=work)
    git("rebase", "--continue", cwd=work)
    lh = rev(work)
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--ff-only", "feature", cwd=work)
    push(work)

    assert check(work, dh, db).returncode == 1
    result = check(work, dh, db, lb, lh)
    assert result.returncode == 0, result.stderr


GENERATED_HEADER = "# Generated by scripts/gen_registry.py — DO NOT HAND-EDIT.\n"


def test_rebase_regenerated_generated_file_passes_with_recorded_landing(tmp_path):
    """A rebase that regenerates a generated file (its lines change) passes the landed
    check from the recorded landing, while the frozen-head check alone stays red."""
    work = make_repo(tmp_path, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\n'})
    db, dh = deliver(work, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\nentry_a = 3\n'})
    move_trunk(work, {"registry.toml": GENERATED_HEADER + 'entry_c = "5"\nentry_x = "1"\n'})
    lb, lh = land(work, ({"registry.toml": GENERATED_HEADER + 'entry_a = "3"\nentry_c = "5"\nentry_x = "1"\n'}, dh))

    assert check(work, dh, db).returncode == 1
    result = check(work, dh, db, lb, lh)
    assert result.returncode == 0, result.stderr


def test_rebase_regenerated_file_delivery_not_on_main_still_fails(tmp_path):
    """A delivery whose regenerated commit sits on a side branch, not main, stays red."""
    work = make_repo(tmp_path, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\n'})
    db, dh = deliver(work, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\nentry_a = 3\n'})
    move_trunk(work, {"registry.toml": GENERATED_HEADER + 'entry_c = "5"\nentry_x = "1"\n'})
    lb = rev(work)
    git("checkout", "--quiet", "-b", "regen-side", cwd=work)
    side_lh = commit(work, {"registry.toml": GENERATED_HEADER + 'entry_a = "3"\nentry_c = "5"\nentry_x = "1"\n'}, reuse=dh)
    git("checkout", "--quiet", "main", cwd=work)

    result = check(work, dh, db, lb, side_lh)
    assert result.returncode == 1
    assert "the recorded head is not on the target ref" in result.stderr


def test_a_dropped_line_in_a_non_generated_file_stays_red_beside_a_generated_one(tmp_path):
    """The generated-file exemption is per path: a delivered line dropped from an ordinary
    file is still caught when the same delivery also carries a regenerated file."""
    work = make_repo(tmp_path, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\n', "f.txt": SEED})
    db, dh = deliver(work, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\nentry_a = 3\n',
                            "f.txt": SEED + "new1\nnew2\n"})
    move_trunk(work, {"registry.toml": GENERATED_HEADER + 'entry_c = "5"\nentry_x = "1"\n',
                      "f.txt": "a\nb\nC\nd\n"})
    lb, lh = land(work, ({"registry.toml": GENERATED_HEADER + 'entry_a = "3"\nentry_c = "5"\nentry_x = "1"\n',
                          "f.txt": "a\nb\nC\nd\nnew1\n"}, dh))
    red = check(work, dh, db, lb, lh)
    assert red.returncode == 1
    assert "a delivered line of f.txt is missing" in red.stderr

    lb2, lh2 = land(work, ({"f.txt": "a\nb\nC\nd\nnew1\nnew2\n"}, dh))
    assert check(work, dh, db, lb, lh2).returncode == 0


def test_a_file_whose_landed_blob_lost_the_generated_header_is_compared(tmp_path):
    """Exempt only while both the delivered and the landed blob carry the header."""
    work = make_repo(tmp_path, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\n'})
    db, dh = deliver(work, {"registry.toml": GENERATED_HEADER + 'entry_x = "1"\nentry_a = 3\n'})
    move_trunk(work, {"registry.toml": GENERATED_HEADER + 'entry_c = "5"\nentry_x = "1"\n'})
    lb, lh = land(work, ({"registry.toml": 'entry_c = "5"\nentry_x = "1"\n'}, dh))

    result = check(work, dh, db, lb, lh)
    assert result.returncode == 1
    assert "a delivered line of registry.toml is missing" in result.stderr


def test_overlap_conflict_resolution_is_red_without_a_waiver_and_green_with_one(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    move_trunk(work, {"f.txt": "a\nX\nc\nd\n"})
    lb, lh = land(work, ({"f.txt": "a\nB X\nc\nd\n"}, dh))

    red = check(work, dh, db, lb, lh)
    assert red.returncode == 1
    assert "a delivered line of f.txt is missing" in red.stderr
    green = check(work, dh, db, lb, lh, waiver="user accepts the merged line")
    assert green.returncode == 0, green.stderr
    assert "content fact waived by the user: user accepts the merged line" in green.stderr


# --- each fact violated on its own ----------------------------------------------------

def test_a_resolution_that_drops_a_delivered_added_line_is_red(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": SEED + "new1\nnew2\n"})
    move_trunk(work, {"f.txt": "a\nb\nC\nd\n"})
    lb, lh = land(work, ({"f.txt": "a\nb\nC\nd\nnew1\n"}, dh))
    assert check(work, dh, db, lb, lh).returncode == 1
    lb2, lh2 = land(work, ({"f.txt": "a\nb\nC\nd\nnew1\nnew2\n"}, dh))
    assert check(work, dh, db, lb, lh2).returncode == 0


def test_a_resolution_that_restores_a_delivered_removed_line_is_red(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nc\nd\n"})
    move_trunk(work, {"f.txt": "a\nb\nC\nd\n"})
    lb, lh = land(work, ({"f.txt": "a\nb\nC\nd\ne\n"}, dh))
    assert check(work, dh, db, lb, lh).returncode == 1


def test_a_recorded_range_with_a_foreign_commit_is_red(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    commit(work, LANDED_NEAR_HUNK, reuse=dh)
    commit(work, {"h.txt": "other\n"}, "someone else's work")
    push(work)
    result = check(work, dh, db, lb, rev(work))
    assert result.returncode == 1
    assert "lacks the task trailer" in result.stderr


def _range_with_extra_commit(tmp_path, extra_message):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    commit(work, LANDED_NEAR_HUNK, reuse=dh)
    commit(work, {"h.txt": "coordinator\n"}, extra_message)
    push(work)
    return check(work, dh, db, lb, rev(work))


def test_a_range_commit_with_the_order_session_line_is_green(tmp_path):
    result = _range_with_extra_commit(tmp_path, f"in-thread edit\n\nAgent-Session: {SID}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("message", [
    "in-thread edit\n\nAgent-Session: other",
    f"in-thread edit\n\nsee Agent-Session: {SID} for context",
])
def test_a_range_commit_with_a_foreign_session_line_is_red(tmp_path, message):
    result = _range_with_extra_commit(tmp_path, message)
    assert result.returncode == 1
    assert "lacks the task trailer" in result.stderr


def test_a_range_missing_a_delivered_message_is_red(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"}, {"g.txt": "g\n"})
    first = rev(work, "feature~1")
    move_trunk(work, TRUNK_NEAR_HUNK)
    lb, lh = land(work, ({**LANDED_NEAR_HUNK, "g.txt": "g\n"}, first))
    result = check(work, dh, db, lb, lh)
    assert result.returncode == 1
    assert "is not in the recorded range" in result.stderr


@pytest.mark.parametrize("message", [
    f"change f\n\nanother body\n\n{TRAILER}\nStage: 1",
    f"change f\n\n{TRAILER}\nStage: 2",
])
def test_a_near_miss_message_is_red(tmp_path, message):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    commit(work, LANDED_NEAR_HUNK, message)
    push(work)
    assert check(work, dh, db, lb, rev(work)).returncode == 1


def test_a_recorded_head_not_on_trunk_is_red_unless_the_frozen_head_path_proves_it(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, {"h.txt": "trunk\n"})
    git("checkout", "--quiet", "-b", "side", cwd=work)
    lh = commit(work, {"f.txt": "a\nB\nc\nd\n"}, reuse=dh)
    git("checkout", "--quiet", "main", cwd=work)
    result = check(work, dh, db, lb, lh)
    assert result.returncode == 1
    assert "the recorded head is not on the target ref" in result.stderr

    git("merge", "--quiet", "--no-edit", "feature", cwd=work)
    push(work)
    assert check(work, dh, db, lb, lh).returncode == 0


def test_a_recorded_head_on_the_local_trunk_only_is_not_proven(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    lh = commit(work, LANDED_NEAR_HUNK, reuse=dh)
    result = check(work, dh, db, lb, lh)
    assert result.returncode == 1
    assert "the recorded head is not on the target ref" in result.stderr
    push(work)
    assert check(work, dh, db, lb, lh).returncode == 0


def test_a_recorded_base_that_is_not_an_ancestor_of_the_head_is_not_proven(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    git("checkout", "--quiet", "-b", "side", lb, cwd=work)
    stray = commit(work, {"s.txt": "s\n"}, "side work")
    git("checkout", "--quiet", "main", cwd=work)
    _, lh = land(work, (LANDED_NEAR_HUNK, dh))
    assert check(work, dh, db, lb, lh).returncode == 0
    result = check(work, dh, db, stray, lh)
    assert result.returncode == 1
    assert "the recorded base is not an ancestor of the recorded head" in result.stderr


def test_a_missing_delivered_commit_falls_through_rather_than_erroring(tmp_path):
    from agentctl.state import recorded_landing_script
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    move_trunk(work, TRUNK_NEAR_HUNK)
    lb, lh = land(work, (LANDED_NEAR_HUNK, dh))
    ghost = "0123456789abcdef" * 2 + "01234567"
    fragment = recorded_landing_script(
        f"git -C {shlex.quote(str(work))}", landed_head=lh, landed_base=lb, delivered_head=ghost,
        delivered_base=db, trailer=TRAILER, refs="main origin/main", waiver=None,
        err=LANDED_GIT_ERROR_EXIT)
    alone = subprocess.run(["bash", "-c", fragment], capture_output=True, text=True)
    assert alone.returncode == 1
    assert f"commit {ghost} is not in the repository" in alone.stderr
    composed = check(work, ghost, db, lb, lh)
    assert composed.returncode == LANDED_GIT_ERROR_EXIT
    assert f"commit {ghost} is not in the repository" in composed.stderr


def test_a_merge_commit_in_the_recorded_range_is_not_proven(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    lb = move_trunk(work, TRUNK_NEAR_HUNK)
    git("checkout", "--quiet", "-b", "side", lb, cwd=work)
    commit(work, LANDED_NEAR_HUNK, reuse=dh)
    git("checkout", "--quiet", "main", cwd=work)
    commit(work, {"k.txt": "k\n"}, f"more\n\n{TRAILER}")
    git("merge", "--quiet", "--no-edit", "side", cwd=work)
    push(work)
    result = check(work, dh, db, lb, rev(work))
    assert result.returncode == 1
    assert "holds a merge commit" in result.stderr


# --- the line comparison --------------------------------------------------------------

def test_delivered_lines_starting_with_dashes_and_pluses_are_compared(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": SEED + "-- dash\n++ plus\n"})
    move_trunk(work, {"f.txt": "a\nb\nC\nd\n"})
    kept_base, kept_head = land(work, ({"f.txt": "a\nb\nC\nd\n-- dash\n++ plus\n"}, dh))
    assert check(work, dh, db, kept_base, kept_head).returncode == 0

    (tmp_path / "other").mkdir()
    other = make_repo(tmp_path / "other")
    db, dh = deliver(other, {"f.txt": SEED + "-- dash\n++ plus\n"})
    move_trunk(other, {"f.txt": "a\nb\nC\nd\n"})
    lb, lh = land(other, ({"f.txt": "a\nb\nC\nd\n-- dash\n"}, dh))
    assert check(other, dh, db, lb, lh).returncode == 1


def test_a_path_with_a_space_is_compared(tmp_path):
    name = "dir/with space.txt"
    work = make_repo(tmp_path, {name: SEED})
    db, dh = deliver(work, {name: "a\nB\nc\nd\n"})
    move_trunk(work, {name: "a\nb\nc\nD\n"})
    lb, lh = land(work, ({name: "a\nB\nc\nD\n"}, dh))
    assert check(work, dh, db, lb, lh).returncode == 0

    lb2, lh2 = land(work, ({name: "a\nb\nc\nD\nzzz\n"}, dh))
    result = check(work, dh, db, lb2, lh2)
    assert result.returncode == 1
    assert f"a delivered line of {name} is missing" in result.stderr


def test_legacy_state_without_delivered_base_compares_the_head_commit_alone(tmp_path):
    work = make_repo(tmp_path)
    _, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    move_trunk(work, {"f.txt": "a\nb\nc\nD\n"})
    lb, lh = land(work, ({"f.txt": "a\nB\nc\nD\n"}, dh))
    assert check(work, dh, None, lb, lh).returncode == 0
    lb2, lh2 = land(work, ({"f.txt": "a\nb\nc\nD\nq\n"}, dh))
    assert check(work, dh, None, lb2, lh2).returncode == 1


def test_an_empty_delivery_is_green_on_a_trailered_recorded_range(tmp_path):
    work = make_repo(tmp_path)
    empty = rev(work)
    commit(work, {"g.txt": "g\n"}, "untrailered")
    push(work)
    assert check(work, empty, empty, empty, rev(work)).returncode == 1
    lb = rev(work)
    commit(work, {"h.txt": "h\n"}, MSG)
    push(work)
    assert check(work, empty, empty).returncode == 1
    assert check(work, empty, empty, lb, rev(work)).returncode == 0


def test_a_git_error_in_the_recorded_path_exits_97(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    move_trunk(work, {"h.txt": "trunk\n"})
    lb, lh = land(work, ({"f.txt": "a\nB\nc\nd\n"}, dh))
    assert check(work, dh, db, lb, lh).returncode == 0
    result = check(work, dh, db, lb, lh, env=_git_shim(tmp_path, "--count"))
    assert result.returncode == LANDED_GIT_ERROR_EXIT


def test_without_a_recorded_landing_the_command_is_the_frozen_head_one(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    plain = landed_state(work, dh, db).render_landed_command(LandedSpec(target="main", delivered_stage=1))
    recorded = landed_state(work, dh, db, "x" * 40, "y" * 40).render_landed_command(
        LandedSpec(target="main", delivered_stage=1))
    assert recorded[0].endswith(plain[0])
    assert "recorded landing" not in plain[0]


def test_a_changed_frozen_head_clears_the_recorded_landing(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    git("checkout", "--quiet", "feature", cwd=work)
    state = landed_state(work, dh, db, db, dh, waiver="w")
    stage = state.stages[0]
    cli._freeze_delivered_head(state, stage, None)
    assert (stage.outcome.landed_head, stage.outcome.landed_base,
            stage.outcome.landed_content_waiver) == (dh, db, "w")
    commit(work, {"g.txt": "g\n"}, MSG)
    cli._freeze_delivered_head(state, stage, None)
    assert (stage.outcome.landed_head, stage.outcome.landed_base,
            stage.outcome.landed_content_waiver) == (None, None, None)


# --- record-landing -------------------------------------------------------------------

@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "projects" / "proj").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    return home / "projects" / "proj" / f"{SID}.jsonl"


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(cli, "time", SimpleNamespace(time=lambda: now["t"]))
    return now


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


QUESTION = "Accept the content loss?"
ACCEPT = f"Accept {WAIVER_MARKER}"
NO_ANSWERS = object()


def ask(call_id: str, ts: float, *, choose: str = ACCEPT, labels=(ACCEPT, "No"),
        answered: bool = True, answers=None) -> list[dict]:
    """The transcript entries of one AskUserQuestion: the tool_use and, when answered,
    the user's tool_result with the harness's `toolUseResult.answers` naming `choose`
    (`answers=NO_ANSWERS` omits toolUseResult)."""
    entries = [{"type": "assistant", "timestamp": iso(ts), "message": {
        "role": "assistant", "content": [{
            "type": "tool_use", "id": call_id, "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": QUESTION, "header": "Landing", "multiSelect": False,
                "options": [{"label": label, "description": ""} for label in labels]}]}}]}}]
    if answered:
        result = {"type": "user", "timestamp": iso(ts + 1), "message": {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id,
                "content": f'User has answered your questions: "{QUESTION}"="{choose}".'}]}}
        if answers is not NO_ANSWERS:
            result["toolUseResult"] = {"questions": entries[0]["message"]["content"][0]["input"]["questions"],
                                       "answers": {QUESTION: choose} if answers is None else answers}
        entries.append(result)
    return entries


def write_asks(path: Path, *asks) -> None:
    """Each item is a (call id, ts) pair — an answered ask whose marked option was
    chosen — or the entries `ask` built."""
    entries = [e for a in asks for e in (ask(*a) if isinstance(a, tuple) else a)]
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


@pytest.fixture
def landed(tmp_path):
    work = make_repo(tmp_path)
    db, dh = deliver(work, {"f.txt": "a\nB\nc\nd\n"})
    move_trunk(work, {"f.txt": "a\nX\nc\nd\n"})
    lb, lh = land(work, ({"f.txt": "a\nB X\nc\nd\n"}, dh))
    store = FileStateStore(tmp_path / "state")
    store.save(landed_state(work, dh, db))
    return SimpleNamespace(work=work, store=store, db=db, dh=dh, lb=lb, lh=lh)


def record(landed, **kw):
    args = {"session": SID, "stage": 1, "head": landed.lh, "base": landed.lb,
            "accept_content_loss": None, **kw}
    return cli.cmd_record_landing(Namespace(**args), store=landed.store)


def record_events(landed):
    return [e for e in landed.store.load(SID).history if e["event"] == "record_landing"]


def outcome_fields(landed):
    o = landed.store.load(SID).stages[0].outcome
    return o.landed_head, o.landed_base, o.landed_content_waiver


def test_record_landing_stores_full_shas_and_logs_a_stamped_event(landed, clock):
    node = landed.store.load(SID).node
    d = record(landed, head=landed.lh[:10], base=landed.lb[:10])
    assert d.ok, d.detail
    assert outcome_fields(landed) == (landed.lh, landed.lb, None)
    [event] = record_events(landed)
    assert (event["stage"], event["head"], event["base"], event["at"]) == (1, landed.lh, landed.lb, 1_000_000.0)
    assert landed.store.load(SID).node == node
    state = landed.store.load(SID)
    command, _ = state.render_landed_command(LandedSpec(target="main", delivered_stage=1))
    assert subprocess.run(["bash", "-c", command], capture_output=True, text=True).returncode == 1


@pytest.mark.parametrize("kw, reason", [
    ({"stage": 9}, "stage 9 does not exist"),
    ({"head": "0" * 40}, "is not a commit"),
    ({"base": "not-a-ref"}, "is not a commit"),
    ({"swap": True}, "is not a proper ancestor"),
    ({"same": True}, "is not a proper ancestor"),
    ({"accept_content_loss": "   "}, "needs a non-empty reason"),
])
def test_record_landing_refusals_change_nothing(landed, kw, reason):
    if kw.pop("swap", False):
        kw = {"head": landed.lb, "base": landed.lh}
    if kw.pop("same", False):
        kw = {"head": landed.lh, "base": landed.lh}
    d = record(landed, **kw)
    assert not d.ok
    assert reason in d.detail
    assert record_events(landed) == []
    assert outcome_fields(landed) == (None, None, None)


def test_record_landing_refuses_a_stage_no_git_landed_check_names(landed):
    state = landed.store.load(SID)
    state.final_check = []
    landed.store.save(state)
    d = record(landed)
    assert not d.ok and "no git landed check names stage 1" in d.detail
    assert record_events(landed) == []


def test_a_waiver_needs_an_earlier_stamped_record(landed, transcripts, clock):
    write_asks(transcripts, ("ask1", 999_000.0))
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "no earlier record_landing" in d.detail
    assert outcome_fields(landed) == (None, None, None)


def test_a_waiver_needs_an_answered_marked_ask_after_the_record(landed, transcripts, clock):
    assert record(landed).ok
    clock["t"] += 10
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "cannot locate or read the transcript" in d.detail
    write_asks(transcripts, ("early", 999_000.0))
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "no answered AskUserQuestion" in d.detail
    assert len(record_events(landed)) == 1


def test_the_waiver_sequence_and_its_one_answer_per_waiver_rule(landed, transcripts, clock):
    assert record(landed).ok
    write_asks(transcripts, ("ask1", 1_000_005.0))
    clock["t"] = 1_000_010.0
    d = record(landed, accept_content_loss="the merged line is what the user wants")
    assert d.ok, d.detail
    assert outcome_fields(landed)[2] == "the merged line is what the user wants"
    state = landed.store.load(SID)
    command, _ = state.render_landed_command(LandedSpec(target="main", delivered_stage=1))
    green = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert green.returncode == 0, green.stderr
    assert "the merged line is what the user wants" in green.stderr

    clock["t"] = 1_000_020.0
    d = record(landed, accept_content_loss="again")
    assert not d.ok and "no answered AskUserQuestion" in d.detail

    write_asks(transcripts, ("ask1", 1_000_005.0), ("ask2", 1_000_025.0))
    clock["t"] = 1_000_030.0
    assert record(landed, accept_content_loss="again").ok
    assert outcome_fields(landed)[2] == "again"

    clock["t"] = 1_000_040.0
    assert record(landed).ok
    assert outcome_fields(landed)[2] is None


@pytest.mark.parametrize("answer", [
    ask("a", 1_000_005.0, choose="No"),
    ask("a", 1_000_005.0, choose="something the user typed"),
    ask("a", 1_000_005.0, answers=NO_ANSWERS),
    ask("a", 1_000_005.0, answers={"another question": ACCEPT}),
    ask("a", 1_000_005.0, labels=(ACCEPT, f"Also accept {WAIVER_MARKER}")),
], ids=["declined", "free-text", "no-answers", "other-question", "no-way-to-decline"])
def test_a_waiver_needs_the_user_to_choose_the_marked_option(landed, transcripts, clock, answer):
    assert record(landed).ok
    write_asks(transcripts, answer)
    clock["t"] = 1_000_010.0
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "the user did not consent" in d.detail
    assert outcome_fields(landed)[2] is None


def test_an_unanswered_marked_ask_after_the_record_licenses_no_waiver(landed, transcripts, clock):
    assert record(landed).ok
    write_asks(transcripts, ask("a", 1_000_005.0, answered=False))
    clock["t"] = 1_000_010.0
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "no answered AskUserQuestion" in d.detail


def test_an_answered_unmarked_ask_after_the_record_licenses_no_waiver(landed, transcripts, clock):
    clock["t"] = 999_990.0
    assert record(landed).ok
    clock["t"] = 1_000_000.0
    assert record(landed).ok
    write_asks(transcripts, ("before", 999_995.0),
               ask("after", 1_000_005.0, choose="Accept", labels=("Accept", "No")))
    clock["t"] = 1_000_010.0
    d = record(landed, accept_content_loss="ok")
    assert not d.ok and "no answered AskUserQuestion" in d.detail


def test_one_answer_licenses_one_waiver_across_stages(landed, transcripts, clock):
    state = landed.store.load(SID)
    second = _stage(2)
    second.outcome.delivered_head, second.outcome.delivered_base = landed.dh, landed.db
    state.stages.append(second)
    state.final_check.append(FinalCheck(command="", kind=CheckKind.LANDED.value,
                                        landed=LandedSpec(target="main", delivered_stage=2)))
    landed.store.save(state)
    assert record(landed, stage=1).ok
    assert record(landed, stage=2).ok
    write_asks(transcripts, ("one", 1_000_005.0))
    clock["t"] = 1_000_010.0
    assert record(landed, stage=1, accept_content_loss="stage 1 loss").ok
    clock["t"] = 1_000_020.0
    d = record(landed, stage=2, accept_content_loss="stage 2 loss")
    assert not d.ok and "no answered AskUserQuestion" in d.detail
    assert landed.store.load(SID).stages[1].outcome.landed_content_waiver is None

    write_asks(transcripts, ("one", 1_000_005.0), ("two", 1_000_025.0))
    clock["t"] = 1_000_030.0
    assert record(landed, stage=2, accept_content_loss="stage 2 loss").ok


def test_record_landing_is_a_user_authority_session_command():
    from lib import widening_targets
    assert "record-landing" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert "record-landing" in cli._SESSION_COMMANDS
    assert cli.COMMANDS["record-landing"] is cli.cmd_record_landing


# --- disclosure at verify-final and resolve --------------------------------------------

def test_a_content_waiver_is_disclosed_at_verify_final_and_resolve(store, venued_fixtures_dir):
    ns = Namespace
    sid = "disclose"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=False, external_effect=False,
                        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / "plan_two_stage.toml")), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation), store=store)
    state = store.load(sid)
    state.stages[0].outcome.landed_content_waiver = "user accepted the merged line"
    store.save(state)

    final = cli.cmd_verify_final(ns(session=sid), store=store)
    assert final.ok, final.detail
    assert final.data["landed_content_waivers"] == [{"stage": 1, "reason": "user accepted the merged line"}]
    assert "content fact was waived by the user" in final.detail

    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched", note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    assert store.load(sid).node == Node.RESOLUTION.value
    resolved = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                                  quality_note=None), store=store)
    assert resolved.ok, resolved.detail
    assert resolved.data["landed_content_waivers"] == [{"stage": 1, "reason": "user accepted the merged line"}]
    assert "content fact was waived by the user" in resolved.detail
