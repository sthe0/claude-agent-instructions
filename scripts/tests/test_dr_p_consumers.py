"""DR-P's commit/push resources live in one slot, `StageGrants.resources`, filled by
`grants.derive_stage_grants`; every consumer of a stage's effective grants reads them from
there. These tests pin that no consumer has its own derivation, that each one sees the
slot, and that the autonomy boundary and the handoff coverage count the resources."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, plan as plan_mod, plan_resources, render, resources as R
from test_grants_own_branch import _git_repo, _plan_doc
from test_replan_autonomy_boundary import Eng, action, autonomy, plan_text
from test_norm_loop import SPAWN

SCRIPTS = Path(__file__).resolve().parent.parent
COMMIT = R.VcsRefResource(".", "feat-a", "commit")
PUSH = R.VcsRefResource("origin", "feat-a", "push")


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


def _branch_plan(branch: str, *, revised: bool = False) -> str:
    return plan_text(revised=revised).replace(
        "depends_on = []\n", f'depends_on = []\nbranch = "{branch}"\n', 1)


def _py_sources() -> list[Path]:
    return [p for root in (SCRIPTS, SCRIPTS / "agentctl", SCRIPTS / "lib")
            for p in root.glob("*.py")]


def test_no_module_outside_grants_names_the_branch_derivation():
    offenders = []
    for path in _py_sources():
        if path.name == "grants.py":
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names = {getattr(node, field, None) for field in ("id", "attr", "name")}
            if any(isinstance(n, str) and "derive_branch_resources" in n for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def _slot(doc):
    return plan_mod._effective_by_stage(doc, venue=doc.meta.repo_root)[1].effective_resources()


def _consumers():
    def effective_for_stage(doc):
        stage = doc.stages[0]
        return plan_mod._effective_grants_for_stage(stage, doc.meta.repo_root).effective_resources()

    def branch_resources(doc):
        return {R.resource_from_dict(d) for d in plan_mod.plan_branch_resources(doc).get(1, [])}

    def materialized(doc):
        entries = plan_mod.materialized_grant_entries(doc)
        return {R.resource_from_dict(d) for d in entries["1"]["branch"]["resources"]}

    def boundary(doc):
        view = plan_resources.compute_boundary_view(doc, "o", venue=doc.meta.repo_root)
        return {r for r in view.resources if isinstance(r, R.VcsRefResource)}

    def plan_wide(doc):
        got = plan_resources.compute_plan_resources(doc, venue=doc.meta.repo_root)
        return {r for r in got[0].resources if isinstance(r, R.VcsRefResource)}

    def resource_set(doc):
        got = plan_resources.resource_set_from_entries([], doc.stages[0], doc.meta.repo_root)
        return {r for r in got.resources if isinstance(r, R.VcsRefResource)}

    return {
        "effective_grants_for_stage": effective_for_stage,
        "effective_by_stage": _slot,
        "plan_branch_resources": branch_resources,
        "materialized_grant_entries": materialized,
        "compute_boundary_view": boundary,
        "compute_plan_resources": plan_wide,
        "resource_set_from_entries": resource_set,
    }


@pytest.mark.parametrize("name", sorted(_consumers()))
def test_every_consumer_sees_the_resources_slot(tmp_path, name):
    _git_repo(tmp_path)
    consumer = _consumers()[name]
    assert {PUSH, COMMIT} <= set(consumer(_plan_doc(tmp_path, "feat-a")))
    assert not [r for r in consumer(_plan_doc(tmp_path, None)) if r.op in ("commit", "push")]


def test_the_grants_hash_the_growth_check_and_the_rendering_see_the_slot(tmp_path):
    _git_repo(tmp_path)
    own, other, none = (_plan_doc(tmp_path, b) for b in ("feat-a", "feat-b", None))
    assert plan_mod.grants_sha256(own) != plan_mod.grants_sha256(none)
    assert plan_mod._grants_grew(none, own) and plan_mod._grants_grew(own, other)
    assert plan_mod.plan_has_any_grants(own)
    assert "push origin:feat-a" in render.render_plan_grants(own)


def test_grants_lines_show_the_own_branch_and_the_refusal(tmp_path):
    _git_repo(tmp_path)
    own = _plan_doc(tmp_path, "feat-a")
    assert "  - own branch (DR-P): commit .:feat-a, push origin:feat-a" in render._grants_lines(
        own.stages[0], str(tmp_path))
    refused = render._grants_lines(_plan_doc(tmp_path, "main").stages[0], str(tmp_path))
    assert any(line.startswith("  - own branch (DR-P) refused:") and "trunk or shared" in line
               for line in refused)
    assert "DR-P" not in "".join(render._grants_lines(_plan_doc(tmp_path, None).stages[0], str(tmp_path)))


def test_a_replan_to_another_branch_goes_to_the_user(eng):
    _git_repo(eng.tmp)
    plan = eng.write(_branch_plan("feat-a"), "approved.toml")
    eng.approve_as_user("u1", plan)
    eng.open("r1", plan)
    eng.run("approve", session="r1", by="agent")
    eng.fail_stage("r1")
    eng.diagnose("r1", difference=False)
    d = eng.run("replan", session="r1", plan=eng.write(_branch_plan("feat-b", revised=True)))
    assert action(d) == "await_user_approval", d
    assert any("feat-b" in json.dumps(r) and "push" in json.dumps(r)
               for r in autonomy(d)["extra_resources"])


def test_a_replan_keeping_the_branch_needs_no_user_approval(eng):
    _git_repo(eng.tmp)
    plan = eng.write(_branch_plan("feat-a"), "approved.toml")
    eng.approve_as_user("u1", plan)
    eng.open("r1", plan)
    eng.run("approve", session="r1", by="agent")
    eng.fail_stage("r1")
    eng.diagnose("r1", difference=False)
    d = eng.run("replan", session="r1", plan=eng.write(_branch_plan("feat-a", revised=True)))
    assert action(d) != "await_user_approval" and "refinement applied" in d["detail"], d


def _linked(tmp_path, resources):
    doc = _plan_doc(tmp_path, "feat-a")
    entries = plan_mod.materialized_grant_entries(doc)
    state = SimpleNamespace(
        parent_link={"grants": [], "resources": [R.resource_to_dict(r) for r in resources],
                     "sha256": "h"},
        runtime_grants={}, repo_root=str(tmp_path), delivery_worktree=None, session_id="s",
        approved_grant_entries=entries,
        approved_grants_sha256=plan_mod.entries_grants_sha256(entries),
        stage=lambda i: doc.stages[0],
        resolve_check_venue=lambda _kind: str(tmp_path))
    return state, doc


def test_a_child_is_refused_the_branch_push_its_parent_does_not_hold(tmp_path):
    _git_repo(tmp_path)
    uncovered = SPAWN.uncovered_grants(_linked(tmp_path, [COMMIT]), 1, [])
    assert [e["resource"] for e in uncovered] == [R.resource_to_dict(PUSH)]
    refusal = SPAWN.grant_widening_refusal(_linked(tmp_path, [COMMIT]), 1, "developer", uncovered)
    assert "Resource: vcs_ref push origin:feat-a" in refusal


def test_a_child_whose_parent_holds_the_branch_resources_is_covered(tmp_path):
    _git_repo(tmp_path)
    assert SPAWN.uncovered_grants(_linked(tmp_path, [COMMIT, PUSH]), 1, []) == []


def test_the_handoff_held_set_counts_the_parents_vcs_resources(tmp_path):
    held, unresolved = cli._handoff_held(_linked(tmp_path, [COMMIT, PUSH])[0].parent_link, str(tmp_path))
    assert {COMMIT, PUSH} <= set(held) and unresolved == []
    assert cli._handoff_held({"grants": []}, str(tmp_path)) == ([], [])


def test_a_non_string_branch_is_refused_on_a_strict_load(tmp_path):
    path = tmp_path / "plan.toml"
    path.write_text(plan_text().replace("depends_on = []\n", "depends_on = []\nbranch = 123\n", 1))
    with pytest.raises(plan_mod.PlanError, match="branch must be a string"):
        plan_mod.load_plan(path, strict=True)
    assert plan_mod.load_plan(path, strict=False).stages[0].branch is None


def test_approved_session_serves_its_branch_resources_not_none(eng):
    _git_repo(eng.tmp)
    eng.approve_as_user("u1", eng.write(_branch_plan("feat-a"), "approved.toml"))
    state = eng.state("u1")
    assert set(cli.stage_branch_resources(state, 1)) == {COMMIT, PUSH}
    live = plan_resources.compute_stage_resources(state.stage(1), str(eng.tmp)).resources
    assert {r for r in live if isinstance(r, R.VcsRefResource)} >= {COMMIT, PUSH}


def test_an_unbound_or_malformed_state_yields_no_branch_resources(eng):
    _git_repo(eng.tmp)
    eng.approve_as_user("u1", eng.write(_branch_plan("feat-a"), "approved.toml"))
    state = eng.state("u1")
    assert set(cli.stage_branch_resources(state, 1)) == {COMMIT, PUSH}
    stored = state.approved_grant_entries
    state.approved_grant_entries = None
    assert cli.stage_branch_resources(state, 1) == []
    state.approved_grant_entries = {k: {**v, "branch": None} for k, v in stored.items()}
    assert cli.stage_branch_resources(state, 1) == []
    state.approved_grant_entries = stored
    state.approved_grants_sha256 = "0" * 64
    assert cli.stage_branch_resources(state, 1) == []


def test_the_runtime_merges_and_the_self_grant_check_count_the_resources(eng):
    _git_repo(eng.tmp)
    eng.approve_as_user("u1", eng.write(_branch_plan("feat-a"), "approved.toml"))
    state = eng.state("u1")
    for merged in (cli._effective_stage_grants, cli._effective_stage_grants_for_self_grant):
        assert set(merged(state, 1).resources) == {COMMIT, PUSH}
    covered = cli._self_grant_resources_for_rule(
        state, cli._effective_stage_grants_for_self_grant(state, 1),
        "Bash(git push origin feat-a)")
    assert covered[1] is None, covered


@pytest.mark.parametrize("customer", [True, False])
def test_a_plan_approved_own_branch_push_is_not_a_question_to_the_user(eng, customer):
    _git_repo(eng.tmp)
    text = _branch_plan("feat-a")
    if not customer:
        assert 'customer_id = "user"' in text
        text = text.replace('customer_id = "user"', 'customer_id = ""')
    eng.approve_as_user("u1", eng.write(text, "approved.toml"))
    state = eng.state("u1")
    coverage = cli._effective_stage_grants_for_self_grant(state, 1)
    resources, reason = cli._self_grant_resources_for_rule(
        state, coverage, "Bash(git push origin feat-a)")
    assert reason is None, (resources, reason)
    resources, reason = cli._self_grant_resources_for_rule(
        state, coverage, "Bash(git push origin main)")
    assert reason == "not-approved", reason


def test_every_stage_grants_construction_outside_grants_passes_resources():
    offenders = []
    for path in _py_sources():
        if path.name == "grants.py":
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "StageGrants" and node.keywords and not any(
                    k.arg == "resources" for k in node.keywords) and path.name in (
                    "cli.py", "spawn-specialist.py"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_resolve_permission_materializes_a_resource_only_refusal(eng):
    _git_repo(eng.tmp)
    eng.approve_as_user("u1", eng.write(_branch_plan("feat-a"), "approved.toml"))
    eng.execute("u1")
    state = eng.state("u1")
    refusal = "\n".join([
        "PERMISSION-REQUEST:", "Refusal: grant-widening",
        "Resource: vcs_ref push origin:feat-a"])
    state.permission_request = cli.PermissionRequest(action="a", stage_index=1, raw=refusal)
    eng.store.save(state)
    d = eng.run("resolve_permission", session="u1", decision="granted", scope="stage", by="user")
    assert d["ok"], d
    added = d["data"]["runtime_grants_added"]
    assert [e["resource"] for e in added] == [R.resource_to_dict(PUSH)]
    assert eng.state("u1").runtime_grants["1"][0]["granted_by"] == "user"
    state = eng.state("u1")
    state.permission_request = cli.PermissionRequest(action="a", stage_index=1, raw=refusal)
    eng.store.save(state)
    d = eng.run("resolve_permission", session="u1", decision="granted", scope="stage", by="agent")
    assert not d["data"]["runtime_grants_added"] if d.get("data") else True


def test_a_refusal_with_only_resource_lines_is_resolvable_end_to_end(tmp_path):
    _git_repo(tmp_path)
    linked = _linked(tmp_path, [COMMIT])
    state, _doc = linked
    uncovered = SPAWN.uncovered_grants(linked, 1, [])
    refusal = SPAWN.grant_widening_refusal(linked, 1, "developer", uncovered)
    assert cli._parse_rule_lines(refusal) == []
    parsed = cli._refusal_vcs_resources(refusal)
    assert parsed == [PUSH]
    state.runtime_grants = {"1": [{
        "resource": R.resource_to_dict(r), "provenance": "runtime", "consumed": False,
        "scope": "stage", "granted_by": "user"} for r in parsed]}
    assert SPAWN.uncovered_grants(linked, 1, []) == []
    state.runtime_grants["1"][0]["granted_by"] = "agent"
    assert SPAWN.uncovered_grants(linked, 1, []) != []
