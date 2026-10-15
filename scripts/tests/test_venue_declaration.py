"""A plan that declares no absolute venue gets no verdict from the process cwd.

`_venue_for` used to fall back to `Path.cwd()`, so the grants, the resolved resources and
the reachability / ephemeral-artifact checks of one plan differed with where the engine was
driven from. The fallback is gone: the venue is `delivery_worktree` / `repo_root` when one
is an absolute path, else None, and every consumer takes its own explicit no-venue path.
"""
from __future__ import annotations

import os

import pytest

from agentctl import plan as plan_mod
from agentctl import plan_resources
from agentctl.plan import _path_is_reachable, _venue_for, load_plan
from agentctl.submission import _ephemeral_artifacts_violations, submission_violations
from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER
from test_authoring_grant_check import _stage


# Resolved at call time: a tree without them must fail these tests, not fail to collect.
def venue_declaration_blockers(doc):
    return plan_mod.venue_declaration_blockers(doc)


def _derive_stage_grants(stage, venue):
    return plan_mod._derive_stage_grants(stage, venue)


def _plan(path, stages, *, repo_root=None, delivery_worktree=None, weight_class="substantive",
          final_check=True):
    meta = [
        "[meta]",
        'task_id = "demo"',
        'goal = "g"',
        'done_criterion = "dc"',
        'criterion_type = "measurable"',
        f'weight_class = "{weight_class}"',
        'external_research = "n/a"',
    ]
    if repo_root is not None:
        meta.append(f'repo_root = "{repo_root}"')
    if delivery_worktree is not None:
        meta.append(f'delivery_worktree = "{delivery_worktree}"')
    order = SUBSTANTIVE_ORDER if weight_class == "substantive" else ""
    final = SUBSTANTIVE_FINAL_CHECK if weight_class == "substantive" and final_check else ""
    path.write_text("\n".join(meta) + "\n" + order + "".join(stages) + final, encoding="utf-8")
    return load_plan(path)


def _relative_artifact_stage():
    return _stage(1, "Produce the report", output_artifacts=["out/report.json"],
                  material_refs=["src/a.py"])


@pytest.fixture
def abs_venue(tmp_path):
    v = tmp_path.resolve() / "venue"
    v.mkdir()
    return str(v)


def test_vd1_no_venue_declared_is_blocked_with_what_it_anchors(tmp_path):
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()])
    (message,) = venue_declaration_blockers(doc)
    assert "declares no absolute venue" in message
    assert "relative output_artifacts ('out/report.json')" in message
    assert "has a verify_command to run" in message
    assert "final_check 1" in message


def test_vd2_an_absolute_repo_root_or_delivery_worktree_declares_the_venue(tmp_path, abs_venue):
    by_root = _plan(tmp_path / "a.toml", [_relative_artifact_stage()], repo_root=abs_venue)
    by_worktree = _plan(tmp_path / "b.toml", [_relative_artifact_stage()],
                        delivery_worktree=abs_venue)
    assert venue_declaration_blockers(by_root) == []
    assert venue_declaration_blockers(by_worktree) == []
    assert _venue_for(by_root) == abs_venue
    assert _venue_for(by_worktree) == abs_venue


def test_vd3_a_relative_venue_is_as_undeclared_as_an_unset_one(tmp_path):
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()], repo_root=".")
    assert _venue_for(doc) is None
    (message,) = venue_declaration_blockers(doc)
    assert "repo_root = '.'" in message


def test_vd4_a_plan_with_nothing_to_anchor_declares_no_blocker(tmp_path):
    stage = _stage(1, "Think", output_artifacts=[f"{tmp_path}/abs.json"]).replace(
        'criterion_type = "measurable"', 'criterion_type = "acceptance_review"')
    doc = _plan(tmp_path / "p.toml", [stage], weight_class="small_change")
    assert venue_declaration_blockers(doc) == []


def test_vd5_venue_is_never_the_process_cwd(tmp_path, monkeypatch):
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()])
    monkeypatch.chdir(tmp_path)
    assert _venue_for(doc) is None
    assert os.getcwd() == str(tmp_path.resolve())


def test_vd6_seam_raises_it_on_the_session_class_whatever_the_plan_declares(tmp_path):
    light = _plan(tmp_path / "light.toml", [_relative_artifact_stage()],
                  weight_class="small_change")
    heavy = _plan(tmp_path / "heavy.toml", [_relative_artifact_stage()])
    (light_blocker,) = venue_declaration_blockers(light)
    (heavy_blocker,) = venue_declaration_blockers(heavy)
    assert light_blocker in submission_violations(light, session_weight_class="substantive")
    assert heavy_blocker in submission_violations(heavy, session_weight_class="substantive ")
    assert heavy_blocker not in submission_violations(heavy, session_weight_class="small_change")
    assert heavy_blocker not in submission_violations(heavy)


def test_vd7_no_venue_rule_is_unresolved_with_its_own_identity():
    res = plan_resources.resolve_rule_grant("Bash(pytest -q)", None)
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"
    assert res.identity == ("no-venue", "pytest -q")
    other = plan_resources.resolve_rule_grant("Bash(pytest -q tests)", None)
    assert other.identity != res.identity


def test_vd7b_wildcard_tail_verdict_does_not_depend_on_the_venue(abs_venue):
    rule = "Bash(python3 scripts/foo.py:*)"
    bare = plan_resources.resolve_rule_grant(rule, None)
    declared = plan_resources.resolve_rule_grant(rule, abs_venue)
    assert (bare.reason_class, bare.identity) == (declared.reason_class, declared.identity)
    assert bare.reason_class == "wildcard-tail"


def test_vd8_boundary_view_without_a_venue_names_every_engine_run_text_unresolved(tmp_path):
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()])
    view = plan_resources.compute_boundary_view(doc, "x" * 64)
    origins = {u["origin"]: tuple(u["identity"]) for u in view.unresolved}
    assert origins["verify_command"] == ("no-venue", "pytest -q")
    assert origins["negative_control"] == ("no-venue", "false")
    assert origins["final_check"][0] == "no-venue"


def test_vd9_derivation_without_a_venue_drops_in_venue_entries_and_keeps_the_rest(
        tmp_path, abs_venue):
    outside = f"{tmp_path.resolve()}/outside/report.json"
    stage_text = _stage(1, "Produce", output_artifacts=["out/tool.py", outside],
                        material_refs=["src/a.py"])
    doc = _plan(tmp_path / "p.toml", [stage_text], weight_class="small_change")
    stage = doc.stages[0]
    bare, _ = _derive_stage_grants(stage, None)
    declared, _ = _derive_stage_grants(stage, abs_venue)

    def by_provenance(grants):
        return {r.provenance for r in grants.allow} | {a.provenance for a in grants.add_dirs}

    assert {"derived:DR-O", "derived:DR-E"} <= by_provenance(declared)
    assert by_provenance(bare) == {"derived:DR-V", "derived:DR-R"}
    assert {r.rule for r in bare.allow} == {
        r.rule for r in declared.allow if r.provenance == "derived:DR-V"}
    assert bare.add_dirs == declared.add_dirs


def test_vd10_derivation_without_a_venue_is_independent_of_the_cwd(tmp_path, monkeypatch):
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()], weight_class="small_change")
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    monkeypatch.chdir(here)
    a = _derive_stage_grants(doc.stages[0], _venue_for(doc))
    monkeypatch.chdir(there)
    b = _derive_stage_grants(doc.stages[0], _venue_for(doc))
    assert a == b


def test_vd11_reachability_reads_the_declared_venue_not_the_cwd(tmp_path, monkeypatch):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _path_is_reachable("marker.txt", [], None) is False
    assert _path_is_reachable("marker.txt", [], ".") is False
    assert _path_is_reachable("marker.txt", [], str(tmp_path)) is True
    assert _path_is_reachable("marker.txt", ["marker.txt"], None) is True


def test_vd12_ephemeral_check_joins_a_relative_artifact_to_the_declared_venue(
        tmp_path, monkeypatch):
    scratch = tmp_path.resolve() / "scratch"
    durable = tmp_path.resolve() / "durable"
    scratch.mkdir()
    durable.mkdir()
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(scratch))
    doc = _plan(tmp_path / "p.toml", [_relative_artifact_stage()], repo_root=str(durable),
                weight_class="small_change")
    stage = doc.stages[0]
    monkeypatch.chdir(scratch)
    assert _ephemeral_artifacts_violations(stage, str(durable)) == []
    assert _ephemeral_artifacts_violations(stage, None) == []
    monkeypatch.chdir(durable)
    assert _ephemeral_artifacts_violations(stage, str(scratch)) != []
