"""Typed-edge delivery: how an edge's provision reaches its consumer is declared, not guessed.

`Supply.delivery` (artifact | continuation | report) is optional. These tests pin the
submission rules for a declared delivery, the bare-edge refusal and legacy load that must
stay as they are, and `_continuation_worktree` honouring an explicit delivery while edges
without one keep the old inference (any depends_on over a spawn stage).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentctl import cli
from agentctl.plan import PlanError, load_plan
from agentctl.submission import submission_violations

from test_supply_edges import BARE_DEPENDS_ON, _write_plan

_OUTPUTS = 'output_artifacts = ["docs/out.md"]\n'


def _plan(tmp_path, edge, *, stage1_outputs=True):
    path = _write_plan(tmp_path / "p.toml", edge=edge)
    if stage1_outputs:
        text = open(path, encoding="utf-8").read()
        marker = 'knowledge = "which places an edge really hands over"\n'
        assert marker in text
        open(path, "w", encoding="utf-8").write(text.replace(marker, marker + _OUTPUTS, 1))
    return path


def _edge(delivery=None, artifact=None):
    lines = '[[stage.supplies]]\non = 1\nelement = "material"\n'
    if artifact:
        lines += f'artifact = "{artifact}"\n'
    if delivery:
        lines += f'delivery = "{delivery}"\n'
    return lines


def test_bare_edge_in_a_new_substantive_plan_is_refused_with_an_actionable_message(tmp_path):
    doc = load_plan(_plan(tmp_path, BARE_DEPENDS_ON))
    problems = submission_violations(doc)
    assert len(problems) == 1
    assert "stage 2" in problems[0] and "stage 1" in problems[0]
    assert "[[stage.supplies]]" in problems[0]


def test_legacy_bare_edge_plan_still_loads_and_renders(tmp_path):
    doc = load_plan(_plan(tmp_path, BARE_DEPENDS_ON), strict=True)
    assert doc.stages[1].depends_on == [1]
    assert doc.stages[1].supplies[0].delivery is None


def test_unknown_delivery_is_a_plan_error(tmp_path):
    with pytest.raises(PlanError, match="unknown delivery"):
        load_plan(_plan(tmp_path, _edge(delivery="telepathy")))


def test_edge_without_delivery_is_never_a_violation(tmp_path):
    assert submission_violations(load_plan(_plan(tmp_path, _edge()))) == []


def test_artifact_delivery_naming_an_undeclared_artifact_is_refused(tmp_path):
    doc = load_plan(_plan(tmp_path, _edge("artifact", "docs/other.md")))
    problems = submission_violations(doc)
    assert len(problems) == 1
    assert "docs/other.md" in problems[0] and "docs/out.md" in problems[0]


def test_artifact_delivery_naming_no_artifact_is_refused(tmp_path):
    problems = submission_violations(load_plan(_plan(tmp_path, _edge("artifact"))))
    assert len(problems) == 1 and "names no artifact" in problems[0]


def test_artifact_delivery_naming_a_declared_artifact_is_accepted(tmp_path):
    doc = load_plan(_plan(tmp_path, _edge("artifact", "docs/out.md")))
    assert submission_violations(doc) == []


def test_continuation_delivery_from_an_in_thread_stage_is_refused(tmp_path):
    problems = submission_violations(load_plan(_plan(tmp_path, _edge("continuation"))))
    assert len(problems) == 1 and "not a spawn stage" in problems[0]


def test_report_delivery_is_accepted(tmp_path):
    assert submission_violations(load_plan(_plan(tmp_path, _edge("report")))) == []


# --- _continuation_worktree ---------------------------------------------------------


def _stage(index, depends_on=(), deliveries=None, spawn=False):
    deliveries = deliveries or {}
    return SimpleNamespace(
        index=index,
        depends_on=list(depends_on),
        supplies=[SimpleNamespace(on=d, delivery=deliveries.get(d)) for d in depends_on],
        is_spawn=lambda: spawn,
    )


def _state(*stages):
    return SimpleNamespace(
        stages=list(stages), delivery_worktree="/wt", repo_root="/repo", task_id="t",
    )


def _consumer_cont(deliveries):
    supplier = _stage(1, spawn=True)
    consumer = _stage(2, [1], deliveries, spawn=True)
    return cli._continuation_worktree(_state(supplier, consumer), consumer)


def test_explicit_continuation_delivery_continues_the_worktree():
    assert _consumer_cont({1: "continuation"}) == "/wt"


@pytest.mark.parametrize("delivery", ["artifact", "report"])
def test_explicit_non_continuation_delivery_never_continues(delivery):
    assert _consumer_cont({1: delivery}) is None


def test_edge_without_delivery_keeps_the_depends_on_inference():
    assert _consumer_cont({}) == "/wt"


def test_mixed_edges_continue_when_any_edge_continues_or_is_undeclared():
    s1, s2 = _stage(1, spawn=True), _stage(2, spawn=True)
    consumer = _stage(3, [1, 2], {1: "report"}, spawn=True)
    assert cli._continuation_worktree(_state(s1, s2, consumer), consumer) == "/wt"
    consumer = _stage(3, [1, 2], {1: "report", 2: "artifact"}, spawn=True)
    assert cli._continuation_worktree(_state(s1, s2, consumer), consumer) is None
