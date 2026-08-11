"""Stage 10 of smd-act-defects-8: `check-order-coverage.py`'s resolver.

`submission.py`'s `_order_violations` already proves `[meta.order.coverage]` is
total in the PRESENCE direction (every declared requirement id has an entry, and
vice versa) — that half is exercised elsewhere, and is not re-tested here. This
file covers the RESOLUTION half `_order_violations` deliberately declines: that
each control string an entry names actually resolves against the plan (a real
stage's `verify_command`, a real `final_check` index, or a real landed-kind
stage) — the gap `resolve_control`/`coverage_violations` closes.

Fixture-domain only: this suite never touches the live smd-act-defects-8 plan.
The one-shot check that THIS plan's own coverage map resolves is a separate CLI
invocation of `check-order-coverage.py` against the committed snapshot
(`fixtures/plan_snapshot_smd-act-defects-8.toml`), run directly as part of this
stage's `verify_command`, not as a pytest case — a fixture plan and the one real
plan under test are deliberately different failure surfaces.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from agentctl.plan import parse_plan

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_order_coverage", ROOT / "scripts" / "check-order-coverage.py"
)
check_order_coverage = importlib.util.module_from_spec(_SPEC)
sys.modules["check_order_coverage"] = check_order_coverage
_SPEC.loader.exec_module(check_order_coverage)

resolve_control = check_order_coverage.resolve_control
coverage_violations = check_order_coverage.coverage_violations
main = check_order_coverage.main


def _stage_dict(index=1, **overrides):
    base = {
        "index": index, "title": f"s{index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do", "verify_command": f"true-{index}",
    }
    base.update(overrides)
    return base


def _doc(stages, order=None, final_check=None):
    meta = {"task_id": "t"}
    if order is not None:
        meta["order"] = order
    data = {"meta": meta, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _order(requirements, coverage):
    return {
        "requirements": [{"id": rid, "text": rid} for rid in requirements],
        "coverage": coverage,
    }


# --- resolve_control: the three grammars, matched by prefix ------------------

def test_verify_command_grammar_resolves_against_a_real_stage():
    doc = _doc([_stage_dict(1)])
    assert resolve_control("stage 1 verify_command", doc) is None


def test_verify_command_grammar_accepts_a_trailing_parenthetical():
    doc = _doc([_stage_dict(1)])
    assert resolve_control("stage 1 verify_command (full CI run)", doc) is None


def test_verify_command_grammar_rejects_a_missing_stage():
    doc = _doc([_stage_dict(1)])
    problem = resolve_control("stage 9 verify_command", doc)
    assert problem is not None and "no stage 9" in problem


def test_verify_command_grammar_rejects_a_stage_with_no_verify_command():
    doc = _doc([_stage_dict(1, verify_command=None)])
    problem = resolve_control("stage 1 verify_command", doc)
    assert problem is not None and "declares no verify_command" in problem


def test_final_check_grammar_resolves_against_a_declared_index():
    doc = _doc([_stage_dict(1)], final_check=[{"command": "true"}])
    assert resolve_control("final_check 1", doc) is None


def test_final_check_grammar_rejects_an_out_of_range_index():
    doc = _doc([_stage_dict(1)], final_check=[{"command": "true"}])
    problem = resolve_control("final_check 2", doc)
    assert problem is not None and "no final_check 2" in problem


def test_landed_assertion_grammar_resolves_against_a_landed_stage():
    doc = _doc([_stage_dict(1, verify_command=None, verify_kind="landed",
                            landed={"target": "main", "delivered_stage": 1})])
    assert resolve_control("stage 1 landed assertion", doc) is None


def test_landed_assertion_grammar_accepts_a_trailing_parenthetical():
    doc = _doc([_stage_dict(1, verify_command=None, verify_kind="landed",
                            landed={"target": "main", "delivered_stage": 1})])
    problem = resolve_control(
        "stage 1 landed assertion (containment in main and origin/main)", doc
    )
    assert problem is None


def test_landed_assertion_grammar_rejects_a_shell_kind_stage():
    doc = _doc([_stage_dict(1)])
    problem = resolve_control("stage 1 landed assertion", doc)
    assert problem is not None and "not a landed-kind criterion" in problem


def test_a_name_outside_the_grammar_is_a_failure_never_a_skip():
    doc = _doc([_stage_dict(1)])
    problem = resolve_control("stage 1 vibes check", doc)
    assert problem is not None and "matches none of the three accepted" in problem


# --- coverage_violations: totality + resolution, combined --------------------

def test_a_fully_covered_and_resolved_order_is_clean():
    doc = _doc(
        [_stage_dict(1)],
        order=_order(["R1"], {"R1": ["stage 1 verify_command"]}),
    )
    assert coverage_violations(doc) == []


def test_a_requirement_with_no_coverage_entry_is_reported():
    doc = _doc([_stage_dict(1)], order=_order(["R1", "R2"],
                                              {"R1": ["stage 1 verify_command"]}))
    violations = coverage_violations(doc)
    assert any("R2" in v and "no coverage entry" in v for v in violations)


def test_a_coverage_key_naming_no_declared_requirement_is_reported():
    doc = _doc([_stage_dict(1)], order=_order(
        ["R1"], {"R1": ["stage 1 verify_command"], "R9": ["stage 1 verify_command"]}
    ))
    violations = coverage_violations(doc)
    assert any("R9" in v and "names no declared requirement" in v for v in violations)


def test_an_unresolvable_control_is_reported_even_though_totality_holds():
    doc = _doc([_stage_dict(1)], order=_order(["R1"], {"R1": ["stage 9 verify_command"]}))
    violations = coverage_violations(doc)
    assert any("no stage 9" in v for v in violations)


def test_an_order_less_plan_is_reported_as_nothing_to_check():
    doc = _doc([_stage_dict(1)])
    violations = coverage_violations(doc)
    assert len(violations) == 1 and "absent" in violations[0]


# --- main(): CLI exit codes ---------------------------------------------------

def test_main_exits_zero_on_a_clean_plan(tmp_path):
    plan_path = tmp_path / "clean.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[meta.order]\n'
        'requirements = [{id = "R1", text = "r1"}]\n'
        '[meta.order.coverage]\n'
        'R1 = ["stage 1 verify_command"]\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n',
        encoding="utf-8",
    )
    assert main(["check-order-coverage.py", str(plan_path)]) == 0


def test_main_exits_one_on_an_unresolvable_control(tmp_path):
    plan_path = tmp_path / "broken.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[meta.order]\n'
        'requirements = [{id = "R1", text = "r1"}]\n'
        '[meta.order.coverage]\n'
        'R1 = ["stage 9 verify_command"]\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n',
        encoding="utf-8",
    )
    assert main(["check-order-coverage.py", str(plan_path)]) == 1


def test_main_exits_two_on_bad_usage():
    assert main(["check-order-coverage.py"]) == 2


def test_main_exits_one_on_a_missing_file():
    assert main(["check-order-coverage.py", "/nonexistent/plan.toml"]) == 1


def test_main_resolves_against_the_committed_smd_act_defects_8_snapshot():
    snapshot = Path(__file__).parent / "fixtures" / "plan_snapshot_smd-act-defects-8.toml"
    assert main(["check-order-coverage.py", str(snapshot)]) == 0
