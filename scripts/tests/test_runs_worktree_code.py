"""`grants.runs_worktree_code`: the stage's declaration that its executor runs code its worktree holds. It rides the
plan (parsed as a bool, a grant for the digests and the growth check), the approved entries, and the norm-handoff
record; unset it leaves every digest and record as it was."""
import pytest

import resource_grant_hook_support as support
from agentctl import norm_handoff
from agentctl.norm_handoff import StageNorm
from agentctl import plan as plan_mod
from agentctl.grants import StageGrants
from agentctl.plan import PlanError

PLAN = """[meta]
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
expected_result_image = "e"
criterion_type = "measurable"
done_criterion = "d"
depends_on = []
output_artifacts = ["mod.py"]
{grants}
"""


def plan_doc(tmp_path, grants="", name="plan"):
    path = tmp_path / f"{name}.toml"
    path.write_text(PLAN.format(venue=tmp_path, grants=grants))
    return plan_mod.load_plan(path, strict=False)


DECLARED = "[stage.grants]\nruns_worktree_code = true\n"
UNDECLARED = "[stage.grants]\nruns_worktree_code = false\n"


def test_the_flag_is_parsed_as_a_bool_and_defaults_to_false(tmp_path):
    assert plan_doc(tmp_path, DECLARED).stages[0].grants.runs_worktree_code is True
    assert not plan_doc(tmp_path, UNDECLARED).stages[0].grants
    assert not plan_doc(tmp_path).stages[0].grants
    with pytest.raises(PlanError, match="runs_worktree_code must be true or false"):
        plan_doc(tmp_path, '[stage.grants]\nruns_worktree_code = "yes"\n', name="bad")


def test_an_unset_flag_leaves_every_digest_as_it_was(tmp_path):
    bare, off = plan_doc(tmp_path), plan_doc(tmp_path, UNDECLARED, name="off")
    assert plan_mod.grants_sha256(bare) == plan_mod.grants_sha256(off)
    assert plan_mod.grants_place(bare.stages[0]) == plan_mod.grants_place(off.stages[0]) == ()
    assert plan_mod.entries_grants_sha256(plan_mod.materialized_grant_entries(bare)) == \
        plan_mod.entries_grants_sha256(plan_mod.materialized_grant_entries(off))
    assert "runs_worktree_code" not in plan_mod.materialized_grant_entries(off)["1"]


def test_a_declared_flag_moves_the_digests_the_entries_and_counts_as_growth(tmp_path):
    bare, declared = plan_doc(tmp_path), plan_doc(tmp_path, DECLARED, name="declared")
    assert plan_mod.grants_sha256(bare) != plan_mod.grants_sha256(declared)
    entries = plan_mod.materialized_grant_entries(declared)
    assert entries["1"]["runs_worktree_code"] is True
    assert plan_mod.entries_grants_sha256(entries) != plan_mod.entries_grants_sha256(plan_mod.materialized_grant_entries(bare))
    assert plan_mod._grants_grew(bare, declared)
    assert not plan_mod._grants_grew(declared, bare)
    assert plan_mod.diff_plans(bare, declared) == "substantive"
    assert plan_mod._effective_grants_for_stage(declared.stages[0], declared.meta.repo_root).runs_worktree_code


def test_a_stage_that_only_declares_the_flag_has_grants():
    assert not StageGrants(runs_worktree_code=True).is_empty()
    assert StageGrants().is_empty()


def test_the_record_carries_the_flag_only_when_true_and_verifies_it_back(tmp_path):
    norm = StageNorm.from_stage(support.stage_for("developer", "feat"))
    common = dict(parent_session="s", stage_index=1, kind="developer", norm=norm, grants=[], depth=1,
                  parent_lineage=None, resources=[], rule_patterns=[])
    off = norm_handoff.build_record(**common)
    on = norm_handoff.build_record(**common, runs_worktree_code=True)
    assert "runs_worktree_code" not in off and on["runs_worktree_code"] is True
    for record, expected in ((off, False), (on, True)):
        path, sha = norm_handoff.write_record(tmp_path / "state", record)
        adopted, why = norm_handoff.verify_env_value(norm_handoff.format_env_value(path, sha))
        assert why is None and adopted["runs_worktree_code"] is expected
