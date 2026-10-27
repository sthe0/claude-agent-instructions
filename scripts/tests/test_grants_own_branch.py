"""DR-P: the plan-declared branch yields exact commit/push VcsRef resources (P1).

Ownership is positive (only the declared branch is derived), trunk/shared names and the
remote's HEAD branch are refused into `dropped`, and the same resources reach the set the
child's handoff carries — computed from the plan alone, so approve time equals spawn time.
"""
from __future__ import annotations

import pytest

from agentctl import grants
from agentctl import plan_resources as PR
from agentctl import resources as R
from agentctl.norm_handoff import build_record
from agentctl.stage_norm import StageNorm
from agentctl.state import Actor, Criterion, Means, Stage, Subject

OWN = "resource-grants-executor-request"
COMMIT = R.VcsRefResource(".", OWN, "commit")
PUSH = R.VcsRefResource("origin", OWN, "push")


def _stage(executor="spawn:developer", branch=OWN):
    return Stage(
        index=1, title="t", subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"), actor=Actor(executor=executor),
        criterion=Criterion(criterion_type="measurable", done_criterion="d"), branch=branch,
    )


@pytest.fixture
def remote_head(monkeypatch):
    def set_head(name):
        monkeypatch.setattr(grants, "remote_head_branch", lambda venue: name)
    set_head("main")
    return set_head


def test_own_branch_derives_exact_commit_and_push(remote_head):
    resources, dropped = grants.derive_branch_resources(_stage(), venue="/v")
    assert resources == [COMMIT, PUSH] and dropped == []


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "release-1.2"])
def test_trunk_and_release_branches_are_refused(remote_head, branch):
    resources, dropped = grants.derive_branch_resources(_stage(branch=branch), venue="/v")
    assert resources == [] and "trunk or shared" in dropped[0]["reason"]


def test_remote_head_branch_is_refused(remote_head):
    remote_head("stable")
    resources, dropped = grants.derive_branch_resources(_stage(branch="stable"), venue="/v")
    assert resources == [] and len(dropped) == 1


def test_unset_remote_head_refuses_the_push_but_keeps_the_local_commit(remote_head):
    remote_head(None)
    resources, dropped = grants.derive_branch_resources(_stage(), venue="/v")
    assert resources == [COMMIT]
    assert "unset or unreadable" in dropped[0]["reason"]


def test_no_declared_branch_derives_nothing_and_reports(remote_head):
    resources, dropped = grants.derive_branch_resources(_stage(branch=None), venue="/v")
    assert resources == [] and "declares no `branch`" in dropped[0]["reason"]


@pytest.mark.parametrize("branch", ["-x", "a b", "a:b", "+a", "a..b", "a*"])
def test_malformed_branch_names_are_refused(remote_head, branch):
    resources, dropped = grants.derive_branch_resources(_stage(branch=branch), venue="/v")
    assert resources == [] and "plain branch name" in dropped[0]["reason"]


def test_tech_writer_commits_but_never_pushes(remote_head):
    resources, _ = grants.derive_branch_resources(_stage("spawn:tech-writer"), venue="/v")
    assert resources == [COMMIT]


@pytest.mark.parametrize("executor", ["spawn:thinker", "in_thread", "spawn:planner"])
def test_other_kinds_derive_nothing_and_report_nothing(remote_head, executor):
    assert grants.derive_branch_resources(_stage(executor), venue="/v") == ([], [])


def test_derive_stage_grants_surfaces_the_refusal(remote_head):
    _g, dropped = grants.derive_stage_grants(_stage(branch="main"), venue="/v")
    assert any("trunk or shared" in d["reason"] for d in dropped)


def test_resources_reach_the_resource_set_and_the_handoff(remote_head):
    stage = _stage()
    got = PR.resource_set_from_entries([], stage, "/v")
    assert COMMIT in got.resources and PUSH in got.resources
    record = build_record(
        parent_session="p", stage_index=1, kind="developer", norm=StageNorm.from_stage(stage, {}),
        grants=[], depth=1, parent_lineage=None, **{
            "resources": got.to_dict()["resources"], "rule_patterns": got.rule_patterns})
    assert R.resource_to_dict(PUSH) in record["resources"]
    assert R.resource_to_dict(COMMIT) in record["resources"]


def test_approve_time_and_spawn_time_sets_are_equal(remote_head):
    stage = _stage()
    at_approve = PR.compute_stage_resources(stage, "/v").resources
    at_spawn = PR.resource_set_from_entries([], stage, "/v").resources
    assert {r for r in at_approve if r.kind == "vcs_ref"} == {r for r in at_spawn if r.kind == "vcs_ref"}


def test_commit_is_exact_and_distinct_from_push_and_land():
    assert COMMIT.covers(R.VcsRefResource(".", OWN, "commit"))
    assert not COMMIT.covers(R.VcsRefResource(".", OWN, "push"))
    assert not COMMIT.covers(R.VcsRefResource(".", "other", "commit"))
    assert not PUSH.covers(R.VcsRefResource("origin", "main", "push"))


def test_remote_head_branch_reads_the_symbolic_ref(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    git("init", "-q", "-b", "trunkish")
    assert grants.remote_head_branch(str(tmp_path)) is None
    git("commit", "-q", "--allow-empty", "-m", "c")
    git("update-ref", "refs/remotes/origin/trunkish", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunkish")
    assert grants.remote_head_branch(str(tmp_path)) == "trunkish"
    assert grants.remote_head_branch(None) is None
