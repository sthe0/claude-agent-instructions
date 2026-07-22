"""The shared planner-return check (scripts/lib/planner_plan_check.py).

Home of:
  * the planner-deliverable contract: the plan is validated through the engine's
    own TOML validator (agentctl.plan.load_plan), must be a substantive `.toml`,
    has NO markdown branch, and fails CLOSED on a broken engine import; and
  * check_planner_return, the wrappers' single entry point, which runs the plan
    check and reports the marker for telemetry even under a prose preamble.

The eight return-marker tests for the shared validate_marker live at their
historical home, test_spawn_specialist_marker.py (which also carries the wrapper
identity tests); this file keeps one smoke that the unified validate_marker reads
a marker on any line.
"""
from __future__ import annotations

import sys

from lib import config_root, planner_plan_check as MOD


# --- extract_marker: the label is read on ANY line ---------------------------

def test_extract_marker_ignores_preamble_prose():
    # The exact regression the caller fail-open caused: a preamble before the marker.
    assert MOD.extract_marker("summary first\nblah\nPLAN-READY: go\nPlan: /x.toml") == "PLAN-READY"


def test_extract_marker_none_when_absent():
    assert MOD.extract_marker("no marker here at all") is None


def test_marker_on_any_line_accepted_by_the_unified_validate_marker():
    # This module is the home of the shared validate_marker; smoke that it reads a
    # marker on a line other than the first — the property both wrappers depend on.
    _, ok = MOD.validate_marker("preamble one\npreamble two\nCOMPLETED: done")
    assert ok is True


# --- planner-deliverable contract: TOML via the engine, no .md branch --------

def _plan_toml(tmp_path, *, weight="substantive", extra_stage=True) -> str:
    body = f'''
[meta]
task_id = "check-plan"
goal = "g"
done_criterion = "d"
criterion_type = "measurable"
weight_class = "{weight}"
external_research = "n/a"

[[stage]]
index = 1
title = "Only stage"
executor = "in_thread"
expected_result_image = "r"
criterion_type = "measurable"
done_criterion = "d"
verify_command = "true"
material = "m"
means = "e"
method = "meth"
conditions = "c"
invariants = "inv"
capability_required = "cap"
[stage.principle]
statement = "s"
source = "src"
derivation = "der follows from src"
confidence = "high"
refutation = "ref"
'''
    p = tmp_path / "plan.toml"
    p.write_text(body)
    return str(p)


def test_toml_plan_accepted(tmp_path):
    plan = _plan_toml(tmp_path)
    text, ok = MOD.validate_planner_plan(f"PLAN-READY: ready\nPlan: {plan}")
    assert ok is True
    assert text.startswith("PLAN-READY:")  # unchanged on success


def test_missing_plan_line_message_names_toml_not_md(tmp_path):
    text, ok = MOD.validate_planner_plan("PLAN-READY: ready but no path")
    assert ok is False
    assert ".toml" in text  # refusal names the TOML convention
    assert str(config_root.plans_dir()) in text  # and the real plans directory


def test_md_plan_rejected_as_malformed(tmp_path):
    md = tmp_path / "plan.md"
    md.write_text("# a markdown plan")
    text, ok = MOD.validate_planner_plan(f"PLAN-READY: ready\nPlan: {md}")
    assert ok is False
    assert "not a .toml" in text
    assert "TOML-only" in text  # states the plans-are-TOML-only convention
    assert str(config_root.plans_dir()) in text


def test_non_substantive_weight_class_rejected(tmp_path):
    plan = _plan_toml(tmp_path, weight="small")
    text, ok = MOD.validate_planner_plan(f"PLAN-READY: ready\nPlan: {plan}")
    assert ok is False
    assert "substantive" in text


def test_plan_error_surfaces_as_malformed(tmp_path):
    # A .toml the engine validator refuses (missing required stage field).
    bad = tmp_path / "bad.toml"
    bad.write_text('[meta]\ntask_id="x"\nweight_class="substantive"\nexternal_research="n/a"\n'
                   '[[stage]]\nindex=1\ntitle="t"\nexecutor="in_thread"\n')
    text, ok = MOD.validate_planner_plan(f"PLAN-READY:\nPlan: {bad}")
    assert ok is False
    assert "failed engine validation" in text


def test_engine_import_failure_fails_closed(tmp_path, monkeypatch):
    plan = _plan_toml(tmp_path)
    # Simulate a broken engine import: `from agentctl.plan import ...` then raises.
    monkeypatch.setitem(sys.modules, "agentctl.plan", None)
    text, ok = MOD.validate_planner_plan(f"PLAN-READY:\nPlan: {plan}")
    assert ok is False  # CLOSED, not open
    assert "Refusing to pass the plan" in text


# --- check_planner_return: the wrappers' single entry point ------------------

def test_preamble_before_marker_still_runs_the_plan_check(tmp_path):
    # A preamble before the marker must NOT let the plan check be skipped: a bad
    # (.md) plan under a preamble is still caught. This is the fail-open regression
    # (v3 wrote a preamble and the check silently did not fire).
    md = tmp_path / "plan.md"
    md.write_text("# md")
    forwarded, ok, marker = MOD.check_planner_return(
        f"Here is my plan summary.\nPLAN-READY: ready\nPlan: {md}", "planner"
    )
    assert ok is False
    assert forwarded.startswith("MALFORMED:")  # the check FIRED under the preamble


def test_check_planner_return_reports_marker_for_telemetry(tmp_path):
    # telemetry's return_marker reads this value — it must find the RIGHT marker
    # even when a preamble precedes it.
    plan = _plan_toml(tmp_path)
    forwarded, ok, marker = MOD.check_planner_return(
        f"Here is my plan summary.\nPLAN-READY: ready\nPlan: {plan}", "planner"
    )
    assert ok is True
    assert marker == "PLAN-READY"


def test_check_planner_return_planner_bad_plan_blocks(tmp_path):
    md = tmp_path / "plan.md"
    md.write_text("# md")
    forwarded, ok, marker = MOD.check_planner_return(
        f"PLAN-READY: ready\nPlan: {md}", "planner"
    )
    assert ok is False
    assert marker == "PLAN-READY"  # marker still correctly identified
    assert forwarded.startswith("MALFORMED:")


def test_check_planner_return_non_planner_skips_plan_check():
    # A developer COMPLETED: must not be run through the plan validator.
    forwarded, ok, marker = MOD.check_planner_return("COMPLETED: shipped it", "developer")
    assert ok is True
    assert marker == "COMPLETED"
    assert forwarded == "COMPLETED: shipped it"


def test_check_planner_return_malformed():
    forwarded, ok, marker = MOD.check_planner_return("no marker", "planner")
    assert ok is False
    assert marker is None
