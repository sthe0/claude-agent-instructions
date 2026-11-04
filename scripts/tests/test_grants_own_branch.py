"""DR-P: the plan-declared branch yields exact commit/push VcsRef resources (P1).

Ownership is positive (only the declared branch is derived), trunk/shared names and the
remote's HEAD branch are refused into `dropped`, and the same resources reach the set the
child's handoff carries — frozen at approve, so a spawn serves what the approval derived.
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


def _derive(stage, *, venue, remote_head=grants.LIVE_REMOTE_HEAD):
    """DR-P's (resources, dropped) as `derive_stage_grants` reports them: the resources slot
    of the one grants object and the DR-P share of its dropped list."""
    derived, dropped = grants.derive_stage_grants(stage, venue=venue, remote_head=remote_head)
    return derived.resources, [d for d in dropped if d.get("rule") == "DR-P"]


@pytest.fixture
def remote_head(monkeypatch):
    def set_head(name):
        monkeypatch.setattr(grants, "remote_head_branch", lambda venue: name)
    set_head("main")
    return set_head


def test_own_branch_derives_exact_commit_and_push(remote_head):
    resources, dropped = _derive(_stage(), venue="/v")
    assert resources == [COMMIT, PUSH] and dropped == []


@pytest.mark.parametrize("branch", [
    "main", "master", "trunk", "release-1.2", "release/1.2", "Main", "TRUNK"])
def test_trunk_and_release_branches_are_refused(remote_head, branch):
    resources, dropped = _derive(_stage(branch=branch), venue="/v")
    assert resources == [] and "trunk or shared" in dropped[0]["reason"]


def test_remote_head_branch_is_refused(remote_head):
    remote_head("stable")
    resources, dropped = _derive(_stage(branch="stable"), venue="/v")
    assert resources == [] and len(dropped) == 1
    assert "trunk or shared" in dropped[0]["reason"]


def test_remote_head_comparison_ignores_case(remote_head):
    remote_head("Stable")
    resources, dropped = _derive(_stage(branch="stable"), venue="/v")
    assert resources == [] and "trunk or shared" in dropped[0]["reason"]


def test_frozen_remote_head_overrides_the_live_one(remote_head):
    remote_head("main")
    resources, dropped = _derive(_stage(), venue="/v", remote_head=OWN)
    assert resources == [] and "trunk or shared" in dropped[0]["reason"]
    resources, _ = _derive(_stage(), venue="/v", remote_head="stable")
    assert resources == [COMMIT, PUSH]


def test_dropped_entries_carry_the_dr_p_tag(remote_head):
    for branch in (None, "x..y", "main"):
        _, dropped = _derive(_stage(branch=branch), venue="/v")
        assert [d["rule"] for d in dropped] == ["DR-P"]


def test_unset_remote_head_refuses_the_push_but_keeps_the_local_commit(remote_head):
    remote_head(None)
    resources, dropped = _derive(_stage(), venue="/v")
    assert resources == [COMMIT]
    assert "unset or unreadable" in dropped[0]["reason"]


def test_no_declared_branch_derives_nothing_and_reports(remote_head):
    resources, dropped = _derive(_stage(branch=None), venue="/v")
    assert resources == [] and "declares no `branch`" in dropped[0]["reason"]


@pytest.mark.parametrize("branch", [
    "-x", "a b", "a:b", "+a", "a..b", "a*", "HEAD", "head", "refs/heads/main", "heads/main",
    "origin/main", "remotes/origin/main", "./main", "a/", "x.lock", "a//b"])
def test_malformed_and_ref_form_branch_names_are_refused(remote_head, branch):
    resources, dropped = _derive(_stage(branch=branch), venue="/v")
    assert resources == [] and "plain branch name" in dropped[0]["reason"]


def test_tech_writer_commits_but_never_pushes(remote_head):
    resources, _ = _derive(_stage("spawn:tech-writer"), venue="/v")
    assert resources == [COMMIT]


@pytest.mark.parametrize("executor", ["spawn:thinker", "in_thread", "spawn:planner"])
def test_other_kinds_derive_nothing_and_report_nothing(remote_head, executor):
    assert _derive(_stage(executor), venue="/v") == ([], [])


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


def _git_repo(path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "c")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return git


@pytest.mark.parametrize("branch", ["tags/v1", "notes/x"])
def test_tag_and_note_namespaces_are_not_branch_names(remote_head, branch):
    resources, dropped = _derive(_stage(branch=branch), venue="/v")
    assert resources == [] and "plain branch name" in dropped[0]["reason"]


def test_a_name_git_resolves_to_a_tag_is_refused_in_a_repository(tmp_path, remote_head):
    git = _git_repo(tmp_path)
    git("tag", "v1")
    git("branch", "tagish/v1")
    assert grants._resolves_outside_heads("v1", str(tmp_path))
    assert not grants._resolves_outside_heads("tagish/v1", str(tmp_path))
    assert not grants._resolves_outside_heads("not-there", str(tmp_path))
    assert not grants._resolves_outside_heads("v1", None)
    resources, dropped = _derive(_stage(branch="v1"), venue=str(tmp_path))
    assert resources == [] and "plain branch name" in dropped[0]["reason"]


_PLAN = """[meta]
repo_root = "{venue}"
weight_class = "small_change"
task_id = "t"
goal = "g"
done_criterion = "d"
criterion_type = "measurable"

[[stage]]
index = 1
title = "S"
executor = "spawn:developer"
expected_result_image = "{image}"
criterion_type = "measurable"
done_criterion = "d"
depends_on = []
output_artifacts = ["mod.py"]
{branch}
"""


def _plan_doc(tmp_path, branch, image="e"):
    from agentctl import plan as plan_mod

    path = tmp_path / f"plan-{branch or 'none'}-{image}.toml"
    path.write_text(_PLAN.format(
        venue=tmp_path, image=image, branch=f'branch = "{branch}"' if branch else ""))
    return plan_mod.load_plan(path, strict=False)


def test_moving_the_declared_branch_is_grant_growth(tmp_path):
    from agentctl import plan as plan_mod

    _git_repo(tmp_path)
    feat_a, feat_b = _plan_doc(tmp_path, "feat-a"), _plan_doc(tmp_path, "feat-b")
    assert plan_mod._grants_grew(feat_a, feat_b)
    assert plan_mod.diff_plans(feat_a, feat_b) == "substantive"
    assert not plan_mod._grants_grew(feat_a, feat_a)
    assert not plan_mod._grants_grew(feat_a, _plan_doc(tmp_path, None))


def test_branch_resources_are_under_the_hash_and_only_when_present(tmp_path):
    from agentctl import plan as plan_mod

    _git_repo(tmp_path)
    own, none = _plan_doc(tmp_path, "feat-a"), _plan_doc(tmp_path, None)
    for doc in (own, none):
        entries = plan_mod.materialized_grant_entries(doc)
        assert plan_mod.entries_grants_sha256(entries) == plan_mod.grants_sha256(doc)
    assert plan_mod.grants_sha256(own) != plan_mod.grants_sha256(_plan_doc(tmp_path, "feat-b"))
    entries = plan_mod.materialized_grant_entries(none)
    bare = plan_mod.entries_grants_sha256(
        {k: {**v, "branch": {"remote_head": "main", "resources": []}} for k, v in entries.items()})
    assert bare == plan_mod.entries_grants_sha256(entries)
    tampered = plan_mod.materialized_grant_entries(own)
    tampered["1"]["branch"]["resources"].append(R.resource_to_dict(R.VcsRefResource("origin", "x", "push")))
    assert plan_mod.entries_grants_sha256(tampered) != plan_mod.grants_sha256(own)


def test_refinement_keeps_only_admitted_branch_resources(tmp_path):
    from agentctl import plan as plan_mod

    _git_repo(tmp_path)
    feat_a, feat_b = _plan_doc(tmp_path, "feat-a"), _plan_doc(tmp_path, "feat-b")
    stored = plan_mod.materialized_grant_entries(feat_a)
    moved, _ = plan_mod.refined_grant_entries(stored, feat_a, feat_b)
    assert moved["1"]["branch"]["resources"] == []
    unmoved, _ = plan_mod.refined_grant_entries(stored, feat_a, feat_a)
    assert unmoved["1"]["branch"] == stored["1"]["branch"]
    unreadable, _ = plan_mod.refined_grant_entries(stored, None, feat_b)
    assert unreadable["1"]["branch"]["resources"] == []
    dropped, _ = plan_mod.refined_grant_entries(stored, feat_a, _plan_doc(tmp_path, None))
    assert dropped["1"]["branch"]["resources"] == []


def test_refinement_never_regrants_a_push_the_approval_refused(tmp_path):
    from agentctl import plan as plan_mod

    git = _git_repo(tmp_path)
    git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    old = _plan_doc(tmp_path, "feat-a")
    stored = plan_mod.materialized_grant_entries(old)
    assert [d["op"] for d in stored["1"]["branch"]["resources"]] == ["commit"]

    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    new = _plan_doc(tmp_path, "feat-a", image="a tighter image")
    assert plan_mod.diff_plans(old, new) == "refinement"
    assert "push" in [d["op"] for d in plan_mod.materialized_grant_entries(new)["1"]["branch"]["resources"]]
    refined, withheld = plan_mod.refined_grant_entries(stored, old, new)
    assert [d["op"] for d in refined["1"]["branch"]["resources"]] == ["commit"]
    assert refined["1"]["branch"]["remote_head"] == stored["1"]["branch"]["remote_head"]
    assert withheld == {"1": ["push origin:feat-a (DR-P)"]}


def test_grants_renderings_list_the_branch_resources(tmp_path):
    from agentctl import plan as plan_mod
    from agentctl import render

    _git_repo(tmp_path)
    doc = _plan_doc(tmp_path, "feat-a")
    assert plan_mod.plan_branch_resources(doc) == {
        1: [R.resource_to_dict(R.VcsRefResource(".", "feat-a", "commit")),
            R.resource_to_dict(R.VcsRefResource("origin", "feat-a", "push"))]}
    for fmt in ("compact", "full"):
        text = render.render_plan_grants(doc, fmt=fmt)
        assert "commit .:feat-a" in text and "push origin:feat-a" in text
    assert "DR-P" not in render.render_plan_grants(_plan_doc(tmp_path, None))


def test_dropped_report_carries_the_dr_p_tag(tmp_path):
    from agentctl import plan as plan_mod

    _git_repo(tmp_path)
    report = plan_mod.dropped_grants_report(_plan_doc(tmp_path, "main"))
    assert [d["rule"] for d in report] == ["DR-P"]
