"""agentctl plan-render: the TOML plan projected to a markdown prose view on demand.

The one invariant a render must never violate is DROPPING A STAGE, so the central
test asserts every stage's index and title survives the round-trip. render_plan_md is
pure (no filesystem); cmd_plan_render wraps it and never writes to disk.
"""
from __future__ import annotations

from agentctl.plan import parse_plan
from agentctl.render import cmd_plan_render, render_plan_md


def _doc(n_stages: int):
    data = {
        "meta": {
            "task_id": "render-test",
            "goal": "g",
            "done_criterion": "d",
            "criterion_type": "measurable",
            "weight_class": "substantive",
            "external_research": "n/a",
        },
        "stage": [
            {
                "index": i,
                "title": f"Stage title number {i}",
                "executor": "in_thread",
                "expected_result_image": f"result {i}",
                "criterion_type": "measurable",
                "done_criterion": f"done {i}",
                "verify_command": "true",
                "material": "m",
                "means": "e",
                "method": "meth",
                "conditions": "c",
                "invariants": "inv",
                "capability_required": "cap",
                "principle": {
                    "statement": f"statement {i}",
                    "source": "src",
                    "derivation": "der follows from src",
                    "confidence": "high",
                    "refutation": "ref",
                },
            }
            for i in range(1, n_stages + 1)
        ],
    }
    return parse_plan(data)


def test_render_includes_every_stage_title():
    doc = _doc(5)
    md = render_plan_md(doc)
    for s in doc.stages:
        assert f"Stage {s.index}: {s.title}" in md, f"stage {s.index} dropped from render"
    # Exactly as many stage headers as stages — no more, no fewer.
    assert md.count("## Stage ") == len(doc.stages)


def test_render_includes_every_stage_done_criterion():
    # The coordinator shows this render at the approval gate; a renderer that dropped
    # a stage's done_criterion would make the gate lie about what is being approved.
    doc = _doc(5)
    md = render_plan_md(doc)
    for i in range(1, 6):
        assert f"done {i}" in md, f"stage {i} done_criterion dropped from render"


def test_plan_render_verb_registered():
    # The renderer is only reachable at the gate if the verb is wired into the CLI
    # dispatch — a rendered view nobody can invoke is not a deliverable.
    from agentctl import cli
    assert "plan-render" in cli.COMMANDS
    assert cli.COMMANDS["plan-render"] is cmd_plan_render


def test_render_is_pure_and_deterministic():
    doc = _doc(3)
    assert render_plan_md(doc) == render_plan_md(doc)


def test_render_includes_meta_and_principle():
    md = render_plan_md(_doc(1))
    assert "render-test" in md
    assert "substantive" in md
    assert "statement 1" in md
    assert "der follows from src" in md


def test_cmd_plan_render_reads_toml_returns_markdown(tmp_path):
    plan = tmp_path / "p.toml"
    plan.write_text(_toml_two_stage())

    class _Args:
        pass
    args = _Args()
    args.plan = str(plan)
    d = cmd_plan_render(args, store=None)
    assert d.ok is True
    assert "Alpha stage" in d.detail and "Beta stage" in d.detail
    assert d.data["markdown"] == d.detail
    # The engine must never have written the render to disk.
    assert list(tmp_path.glob("*.md")) == []


def _toml_two_stage() -> str:
    return '''
[meta]
task_id = "cmd-render"
goal = "g"
done_criterion = "d"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "n/a"

[[stage]]
index = 1
title = "Alpha stage"
executor = "in_thread"
expected_result_image = "r1"
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "true"
material = "m"
means = "e"
method = "meth"
conditions = "c"
invariants = "inv"
capability_required = "cap"
[stage.principle]
statement = "s1"
source = "src"
derivation = "der1 follows from src"
confidence = "high"
refutation = "ref"

[[stage]]
index = 2
title = "Beta stage"
executor = "spawn:developer"
expected_result_image = "r2"
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "true"
material = "m"
means = "e"
method = "meth"
conditions = "c"
invariants = "inv"
capability_required = "cap"
depends_on = [1]
[stage.principle]
statement = "s2"
source = "src"
derivation = "der2 follows from src"
confidence = "medium"
refutation = "ref"
'''
