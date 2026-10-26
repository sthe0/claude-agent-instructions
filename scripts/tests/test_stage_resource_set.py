"""The typed resource set a spawned child may change (`plan_resources.stage_resource_set`),
and its two carriers: `agentctl stage-grants` data and the norm handoff record.

Resolved declared+derived rules, add_dirs with their mode, the stage's spawn/landed
resources and the acceptEdits kind baseline are typed resources; a wildcard-tail Bash rule
is kept as text in `rule_patterns` because its argv is unbounded.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli
from agentctl import plan_resources as PR
from agentctl import resources as R
from agentctl.norm_handoff import build_record, verify_env_value, write_record
from agentctl.plan import load_plan
from agentctl.stage_norm import StageNorm
from agentctl.state import CheckKind
from test_stage_grants import _to_executing, _write_plan_with_grants_block, ns

SPAWN_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

GRANTS_TOML = (
    "[stage.grants]\n"
    'allow = ["Bash(git status:*)", "Edit(//work/venue/mod.py)", "Read(//work/venue/notes.md)"]\n'
    "\n"
    "[[stage.grants.add_dirs]]\n"
    'path = "/work/scratch"\n'
    'mode = "read"\n'
)


def _stage_1(plan_path):
    return next(s for s in load_plan(str(plan_path)).stages if s.index == 1)


def _stage_with_grants(fixtures_dir, tmp_path):
    path = _write_plan_with_grants_block(fixtures_dir, tmp_path, GRANTS_TOML, "plan_resource_set.toml")
    return _stage_1(path), path


def _bare_stage():
    return _stage_1(FIXTURES / "plan_two_stage.toml")


def test_declared_edit_and_read_rules_become_file_resources(fixtures_dir, tmp_path):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir=None, kind="thinker")
    assert R.FileResource("/work/venue/mod.py", "write") in got.resources
    assert R.FileResource("/work/venue/notes.md", "read") in got.resources


def test_add_dir_keeps_its_mode(fixtures_dir, tmp_path):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir=None, kind="thinker")
    assert R.FileResource("/work/scratch", "read") in got.resources
    assert R.FileResource("/work/scratch", "write") not in got.resources


def test_wildcard_tail_rule_is_kept_as_pattern_not_as_resource(fixtures_dir, tmp_path):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir=None, kind="thinker")
    assert "Bash(git status:*)" in got.rule_patterns
    assert all(r.kind != "file" or "git status" not in r.path for r in got.resources)


def test_unresolved_non_wildcard_rule_is_in_neither_list():
    entries = [{"rule": "WebFetch(domain:code.claude.com)"}, {"rule": "not a rule"}]
    got = PR.resource_set_from_entries(entries, _bare_stage(), "/v", workdir=None, kind=None)
    assert got.rule_patterns == []
    assert not any(r.kind == "file" for r in got.resources)


def test_spawn_resource_of_the_stage_is_included(fixtures_dir, tmp_path):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir=None, kind="thinker")
    assert R.SpecialistResource("developer") in got.resources


@pytest.mark.parametrize("kind", ["developer", "tech-writer"])
def test_edit_capable_kinds_get_workdir_write_baseline(fixtures_dir, tmp_path, kind):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir="/work/wt", kind=kind)
    assert R.FileResource("/work/wt", "write") in got.resources


@pytest.mark.parametrize("kind", ["thinker", "planner", "code-reviewer", None])
def test_other_kinds_get_no_baseline(fixtures_dir, tmp_path, kind):
    stage, _ = _stage_with_grants(fixtures_dir, tmp_path)
    got = PR.stage_resource_set(stage, str(tmp_path), workdir="/work/wt", kind=kind)
    assert not any(r.kind == "file" and r.path == "/work/wt" for r in got.resources)


def test_baseline_needs_a_workdir():
    got = PR.resource_set_from_entries([], _bare_stage(), "/v", workdir=None, kind="developer")
    assert not any(r.kind == "file" for r in got.resources)


def test_duplicates_are_collapsed():
    entries = [{"path": "/w", "mode": "write"}, {"rule": "Edit(//w)"}, {"path": "/w", "mode": "write"}]
    got = PR.resource_set_from_entries(entries, _bare_stage(), "/v", workdir="/w", kind="developer")
    assert got.resources.count(R.FileResource("/w", "write")) == 1


def test_baseline_kinds_match_the_acceptedits_trusted_kinds():
    spec = importlib.util.spec_from_file_location("spawn_specialist_resource_set", SPAWN_SCRIPT)
    spawn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spawn)
    assert set(PR.KIND_RESOURCE_BASELINES) == set(spawn._ACCEPT_EDITS_TRUSTED_KINDS)


def test_landed_stage_contributes_its_vcs_ref():
    stage = _bare_stage()
    stage.criterion.verify_kind = CheckKind.LANDED.value
    stage.criterion.landed = SimpleNamespace(provider="git", remote="origin", target="main")
    got = PR.resource_set_from_entries([], stage, "/v", workdir=None, kind=None)
    assert R.VcsRefResource("origin", "main", "land") in got.resources


# --- resource_from_dict: the inverse of _resource_to_dict ----------------------------------

@pytest.mark.parametrize("resource", [
    R.FileResource("/a/b", "write"),
    R.FileResource("/a/b", "read"),
    R.VcsRefResource("origin", "main", "land"),
    R.SpecialistResource("developer"),
])
def test_resource_from_dict_round_trips(resource):
    assert PR.resource_from_dict(PR._resource_to_dict(resource)) == resource


def test_resource_from_dict_refuses_unknown_kind():
    with pytest.raises(ValueError, match="unknown resource kind"):
        PR.resource_from_dict({"kind": "teleporter"})


def test_resource_from_dict_refuses_foreign_fields():
    with pytest.raises(ValueError, match="malformed"):
        PR.resource_from_dict({"kind": "file", "path": "/x", "mode": "write", "bogus": 1})


# --- carriers -------------------------------------------------------------------------------

def test_stage_grants_data_carries_resources_and_rule_patterns(store, fixtures_dir, tmp_path):
    sid = "stage-resource-set-data"
    _, plan_path = _stage_with_grants(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert directive.ok
    assert "Bash(git status:*)" in directive.data["rule_patterns"]
    resources = directive.data["resources"]
    assert {"kind": "file", "path": "/work/venue/mod.py", "mode": "write"} in resources
    assert {"kind": "specialist", "role": "developer"} in resources
    assert all(PR.resource_from_dict(d) for d in resources)


def _norm():
    return StageNorm.from_stage(_bare_stage(), {})


def test_handoff_record_carries_both_keys_and_the_reader_returns_them(tmp_path):
    resources = [{"kind": "file", "path": "/w", "mode": "write"}]
    record = build_record(
        parent_session="p", stage_index=1, kind="developer", norm=_norm(), grants=[], depth=1,
        parent_lineage=None, resources=resources, rule_patterns=["Bash(git status:*)"])
    assert record["resources"] == resources
    assert record["rule_patterns"] == ["Bash(git status:*)"]
    path, sha = write_record(tmp_path, record)
    link, reason = verify_env_value(f"{path}:{sha}")
    assert reason is None
    assert link["resources"] == resources
    assert link["rule_patterns"] == ["Bash(git status:*)"]


def test_record_without_the_keys_reads_back_empty(tmp_path):
    record = build_record(
        parent_session="p", stage_index=1, kind="developer", norm=_norm(), grants=[], depth=1,
        parent_lineage=None)
    del record["resources"], record["rule_patterns"]
    path, sha = write_record(tmp_path, record)
    link, _ = verify_env_value(f"{path}:{sha}")
    assert link["resources"] == [] and link["rule_patterns"] == []
