"""The grounding judge at the plan seams.

Stage 16's judge says whether the knowledge a stage cites supports its principle; this file pins that the
verdict reaches every seam that admits or reviews plan bytes: the submission check (links, the given
rule, ontology adequacy, order quotations), the essence (an override is shown to the user), the
check-coverage pre-flight, the review bundles, and `plan-review --verdict pass`.

Every test enters through the real entry point -- `_submission_problems`, `present-plan`, `check-coverage`,
`plan-review`, the bundle renderers -- over a tmp corpus the real loader reads, with a fixture runner that
answers by reading the prompt it is given (no live model). Each reaches a seam through the module
attribute a mutation replaces, so one broken seam turns exactly its tests red by assertion.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from test_grounding_norm import (  # noqa: F401  (the plan template and the session helpers of the structural check)
    _PLAN, _approved, _doc, _finish_stage_1, _to_plan_ready, ns)

TASK = "grounding-task"
TODAY = date.today().isoformat()
STALE = "2020-01-01"
BAD = "BADLINK"
BAD_DISTINCTION = "BADDIST"
REASON = "the order itself names this source, kept on purpose"
GROUNDED = "regularity:reg-a"


def _put(root, rel, schema, body, extra=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.stem}\ndescription: a leaf\nschema: {schema}\n{extra}---\n\n{body}\n",
                    encoding="utf-8")


def _given(root, slug, verified, source="docs/spec.md"):
    _put(root, f"regularities/{slug}.md", "regularity/v1",
         "## Regularity\n\nThe vendor allows ten calls a second.\n\n## Given\n\nRead in the spec.\n\n"
         "## Refutation\n\nA call refused below ten a second.",
         f"given: doc\ngiven_source: {source}\nlast_verified: {verified}\n")


def _order_derived(root, slug, quote, verified=TODAY):
    _put(root, f"regularities/{slug}.md", "regularity/v1",
         f"## Regularity\n\nA generalization drawn from the order.\n\n## Derived from order\n\n{quote}",
         f"introduced_by: {TASK}\norder_derived: {TASK}\nlast_verified: {verified}\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    from agentctl import grounding
    from agentctl import grounding_judge as GJ

    leaves = tmp_path / "engine" / "memory-global" / "leaves"
    _put(leaves, "regularities/reg-a.md", "regularity/v1",
         "## Regularity\n\nA parser that stays lenient keeps old corpora loadable.\n\n## Generality\n\ng\n\n"
         "## Refutation\n\nr\n\n## Induced from\n\ne",
         f"induced_from: [experience:exp-a]\nlast_verified: {TODAY}\n")
    _put(leaves, "experience/exp-a.md", "difficulty/v1", "body")
    _put(leaves, "ontology/onto-a.md", "ontology/v1",
         f"## Distinction\n\nA hot path against a cold one {BAD_DISTINCTION}.\n\n## Difficulty\n\nd\n\n"
         "## Adequacy\n\nCuts by call rate.")
    repo = tmp_path / "repo"
    (repo / ".claude" / "agent-memory").mkdir(parents=True)
    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: leaves)
    monkeypatch.setattr(GJ, "cache_dir", lambda: tmp_path / "judge-cache")
    return SimpleNamespace(leaves=leaves, repo=repo, tmp=tmp_path)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


# --- the fixture runner -------------------------------------------------------------------------------


def _between(text, start, end):
    head = text.split(start, 1)[1] if start in text else ""
    return head.split(end, 1)[0]


class Judge:
    """Answers a grounding prompt by reading the part of it a marker lives in: BADLINK in the prescription is
    an `insufficient` link, BADDIST in an ontology ground is `inadequate_distinction` (only when the prompt
    asks the ontology question), UNFAITHFUL / DEMAND in an order quotation flag it. Any other prompt is
    unanswered, as a judge that is down."""

    def __init__(self):
        self.prompts = []

    def __call__(self, argv, *, timeout=None, stdin=None, **_):
        from agentctl.dispatch import RunResult

        prompt = stdin or ""
        self.prompts.append(prompt)
        if "QUOTATION:" in prompt:
            quote = _between(prompt, "QUOTATION:\n", "\n\nSTATEMENT:")
            reply = {"faithful": "UNFAITHFUL" not in quote, "demand": "DEMAND" in quote, "reason": "fixture"}
        elif prompt.startswith("You judge one link"):
            defects = []
            if BAD in _between(prompt, "PRESCRIPTION:\n", "\n\nLISTED GROUNDS:"):
                defects.append("insufficient")
            if "Ontology adequacy" in prompt and BAD_DISTINCTION in _between(
                    prompt, "LISTED GROUNDS:\n", "\n\nCANDIDATES"):
                defects.append("inadequate_distinction")
            reply = {"defects": defects, "reason": "fixture"}
        else:
            return RunResult(1, "", "not a grounding prompt")
        return RunResult(0, json.dumps(reply), "")


def _down(argv, *, timeout=None, stdin=None, **_):
    from agentctl.dispatch import RunResult

    return RunResult(1, "", "no such model")


# --- plans and entry points --------------------------------------------------------------------------


def _plan(world, *, statement1="s", source1=GROUNDED, source2="experience:exp-a", override1=None,
          override2=None, name="p.toml", knowledge_refs=None):
    def keyed(value, override):
        return json.dumps(value) + (f"\ngrounding_override = {json.dumps(override)}" if override else "")

    text = _PLAN.format(
        task=TASK, repo=world.repo, delivery="", source1=json.dumps(source1), source2=keyed(source2, override2),
        title2=json.dumps("the stage under test"), statement1=keyed(statement1, override1),
        procedure2=json.dumps("1. Read submission.py's loop. 2. Add the row. 3. Run pytest -q."))
    if knowledge_refs is not None:
        text = text.replace('knowledge_refs = ["scripts/agentctl/plan.py"]',
                            f"knowledge_refs = {json.dumps(knowledge_refs)}")
    path = world.tmp / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _problems(plan, runner, *, state=None, baseline=None, warnings=None):
    from agentctl import cli

    return cli._submission_problems(_doc(plan), runner, "SUBSTANTIVE", state=state, baseline=baseline,
                                    warnings=warnings)


def _of_stage(problems, index):
    return [p for p in problems if f"stage {index} [stage.principle].source" in p]


def _review(store, plan, runner, verdict="pass", session="gn", scope=None):
    from agentctl import cli
    import hashlib

    digest = hashlib.sha256(open(plan, "rb").read()).hexdigest()
    return cli.cmd_plan_review(
        ns(session=session, target=None, scope=scope, verdict=verdict, reviewer="thinker", concerns=None,
           concern_ids=None, note="", regression_command=None, plan_digest=digest),
        store=store, runner=runner)


def _refused(directive):
    return "plan-review refuses pass" in directive.detail


# --- the submission seam -----------------------------------------------------------------------------


def test_submission_refuses_defective_link(world):
    problems = _problems(_plan(world, statement1=f"Cache it {BAD}"), Judge())

    stage1 = _of_stage(problems, 1)
    assert len(stage1) == 1, problems
    assert "does not support the principle" in stage1[0] and "insufficient" in stage1[0]
    assert not _of_stage(problems, 2)


def test_submission_override_admits(world):
    plan = _plan(world, statement1=f"Cache it {BAD}", override1=REASON)

    assert not _of_stage(_problems(plan, Judge()), 1)
    assert _of_stage(_problems(_plan(world, statement1=f"Cache it {BAD}", name="q.toml"), Judge()), 1)


def test_submission_judge_down_warns(world):
    warnings = []

    problems = _problems(_plan(world, statement1=f"Cache it {BAD}"), _down, warnings=warnings)

    assert not _of_stage(problems, 1)
    assert any("grounding judge unavailable" in w for w in warnings), warnings


def test_exempt_stage_not_judged(store, world):
    plan = _plan(world, statement1=f"Cache it {BAD}")
    state = _approved(store, plan)
    _finish_stage_1(store)
    state = store.load("gn")

    exempt = _problems(plan, Judge(), state=state, baseline=_doc(plan))
    fresh = _problems(plan, Judge())

    assert not _of_stage(exempt, 1)
    assert _of_stage(fresh, 1)


def test_exempt_set_judges_edited_passed_principle(store, world):
    approved = _plan(world, statement1="s")
    state = _approved(store, approved)
    _finish_stage_1(store)
    state = store.load("gn")
    edited = _plan(world, statement1=f"Cache it {BAD}", name="edited.toml")

    problems = _problems(edited, Judge(), state=state, baseline=_doc(approved))

    assert _of_stage(problems, 1), problems


def test_exempt_set_same_at_review_and_submission(store, world, monkeypatch):
    from agentctl import cli

    plan = _plan(world, statement1=f"Cache it {BAD}")
    _approved(store, plan)
    _finish_stage_1(store)
    state = store.load("gn")
    callers = []
    real = cli._grounding_exempt_stages

    def spy(*a, **kw):
        callers.append(_stack_names())
        return real(*a, **kw)

    monkeypatch.setattr(cli, "_grounding_exempt_stages", spy)

    at_submission = _problems(plan, Judge(), state=state, baseline=_doc(plan))
    at_review = _review(store, plan, Judge())

    assert not _of_stage(at_submission, 1), at_submission
    assert not _refused(at_review), at_review.detail
    assert any("_submission_problems" in names for names in callers)
    assert any("cmd_plan_review" in names for names in callers)


def _stack_names():
    names, frame = set(), sys._getframe(1)
    while frame is not None:
        names.add(frame.f_code.co_name)
        frame = frame.f_back
    return names


# --- the given rule ----------------------------------------------------------------------------------


def test_unverified_given_refused(world):
    _given(world.leaves, "reg-given-stale", STALE)

    problems = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-given-stale"), Judge())

    stage1 = _of_stage(problems, 1)
    assert len(stage1) == 1, problems
    assert "regularity:reg-given-stale is an unverified given" in stage1[0]


def test_stale_given_refused_judge_down(world):
    _given(world.leaves, "reg-given-stale", STALE)

    problems = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-given-stale"), _down)

    assert any("unverified given" in p for p in _of_stage(problems, 1)), problems


def test_given_checked_by_stage_admitted(world):
    _given(world.leaves, "reg-given-stale", STALE, source="scripts/agentctl/plan.py")

    named = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-given-stale"), Judge())
    unnamed = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-given-stale", name="q.toml",
                              knowledge_refs=["scripts/agentctl/other.py"]), Judge())

    assert not _of_stage(named, 1), named
    assert any("unverified given" in p for p in _of_stage(unnamed, 1))


def test_fresh_given_admitted(world):
    _given(world.leaves, "reg-given-fresh", TODAY)

    problems = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-given-fresh"), Judge())

    assert not _of_stage(problems, 1), problems


def test_given_order_derived_checked(world):
    _order_derived(world.leaves, "reg-order-old", "the order says how things are", verified=STALE)
    _order_derived(world.leaves, "reg-order-new", "the order says how things are", verified=TODAY)

    old = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-order-old"), Judge())
    new = _problems(_plan(world, source1=f"{GROUNDED}; regularity:reg-order-new", name="q.toml"), Judge())

    assert any("regularity:reg-order-old is an unverified given" in p for p in _of_stage(old, 1)), old
    assert not _of_stage(new, 1), new


# --- the ontology and the order quotation ------------------------------------------------------------


def test_ontology_ground_adequacy_judged(world):
    problems = _problems(_plan(world, source1=f"{GROUNDED}; ontology:onto-a"), Judge())

    stage1 = _of_stage(problems, 1)
    assert len(stage1) == 1, problems
    assert "inadequate_distinction" in stage1[0]


def test_order_derived_quote_judged(world):
    _order_derived(world.leaves, "reg-faithful", "the order says how things are")
    _order_derived(world.leaves, "reg-unfaithful", "UNFAITHFUL words the regularity does not say")

    problems = _problems(_plan(world), Judge())

    quote = [p for p in problems if "order quotation" in p]
    assert len(quote) == 1, problems
    assert "regularity:reg-unfaithful" in quote[0] and "does not say" in quote[0]


def test_order_quote_demand_flagged(world):
    _order_derived(world.leaves, "reg-fact", "the order says how things are")
    _order_derived(world.leaves, "reg-demand", "DEMAND: the user wants every stage listed")

    problems = _problems(_plan(world), Judge())

    quote = [p for p in problems if "order quotation" in p]
    assert len(quote) == 1, problems
    assert "regularity:reg-demand" in quote[0] and "states a demand" in quote[0]


# --- the essence -------------------------------------------------------------------------------------


def _present(store, plan, rendering, tmp):
    from agentctl import cli
    from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE

    _to_plan_ready(store, plan)
    file = tmp / "essence.md"
    file.write_text(rendering, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session="gn", kind=PLAN_PRESENTATION_KIND_ESSENCE, rendering_file=str(file), emit_skeleton=False),
        store=store)


def _essence(plan, *, overrides):
    from agentctl import cli, grounding_gate
    from agentctl.render import render_plan_grants

    doc = _doc(plan)
    parts = ["## Essence", render_plan_grants(doc, fmt="compact"), cli._generalizations_block(doc)]
    if overrides:
        parts.append(grounding_gate.overrides_block(doc))
    return "\n\n".join(parts)


def test_essence_lacks_overrides_refused(store, world):
    plan = _plan(world)

    refused = _present(store, plan, _essence(plan, overrides=False), world.tmp)

    assert refused.ok is False
    assert "Grounding overrides (for approval):" in refused.data["overrides_block"]
    accepted = _present(store, plan, _essence(plan, overrides=True), world.tmp)
    assert accepted.ok is True, accepted.detail


def test_overrides_block_lists_reason(store, world):
    plan = _plan(world, statement1=f"Cache it {BAD}", override1=REASON)

    refused = _present(store, plan, _essence(plan, overrides=False), world.tmp)

    block = refused.data["overrides_block"]
    assert "Grounding overrides (for approval):" in block
    assert f"- stage 1: {REASON}" in block
    accepted = _present(store, plan, _essence(plan, overrides=True), world.tmp)
    assert accepted.ok is True, accepted.detail


# --- check-coverage and the review bundles ------------------------------------------------------------


def test_coverage_reports_verdicts(store, world):
    from agentctl import cli

    plan = _plan(world, statement1=f"Cache it {BAD}")
    _to_plan_ready(store, plan)

    d = cli.cmd_check_coverage(ns(session="gn", new=plan), store=store, runner=Judge())

    assert "- stage 1: defective (insufficient)" in d.detail
    assert "- stage 2: sound" in d.detail
    assert d.data["grounding_verdicts"] == [
        line for line in d.detail.splitlines() if line.startswith("- stage ")]


def test_review_bundle_carries_verdicts(store, world, tmp_path):
    from agentctl import render

    plan = _plan(world, statement1=f"Cache it {BAD}")
    _to_plan_ready(store, plan)
    _problems(plan, Judge())
    doc = _doc(plan)

    unit = render.render_unit_review_bundle(doc, "unit:1", plan_sha256="0" * 64)
    pair = render.render_pair_review_bundle(doc, "2-1", plan_sha256="0" * 64, view_dir=tmp_path)

    for bundle in (unit, pair):
        assert "## Grounding verdicts" in bundle
        assert "- stage 1: defective (insufficient)" in bundle
        assert "- stage 2: sound" in bundle


# --- plan-review --------------------------------------------------------------------------------------


def test_pass_refused_on_defect(store, world):
    plan = _plan(world, statement1=f"Cache it {BAD}")
    _to_plan_ready(store, plan)

    refused = _review(store, plan, Judge())

    assert refused.ok is False and _refused(refused), refused.detail
    assert any("stage 1" in p and "insufficient" in p for p in refused.data["problems"])


def test_pass_allowed_after_override(store, world):
    plan = _plan(world, statement1=f"Cache it {BAD}", override1=REASON)
    _to_plan_ready(store, plan)

    allowed = _review(store, plan, Judge())

    assert not _refused(allowed), allowed.detail


def test_pass_allowed_judge_down(store, world):
    plan = _plan(world, statement1=f"Cache it {BAD}")
    _to_plan_ready(store, plan)

    allowed = _review(store, plan, _down)

    assert not _refused(allowed), allowed.detail
    assert any("grounding judge unavailable" in a for a in allowed.data.get("advisories", [])), allowed.data
