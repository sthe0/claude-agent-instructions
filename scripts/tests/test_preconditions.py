"""Conditions of the transformation, separate from the preconditions of starting.

Defect 6 of the SMD act-modelling rework. A stage had ONE field for two different
questions — what must already be true before it may start, and what must hold of the world
for its own transformation to go through — so the second was routinely spent on the first's
degenerate case, "the stage before me is done", which `depends_on` already records
structurally. The plan then declared no transformation conditions at all while appearing to.

The remedy is two halves that must arrive TOGETHER, and their simultaneity is what these
tests pin as much as either half alone:

  * STRUCTURAL, and unconditional: `preconditions` is a place of its own, required of a
    substantive stage at the submission seam. It cannot be a loader requirement — see
    submission.py's module docstring for why every new requirement binds there.
  * JUDGED, and fail-open: a `conditions` exhausted by restating `depends_on` is refused,
    with a message naming `preconditions` as where the sentence belongs. A structural
    prefilter proposes and a model disposes (conditions.py); no reachable judge means no
    refusal at all.

Refusing a restatement while NOT requiring `preconditions` would be a trap — the engine
telling an author to move a sentence into a field the grade never gave them. That is why
the judged refusal fires only inside the substantive branch that also demands the field,
and why a change that keeps one half and drops the other is a defect even if the suite's
other tests stay green.

One test here pins a move that was REJECTED: `gates.replan_coverage_blockers` still reads
the PRESERVE haystack from `conditions` + `invariants`, and `preconditions` was deliberately
NOT added to it. A later tidying that narrows the haystack to invariants alone — or widens
it to the new field — turns that test red rather than silently changing which corrected
plans clear the coverage gate.
"""
from __future__ import annotations

import json
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

import pytest

from agentctl import cli, gates, plan as plan_mod
from agentctl.conditions import restatement_prefilter
from agentctl.dispatch import RunResult
from agentctl.plan import (
    diff_plans,
    preconditions_place,
    stage_carry_key,
    stage_question_key,
)
from agentctl.state import (
    Actor,
    Criterion,
    Critique,
    Means,
    Node,
    Stage,
    Subject,
    Supply,
)
from agentctl.submission import submission_violations

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER

# A restatement: everything it says, `depends_on = [1]` already said.
RESTATEMENT = "Stage 1 is complete and its output has landed."
# A genuine transformation condition: nothing in it is a fact about an earlier stage.
GENUINE = "The live state store is writable and the corpus fixture directory is present."

_PLAN = """
[meta]
task_id = "pc"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage that goes first"
executor = "in_thread"
expected_result_image = "The parser reads the new key."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
material = "m1"
means = "bash"
method = "run"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
conditions = "The tree is clean."
preconditions = "The branch is checked out."
invariants = "n1"
capability_required = "cap"
material_refs = ["scripts/agentctl/plan.py"]
knowledge_refs = ["scripts/agentctl/state.py"]
knowledge = "how the loader stays lenient"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = "the stage under test"
executor = "in_thread"
expected_result_image = "The seam refuses a plan that omits the place."
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "pytest -q"
material = "m2"
means = "bash"
method = "run"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
conditions = {conditions}
{preconditions}invariants = "the canon checkout stays read-only"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/conditions.py"]
knowledge = "where a submission requirement may bind"
# The edge is element-bearing, and `depends_on = [1]` is what it replaces: the submission
# grade now refuses an edge that states an ordering without stating a provision
# (test_supply_edges.py). It derives the same `depends_on = [1]` the restatement prefilter
# below reads, so what these tests pin is unchanged by the typing.
[[stage.supplies]]
on = 1
element = "result"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK


def ns(**kw):
    return Namespace(**kw)


def _write_plan(path: Path, *, conditions: str, preconditions: str | None) -> str:
    line = "" if preconditions is None else f"preconditions = {json.dumps(preconditions)}\n"
    path.write_text(
        _PLAN.format(conditions=json.dumps(conditions), preconditions=line),
        encoding="utf-8",
    )
    return str(path)


def _judge(verdict: str):
    """A judge runner that always answers `verdict`."""
    def run(argv, timeout=None):
        return RunResult(0, verdict + "\n", "")
    return run


def _judge_unavailable(argv, timeout=None):
    """The judge cannot be reached — the shape a missing/failing `claude -p` takes."""
    return RunResult(1, "", "no such model")


@pytest.fixture(autouse=True)
def _advisor_on(monkeypatch):
    """The seam resolves its judge through advisor.resolve_enabled, whose documented
    force-on knob is this env var. Set explicitly so these tests do not depend on the
    machine's config.md advisor-mode."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")


def _submit(store, plan_path, runner, session="pc"):
    cli.cmd_start(ns(session=session, task="pc", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store, runner=runner)


def _problems(d) -> list[str]:
    return list(d.data.get("problems", []))


# --- the structural half: the place is required, unconditionally ------------


def test_substantive_stage_without_preconditions_is_refused_at_submission(store, tmp_path):
    """The place exists only if something insists on it. Without this refusal
    `preconditions` is an optional field nobody fills, and the defect survives with a new
    key beside it — every stage still writing its starting requirements into `conditions`,
    or writing none at all.

    Refused at SUBMISSION and not at load: a loader-side requirement is retroactive over
    plans already approved (submission.py's module docstring). The session staying at
    PLANNING is half the assertion — a refusal that advanced the node would strand it with
    an armed gate and no edge back."""
    plan = _write_plan(tmp_path / "p.toml", conditions=GENUINE, preconditions=None)

    d = _submit(store, plan, _judge("CONDITION"))

    assert d.ok is False
    assert d.action == "fix_plan"
    assert store.load("pc").node == Node.PLANNING.value
    assert any("missing 'preconditions'" in p and "stage 2" in p for p in _problems(d)), (
        _problems(d)
    )


def test_preconditions_is_absent_from_the_loader_requirement_set(tmp_path):
    """The requirement must NOT have leaked into `plan._validate_substantive_stage`, the
    loader-side enumerator. A plan missing `preconditions` has to LOAD — strictly, and
    without raising — because seven in-session call sites re-read plans the session already
    accepted, and three attestations are computed over their exact bytes."""
    plan = _write_plan(tmp_path / "p.toml", conditions=GENUINE, preconditions=None)

    doc = plan_mod.load_plan(plan, strict=True)

    assert doc.stages[1].preconditions is None
    assert "preconditions" not in plan_mod._SUBSTANTIVE_STAGE_FIELDS


# --- the judged half: a prefilter proposes, a model disposes, silence accepts ---


def test_conditions_restatement_is_refused_and_names_preconditions(store, tmp_path):
    """The refusal's message is the load-bearing part, not the refusal. An author told
    only "this is wrong" has nowhere to put the sentence; the message must name the field
    that now exists for it — and the same submission requires that field, so the place is
    always there to move into."""
    plan = _write_plan(tmp_path / "p.toml", conditions=RESTATEMENT, preconditions="p2")

    d = _submit(store, plan, _judge("RESTATEMENT"))

    assert d.ok is False
    restatements = [p for p in _problems(d) if "only restates" in p]
    assert len(restatements) == 1, _problems(d)
    assert "stage 2" in restatements[0]
    assert "preconditions" in restatements[0]


def test_conditions_restatement_is_accepted_when_the_judge_is_unavailable(store, tmp_path):
    """Fail-open, and this is the direction that matters most on this check: the verdict
    it feeds REFUSES a plan. The same bytes the test above turns away must submit here,
    with no judge to ask — byte-identical to the check not existing. A prefilter is never
    allowed to refuse on its own."""
    plan = _write_plan(tmp_path / "p.toml", conditions=RESTATEMENT, preconditions="p2")
    assert restatement_prefilter(RESTATEMENT, [1]), "fixture drift: this no longer prefilters"

    d = _submit(store, plan, _judge_unavailable)

    assert d.ok is True
    assert d.marker == "PLAN-READY"
    assert store.load("pc").node == Node.PLAN_READY.value
    assert not [p for p in _problems(d) if "only restates" in p]

    # The same property one layer down, where it is a defaults question: a caller that
    # passes no judge at all gets the list it got before this check existed. Every
    # pre-existing caller of the seam is such a caller.
    doc = plan_mod.load_plan(plan)
    assert not [p for p in submission_violations(doc, session_weight_class="substantive")
                if "only restates" in p]


def test_genuine_conditions_never_reach_the_preconditions_refusal(store, tmp_path):
    """Both stubs, and the first one is the sharper claim. Under a judge stubbed to say
    RESTATEMENT about everything, a genuine condition still submits — so the acceptance
    comes from the prefilter declining to ASK, not from a lenient answer. Inspecting the
    calls is what proves it: a widened prefilter that started asking about ordinary
    conditions would put every substantive plan's fate in a model's hands.

    (The seam runs other advisory judges on the same runner, so the assertion is that no
    call carried THIS question, not that no call was made at all.)"""
    plan = _write_plan(tmp_path / "p.toml", conditions=GENUINE, preconditions="p2")

    asked: list[str] = []

    def counting_judge(argv, timeout=None):
        asked.extend(argv)
        return RunResult(0, "RESTATEMENT\n", "")

    d = _submit(store, plan, counting_judge)
    assert d.ok is True
    assert not [a for a in asked if "PRECONDITIONS" in a], (
        "the prefilter asked a model about a plain transformation condition"
    )
    assert not [p for p in _problems(d) if "only restates" in p]

    assert _submit(store, plan, _judge_unavailable, session="pc2").ok is True


# --- the move that was rejected --------------------------------------------


def test_preserved_item_in_conditions_still_satisfies_the_coverage_haystack(tmp_path):
    """`gates.replan_coverage_blockers` reads its PRESERVE haystack from `conditions` +
    `invariants`, and this stage did NOT touch it. Narrowing it to invariants alone was
    considered and rejected: a similarity an author carried into a stage's conditions is
    carried, and a gate that stopped seeing it would block correct replans in the middle
    of the difficulty cycle, which is the worst possible moment.

    The second half pins the other direction — `preconditions` is deliberately not in the
    haystack. Adding it would let a similarity be "preserved" by a sentence about what was
    true before the stage started, which is not where a preserved invariant lives."""
    # A sentinel that appears in NEITHER stage's `invariants` in the template, so the
    # conditions half of the haystack is the only thing that can answer for it.
    preserved = "the frozen plan corpus is never migrated"
    critique = Critique(functional_ground="fg", replanning_task="rt",
                        invariants_to_preserve=[preserved])

    doc = plan_mod.load_plan(
        _write_plan(tmp_path / "p.toml", conditions=preserved, preconditions="p2")
    )
    assert gates.replan_coverage_blockers(doc, doc, critique) == []

    moved = plan_mod.load_plan(
        _write_plan(tmp_path / "q.toml", conditions=GENUINE, preconditions=preserved)
    )
    blockers = gates.replan_coverage_blockers(moved, moved, critique)
    assert len(blockers) == 1
    assert preserved in blockers[0]


# --- the standing obligation: one new field, all four change decisions ------


def _stage(tag, *, preconditions=None, vvaf=None):
    return Stage(
        index=2,
        title=f"title-{tag}",
        subject=Subject(material="m", result="r", invariants="i"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="d",
                            verify_command="pytest -q", expected_exit=0,
                            verify_venue_at_final=vvaf),
        conditions="c",
        preconditions=preconditions,
        supplies=[Supply(on=1)],
    )


def test_preconditions_enters_every_change_decision_function(tmp_path):
    """A field that enters one change decision and not the others is how a correction gets
    silently dropped — an author's edit that the engine reads as `no_change`, or a PASSED
    stage carried forward across a replan that rewrote it. Four decisions, and this asserts
    all four rather than a representative one.

    Two encoding properties are asserted with them, because they are what makes the
    addition safe on already-live sessions. UNDECLARED contributes nothing, so a plan
    predating the field keeps the exact key it had (stage_question_key is persisted in
    Question.disposed_at_key and compared across processes — a `... or ""` default would
    flip every disposed question of every live session to a spurious staleness blocker).
    And the value is NESTED, so it cannot flatten into the conditional splice beside it."""
    absent, declared = _stage("a"), _stage("a", preconditions="the tree is clean")

    # (0) declared-only: absent contributes nothing at all to any key
    assert preconditions_place(absent) == ()
    assert len(stage_carry_key(declared)) == len(stage_carry_key(absent)) + 1

    # (0b) no collision with the other conditionally-spliced field
    assert stage_carry_key(_stage("a", preconditions="delivery")) != stage_carry_key(
        _stage("a", vvaf="delivery")
    )

    # (1) carry-forward: a rewritten stage may not keep its PASSED outcome
    assert stage_carry_key(absent) != stage_carry_key(declared)
    # (2) question staleness: an answer given against the old bytes is stale
    assert stage_question_key(absent) != stage_question_key(declared)
    # (3) refinement vs no_change: the edit must not vanish in the diff
    old = plan_mod.load_plan(_write_plan(tmp_path / "old.toml", conditions=GENUINE,
                                         preconditions="p2"))
    new = deepcopy(old)
    new.stages[1].preconditions = "p2, and the advisor is reachable"
    assert diff_plans(old, new) == "refinement"
    # (4) approve/replan re-materialization: the live stage must track the plan bytes
    live = _stage("a")
    cli._apply_refined_stage_fields(live, declared)
    assert live.preconditions == declared.preconditions
