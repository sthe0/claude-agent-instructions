"""Element scoping of the question-invalidation key.

`stage_question_key` answers one question — "did the bytes this question was answered
against change?" — and answered it for the WHOLE stage, so editing any field of a stage
re-blocked every question bound to any of its elements. A stage is edited on nearly every
replan, so the price was paid on nearly every replan, and the route out
(`question-rebind --confirm-still-valid`) is a re-confirmation the author has no way to
perform honestly for an element that did not move: the verb gets exercised as a formality,
which is worse than the blocker it clears.

What is pinned here, in the order the file walks it:

  1. the declared mapping is TOTAL over the target vocabulary, and its COMPLEMENT over
     `Stage`'s own field set is declared too — so neither a new vocabulary name nor a new
     stage field can end up silently invalidating nothing;
  2. an element key moves for its own fields and stands still for its siblings';
  3. every element contribution is TAGGED, so two elements carrying identical text (or,
     more commonly, both undeclared) cannot collide — the collision this key family has
     already been bitten by twice (`plan.procedure_place`);
  4. the whole-stage form is byte-identical to what it was before scoping existed, both on
     a synthetic stage declaring none of the conditional places and on nine real stages;
  5. a stamp written before scoping existed still discharges rule 12, evidenced against
     the real question bag of a real session rather than argued.

The evidence for 4 and 5 is `fixtures/premise_stamps_before_element_scoping.json`: the
stage declarations and premise bag of a live session, copied out of its state file before
that session was reset. Those 22 `disposed_at_key` values were stamped by the engine as it
stood before this change, and no migration could ever rewrite them — a stamp is a digest of
a plan version that may no longer exist anywhere. The fixture's one honest limit: all nine
of its stages declare `knowledge`, `preconditions` AND `procedure`, so it does not exercise
the absent-conditional-place identity for those three. `test_a_stage_declaring_no_
conditional_place_keeps_its_pinned_digest` is the only cover for that case here (with
`test_knowledge_place.py` and `test_preconditions.py` covering it for the whole-stage form).
"""
from __future__ import annotations

import json
import pathlib

import pytest
from dataclass_domain import leaf_paths

from agentctl import premise
from agentctl.plan import (
    _ELEMENT_FIELDS,
    _WHOLE_STAGE_DEFINITION,
    parse_plan,
    stage_element_keys,
    stage_question_key,
)
from agentctl.state import Stage
from agentctl.text_shape import ELEMENT_NAMES, WHOLE_STAGE_ELEMENT

_RULE_12 = "changed since this question was disposed"

_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "premise_stamps_before_element_scoping.json"
)

# Every leaf of `Stage` that NO element of the target vocabulary claims, each with the
# reason it is unclaimed. Modelled on `test_renormalization.py`'s `_STAGE_RESIDUAL_
# COVERAGE`: totality over the vocabulary is not enough on its own, because a payload
# field no name claims is a hole in the other direction — a rewrite of it would invalidate
# nothing. `supplies` was exactly that until it was attached to `material`.
_UNCLAIMED_STAGE_LEAVES = {
    "index": "the stage's identity, not its content — a question binds THROUGH it "
             "(`stage:<n>.<element>`), so a changed index is rule 2's dangling edge",
    "title": "the stage's name, not any element's content. No target names it, and an "
             "answer given against an element does not rest on what the stage is called",
    "actor.cost_tier": "an execution price, not a place an answer can rest on "
                       "(test_effort_declaration.py pins its exclusion from both forms)",
    "criterion.observation": "written at record-result time, not authored in the plan — "
                             "the criterion's declaration is what a question is answered "
                             "against, not the observation that later satisfied it",
    "output_artifacts": "the paths a stage promises to produce. An answer about the "
                        "result rests on `subject.result`, which does claim its element",
    "control": "the FIELD is written by `record-result --control` after the fact, never "
               "authored in the plan. The vocabulary NAME `control` is separate and is "
               "mapped, to the whole stage",
    "outcome.status": "the mutable execution record, not the declaration",
    "outcome.actual": "the mutable execution record, not the declaration",
    "outcome.fail_digests": "the mutable execution record, not the declaration",
    "outcome.cost_usd": "the mutable execution record, not the declaration",
    "outcome.duration_ms": "the mutable execution record, not the declaration",
    "outcome.spawn_count": "the mutable execution record, not the declaration",
    "outcome.delivered_head": "the mutable execution record, not the declaration",
}

_BARE_STAGE = {
    "index": 1,
    "title": "Bare stage",
    "executor": "in_thread",
    "material": "the file as it stands",
    "expected_result_image": "the file with the flag added",
    "criterion_type": "measurable",
    "done_criterion": "the flag is present",
    "means": "Edit",
    "method": "add the flag",
}

# The whole-stage digest of `_BARE_STAGE` — a stage declaring NONE of the conditional
# places (no knowledge, no refs, no preconditions, no procedure, no principle, no
# supplies, no verify_venue_at_final) — recorded before element scoping was added.
_BARE_STAGE_WHOLE_KEY = "84c1140cd998cae6b9308a05e0ffe26b7d0a4123c2606e9af835a3ef3370d177"


def _stage(**overrides):
    raw = dict(_BARE_STAGE)
    raw.update(overrides)
    return parse_plan({"meta": {"task_id": "t"}, "stage": [raw]}).stages[0]


def _dependent_stage(supply):
    """Stage 2 of a two-stage plan, supplied by stage 1 — a supply edge cannot be
    declared on a lone stage (`_validate_graph` rejects the dangling `on`)."""
    second = dict(_BARE_STAGE, index=2, title="Dependent stage", supplies=[supply])
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [dict(_BARE_STAGE), second]})
    return doc.stages[1]


def _researched(target, disposed_at_key):
    return premise.Question(
        id="q1", target=target, question="does this hold?", disposition="researched",
        own_research="read the surrounding code", answer="it holds for the stated reason",
        source="scripts/agentctl/plan.py", derivation="follows from the payload order",
        disposed_at_key=disposed_at_key,
    )


def _rule_12_blocks(question, stage_keys) -> bool:
    return any(
        _RULE_12 in b
        for b in premise.validate_questions([question], stage_keys=stage_keys)
    )


# --- 1. the mapping is total in both directions --------------------------------------

def test_the_element_mapping_is_total_over_the_target_vocabulary():
    assert set(_ELEMENT_FIELDS) == set(ELEMENT_NAMES), (
        "every name a question target may carry needs a declared contribution: a name "
        "with none would fall through to KeyError at dispose time, and a name quietly "
        "mapped to the whole stage would re-block its siblings' questions forever"
    )


def test_the_element_mapping_exhausts_the_stage_s_field_set():
    claimed = {
        path
        for paths in _ELEMENT_FIELDS.values()
        if paths is not _WHOLE_STAGE_DEFINITION
        for path in paths
    }
    assert claimed & set(_UNCLAIMED_STAGE_LEAVES) == set()
    assert claimed | set(_UNCLAIMED_STAGE_LEAVES) == set(leaf_paths(Stage)), (
        "a leaf of Stage is either claimed by an element of the target vocabulary or "
        "recorded in _UNCLAIMED_STAGE_LEAVES with the reason no element claims it — "
        "record the decision rather than letting a new field invalidate nothing"
    )


def test_a_vocabulary_name_without_a_contribution_is_refused_not_widened():
    with pytest.raises(KeyError):
        stage_question_key(_stage(), "a_name_the_mapping_does_not_have")


# --- 2. an element key moves for its own fields only ---------------------------------

def test_editing_one_element_leaves_its_siblings_keys_unchanged():
    before = stage_element_keys(_stage())
    after = stage_element_keys(_stage(method="add the flag, then run the linter"))
    assert before["method"] != after["method"]
    moved = {name for name in before if before[name] != after[name]}
    assert moved == {"method", WHOLE_STAGE_ELEMENT, "control", "order", "requirements"}


def test_a_supplies_rewrite_moves_the_material_key():
    """`supplies` is material handed over by another stage, so a rewritten supply edge
    must invalidate a question answered against the material — the hole that totality
    over the vocabulary alone would have left open."""
    plain = _dependent_stage({"on": 1, "element": "result"})
    respecified = _dependent_stage({"on": 1, "element": "result",
                                    "artifact": "the parsed table"})
    assert stage_question_key(plain, "material") != stage_question_key(
        respecified, "material")


def test_the_criterion_cluster_is_coarser_than_done_criterion():
    """Granularity follows the name the author chose, never finer: `criterion` covers the
    whole cluster, `done_criterion` only its own field."""
    verify_edited = _stage(verify_command="python3 -m pytest -q")
    assert stage_question_key(_stage(), "criterion") != stage_question_key(
        verify_edited, "criterion")
    assert stage_question_key(_stage(), "done_criterion") == stage_question_key(
        verify_edited, "done_criterion")

    criterion_edited = _stage(done_criterion="the flag is present and documented")
    assert stage_question_key(_stage(), "done_criterion") != stage_question_key(
        criterion_edited, "done_criterion")
    assert stage_question_key(_stage(), "criterion") != stage_question_key(
        criterion_edited, "criterion")


def test_the_three_fieldless_names_key_to_the_whole_stage():
    """`order` and `requirements` live on `[meta.order]` and `control` is written after
    the fact, so a question on one of those has no stage field of its own to bind to and
    binds to the stage's whole definition."""
    stage = _stage()
    whole = stage_question_key(stage)
    for name in ("order", "requirements", "control"):
        assert stage_question_key(stage, name) == whole


# --- 3. contributions are tagged -----------------------------------------------------

def test_elements_carrying_identical_text_key_differently():
    stage = _stage(means="the same sentence", method="the same sentence")
    assert stage_question_key(stage, "means") != stage_question_key(stage, "method")


def test_undeclared_elements_do_not_collide_with_each_other():
    keys = stage_element_keys(_stage())
    undeclared = ("invariants", "knowledge", "conditions", "preconditions", "procedure",
                  "capability", "principle")
    assert len({keys[name] for name in undeclared}) == len(undeclared)


# --- 4. the whole-stage form is unchanged --------------------------------------------

def test_a_stage_declaring_no_conditional_place_keeps_its_pinned_digest():
    """The V4 identity, now load-bearing for a second reason: it is what every stamp
    written before element scoping matches."""
    assert stage_question_key(_stage()) == _BARE_STAGE_WHOLE_KEY


def test_stage_element_keys_carries_the_reserved_whole_stage_entry():
    keys = stage_element_keys(_stage())
    assert keys[WHOLE_STAGE_ELEMENT] == stage_question_key(_stage())
    assert set(keys) == set(ELEMENT_NAMES) | {WHOLE_STAGE_ELEMENT}


# --- 5. real stamps, real verdicts ---------------------------------------------------

def _fixture():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_the_real_stages_keep_their_recorded_whole_stage_digests():
    data = _fixture()
    recomputed = {
        str(s.index): stage_question_key(s)
        for s in (Stage.from_dict(raw) for raw in data["stages"])
    }
    assert recomputed == data["whole_stage_keys"]


def test_every_real_stamp_keeps_the_verdict_it_had_before_scoping():
    """No stamp of a real session newly blocks, and none is newly discharged. Both halves
    matter: the first is the back-compatibility claim, the second is the guard against
    buying it by accepting anything at all."""
    data = _fixture()
    stages = [Stage.from_dict(raw) for raw in data["stages"]]
    element_keys = {s.index: stage_element_keys(s) for s in stages}
    whole_keys = {int(k): v for k, v in data["whole_stage_keys"].items()}
    questions = premise.questions_from_dicts(data["questions"])

    blocked_before, blocked_after = set(), set()
    for q in questions:
        parsed = premise.parse_target(q.target)
        assert parsed is not None
        kind, stage_index, _element = parsed
        if kind != "stage":
            continue
        if q.disposed_at_key != whole_keys[stage_index]:
            blocked_before.add(q.id)
        if _rule_12_blocks(q, element_keys):
            blocked_after.add(q.id)

    assert blocked_after == blocked_before
    assert blocked_before, "fixture must contain stamps that DO block, or it proves nothing"
    stage_bound = {q.id for q in questions if q.target.startswith("stage:")}
    assert stage_bound - blocked_before, (
        "fixture must contain stamps that discharge, or the equality above is vacuous"
    )


def test_a_real_stamp_survives_an_edit_to_a_sibling_element():
    """The payoff, on real bytes: take a real question that discharges today, rewrite an
    element of its stage OTHER than the one it targets, and it must still discharge —
    where before this change any edit to that stage re-blocked it."""
    data = _fixture()
    stages = [Stage.from_dict(raw) for raw in data["stages"]]
    whole_keys = {int(k): v for k, v in data["whole_stage_keys"].items()}
    by_index = {s.index: s for s in stages}

    discharging = [
        q for q in premise.questions_from_dicts(data["questions"])
        if q.target.startswith("stage:")
        and q.disposed_at_key == whole_keys[premise.parse_target(q.target)[1]]
    ]
    assert discharging

    for q in discharging:
        _kind, stage_index, element = premise.parse_target(q.target)
        stage = by_index[stage_index]
        edited = Stage.from_dict(
            dict(next(raw for raw in data["stages"] if raw["index"] == stage_index),
                 title=stage.title + " (retitled)")
        )
        rebound = _researched(q.target, stage_question_key(stage, element))
        assert not _rule_12_blocks(rebound, {stage_index: stage_element_keys(edited)}), (
            f"a question on stage {stage_index}'s {element} must survive an edit "
            f"elsewhere in that stage"
        )
