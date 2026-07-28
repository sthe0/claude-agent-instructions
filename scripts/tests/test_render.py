"""agentctl plan-render: the TOML plan projected to a markdown prose view on demand.

The one invariant a render must never violate is DROPPING A STAGE, so the central
test asserts every stage's index and title survives the round-trip. render_plan_md is
pure (no filesystem); cmd_plan_render wraps it and never writes to disk.
"""
from __future__ import annotations

from agentctl.plan import load_plan, parse_plan
from agentctl.render import cmd_plan_render, render_plan_md


def _doc(n_stages: int, verify_venue=None, verify_venue_at_final=None):
    # verify_venue/verify_venue_at_final, when given, are attached to every
    # stage — used by the schema-24 second-venue rendering tests below.
    venue_keys = {}
    if verify_venue is not None:
        venue_keys["verify_venue"] = verify_venue
    if verify_venue_at_final is not None:
        venue_keys["verify_venue_at_final"] = verify_venue_at_final
    meta = {
        "task_id": "render-test",
        "goal": "g",
        "done_criterion": "d",
        "criterion_type": "measurable",
        "weight_class": "substantive",
        "external_research": "n/a",
    }
    if verify_venue_at_final is not None:
        # V3 requires a delivery_worktree distinct from repo_root before a stage
        # may declare a second venue that differs from verify_venue.
        meta["repo_root"] = "/tmp/render-test-repo"
        meta["delivery_worktree"] = "/tmp/render-test-worktree"
    data = {
        "meta": meta,
        "stage": [
            {
                "index": i,
                "title": f"Stage title number {i}",
                "executor": "in_thread",
                "expected_result_image": f"result {i}",
                "criterion_type": "measurable",
                "done_criterion": f"done {i}",
                "verify_command": "true",
                **venue_keys,
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


def test_render_landed_stage_criterion_and_final_check(fixtures_dir):
    # A landed check has no verify_command/command to print — render must show the
    # DECLARATIVE fields (which stage, target, remote) instead of an empty bullet,
    # and every stage must still appear (the one render invariant).
    doc = load_plan(fixtures_dir / "plan_landed_example.toml", strict=True)
    md = render_plan_md(doc)
    assert "## Stage 1: Deliver the change" in md
    assert (
        "**Landed check:** stage 1's delivered commit must be contained in "
        "`ticket/agentctl-landed-check-kind` and "
        "`origin/ticket/agentctl-landed-check-kind`"
    ) in md
    assert (
        "**landed check:** stage 1's delivered commit must be contained in "
        "`main` and `origin/main`"
    ) in md
    # No leftover empty-command bullet from the FinalCheck.command == "" default.
    assert "``" not in md


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


def test_render_shows_both_venues_when_verify_venue_at_final_declared():
    # Schema 24: a stage that opts into the two-moment lifecycle renders a
    # second-venue bullet naming both the execution and resolution venues.
    doc = _doc(2, verify_venue="delivery", verify_venue_at_final="repo_root")
    md = render_plan_md(doc)
    assert "**Verified in:** delivery; re-verified at resolution in repo_root" in md


def test_render_omits_second_venue_line_without_verify_venue_at_final():
    # V4's byte-identity invariant, pinned in the projection: a plan that never
    # declares verify_venue_at_final renders no "re-verified at resolution" line,
    # exactly as it did before the field existed. This guards against the append
    # escaping its `is not None` guard and silently changing every legacy plan.
    md = render_plan_md(_doc(2))
    assert "re-verified at resolution" not in md
