"""Whole-suite advisory: a plan whose checks run the whole test suite more than once, or
that declares verify_reads on a whole-suite check, gets a warning at submit-plan.

Warn-only, with no blocker twin: whether a whole-suite run is justified is the plan
author's judgement; the detector only reads each command segment's program and
arguments.
"""
from __future__ import annotations

import pytest

from agentctl import cli
from agentctl import plan as plan_module
from agentctl.plan import parse_plan
from agentctl.store import FileStateStore

from test_submit_plan_surfaces import _PLAN_WITH_DUPLICATE_FC, _session

MARK = "whole-suite"
CAP_RUN = "bash scripts/cap-run.sh -- python3 -m pytest -q -p no:cacheprovider -n 4"


def _stage(command, index=1, reads=None):
    stage = {
        "index": index, "title": f"s{index}", "executor": "in_thread",
        "expected_result_image": "i", "done_criterion": "d", "verify_command": command,
    }
    if reads is not None:
        stage["verify_reads"] = reads
    return stage


def _warnings(stage_commands, final_commands=(), *, stage_reads=None, final_reads=None):
    final = []
    for command in final_commands:
        entry = {"command": command}
        if final_reads is not None:
            entry["verify_reads"] = final_reads
        final.append(entry)
    doc = parse_plan({
        "meta": {"task_id": "t"},
        "stage": [_stage(c, i, stage_reads) for i, c in enumerate(stage_commands, 1)],
        "final_check": final,
    })
    return plan_module.whole_suite_warnings(doc.stages, doc.meta.final_check)


def test_two_distinct_whole_suite_checks_warn_once():
    found = _warnings(["python3 -m pytest -q"], ["python3 -m pytest -q -x"])
    assert len(found) == 1
    assert MARK in found[0]
    assert "stage 1" in found[0] and "final_check 1" in found[0]


def test_two_whole_suite_stages_warn():
    assert len(_warnings(["pytest -q", "python3 scripts/verify-all.py"])) == 1


def test_one_whole_suite_check_is_silent():
    assert _warnings(["python3 -m pytest -q"]) == []
    assert _warnings(["pytest tests/unit_a.py"], ["python3 -m pytest -q"]) == []


def test_a_final_check_identical_to_a_whole_suite_stage_counts_once():
    assert _warnings(["python3 -m pytest -q"], ["python3 -m pytest -q"]) == []
    assert _warnings(["python3 -m pytest -q"], ["python3   -m  pytest -q"]) == []


@pytest.mark.parametrize("command", [
    "pytest -q tests/test_a.py",
    "python3 -m pytest scripts/tests/test_a.py",
    "pytest scripts/tests/test_a.py::test_one",
    "pytest -k needle",
    "pytest -m slow",
    "pytest -q -k 'a or b' -n 4",
    "pytest scripts/tests/sub",
    "python3 scripts/verify-all.py --staged",
    "python3 status.py",
    "make test",
])
def test_scoped_or_unrelated_commands_are_not_whole_suite(command):
    assert _warnings([command], ["python3 -m pytest -q"]) == []
    assert _warnings([command, "pytest -q tests/other.py"], [command]) == []


@pytest.mark.parametrize("command", [
    "pytest",
    "python3 -m pytest -q",
    "py.test -q",
    CAP_RUN,
    "export LC_ALL=C.UTF-8 && " + CAP_RUN,
    "pytest scripts/tests",
    "pytest scripts/tests/",
    "pytest ./scripts/tests",
    "pytest .",
    "pytest -n 4 -p no:cacheprovider",
    "pytest -x --maxfail 2",
    "python3 scripts/verify-all.py",
    "echo start && pytest -q && echo done",
])
def test_whole_suite_forms_are_detected(command):
    assert len(_warnings([command], ["python3 -m pytest --tb=short -q"])) == 1


def test_verify_reads_on_a_whole_suite_check_warns_even_alone():
    found = _warnings(["python3 -m pytest -q"], stage_reads=["docs/a.md"])
    assert len(found) == 1
    assert "verify_reads" in found[0]
    assert len(_warnings(["pytest tests/a.py"], ["python3 -m pytest -q"], final_reads=[])) == 1


def test_verify_reads_on_a_targeted_check_is_silent():
    assert _warnings(["pytest tests/a.py"], ["pytest tests/b.py"], stage_reads=["docs/a.md"], final_reads=[]) == []


def test_existing_scope_warning_is_unchanged():
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [_stage("pytest")]})
    assert len(plan_module.verify_command_scope_warnings(doc.stages)) == 1


def test_submit_plan_carries_the_warning_and_accepts_the_plan(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text(
        _PLAN_WITH_DUPLICATE_FC.format(
            root=str(tmp_path), verify="python3 -m pytest --version",
            final_cmd="python3 -m pytest --version -q",
        ),
        encoding="utf-8",
    )
    store = FileStateStore(tmp_path / "state")
    _session(store, "ws")
    directive = cli.cmd_submit_plan(type("A", (), {"session": "ws", "plan": str(plan)})(), store=store)
    assert directive.ok is True, directive.data.get("problems")
    assert directive.marker == "PLAN-READY"
    assert [a for a in directive.data.get("advisories", []) if MARK in a]
