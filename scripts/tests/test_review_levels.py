"""Two review levels: the pair review judges stage orders, the method review judges a sub-plan.

A pair review of a plan on a new-form order is shown each stage as an order (what it delivers and
the cover it carries) and never who executes it or how the work is divided; a method concern is
routed to the method review of the stage's sub-plan, a review record of kind `method` in the
record's `reviews`. That record blocks the presentation of a new-form plan and the start of a
decided-later stage, and nothing else: a decided-later stage is presented without one and an
old-form plan is never asked for it. Nothing reaches a model judge or a model CLI.
"""
from __future__ import annotations

import copy

import pytest

from agentctl import gates, render, sub_plan
from agentctl.plan import load_plan
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

import test_method_statement_rule as methods
import test_subplan_presentation_dispatch as base
from test_subplan_presentation_dispatch import (  # noqa: F401  (fixtures)
    SESSION, Spawns, _agent_approves_stage_resources, _agree_estimate, _described, _executing,
    _plan_approval_sets_resources, _put, _rec, _start_blockers, home_store, ns,
)


@pytest.fixture
def store(home_store):
    return home_store


def _described_doc(tmp_path, *, place="the engine"):
    """The two-stage new-form plan with its stages in the description form (`place` set) or, with
    `place=None`, in the old form."""
    doc = load_plan(methods._two_stage_plan(tmp_path))
    for stage in doc.stages:
        stage.place = place
    return doc


def _bundle(doc, pair, tmp_path):
    return render.render_pair_review_bundle(doc, pair, plan_sha256="0" * 64, view_dir=tmp_path)


def test_pair_review_receives_stage_orders_and_cover_and_does_not_report_executor_or_division_of_work(
        tmp_path):
    doc = _described_doc(tmp_path)
    for stage in doc.stages:
        stage.actor.executor = "spawn:developer"
    first = doc.stages[0]
    reported = ("**Executor:**", "spawn:developer", "**Place:**", "**Binding conditions", "**Means",
                "**Method:**", "**Procedure")
    for pair in ("base-1", "2-1"):
        text = _bundle(doc, pair, tmp_path)
        assert f"Stage {first.index}: {first.title}" in text
        assert first.criterion.done_criterion in text
        for word in reported:
            assert word not in text, (pair, word)
    assert render.METHOD_CONCERN_ROUTING in _bundle(doc, "2-1", tmp_path)


def test_a_stage_in_the_old_form_is_shown_as_before(tmp_path):
    doc = _described_doc(tmp_path, place=None)
    for stage in doc.stages:
        stage.actor.executor = "spawn:developer"
    text = _bundle(doc, "2-1", tmp_path)
    assert "**Executor:** spawn:developer" in text
    assert render.node_file_text(doc, 1) == render.render_stage_brief(doc, 1)


def test_a_method_concern_is_routed_to_the_executor_level_review(tmp_path):
    new_form = _bundle(_described_doc(tmp_path), "2-1", tmp_path)
    old = load_plan(methods._plan_file(tmp_path, two_sided=False, name="old"))
    old_text = render.render_stage_brief(old, 1)
    assert "executor-level method review" in new_form
    assert render.METHOD_CONCERN_ROUTING not in old_text


def test_node_text_of_a_description_stage_is_the_order_and_binds_the_pair_review_alone(tmp_path):
    """The pair review's currency keys on the node text: an edit of how the stage is executed
    leaves it standing, an edit of what the stage delivers moves it."""
    doc = _described_doc(tmp_path)
    before = render.node_file_text(doc, 1)
    moved = copy.deepcopy(doc)
    moved.stages[0].actor.executor = "spawn:tech-writer"
    assert render.node_file_text(moved, 1) == before
    changed = copy.deepcopy(doc)
    changed.stages[0].criterion.done_criterion += " and the log is empty"
    assert render.node_file_text(changed, 1) != before


# --- the method review on the record -------------------------------------------------------


def _essence(store, venued_fixtures_dir, tmp_path, monkeypatch, **kw):
    return base._essence_session(store, venued_fixtures_dir, tmp_path, monkeypatch, **kw)


def _record_without_review():
    record = _rec(1)
    record.reviews = []
    return record


def _unplanned(store, plan):
    return gates.unplanned_stage_blockers(store.load(SESSION), plan, store=store)


def test_review_record_through_subplan_field_lifts_blocker(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    plan = _essence(store, venued_fixtures_dir, tmp_path, monkeypatch)
    _put(store, _record_without_review())
    assert any("no method review" in line for line in _unplanned(store, plan))

    asking_again = _record_without_review()
    asking_again.reviews = [{"kind": sub_plan.METHOD_REVIEW_KIND, "verdict": "revise", "by": "r"}]
    _put(store, asking_again)
    assert any("no method review" in line for line in _unplanned(store, plan))

    other_kind = _record_without_review()
    other_kind.reviews = [{"kind": "pair", "verdict": "pass", "by": "r"}]
    _put(store, other_kind)
    assert any("no method review" in line for line in _unplanned(store, plan))

    unbound = _record_without_review()
    unbound.reviews = [{"kind": sub_plan.METHOD_REVIEW_KIND, "verdict": "pass", "by": "r"}]
    _put(store, unbound)
    assert any("no method review" in line for line in _unplanned(store, plan))

    stale = _record_without_review()
    stale.reviews = [{"kind": sub_plan.METHOD_REVIEW_KIND, "verdict": "pass", "by": "r",
                      "bound_to": "0" * 64}]
    _put(store, stale)
    assert any("no method review" in line for line in _unplanned(store, plan))

    _put(store, base._method_reviewed(_record_without_review()))
    assert not any("method review" in line for line in _unplanned(store, plan))


def _framed_record():
    from types import SimpleNamespace

    from test_stage_order_subplan import _frame, _session

    state = _session("p", plan_stack=[_frame(1)])
    record = _record_without_review()
    sub_plan.store(state, record, 0)
    return state, record, lambda r: SimpleNamespace(meta=SimpleNamespace(sub_plan=r))


@pytest.mark.parametrize("verdict, written", [("pass", True), ("override", True), ("revise", False)])
def test_the_engine_writes_the_method_review_of_the_record_a_passing_whole_plan_review_read(
        verdict, written):
    """The writer is the engine's: a passing verdict (or the user's override) is stored into the
    record the reviewer read, bound to that record's digest, and lifts the dispatch blocker; a
    verdict asking for a revision writes nothing."""
    state, record, doc_of = _framed_record()
    assert not sub_plan.method_review_recorded(
        sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]))
    assert sub_plan.record_method_review(
        state, None, doc_of(record), verdict=verdict, reviewer="thinker",
        plan_sha256="a" * 64, at=5.0) is written
    stored = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"])
    assert sub_plan.method_review_recorded(stored) is written
    if written:
        [review] = stored.reviews
        assert review["kind"] == sub_plan.METHOD_REVIEW_KIND and review["verdict"] == verdict
        assert review["bound_to"] == sub_plan.approval_digest(stored) and review["plan_sha256"] == "a" * 64


def test_the_method_review_is_not_written_into_a_record_the_executor_changed_since_it_was_read():
    state, record, doc_of = _framed_record()
    read = _record_without_review()
    read.method = "a method the reviewer read, rewritten since"
    assert sub_plan.record_method_review(
        state, None, doc_of(read), verdict="pass", reviewer="thinker", plan_sha256="a" * 64,
        at=5.0) is False
    assert sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]).reviews == []
    assert sub_plan.record_method_review(
        state, None, None, verdict="pass", reviewer="thinker", plan_sha256="a" * 64, at=5.0) is False


def test_a_record_cannot_carry_the_method_review_it_is_judged_by():
    """A `method` review arriving in the submitted record is refused before anything is stored: the
    executor cannot lift its own dispatch blocker; a review of any other kind is carried as before
    and an engine-written review survives the executor's rewrite."""
    state, record, doc_of = _framed_record()

    class Carrier:
        def load(self, sid):
            return state

    forged = _record_without_review()
    forged.reviews = [{"kind": sub_plan.METHOD_REVIEW_KIND, "verdict": "pass", "by": "executor"}]
    with pytest.raises(sub_plan.SubPlanError, match="written by the engine"):
        sub_plan.record_carried_by(state, doc_of(forged), Carrier())
    assert sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]).reviews == []

    assert sub_plan.record_method_review(
        state, None, doc_of(record), verdict="pass", reviewer="thinker", plan_sha256="a" * 64,
        at=5.0)
    rewritten = _record_without_review()
    rewritten.method = "a rewritten method"
    sub_plan.record_carried_by(state, doc_of(rewritten), Carrier())
    stored = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"])
    assert stored.method == "a rewritten method" and not sub_plan.method_review_recorded(stored)
    assert stored.reviews == []


@pytest.mark.parametrize("rewrite, keeps", [
    (lambda r: setattr(r, "method", "a rewritten method"), False),
    (lambda r: setattr(r, "procedure", "1. a rewritten procedure"), False),
    (lambda r: setattr(r, "conditions", "- a rewritten condition"), False),
    (lambda r: setattr(r, "means", "Bash"), False),
    (lambda r: r.resource_request.append({"stage": 1, "allow": ["Read"], "add_dirs": []}), False),
    (lambda r: setattr(r, "estimate", sub_plan.Estimate(minutes=99, usd=9.0, author="a",
                                                         confirmer="a", basis="sized again")), True),
    (lambda r: setattr(r, "building_place", sub_plan.BuildingPlace(session="x", pushed=1, popped=2)), True),
], ids=["method", "procedure", "conditions", "means", "resource-request", "estimate", "building-place"])
def test_the_store_keeps_the_method_review_through_a_rewrite_only_of_what_the_review_did_not_judge(
        rewrite, keeps):
    """`store` writes a record that carries no review over the stored one: the engine's review
    stays only when the authored parts it is bound to are unchanged (the estimate and the place
    the sub-plan was built in are not among them)."""
    state, record, doc_of = _framed_record()
    assert sub_plan.record_method_review(
        state, None, doc_of(record), verdict="pass", reviewer="thinker", plan_sha256="a" * 64, at=5.0)
    rewritten = _record_without_review()
    rewrite(rewritten)
    sub_plan.store(state, rewritten, 0)
    stored = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"])
    assert sub_plan.method_review_recorded(stored) is keeps
    assert bool(stored.reviews) is keeps


def test_missing_method_review_blocks_presentation_of_new_form_plan_but_not_decided_later_or_old_form(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    plan = _essence(store, venued_fixtures_dir, tmp_path, monkeypatch)
    _put(store, _record_without_review())
    assert any("method review" in line for line in _unplanned(store, plan))

    lazy_plan = base._new_form_plan(venued_fixtures_dir, tmp_path, lazy=(1, 2))
    assert _unplanned(store, lazy_plan) == []

    old_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    assert _unplanned(store, old_plan) == []


def _decided_later_start(store, venued_fixtures_dir, tmp_path, record):
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True)
    _put(store, record)
    state = store.load(SESSION)
    _agree_estimate(state)
    store.save(state)
    _plan_approval_sets_resources(store)
    assert base._present_and_approve(store, tmp_path).ok


def test_decided_later_stage_without_method_review_refused_at_dispatch_until_recorded(
        store, venued_fixtures_dir, tmp_path):
    _decided_later_start(store, venued_fixtures_dir, tmp_path, _record_without_review())
    blockers = _start_blockers(store)
    assert any("no method review" in line for line in blockers), blockers
    assert not any("estimate" in line for line in blockers), blockers

    state = store.load(SESSION)
    bound = sub_plan.approval_digest(sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"]))
    sub_plan.add_review(state, 1, {"kind": sub_plan.METHOD_REVIEW_KIND, "verdict": "pass", "by": "r",
                                   "bound_to": bound}, level=0)
    store.save(state)
    assert not any("method review" in line for line in _start_blockers(store))


@pytest.mark.parametrize("in_thread", [False, True], ids=["spawned", "in-thread"])
@pytest.mark.parametrize("given, refused_for", [
    ("approval", "estimate"),
    ("agreement", "approve"),
    ("both", None),
])
def test_each_of_the_two_conditions_alone_leaves_the_decided_later_stage_refused(
        store, venued_fixtures_dir, tmp_path, in_thread, given, refused_for):
    """The user's sub-plan approval alone, and the estimate confirmed and agreed with the method
    review alone, both refuse the stage; with both it is dispatched. The method review sits in the
    record (`_rec`) throughout, so what is left to refuse is the other condition."""
    _executing(store, venued_fixtures_dir)
    _described(store, lazy=True, in_thread=in_thread)
    _put(store, _rec(1))
    if given in ("agreement", "both"):
        state = store.load(SESSION)
        _agree_estimate(state)
        store.save(state)
        _plan_approval_sets_resources(store)
    if given in ("approval", "both"):
        assert base._present_and_approve(store, tmp_path).ok
    if given == "both":
        assert _agent_approves_stage_resources(store).ok
    blockers = _start_blockers(store)
    if refused_for is None:
        assert blockers == []
    else:
        assert blockers and any(refused_for in line for line in blockers), blockers
        assert not any("method review" in line for line in blockers)
