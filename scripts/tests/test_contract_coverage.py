"""Stage 10 of smd-act-defects-8: the planner-contract doc coverage walk, and a
mutation-derived enumerator over the four functions that decide whether a stage-field
edit is seen by any part of the replan/carry-forward/question-invalidation machinery.

Part A ties the planner's authoring contract (SKILL.md + policy.md) to submission.py's
own three field tables — the same guard test_rejected_shapes.py gives the SUBMISSION
seam's refusal behavior, aimed instead at the DOCUMENTATION an author reads before ever
reaching that seam.

Part B answers a different question than test_renormalization.py's
`test_the_stage_residual_exhausts_the_stage_s_field_set` — that one asks whether the
RENORMALIZATION residual (gates._renorm_stage_residual, the re-sequencing gate at
`replan`) accounts for every Stage leaf. This asks whether the four ordinary
CHANGE-DECISION functions do: `plan.stage_carry_key` (PASSED carry-forward),
`cli._apply_refined_stage_fields` (what a refinement replan copies onto the live
stage), `plan.diff_plans` (refinement-vs-substantive classification), and
`plan.stage_question_key` (premise invalidation). The four do NOT cover the same set
by design — several asymmetries below are load-bearing (e.g. `stage_carry_key` omits
`principle`/`supplies.element` because carry-forward never needed them, per its own
docstring) — so this is a CONTRACT test pinning each function's ACTUAL, current
behavior per leaf, not an aspirational "everything must be seen everywhere" assertion.
Where the pinned behavior looks like a gap rather than a deliberate asymmetry, that is
recorded in the leaf's comment below and in this stage's own report, not silently
fixed here — this file's job is to make the current shape visible and guarded, not to
re-decide it.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from agentctl.cli import _apply_refined_stage_fields
from agentctl.plan import (
    PlanDoc, PlanMeta, diff_plans, stage_carry_key, stage_question_key,
)
from agentctl.state import (
    Actor, Criterion, LandedSpec, Means, Outcome, Principle, Stage, Subject, Supply,
)
from agentctl.submission import (
    _ORDER_PARTS, _SUBSTANTIVE_META_FIELDS, _SUBSTANTIVE_SUBMISSION_FIELDS,
)

from dataclass_domain import leaf_paths

ROOT = Path(__file__).resolve().parents[2]

# --- Part A: the planner contract documents every submission-seam field -----


def _contract_text() -> str:
    skill = (ROOT / "skills" / "specializations" / "planner" / "SKILL.md").read_text()
    policy = (ROOT / "skills" / "specializations" / "planner" / "policy.md").read_text()
    return skill + "\n" + policy


#: A markdown list-item line — top-level `- `/`N. ` or an indented sub-bullet. Every
#: genuine field explanation in SKILL.md/policy.md takes this shape (a glossary
#: bullet or a numbered Plan-format item); a label mentioned only in running prose
#: that is not itself a list item is exactly the coincidental case the review flagged
#: (`method`/`goal`/`coverage`/... as ordinary English, or an unrelated identifier).
_LIST_ITEM_RE = re.compile(r"^\s*(?:-\s+|\d+\.\s+)")

#: Splits a bullet at its first em dash. Every field-glossary bullet in this contract
#: is shaped `- **\`label\`** — description`: the label sits in the NAMING term
#: before the dash, the rationale after it. A backticked identifier that shows up
#: only in the elaboration — e.g. `method`/`procedure` used as an aside inside the
#: pre-existing "Procedure:" bullet's prose, or `final_check` inside an *example*
#: embedded in the Order-coverage bullet's elaboration — is a passing reference, not
#: the bullet naming that field, and the dash split is what tells the two apart.
_TERM_SPLIT_RE = re.compile(r"—")


def _documenting_bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if _LIST_ITEM_RE.match(line)]


def _label_documented(label: str, text: str) -> bool:
    """True when `label` is named — appears inside a backticked code span within the
    naming term of a markdown list-item line — not merely somewhere across ~235
    lines of prose. This is the tightened form of the old `label in _contract_text()`
    bare substring test, which passed vacuously on any incidental appearance of an
    ordinary English word (`method`, `goal`, `knowledge`, `coverage`, `requirements`,
    ...) or an unrelated code identifier (e.g. `plan.goal` inside unrelated
    premise-gate prose) anywhere in the concatenated SKILL.md + policy.md text."""
    for bullet in _documenting_bullets(text):
        term = _TERM_SPLIT_RE.split(bullet, maxsplit=1)[0]
        for span in re.findall(r"`([^`]+)`", term):
            if label in span:
                return True
    return False


@pytest.mark.parametrize("label", tuple(l for _a, l, _s in _SUBSTANTIVE_SUBMISSION_FIELDS))
def test_every_stage_submission_field_is_named_in_the_planner_contract(label):
    assert _label_documented(label, _contract_text()), (
        f"{label!r} is a substantive-stage submission requirement "
        f"(submission._SUBSTANTIVE_SUBMISSION_FIELDS) with no mention in the planner's "
        f"own authoring contract — an author following SKILL.md/policy.md alone would "
        f"never learn the seam refuses a plan omitting it"
    )


@pytest.mark.parametrize("label", tuple(l for _a, l in _SUBSTANTIVE_META_FIELDS))
def test_every_meta_submission_field_is_named_in_the_planner_contract(label):
    assert _label_documented(label, _contract_text()), (
        f"{label!r} is a substantive-plan [meta] submission requirement "
        f"(submission._SUBSTANTIVE_META_FIELDS) with no mention in the planner contract"
    )


@pytest.mark.parametrize("name,_why", _ORDER_PARTS)
def test_every_order_part_is_named_in_the_planner_contract(name, _why):
    assert _label_documented(name, _contract_text()), (
        f"[meta.order].{name} is a required order part (submission._ORDER_PARTS) with "
        f"no mention in the planner contract"
    )


def test_the_coverage_map_is_named_in_the_planner_contract():
    assert _label_documented("coverage", _contract_text())


def test_the_material_refs_knowledge_refs_overlap_smell_is_documented():
    text = _contract_text()
    assert "material_refs" in text and "knowledge_refs" in text
    assert "smell" in text, (
        "the contract must say that a symbol in BOTH material_refs and knowledge_refs "
        "is a smell the stage's own prose must justify — this is a documented "
        "convention, not a submission refusal, so nothing in submission.py enforces it"
    )


# --- Part B: the change-decision function family, per Stage leaf ------------


def _stage(index=2, **over):
    fields = dict(
        index=index, title="the stage under test",
        subject=Subject(material="m", result="r", invariants="inv",
                         material_refs=["a/b.py"], knowledge_refs=["c/d.py"]),
        means=Means(means="bash", method="run", procedure="1. a. 2. b."),
        actor=Actor(executor="in_thread", capability_required="cap", cost_tier="medium"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="dc",
            verify_command="pytest -q", expected_exit=0, observation="",
            verify_venue="delivery", verify_kind="landed",
            landed=LandedSpec(target="main", delivered_stage=1, remote="origin"),
            verify_venue_at_final="repo_root",
        ),
        principle=Principle(statement="s", source="src", derivation="der",
                             confidence="high", refutation="r"),
        conditions="cond", preconditions="pre", knowledge="know",
        supplies=[Supply(on=1, element="result", artifact="x")],
        output_artifacts=["out/path.py"], outcome=Outcome(), control=None,
    )
    fields.update(over)
    return Stage(**fields)


def _doc(stage):
    meta = PlanMeta(task_id="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", weight_class="substantive",
                     external_research="none applies", repo_root=None,
                     delivery_worktree=None, final_check=[], order=None)
    return PlanDoc(meta=meta, stages=[_stage(index=1), stage])


def _get(obj, dotted):
    """Walk a dotted `leaf_paths` path, transparently indexing into a `list[Supply]`
    at [0] — the shape `leaf_paths` flattens a list-of-dataclasses field to (one
    element's fields, unindexed), matching `dataclass_domain`'s own convention."""
    for part in dotted.split("."):
        if isinstance(obj, list):
            obj = obj[0]
        obj = getattr(obj, part)
    return obj


def _set(obj, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        if isinstance(obj, list):
            obj = obj[0]
        obj = getattr(obj, part)
    if isinstance(obj, list):
        obj = obj[0]
    setattr(obj, parts[-1], value)


def _mutate(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return (value or "") + "-MUTATED"
    if isinstance(value, int):
        return value + 1
    if isinstance(value, list):
        return list(value) + ["MUTATED"]
    if value is None:
        return "MUTATED"
    raise AssertionError(f"no mutation strategy for {value!r}")


#: `index` is matched-on by every function in this family (which stage an edit
#: belongs to), not a field OF a stage's definition — excluded from the leaf domain
#: below the same way `renormalization_blockers`'s own residual excludes it.
_EXCLUDED_FROM_ENUMERATION = {"index"}

_CD = ("carry_key", "apply_refined", "diff_plans", "question_key")

#: Per Stage leaf, which of the four change-decision functions detect a change to it —
#: derived by mutation in the test below, not asserted from reading the code alone.
#: `diff_plans` is recorded as ONE column though it runs two internal comparisons
#: (`_structural_signature`, then the `_prose` closure) because `_prose` has no
#: standalone entry point to call in isolation; a leaf marked here detects via either.
#:
#: Four cells below are gaps rather than intentional asymmetries — cross-checked
#: against the source, not left to the mutation run's silence alone, and surfaced in
#: this stage's own report rather than fixed here (this stage's engine-code edit is
#: authorized for exactly one line, `verify_venue_at_final`'s copy, not these):
#:  - `actor.capability_required`: invisible to carry_key, apply_refined, AND
#:    diff_plans. A replan correcting ONLY the required capability diffs as
#:    'no_change' and the correction is silently dropped — the exact failure class
#:    this key family exists to close (diff_plans' own cost_tier/verify_venue
#:    precedent comments name this pattern explicitly).
#:  - `supplies.element` / `supplies.artifact`: invisible to carry_key BY DESIGN
#:    (matching stage_question_key's own docstring: "carry-forward never needed
#:    them"), but ALSO invisible to diff_plans, which is not a stated design —
#:    `_structural_signature` reads only `depends_on`, i.e. `.on`.
#:  - `output_artifacts`: invisible to ALL FOUR. Not copied by
#:    `_apply_refined_stage_fields`, so a refinement replan correcting a stage's
#:    declared output_artifacts leaves the LIVE stage stale; and undetected by
#:    diff_plans, so such an edit diffs as 'no_change' outright.
#:  - `principle.*`: not copied by `_apply_refined_stage_fields`. Within that
#:    function's own stated contract (COVER `stage_carry_key`, which does not read
#:    `principle` either — so not a violation of it), but it means a corrected
#:    principle never reaches the live stage on a refinement replan, only on a fresh
#:    submission.
_STAGE_LEAF_COVERAGE: dict[str, frozenset[str]] = {
    "title": frozenset(_CD),
    "subject.material": frozenset({"apply_refined", "question_key"}),
    "subject.result": frozenset(_CD),
    "subject.invariants": frozenset(_CD),
    "subject.material_refs": frozenset(_CD),
    "subject.knowledge_refs": frozenset(_CD),
    "means.means": frozenset(_CD),
    "means.method": frozenset(_CD),
    "means.procedure": frozenset(_CD),
    "actor.executor": frozenset(_CD),
    "actor.capability_required": frozenset({"question_key"}),
    "actor.cost_tier": frozenset({"apply_refined", "diff_plans"}),
    "criterion.criterion_type": frozenset(_CD),
    "criterion.done_criterion": frozenset(_CD),
    "criterion.verify_command": frozenset(_CD),
    "criterion.expected_exit": frozenset(_CD),
    "criterion.observation": frozenset(),
    "criterion.verify_venue": frozenset(_CD),
    "criterion.verify_kind": frozenset(_CD),
    "criterion.landed.target": frozenset(_CD),
    "criterion.landed.delivered_stage": frozenset(_CD),
    "criterion.landed.remote": frozenset(_CD),
    "criterion.verify_venue_at_final": frozenset(_CD),
    "principle.statement": frozenset({"question_key"}),
    "principle.source": frozenset({"question_key"}),
    "principle.derivation": frozenset({"question_key"}),
    "principle.confidence": frozenset({"question_key"}),
    "principle.refutation": frozenset({"question_key"}),
    "conditions": frozenset(_CD),
    "preconditions": frozenset(_CD),
    "knowledge": frozenset(_CD),
    "supplies.on": frozenset(_CD),
    "supplies.element": frozenset({"apply_refined", "question_key"}),
    "supplies.artifact": frozenset({"apply_refined", "question_key"}),
    "output_artifacts": frozenset(),
    "outcome.status": frozenset(),
    "outcome.actual": frozenset(),
    "outcome.fail_digests": frozenset(),
    "outcome.cost_usd": frozenset(),
    "outcome.duration_ms": frozenset(),
    "outcome.spawn_count": frozenset(),
    "outcome.delivered_head": frozenset(),
    "control": frozenset(),
}


def test_the_stage_leaf_coverage_map_is_exactly_the_stage_leaf_set():
    leaves = set(leaf_paths(Stage)) - _EXCLUDED_FROM_ENUMERATION
    assert leaves == set(_STAGE_LEAF_COVERAGE), (
        f"a Stage leaf is in neither this map nor its exclusion set: "
        f"{sorted(leaves - set(_STAGE_LEAF_COVERAGE))} unaccounted, "
        f"{sorted(set(_STAGE_LEAF_COVERAGE) - leaves)} listed but gone"
    )


@pytest.mark.parametrize("leaf,expected", sorted(_STAGE_LEAF_COVERAGE.items()))
def test_change_decision_coverage_is_pinned_by_mutation(leaf, expected):
    old_stage = _stage()
    new_stage = _stage()
    _set(new_stage, leaf, _mutate(_get(old_stage, leaf)))
    assert _get(old_stage, leaf) != _get(new_stage, leaf), "mutation was a no-op"

    detected = set()
    if stage_carry_key(old_stage) != stage_carry_key(new_stage):
        detected.add("carry_key")
    cur = copy.deepcopy(old_stage)
    _apply_refined_stage_fields(cur, new_stage)
    if _get(cur, leaf) != _get(old_stage, leaf):
        detected.add("apply_refined")
    if diff_plans(_doc(old_stage), _doc(new_stage)) != "no_change":
        detected.add("diff_plans")
    if stage_question_key(old_stage) != stage_question_key(new_stage):
        detected.add("question_key")

    assert detected == expected, (
        f"{leaf}: mutating it is detected by {sorted(detected)}, but "
        f"_STAGE_LEAF_COVERAGE says {sorted(expected)} — either the coverage map is "
        f"stale or a change-decision function's behavior moved"
    )
