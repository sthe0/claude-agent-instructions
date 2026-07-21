import pytest

from agentctl.plan import PlanError, diff_plans, final_check_venue_warnings, load_plan, parse_plan
from agentctl.state import FinalCheck


def test_load_two_stage_plan(fixtures_dir):
    doc = load_plan(fixtures_dir / "plan_two_stage.toml")
    assert doc.meta.task_id == "demo-two-stage"
    assert [s.index for s in doc.stages] == [1, 2]
    assert doc.stages[0].is_spawn()
    assert doc.stages[0].spawn_kind() == "developer"
    assert doc.stages[1].depends_on == [1]


def test_missing_meta_raises():
    with pytest.raises(PlanError):
        parse_plan({"stage": [{"title": "x"}]})


def test_no_stages_raises():
    with pytest.raises(PlanError):
        parse_plan({"meta": {"task_id": "t"}})


def test_missing_required_stage_field_raises():
    with pytest.raises(PlanError):
        parse_plan({"meta": {"task_id": "t"}, "stage": [{"index": 1, "title": "x"}]})


def test_duplicate_indices_raise():
    data = {
        "meta": {"task_id": "t"},
        "stage": [
            {"index": 1, "title": "a", "executor": "in_thread",
             "expected_result_image": "i", "done_criterion": "c"},
            {"index": 1, "title": "b", "executor": "in_thread",
             "expected_result_image": "i", "done_criterion": "c"},
        ],
    }
    with pytest.raises(PlanError):
        parse_plan(data)


def test_diff_no_change(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage.toml")
    assert diff_plans(a, b) == "no_change"


def test_diff_refinement(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage_refined.toml")
    assert diff_plans(a, b) == "refinement"


def test_diff_substantive(fixtures_dir):
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    b = load_plan(fixtures_dir / "plan_two_stage_substantive.toml")
    assert diff_plans(a, b) == "substantive"


def test_diff_means_only_change_is_refinement(fixtures_dir):
    """A stage whose means/method change but whose structural signature
    (executor/depends_on/done_criterion/...) is unchanged is a refinement — the
    overcome-difficulty replan adjusts the means without re-arming approval."""
    a = load_plan(fixtures_dir / "plan_two_stage_means.toml")
    b = load_plan(fixtures_dir / "plan_two_stage_means_changed.toml")
    assert diff_plans(a, b) == "refinement"


# --- optional executable verify_command / expected_exit on a stage criterion ---

def test_verify_command_parsed():
    data = {
        "meta": {"task_id": "t"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
            "verify_command": "pytest -q", "expected_exit": 0,
        }],
    }
    doc = parse_plan(data)
    assert doc.stages[0].criterion.verify_command == "pytest -q"
    assert doc.stages[0].criterion.expected_exit == 0


def test_verify_command_nonzero_expected_exit_parsed():
    data = {
        "meta": {"task_id": "t"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
            "verify_command": "test -f missing", "expected_exit": 1,
        }],
    }
    doc = parse_plan(data)
    assert doc.stages[0].criterion.expected_exit == 1


def test_verify_command_absent_defaults():
    """A stage without verify_command parses with None / exit 0 defaults (legacy plans)."""
    data = {
        "meta": {"task_id": "t"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
    }
    doc = parse_plan(data)
    assert doc.stages[0].criterion.verify_command is None
    assert doc.stages[0].criterion.expected_exit == 0


# --- optional plan-level repo_root (verify_command working directory) ---

def test_repo_root_parsed():
    data = {
        "meta": {"task_id": "t", "repo_root": "/abs/repo"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
    }
    assert parse_plan(data).meta.repo_root == "/abs/repo"


def test_repo_root_absent_defaults_none():
    """A plan without repo_root parses with None — byte-identical to legacy plans."""
    data = {
        "meta": {"task_id": "t"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
    }
    assert parse_plan(data).meta.repo_root is None


# --- optional plan-level delivery_worktree + final_check venue lint (#45) ---

def test_delivery_worktree_parsed():
    data = {
        "meta": {"task_id": "t", "delivery_worktree": "/abs/worktree"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
    }
    assert parse_plan(data).meta.delivery_worktree == "/abs/worktree"


def test_delivery_worktree_absent_defaults_none():
    """A plan without delivery_worktree parses with None — byte-identical to legacy plans."""
    data = {
        "meta": {"task_id": "t"},
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
    }
    assert parse_plan(data).meta.delivery_worktree is None


def test_final_check_venue_warns_on_repo_root_cd(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    fc = FinalCheck(command=f"cd {repo_root} && pytest -q", expected_exit=0, label="all tests")
    warnings = final_check_venue_warnings([fc], str(repo_root), str(worktree))
    assert len(warnings) == 1
    assert "repo_root" in warnings[0]
    assert str(worktree) in warnings[0]


def test_final_check_venue_silent_when_cd_targets_worktree(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    fc = FinalCheck(command=f"cd {worktree} && pytest -q", expected_exit=0, label="all tests")
    assert final_check_venue_warnings([fc], str(repo_root), str(worktree)) == []


def test_final_check_venue_silent_when_delivery_worktree_unset(tmp_path):
    repo_root = tmp_path / "repo"
    fc = FinalCheck(command=f"cd {repo_root} && pytest -q", expected_exit=0, label="all tests")
    assert final_check_venue_warnings([fc], str(repo_root), None) == []


def test_final_check_venue_lint_never_blocks_plan_parsing():
    """The venue lint is advisory-only, computed by a separate non-raising
    function — parse_plan itself never rejects a venue mismatch (the CLI layer
    calls final_check_venue_warnings onto the advisories channel, not problems)."""
    data = {
        "meta": {
            "task_id": "t",
            "repo_root": "/abs/repo",
            "delivery_worktree": "/abs/worktree",
        },
        "stage": [{
            "index": 1, "title": "x", "executor": "in_thread",
            "expected_result_image": "i", "done_criterion": "c",
        }],
        "final_check": [{"command": "cd /abs/repo && pytest -q", "expected_exit": 0}],
    }
    doc = parse_plan(data)
    assert doc.meta.delivery_worktree == "/abs/worktree"
    assert len(final_check_venue_warnings(doc.meta.final_check, doc.meta.repo_root, doc.meta.delivery_worktree)) == 1


# --- 8-element activity-structure (weight_class = "substantive") ---

def _minimal_stage(index=1, **overrides):
    base = {
        "index": index,
        "title": "Do something",
        "executor": "in_thread",
        "expected_result_image": "thing done",
        "done_criterion": "check passes",
    }
    base.update(overrides)
    return base


def _full_substantive_stage(index=1):
    return {
        **_minimal_stage(index),
        "material": "existing code",
        "means": "Edit tool",
        "method": "add the field",
        "conditions": "EXECUTING node",
        "invariants": "legacy plans unchanged",
        "capability_required": "Python",
        "verify_command": "python3 -m pytest tests/ -q",
        "principle": {
            "statement": "additive-optional keeps backward compat",
            "source": "leaf-schema.md precedent",
            "derivation": "that precedent added an optional field and no loader broke, so the same shape applies here",
            "confidence": "high",
            "refutation": "refuted if existing fixture breaks",
        },
    }


def _substantive_meta():
    return {"task_id": "t", "weight_class": "substantive", "external_research": "checked wiki; none applies"}


def test_substantive_full_parses_ok():
    data = {"meta": _substantive_meta(), "stage": [_full_substantive_stage()]}
    doc = parse_plan(data)
    assert doc.meta.weight_class == "substantive"
    assert doc.meta.external_research == "checked wiki; none applies"
    assert doc.stages[0].index == 1


def test_substantive_missing_external_research_raises():
    meta = _substantive_meta()
    del meta["external_research"]
    with pytest.raises(PlanError, match="external_research"):
        parse_plan({"meta": meta, "stage": [_full_substantive_stage()]})


def test_non_substantive_without_external_research_ok():
    """Legacy/non-substantive plans do not require external_research."""
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [_minimal_stage()]})
    assert doc.meta.external_research is None


def test_substantive_missing_principle_raises():
    stage = _full_substantive_stage()
    del stage["principle"]
    with pytest.raises(PlanError, match="principle"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_missing_principle_subfield_raises():
    stage = _full_substantive_stage()
    del stage["principle"]["source"]
    with pytest.raises(PlanError, match="source"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_stage_requires_principle_derivation():
    stage = _full_substantive_stage()
    del stage["principle"]["derivation"]
    with pytest.raises(PlanError, match="derivation"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_principle_derivation_may_not_echo_source():
    stage = _full_substantive_stage()
    stage["principle"]["derivation"] = stage["principle"]["source"]
    with pytest.raises(PlanError, match="derivation must differ from source"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_principle_derivation_may_not_echo_statement():
    stage = _full_substantive_stage()
    stage["principle"]["derivation"] = stage["principle"]["statement"]
    with pytest.raises(PlanError, match="derivation must differ from statement"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_principle_derivation_rejects_placeholder():
    stage = _full_substantive_stage()
    stage["principle"]["derivation"] = "TODO"
    with pytest.raises(PlanError, match="derivation"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_nonsubstantive_plan_loads_without_derivation():
    """A non-substantive plan whose principle omits derivation still parses (grandfather)."""
    stage = _minimal_stage()
    stage["principle"] = {
        "statement": "s", "source": "src", "confidence": "high", "refutation": "r",
    }
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [stage]})
    assert doc.stages[0].principle is not None
    assert doc.stages[0].principle.derivation == ""


def test_substantive_missing_capability_required_raises():
    stage = _full_substantive_stage()
    del stage["capability_required"]
    with pytest.raises(PlanError, match="capability_required"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_missing_material_raises():
    stage = _full_substantive_stage()
    del stage["material"]
    with pytest.raises(PlanError, match="material"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_non_substantive_without_new_fields_ok():
    """Non-substantive plans with only the legacy required fields still parse."""
    data = {
        "meta": {"task_id": "t", "weight_class": "small_change"},
        "stage": [_minimal_stage()],
    }
    doc = parse_plan(data)
    assert doc.meta.weight_class == "small_change"


def test_absent_weight_class_without_new_fields_ok():
    """Plans without weight_class (legacy) parse with only the legacy required fields."""
    data = {"meta": {"task_id": "t"}, "stage": [_minimal_stage()]}
    doc = parse_plan(data)
    assert doc.meta.weight_class is None


def test_diff_weight_class_change_is_substantive(fixtures_dir):
    """Changing weight_class triggers a substantive diff."""
    a = load_plan(fixtures_dir / "plan_two_stage.toml")
    import copy
    b_doc = copy.deepcopy(a)
    b_doc.meta.weight_class = "substantive"
    from agentctl.plan import diff_plans as _diff
    assert _diff(a, b_doc) == "substantive"


# --- typed provision-graph and confidence-enum validators ---

def test_substantive_bad_confidence_raises():
    stage = _full_substantive_stage()
    stage["principle"]["confidence"] = "very-sure"
    with pytest.raises(PlanError, match="confidence"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_dangling_supply_on_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [_minimal_stage(1, supplies=[{"on": 9}])],
    }
    with pytest.raises(PlanError, match="dangling"):
        parse_plan(data)


def test_unknown_substantive_element_raises():
    s1 = _full_substantive_stage(1)
    s2 = _full_substantive_stage(2)
    s2["supplies"] = [{"on": 1, "element": "bogus-element"}]
    with pytest.raises(PlanError, match="element"):
        parse_plan({"meta": _substantive_meta(), "stage": [s1, s2]})


def test_known_substantive_element_ok():
    s1 = _full_substantive_stage(1)
    s2 = _full_substantive_stage(2)
    s2["supplies"] = [{"on": 1, "element": "result"}]
    doc = parse_plan({"meta": _substantive_meta(), "stage": [s1, s2]})
    assert doc.stages[1].depends_on == [1]


def test_dependency_cycle_raises():
    data = {
        "meta": {"task_id": "t"},
        "stage": [
            _minimal_stage(1, supplies=[{"on": 2}]),
            _minimal_stage(2, supplies=[{"on": 1}]),
        ],
    }
    with pytest.raises(PlanError, match="cycle"):
        parse_plan(data)


def test_substantive_measurable_requires_verify_command():
    """A substantive measurable stage without verify_command is a PlanError."""
    stage = _full_substantive_stage()
    del stage["verify_command"]
    with pytest.raises(PlanError, match="verify_command"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_measurable_with_verify_command_ok():
    """A substantive measurable stage WITH verify_command parses without error."""
    doc = parse_plan({"meta": _substantive_meta(), "stage": [_full_substantive_stage()]})
    assert doc.stages[0].criterion.verify_command == "python3 -m pytest tests/ -q"


def test_substantive_acceptance_review_without_verify_command_ok():
    """A substantive acceptance_review stage does NOT need verify_command."""
    stage = _full_substantive_stage()
    del stage["verify_command"]
    stage["criterion_type"] = "acceptance_review"
    doc = parse_plan({"meta": _substantive_meta(), "stage": [stage]})
    assert doc.stages[0].criterion.criterion_type == "acceptance_review"


# --- Principle anti-template (placeholder / self-echoing fields) ---

@pytest.mark.parametrize("sub,placeholder", [
    ("statement", "TBD"),
    ("source", "N/A"),
    ("refutation", "todo"),
])
def test_substantive_placeholder_principle_field_raises(sub, placeholder):
    stage = _full_substantive_stage()
    stage["principle"][sub] = placeholder
    with pytest.raises(PlanError, match="placeholder"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_refutation_identical_to_statement_raises():
    stage = _full_substantive_stage()
    stage["principle"]["refutation"] = stage["principle"]["statement"]
    with pytest.raises(PlanError, match="refutation"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_refutation_identical_to_statement_after_normalization_raises():
    """Case/whitespace-only rephrasing must not defeat the distinctness check."""
    stage = _full_substantive_stage()
    stage["principle"]["refutation"] = "  " + stage["principle"]["statement"].upper() + "  "
    with pytest.raises(PlanError, match="refutation"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_statement_identical_to_method_raises():
    stage = _full_substantive_stage()
    stage["principle"]["statement"] = stage["method"]
    with pytest.raises(PlanError, match="method"):
        parse_plan({"meta": _substantive_meta(), "stage": [stage]})


def test_substantive_genuine_principle_passes():
    """A principle whose fields are non-placeholder and mutually distinct parses fine."""
    doc = parse_plan({"meta": _substantive_meta(), "stage": [_full_substantive_stage()]})
    assert doc.stages[0].principle.statement == "additive-optional keeps backward compat"


def test_non_substantive_placeholder_principle_field_ok():
    """Anti-template validation only applies to substantive plans; legacy/small-change
    plans with a principle table (or none at all) are unaffected."""
    stage = _minimal_stage()
    stage["principle"] = {
        "statement": "TBD", "source": "TBD", "confidence": "high", "refutation": "TBD",
    }
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [stage]})
    assert doc.stages[0].principle.statement == "TBD"


# --- lenient (strict=False) OLD-side snapshot load: bypasses schema tightened
# after a plan was approved, while the strict/default path keeps rejecting it ---

def test_lenient_snapshot_missing_derivation_parses_and_diffs():
    """A substantive snapshot frozen before [stage.principle].derivation became
    required must still load — and stay diffable — via strict=False."""
    stage = _full_substantive_stage()
    del stage["principle"]["derivation"]
    data = {"meta": _substantive_meta(), "stage": [stage]}

    doc = parse_plan(data, strict=False)
    assert doc.stages[0].principle.derivation == ""
    assert doc.stages[0].principle.statement == stage["principle"]["statement"]

    new_doc = parse_plan({"meta": _substantive_meta(), "stage": [_full_substantive_stage()]})
    assert diff_plans(doc, new_doc) in {"no_change", "refinement", "substantive"}


def test_lenient_snapshot_strict_still_rejects_missing_derivation():
    """The strict/default path (cmd_submit_plan, the NEW side of cmd_replan) must
    still raise on the exact input the lenient path above accepts."""
    stage = _full_substantive_stage()
    del stage["principle"]["derivation"]
    data = {"meta": _substantive_meta(), "stage": [stage]}

    with pytest.raises(PlanError, match="derivation"):
        parse_plan(data)


def test_lenient_snapshot_missing_external_research_parses():
    """A second substantive-only gate (the plan-level external_research meta
    requirement, distinct from the per-stage principle check above) is also
    bypassed on the lenient path — proving the relaxation covers the whole
    submission-grade validation, not just one subfield."""
    meta = _substantive_meta()
    del meta["external_research"]
    data = {"meta": meta, "stage": [_full_substantive_stage()]}

    doc = parse_plan(data, strict=False)
    assert doc.meta.external_research is None

    new_doc = parse_plan({"meta": _substantive_meta(), "stage": [_full_substantive_stage()]})
    assert diff_plans(doc, new_doc) in {"no_change", "refinement", "substantive"}


def test_lenient_snapshot_strict_still_rejects_missing_external_research():
    meta = _substantive_meta()
    del meta["external_research"]
    data = {"meta": meta, "stage": [_full_substantive_stage()]}

    with pytest.raises(PlanError, match="external_research"):
        parse_plan(data)


def test_lenient_snapshot_strict_executor_alias_still_works():
    """strict_executor is a retained back-compat alias for strict — the sole
    caller (cli.py's cmd_replan OLD-side load) predates the rename."""
    stage = _full_substantive_stage()
    del stage["principle"]["derivation"]
    data = {"meta": _substantive_meta(), "stage": [stage]}

    doc = parse_plan(data, strict_executor=False)
    assert doc.stages[0].principle.derivation == ""


def test_supplies_derive_depends_on():
    """Explicit supplies feed the derived depends_on projection."""
    data = {
        "meta": {"task_id": "t"},
        "stage": [
            _minimal_stage(1),
            _minimal_stage(2, supplies=[{"on": 1, "artifact": "x.py"}]),
        ],
    }
    doc = parse_plan(data)
    assert doc.stages[1].depends_on == [1]
    assert doc.stages[1].supplies[0].artifact == "x.py"
