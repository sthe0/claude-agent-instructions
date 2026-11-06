"""The grounding check at the plan-submission seam.

A stage of a substantive plan rests on knowledge: its `[stage.principle].source` names only
regularity, experience and case leaves. The check resolves each ref structurally -- by slug,
corpus area and frontmatter `schema:` -- so a leaf that merely EXISTS under the cited slug is
not enough, a path or a quotation is not a ref, and a principle (a norm, not a ground) is
refused with the regularities it is grounded in.

Every test builds its leaves as files in a tmp corpus read by the real loader, imports the
modules it exercises inside its body, and mixes one valid ref with each bad item, so a
mutation of one rule turns exactly the tests of that rule red by assertion.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER

TASK = "grounding-task"
GOOD = "regularity:reg-a"

_LEAF = "---\nname: {slug}\ndescription: a leaf\nschema: {schema}\n{extra}---\nbody\n"


def _leaf(root: Path, rel: str, schema: str, extra: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_LEAF.format(slug=path.stem, schema=schema, extra=extra), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A global corpus, a project memory dir and a delivery worktree, all under tmp_path;
    the engine's own leaves root is pointed at the tmp corpus."""
    from agentctl import grounding

    leaves = tmp_path / "engine" / "memory-global" / "leaves"
    _leaf(leaves, "regularities/reg-a.md", "regularity/v1")
    _leaf(leaves, "experience/exp-a.md", "difficulty/v1")
    _leaf(leaves, "principles/cases/case-a.md", "case/v1")
    _leaf(leaves, "regularities/old-leaf.md", "leaf/v1")
    _leaf(leaves, "principles/prin-a.md", "principle/v1", "grounded_in: [regularity:reg-a]\n")

    repo = tmp_path / "repo"
    project = repo / ".claude" / "agent-memory"
    _leaf(project, "experience/exp-p.md", "difficulty/v1")
    _leaf(project, "principles/prin-p.md", "principle/v1")

    worktree = tmp_path / "wt"
    staged = worktree / "memory-global" / "leaves"
    staged.mkdir(parents=True)

    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: leaves)
    return Namespace(leaves=leaves, repo=repo, project=project, worktree=worktree,
                     staged=staged, tmp=tmp_path)


def _corpus(world):
    from agentctl import grounding
    return grounding.load_ground_corpus(world.leaves, world.project)


def _problems(source, world):
    from agentctl import grounding
    return grounding.source_problems(source, _corpus(world))


# --- the structural rule, per kind of refusal -------------------------------


def test_leaf_v1_refused(world):
    problems = _problems(f"{GOOD}; regularity:old-leaf", world)
    assert len(problems) == 1
    assert "regularity:old-leaf" in problems[0]
    assert "reg-a" not in problems[0]


def test_kind_mismatch_refused(world):
    problems = _problems(f"{GOOD}; case:reg-a", world)
    assert len(problems) == 1
    assert "case:reg-a" in problems[0]


def test_missing_slug_refused(world):
    problems = _problems(f"{GOOD}; regularity:no-such-leaf", world)
    assert len(problems) == 1
    assert "no-such-leaf" in problems[0]


def test_path_source_refused(world):
    problems = _problems(f"{GOOD}; scripts/agentctl/plan.py", world)
    assert len(problems) == 1
    assert "scripts/agentctl/plan.py" in problems[0]


def test_quote_source_refused(world):
    problems = _problems(f'{GOOD}; "a quoted phrase of the order"', world)
    assert len(problems) == 1
    assert "a quoted phrase of the order" in problems[0]


def test_empty_source_refused(world):
    assert _problems("", world)
    assert _problems("   ", world)


def test_principle_ref_refused(world):
    problems = _problems(f"{GOOD}; principle:prin-a", world)
    assert len(problems) == 1
    assert "principle:prin-a" in problems[0]


def test_principle_refusal_names_grounds(world):
    problems = _problems("principle:prin-a", world)
    assert len(problems) == 1
    assert "cite the regularities it is grounded in: regularity:reg-a" in problems[0]


def test_project_principle_refused(world):
    problems = _problems(f"{GOOD}; project:principle:prin-p", world)
    assert len(problems) == 1
    assert "project:principle:prin-p" in problems[0]


def test_regularity_accepted(world):
    assert _problems("regularity:reg-a", world) == []


def test_experience_accepted(world):
    assert _problems(f"{GOOD}; experience:exp-a", world) == []


def test_case_accepted(world):
    assert _problems(f"{GOOD}; case:case-a", world) == []


# --- the plans the seam reads -----------------------------------------------

_PLAN = """
[meta]
task_id = "{task}"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"
repo_root = "{repo}"
{delivery}""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage that goes first"
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
statement = {statement1}
source = {source1}
derivation = "der"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = {title2}
executor = "in_thread"
expected_result_image = "The seam refuses a plan whose ground is not knowledge."
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "pytest -q"
negative_control = "false"
material = "m2 (traces to R1)"
means = "bash"
method = "Extend the existing submission enumerator rather than adding a parallel one."
procedure = {procedure2}
conditions = "The advisor is reachable."
preconditions = "Stage 1's field exists."
invariants = "the frozen corpus plans keep loading byte-for-byte"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/procedure.py"]
knowledge = "where a submission requirement may bind"
[[stage.supplies]]
on = 1
element = "result"
[stage.principle]
statement = "s"
source = {source2}
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK


def _write_plan(world, name="p.toml", *, source1=GOOD, source2="experience:exp-a",
                title2="the stage under test", statement1="s",
                procedure2="1. Read submission.py's loop. 2. Add the row. 3. Run pytest -q.",
                worktree=False, task=TASK) -> str:
    delivery = f'delivery_worktree = "{world.worktree}"\n' if worktree else ""
    text = _PLAN.format(
        task=task, repo=world.repo, delivery=delivery,
        source1=json.dumps(source1), source2=json.dumps(source2),
        title2=json.dumps(title2), statement1=json.dumps(statement1),
        procedure2=json.dumps(procedure2))
    path = world.tmp / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _doc(path):
    from agentctl.plan import load_plan
    return load_plan(path, strict=False)


def _convert_to_leaf_v1(world, slug="reg-a"):
    """The cited leaf is converted away from regularity/v1 AFTER the plan was approved."""
    path = world.leaves / "regularities" / f"{slug}.md"
    path.write_text(_LEAF.format(slug=slug, schema="leaf/v1", extra=""), encoding="utf-8")


def ns(**kw):
    return Namespace(**kw)


def _judge_unavailable(argv, timeout=None):
    from agentctl.dispatch import RunResult
    return RunResult(1, "", "no such model")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _to_plan_ready(store, plan_path, session="gn"):
    from agentctl import cli
    cli.cmd_start(ns(session=session, task="gn", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store,
                               runner=_judge_unavailable)


def _approved(store, plan_path, session="gn"):
    from agentctl import cli
    d = _to_plan_ready(store, plan_path, session)
    assert d.ok is True, d.data
    cli.cmd_approve(ns(session=session, by="user"), store=store, runner=_judge_unavailable)
    return store.load(session)


def _finish_stage_1(store, session="gn"):
    from agentctl.state import StageStatus
    state = store.load(session)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    store.save(state)


def _grounding_problems_of(d):
    return [p for p in d.data.get("problems", []) if "[stage.principle].source" in p]


def test_submission_seam_refuses(store, world):
    plan = _write_plan(world, source2=f"{GOOD}; regularity:old-leaf")

    d = _to_plan_ready(store, plan)

    assert d.ok is False
    problems = _grounding_problems_of(d)
    assert len(problems) == 1, d.data
    assert "stage 2" in problems[0]
    assert "regularity:old-leaf" in problems[0]


def test_frozen_stage_exempt(world):
    from agentctl import grounding

    doc = _doc(_write_plan(world, source1="regularity:old-leaf"))
    corpus = _corpus(world)

    judged = grounding.grounding_violations(doc, corpus=corpus)
    exempt = grounding.grounding_violations(doc, corpus=corpus, exempt_stages=frozenset({1}))

    assert any(p.startswith("stage 1 ") for p in judged)
    assert exempt == []


def test_renormalize_exempts_finished(store, world):
    from agentctl import cli

    _approved(store, _write_plan(world))
    _finish_stage_1(store)
    _convert_to_leaf_v1(world)

    d = cli.cmd_replan(
        ns(session="gn", renormalize=True, coverage_waiver=None, normalization_waiver=None,
           cost_log=None,
           plan=_write_plan(world, "q.toml", procedure2="1. Add the row. 2. Run the suite.")),
        store=store, runner=_judge_unavailable)

    assert _grounding_problems_of(d) == []
    assert d.ok is True, d.data


def test_replan_exempts_finished(store, world):
    from agentctl import cli

    _approved(store, _write_plan(world))
    _finish_stage_1(store)
    _convert_to_leaf_v1(world)

    d = cli.cmd_replan(ns(session="gn", plan=_write_plan(world, "q.toml", title2="renamed")),
                       store=store, runner=_judge_unavailable)

    assert _grounding_problems_of(d) == []
    assert d.ok is True, d.data


def test_approve_edit_exempts_finished(store, world):
    from agentctl import cli

    plan = _write_plan(world)
    d = _to_plan_ready(store, plan)
    assert d.ok is True, d.data
    _finish_stage_1(store)
    _convert_to_leaf_v1(world)
    _write_plan(world, title2="renamed in place")

    approved = cli.cmd_approve(ns(session="gn", by="user"), store=store,
                               runner=_judge_unavailable)

    assert _grounding_problems_of(approved) == []
    assert approved.ok is True, approved.data


def test_edited_finished_principle_judged(store, world):
    from agentctl import cli

    _approved(store, _write_plan(world))
    _finish_stage_1(store)
    _convert_to_leaf_v1(world)

    d = cli.cmd_replan(
        ns(session="gn", plan=_write_plan(world, "q.toml", statement1="a different statement")),
        store=store, runner=_judge_unavailable)

    problems = _grounding_problems_of(d)
    assert any(p.startswith("stage 1 ") for p in problems), d.data


def test_approve_edited_finished_principle_judged(store, world):
    from agentctl import cli

    _approved(store, _write_plan(world))
    _finish_stage_1(store)
    _convert_to_leaf_v1(world)
    _write_plan(world, statement1="a different statement")

    state = store.load("gn")
    violations = cli._refresh_caches_from_plan_path(
        state, runner=_judge_unavailable, baseline=cli._baseline_plan_for_tier(store, state))

    assert any(p.startswith("stage 1 ") and "[stage.principle].source" in p
               for p in violations), violations


# --- the corpora the seam reads ---------------------------------------------


def test_project_tier_resolved(world):
    from agentctl import cli

    doc = _doc(_write_plan(world, source2=f"{GOOD}; project:experience:exp-p"))

    assert cli._grounding_problems(doc) == []


def test_project_ref_resolves_project_first_then_global(world):
    """A project ref resolves to the global leaf when the project tier lacks the slug; the miss
    that remains is a slug present in neither tier."""
    assert _problems(f"{GOOD}; project:regularity:reg-a", world) == []

    problems = _problems(f"{GOOD}; project:regularity:reg-nowhere", world)

    assert len(problems) == 1
    assert "project:regularity:reg-nowhere" in problems[0]


def test_same_slug_in_another_area_does_not_shadow(world):
    _leaf(world.leaves, "regularities/dup.md", "regularity/v1")
    _leaf(world.leaves, "experience/dup.md", "difficulty/v1")

    assert _problems("regularity:dup; experience:dup", world) == []


def test_missing_engine_leaves_root_is_one_named_problem(world, monkeypatch):
    from agentctl import cli, grounding

    missing = world.tmp / "no-such-root"
    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: missing)
    doc = _doc(_write_plan(world))

    problems = cli._grounding_problems(doc)

    assert len(problems) == 1
    assert str(missing) in problems[0]


def test_staged_generalization_resolved(world):
    from agentctl import cli

    _leaf(world.staged, "regularities/reg-new.md", "regularity/v1",
          f"introduced_by: {TASK}\n")
    doc = _doc(_write_plan(world, source2="regularity:reg-new", worktree=True))

    assert cli._grounding_problems(doc) == []


def test_staged_foreign_refused(world):
    from agentctl import cli

    _leaf(world.staged, "regularities/reg-new.md", "regularity/v1",
          "introduced_by: some-other-task\n")
    doc = _doc(_write_plan(world, source2=f"{GOOD}; regularity:reg-new", worktree=True))

    problems = cli._grounding_problems(doc)

    assert len(problems) == 1
    assert "regularity:reg-new" in problems[0]
