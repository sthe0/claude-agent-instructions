"""StageNorm: the stage's norm as one serializable object whose three digests replace
three hand-maintained field lists.

What must hold:
  - `review_digest` IS the whole-stage `stage_question_key` (persisted in
    `Question.disposed_at_key`, so every legacy stage must hash identically);
  - `interface_digest` IS `stage_interface_digest`, `carry_key`/`carry_digest` is what
    `stage_carry_key` returns, and those two functions are only delegates;
  - the carry identity sees an edge's element, artifact and delivery, not just the
    supplier index (the case the index-only key missed);
  - `Supply.delivery` is optional, closed-vocabulary and round-trips through state.

New symbols are imported inside test bodies so that on a tree without the module the
file fails on assertions, not at collection.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict

import pytest

from agentctl.plan import (
    _ELEMENT_FIELDS,
    PlanError,
    _leaf_values as plan_leaf_values,
    knowledge_place,
    load_plan,
    negative_control_place,
    parse_plan,
    preconditions_place,
    procedure_place,
    stage_carry_key,
    stage_interface_digest,
    stage_question_key,
)
from agentctl.state import Stage
from agentctl.text_shape import normalize_string

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
_PLANS_HOME = pathlib.Path.home() / ".claude-agent" / "plans"

_BARE = {
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


def _norm_cls():
    from agentctl.stage_norm import StageNorm
    return StageNorm


def _doc(*extra_stages, **overrides):
    first = dict(_BARE, **overrides)
    return parse_plan({"meta": {"task_id": "t"}, "stage": [first, *extra_stages]})


def _consumer(**edge):
    """A two-stage plan whose stage 2 is supplied by stage 1 through one edge."""
    second = dict(_BARE, index=2, title="Consumer", supplies=[dict(on=1, **edge)])
    doc = parse_plan({"meta": {"task_id": "t"}, "stage": [dict(_BARE), second]})
    return doc, doc.stages[1]


def _loadable_plans(paths):
    for path in paths:
        try:
            yield path, load_plan(path)
        except Exception:  # an unloadable fixture is not this test's subject
            continue


def _fixture_stages():
    paths = sorted(_FIXTURES.glob("*.toml")) + sorted((_FIXTURES / "plan_corpus").glob("*.toml"))
    stages = [(doc, s) for _, doc in _loadable_plans(paths) for s in doc.stages]
    assert len(stages) > 40, "the in-repo fixture plans should supply a real corpus"
    return stages


def _legacy_question_key(stage) -> str:
    """The whole-stage `stage_question_key` payload exactly as it was at 02c9e55f, before
    StageNorm: an independent reference, so review_digest cannot drift with its own
    implementation."""
    p = stage.principle
    payload = repr((
        stage.actor.executor,
        stage.actor.capability_required,
        tuple(sorted(stage.depends_on)),
        stage.criterion.done_criterion,
        stage.criterion.criterion_type,
        stage.criterion.verify_command,
        stage.criterion.expected_exit,
        stage.title,
        stage.subject.material,
        stage.subject.result,
        stage.subject.invariants,
        stage.means.means,
        stage.means.method,
        stage.conditions,
        ((p.statement, p.source, p.derivation, p.confidence, p.refutation)
         if p is not None else None),
        tuple((s.on, s.element, s.artifact) for s in stage.supplies),
        normalize_string(stage.criterion.verify_venue),
        normalize_string(stage.criterion.verify_kind),
        stage.criterion.landed,
        *((normalize_string(stage.criterion.verify_venue_at_final),)
          if stage.criterion.verify_venue_at_final else ()),
        *knowledge_place(stage),
        *preconditions_place(stage),
        *procedure_place(stage),
        *negative_control_place(stage),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- StageNorm as data --------------------------------------------------------------

def test_norm_round_trips_through_json_with_every_digest_intact():
    StageNorm = _norm_cls()
    for doc, stage in _fixture_stages():
        norm = StageNorm.from_stage(stage, doc=doc)
        again = StageNorm.from_dict(json.loads(json.dumps(norm.to_dict())))
        assert again == norm, f"stage {stage.index} of {doc.meta.task_id}"
        assert again.review_digest() == norm.review_digest()
        assert again.carry_digest() == norm.carry_digest()


def test_from_dict_refuses_an_unknown_field_instead_of_dropping_it():
    StageNorm = _norm_cls()
    doc, stage = next(iter(_fixture_stages()))
    payload = StageNorm.from_stage(stage, doc=doc).to_dict()
    payload["methd"] = "typo"
    with pytest.raises(ValueError, match="methd"):
        StageNorm.from_dict(payload)


def test_norm_is_built_from_a_stage_alone_and_records_supplier_interfaces():
    StageNorm = _norm_cls()
    doc, consumer = _consumer(element="knowledge")
    supplier = doc.stages[0]
    norm = StageNorm.from_stage(consumer, {1: supplier})
    assert norm.supplier_interfaces == ((1, StageNorm.from_stage(supplier).interface_digest()),)
    assert norm.edges == ((1, "knowledge", None, None),)
    assert StageNorm.from_stage(consumer).supplier_interfaces == ()


# --- the three digests are projections of the keys they replace ---------------------

def test_review_digest_is_the_whole_stage_question_key_for_every_fixture_stage():
    StageNorm = _norm_cls()
    for doc, stage in _fixture_stages():
        digest = StageNorm.from_stage(stage).review_digest()
        assert digest == _legacy_question_key(stage), f"stage {stage.index} of {doc.meta.task_id}"
        assert digest == stage_question_key(stage)


def test_review_digest_is_the_question_key_for_the_plans_in_the_home_directory():
    StageNorm = _norm_cls()
    paths = sorted(_PLANS_HOME.glob("*.toml"))[:60]
    if not paths:
        pytest.skip("no local plans to sample")
    checked = 0
    for _, doc in _loadable_plans(paths):
        for stage in doc.stages:
            assert StageNorm.from_stage(stage).review_digest() == _legacy_question_key(stage)
            checked += 1
    assert checked or pytest.skip("no local plan loaded")


def test_interface_digest_equals_stage_interface_digest_including_a_blank_interface():
    StageNorm = _norm_cls()
    for doc, stage in _fixture_stages():
        if stage.subject.result.strip() and stage.criterion.done_criterion.strip():
            assert StageNorm.from_stage(stage).interface_digest() == stage_interface_digest(doc, stage)
    blank = _doc(expected_result_image="  ")
    stage = blank.stages[0]
    assert StageNorm.from_stage(stage, doc=blank).interface_digest() == stage_interface_digest(blank, stage)
    edited = _doc(expected_result_image="  ", method="a different method")
    assert stage_interface_digest(blank, stage) != stage_interface_digest(edited, edited.stages[0]), (
        "a blank-interface stage hashes its full brief, so a method edit moves it"
    )


def test_a_blank_interface_without_a_doc_is_refused_not_guessed():
    StageNorm = _norm_cls()
    stage = _doc(expected_result_image="  ").stages[0]
    with pytest.raises(ValueError, match="blank interface"):
        StageNorm.from_stage(stage).interface_digest()


def test_the_legacy_functions_delegate_to_the_norm():
    StageNorm = _norm_cls()
    doc, consumer = _consumer(element="knowledge", artifact="k.md")
    assert stage_carry_key(consumer) == StageNorm.from_stage(consumer).carry_key()
    assert stage_interface_digest(doc, consumer) == StageNorm.from_stage(consumer, doc=doc).interface_digest()
    assert stage_question_key(consumer) == StageNorm.from_stage(consumer).review_digest()
    assert StageNorm.from_stage(consumer).carry_digest() == hashlib.sha256(
        repr(stage_carry_key(consumer)).encode("utf-8")).hexdigest()


# --- the carry identity sees typed edges ---------------------------------------------

@pytest.mark.parametrize("a, b", [
    (dict(element="knowledge"), dict(element="material")),
    (dict(element="knowledge", artifact="a.md"), dict(element="knowledge", artifact="b.md")),
    (dict(element="knowledge"), dict(element="knowledge", artifact="a.md")),
    (dict(element="knowledge", delivery="report"), dict(element="knowledge", delivery="continuation")),
    (dict(element="knowledge"), dict(element="knowledge", delivery="report")),
])
def test_carry_digest_moves_when_an_edge_is_retyped_at_the_same_supplier(a, b):
    StageNorm = _norm_cls()
    one, two = _consumer(**a)[1], _consumer(**b)[1]
    assert one.depends_on == two.depends_on
    assert StageNorm.from_stage(one).carry_digest() != StageNorm.from_stage(two).carry_digest()


def test_carry_digest_ignores_the_order_the_edges_are_declared_in():
    StageNorm = _norm_cls()
    third = dict(_BARE, index=3, title="Third")
    def build(order):
        edges = {1: dict(on=1, element="knowledge"), 2: dict(on=2, element="material")}
        consumer = dict(_BARE, index=4, title="Consumer", supplies=[edges[i] for i in order])
        second = dict(_BARE, index=2, title="Second")
        doc = parse_plan({"meta": {"task_id": "t"}, "stage": [dict(_BARE), second, third, consumer]})
        return doc.stages[3]
    assert StageNorm.from_stage(build((1, 2))).carry_digest() == StageNorm.from_stage(build((2, 1))).carry_digest()


def test_carry_digest_is_stable_and_blind_to_the_principle_and_material():
    StageNorm = _norm_cls()
    base = _doc().stages[0]
    other = _doc(material="something else entirely").stages[0]
    assert StageNorm.from_stage(base).carry_digest() == StageNorm.from_stage(other).carry_digest()
    assert StageNorm.from_stage(base).carry_digest() == StageNorm.from_stage(_doc().stages[0]).carry_digest()
    assert StageNorm.from_stage(base).review_digest() != StageNorm.from_stage(other).review_digest()


# --- Supply.delivery -----------------------------------------------------------------

def test_delivery_is_optional_and_a_legacy_edge_carries_none():
    _, consumer = _consumer(element="knowledge")
    assert [s.delivery for s in consumer.supplies] == [None]
    legacy = asdict(consumer)
    for supply in legacy["supplies"]:
        del supply["delivery"]
    assert [s.delivery for s in Stage.from_dict(legacy).supplies] == [None]


@pytest.mark.parametrize("delivery", ["artifact", "continuation", "report"])
def test_each_vocabulary_delivery_parses_and_survives_the_state_round_trip(delivery):
    _, consumer = _consumer(element="knowledge", artifact="k.md", delivery=delivery)
    assert [s.delivery for s in consumer.supplies] == [delivery]
    restored = Stage.from_dict(json.loads(json.dumps(asdict(consumer))))
    assert [s.delivery for s in restored.supplies] == [delivery]


def test_an_unknown_delivery_is_a_plan_error_naming_the_stage_edge_and_vocabulary():
    with pytest.raises(PlanError) as exc:
        _consumer(element="knowledge", delivery="telepathy")
    message = str(exc.value)
    assert "stage 2" in message and "stage 1" in message and "telepathy" in message
    for allowed in ("artifact", "continuation", "report"):
        assert allowed in message


def test_declaring_a_delivery_moves_the_review_digest_and_an_absent_one_does_not():
    StageNorm = _norm_cls()
    plain = _consumer(element="knowledge")[1]
    typed = _consumer(element="knowledge", delivery="report")[1]
    assert StageNorm.from_stage(plain).review_digest() == _legacy_question_key(plain)
    assert StageNorm.from_stage(typed).review_digest() != StageNorm.from_stage(plain).review_digest()


def test_a_declared_delivery_moves_the_material_element_key_and_an_absent_one_does_not():
    plain = _consumer(element="knowledge")[1]
    typed = _consumer(element="knowledge", delivery="report")[1]
    assert stage_question_key(typed, "material") != stage_question_key(plain, "material")
    assert stage_question_key(typed, "result") == stage_question_key(plain, "result")
    assert stage_question_key(plain, "material") == hashlib.sha256(repr((
        "material", tuple(plan_leaf_values(plain, p) for p in _ELEMENT_FIELDS["material"]),
    )).encode("utf-8")).hexdigest()
