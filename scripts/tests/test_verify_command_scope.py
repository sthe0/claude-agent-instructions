"""Deterministic verify_command scope lint (experience leaf 2026-06-29): warns
when a stage's verify_command runs an aggregate suite (verify-all.py, a bare
pytest invocation) without a scope flag, so a legitimate stage-scoped verify
does not get false-failed by pre-existing unrelated reds. Warn-only — never
blocks the plan-approval gate.
"""
from argparse import Namespace

from agentctl import cli
from agentctl.plan import parse_plan, verify_command_scope_warnings
from agentctl.store import FileStateStore


def ns(**kw):
    return Namespace(**kw)


def _stage(verify_command, title="s", index=1):
    return {
        "index": index,
        "title": title,
        "executor": "in_thread",
        "expected_result_image": "i",
        "done_criterion": "d",
        "verify_command": verify_command,
    }


def _warnings_for(verify_command, title="s"):
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [_stage(verify_command, title=title)]})
    return verify_command_scope_warnings(doc.stages)


def test_bare_pytest_module_invocation_warns():
    assert len(_warnings_for("python -m pytest")) == 1


def test_verify_all_without_staged_warns():
    assert len(_warnings_for("scripts/verify-all.py")) == 1


def test_verify_all_staged_is_silent():
    assert _warnings_for("scripts/verify-all.py --staged") == []


def test_pytest_explicit_path_is_silent():
    assert _warnings_for("pytest scripts/tests/test_x.py") == []


def test_pytest_dash_k_selection_is_silent():
    assert _warnings_for("pytest -k foo") == []


def test_pytest_dash_m_marker_selection_is_silent():
    assert _warnings_for("pytest -m slow") == []


def test_bare_pytest_no_args_warns():
    assert len(_warnings_for("pytest")) == 1


def test_non_aggregate_command_is_silent():
    assert _warnings_for("python3 status.py") == []


def test_empty_verify_command_is_silent():
    assert _warnings_for(None) == []
    assert _warnings_for("") == []


def test_multi_command_unscoped_tail_warns():
    warnings = _warnings_for("python3 status.py && python -m pytest")
    assert len(warnings) == 1


def test_warning_names_stage_index_and_title():
    warnings = _warnings_for("python -m pytest", title="Run everything")
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "Run everything" in warnings[0]


def test_submit_plan_attaches_scope_advisory_without_blocking(tmp_path):
    """Integration: an aggregate-unscoped verify_command in a submitted TOML plan
    surfaces the scope warning in the submit-plan Directive's advisories while
    ok/node/marker stay exactly the PLAN-READY hard-gate shape."""
    store = FileStateStore(tmp_path / "state")
    sid = "scope-lint"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                      criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                         wall_clock_min=60, tracker_key=None, architectural=True,
                         external_effect=False, new_dependency=False,
                         public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)

    plan = tmp_path / "plan.toml"
    plan.write_text(
        """
[meta]
task_id = "demo"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"

[[stage]]
index = 1
title = "Run everything"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
verify_command = "python -m pytest"
""",
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)

    assert d.ok is True
    from agentctl.state import Node
    assert d.node == Node.PLAN_READY.value
    assert d.marker == "PLAN-READY"
    advisories = d.data.get("advisories", [])
    assert any("aggregate" in a and "stage 1" in a for a in advisories)
