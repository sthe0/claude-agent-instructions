"""The essence of a substantive plan must contain the engine's own list of the regularities and
principles the plan introduces (the 'New generalizations' block).

The list is generated from the corpus the submission seam loads (the delivery worktree's staged
leaves included) by `introduced_by` equal to the plan's task id, whether or not a stage cites the
leaf; `plan-generalizations` prints it and `present-plan --kind essence` refuses a rendering that
does not contain it, the empty block included.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER

TASK = "gen-task"
QUOTE = "the user wants every new generalization shown before approval"

_LEAF = "---\nname: {slug}\ndescription: a leaf\nschema: {schema}\n{extra}---\n{body}\n"

_PLAN = """
[meta]
task_id = "{task}"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"
repo_root = "{repo}"
delivery_worktree = "{worktree}"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage"
executor = "in_thread"
expected_result_image = "The parser reads the new key."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
negative_control = "false"
material = "m1 (traces to R1)"
means = "bash"
method = "Add the field to the typed struct, keeping the corpus loadable."
procedure = "1. Read state.py's Means. 2. Add the field with its default."
conditions = "The tree is clean."
preconditions = "The branch is checked out."
invariants = "n1"
capability_required = "cap"
material_refs = ["scripts/agentctl/state.py"]
knowledge_refs = ["scripts/agentctl/plan.py"]
knowledge = "how the loader stays lenient"
[stage.principle]
statement = "s"
source = {source}
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK


def ns(**kw):
    return Namespace(**kw)


def _regularity(slug, statement, *, introduced_by=None, order_quote=None):
    extra = f"introduced_by: {introduced_by}\n" if introduced_by else ""
    body = f"## Regularity\n\n{statement}\n\nA second paragraph that stays out of the block.\n"
    if order_quote:
        extra += f"order_derived: {introduced_by}\n"
        body += f"\n## Derived from order\n\n{order_quote}\n"
    return _LEAF.format(slug=slug, schema="regularity/v1", extra=extra, body=body)


def _principle(slug, statement, *, introduced_by):
    return _LEAF.format(
        slug=slug, schema="principle/v1",
        extra=f"grounded_in: [regularity:reg-base]\nintroduced_by: {introduced_by}\n",
        body=f"## Principle\n\n{statement}\n")


class _World(Namespace):
    def put(self, rel, text, *, staged=False):
        ((self.staged if staged else self.leaves) / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A global corpus, a delivery worktree with a staged-leaves dir, and the engine's leaves
    root pointed at the corpus."""
    from agentctl import grounding

    leaves = tmp_path / "engine" / "memory-global" / "leaves"
    (leaves / "regularities").mkdir(parents=True)
    (leaves / "principles").mkdir(parents=True)
    staged = tmp_path / "wt" / "memory-global" / "leaves"
    (staged / "regularities").mkdir(parents=True)
    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: leaves)
    world = _World(leaves=leaves, staged=staged, tmp=tmp_path)
    world.put(
        "regularities/reg-base.md", _regularity("reg-base", "A base regularity, introduced by nobody."))
    return world


def _plan(world, *, task=TASK, source="regularity:reg-base"):
    text = _PLAN.format(task=task, repo=world.tmp / "repo", worktree=world.tmp / "wt",
                        source=json.dumps(source))
    path = world.tmp / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _printed(plan_path) -> str:
    from agentctl import cli

    directive = cli.cmd_plan_generalizations(ns(plan=plan_path))
    assert directive.ok is True, directive.detail
    return directive.detail


def _present(store, plan_path, rendering, tmp_path):
    from agentctl import cli
    from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE

    cli.cmd_start(ns(session="g", task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    state = store.load("g")
    state.plan_path = plan_path
    store.save(state)
    rendering_file = tmp_path / "essence.md"
    rendering_file.write_text(rendering, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session="g", kind=PLAN_PRESENTATION_KIND_ESSENCE,
           rendering_file=str(rendering_file), emit_skeleton=False),
        store=store)


def _grants_block(plan_path) -> str:
    from agentctl.plan import load_plan
    from agentctl.render import render_plan_grants

    return render_plan_grants(load_plan(plan_path), fmt="compact")


def test_block_lists_introduced(world):
    world.put("regularities/reg-new.md",
              _regularity("reg-new", "Every new thing is listed.", introduced_by=TASK))

    block = _printed(_plan(world, source="regularity:reg-new"))

    assert block.splitlines()[0] == "New generalizations (for approval):"
    assert "- regularity:reg-new — Every new thing is listed.\n" in block
    assert "second paragraph" not in block


def test_verb_prints_block(world):
    from agentctl import cli

    world.put("regularities/reg-new.md",
              _regularity("reg-new", "Every new thing is listed.", introduced_by=TASK))

    directive = cli.cmd_plan_generalizations(ns(plan=_plan(world)))

    assert directive.ok is True
    assert "- regularity:reg-new — Every new thing is listed." in directive.detail
    assert directive.data["generalizations_block"] == directive.detail


def test_block_omits_foreign(world):
    world.put("regularities/reg-foreign.md",
              _regularity("reg-foreign", "Another task's.", introduced_by="other-task"))

    block = _printed(_plan(world, source="regularity:reg-foreign"))

    assert "reg-foreign" not in block
    assert "- none:" in block


def test_block_shows_order_quote(world):
    world.put("regularities/reg-order.md",
              _regularity("reg-order", "From the order.", introduced_by=TASK, order_quote=QUOTE))

    block = _printed(_plan(world, source="regularity:reg-order"))

    assert "- regularity:reg-order — From the order." in block
    assert f"  order quote: {QUOTE}" in block


def test_essence_without_block_refused(store, world):
    world.put("regularities/reg-new.md",
              _regularity("reg-new", "Every new thing is listed.", introduced_by=TASK))
    plan = _plan(world, source="regularity:reg-new")
    grants = _grants_block(plan)

    refused = _present(store, plan, "## Essence\n\n" + grants, world.tmp)

    assert refused.ok is False
    assert "- regularity:reg-new — Every new thing is listed." in refused.data["generalizations_block"]
    accepted = _present_again(store, plan, "## Essence\n\n" + grants + "\n" + _printed(plan), world.tmp)
    assert accepted.ok is True, accepted.data


def _present_again(store, plan_path, rendering, tmp_path):
    from agentctl import cli
    from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE

    rendering_file = tmp_path / "essence2.md"
    rendering_file.write_text(rendering, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session="g", kind=PLAN_PRESENTATION_KIND_ESSENCE,
           rendering_file=str(rendering_file), emit_skeleton=False),
        store=store)


def test_empty_block_still_required(store, world):
    plan = _plan(world)
    grants = _grants_block(plan)

    refused = _present(store, plan, "## Essence\n\n" + grants, world.tmp)

    assert refused.ok is False
    assert "- none:" in refused.data["generalizations_block"]
    accepted = _present_again(store, plan, "## Essence\n\n" + grants + "\n" + _printed(plan), world.tmp)
    assert accepted.ok is True, accepted.data


def test_block_empty_without_task_id(world):
    world.put("regularities/reg-unstamped.md", _regularity("reg-unstamped", "Nobody stamped this."))

    import dataclasses

    from agentctl import cli
    from agentctl.plan import load_plan

    doc = load_plan(_plan(world))
    unlabeled = dataclasses.replace(doc, meta=dataclasses.replace(doc.meta, task_id=""))

    block = cli._generalizations_block(unlabeled)

    assert "reg-unstamped" not in block
    assert "- none:" in block


def test_block_lists_staged(store, world):
    world.put("regularities/reg-staged.md",
              _regularity("reg-staged", "Only in the worktree.", introduced_by=TASK), staged=True)
    plan = _plan(world)
    grants = _grants_block(plan)

    assert "- regularity:reg-staged — Only in the worktree." in _printed(plan)
    refused = _present(store, plan, "## Essence\n\n" + grants, world.tmp)
    assert refused.ok is False
    assert "reg-staged" in refused.data["generalizations_block"]


def test_block_lists_uncited_introduced(world):
    world.put("regularities/reg-lone.md",
              _regularity("reg-lone", "Nobody cites it.", introduced_by=TASK))

    block = _printed(_plan(world, source="regularity:reg-base"))

    assert "- regularity:reg-lone — Nobody cites it. (not cited by any stage)" in block


def test_line_without_statement_section_shows_placeholder(world):
    world.put("regularities/reg-bare.md",
              _LEAF.format(slug="reg-bare", schema="regularity/v1",
                           extra=f"introduced_by: {TASK}\n", body="No statement heading here."))

    block = _printed(_plan(world))

    assert "- regularity:reg-bare — (no statement section)" in block


def test_block_lists_stamped_principle(world):
    world.put("principles/prin-new.md", _principle("prin-new", "To X, do Y.", introduced_by=TASK))

    block = _printed(_plan(world, source="regularity:reg-base; principle:prin-new"))

    assert "- principle:prin-new — To X, do Y.\n" in block
