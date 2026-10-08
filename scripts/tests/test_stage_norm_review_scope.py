"""Review scope follows what a stage's consumers rely on, not the stage's whole text.

A stage whose review digest moved is always in scope; its direct consumers join it only
when its INTERFACE digest moved (`gates.plan_review_delta`). A topological pair whose
bundle shows the service only as its declared product is current or stale by that
product (`service_interface`), not by the service's construction. A baseline that
predates the interface digests cannot show an interface unchanged, so it widens to
consumers and never narrows.

Fixture: `_bounded_data` -- stage 2 relies on 1, stage 3 relies on 2, stage 4 is alone.
Pairs: base-plan, plan-3, plan-4, 2-1, 3-2 (pair `b-s`: base b is the consumer, service
s its supplier)."""
from __future__ import annotations

import dataclasses

import pytest

from agentctl import gates
from agentctl.state import PlanReview

from test_plan_review_topo import (  # noqa: F401  (fixtures + helpers)
    _blockers,
    _bounded_data,
    _delta,
    _effects,
    _hand_baseline,
    _stage,
    _w,
    make_env,
)


def _construction(index):
    return lambda d: d["stage"][index - 1].update(method=f"method-{index} edited")


def _interface(index):
    return lambda d: d["stage"][index - 1].update(expected_result_image=f"img {index} edited")


def _scope(env):
    return gates.plan_review_delta(env.state(), env.doc())


def _stage_pass(env, index):
    """Record a stage-scoped pass. The directive's `ok` reports whether the whole gate
    cleared, which it need not -- another stage may still be owed -- so the record
    itself is what is checked."""
    env.record(f"stage:{index}", "pass")
    assert env.state().plan_stage_reviews[f"stage:{index}"].verdict == "pass"


@pytest.fixture
def whole_pass(make_env):
    """The bounded plan under a whole-plan pass that records per-stage interface digests."""
    env = make_env(_bounded_data())
    assert env.record("", "pass").ok
    return env


def test_construction_change_scopes_only_that_stage(whole_pass):
    whole_pass.edit(_construction(1))
    assert _scope(whole_pass) == (False, {1})


def test_interface_change_scopes_the_stage_and_its_direct_consumers_only(whole_pass):
    whole_pass.edit(_interface(1))
    assert _scope(whole_pass) == (False, {1, 2})


def test_interface_change_of_a_leaf_adds_no_consumer(whole_pass):
    whole_pass.edit(_interface(3))
    assert _scope(whole_pass) == (False, {3})


def test_two_stage_refinement_scopes_exactly_the_union_of_both_rules(whole_pass):
    def refine(d):
        _interface(2)(d)
        _construction(4)(d)

    whole_pass.edit(refine)
    assert _scope(whole_pass) == (False, {2, 3, 4})


def test_meta_change_is_a_whole_plan_review(whole_pass):
    whole_pass.edit(lambda d: d["meta"].update(goal="another goal"))
    assert _scope(whole_pass) == (True, set())


def test_legacy_baseline_without_interface_digests_includes_consumers(make_env):
    env = make_env(_bounded_data())
    _hand_baseline(env)
    env.edit(_construction(1))
    assert _scope(env) == (False, {1, 2})


def test_delta_names_a_stage_in_scope_only_as_a_consumer(whole_pass):
    whole_pass.edit(_interface(1))
    d = _delta(whole_pass)
    assert d.data["stages"] == [1, 2]
    assert d.data["consumer_of"] == {"2": [1]}
    assert "stage 2: consumer of 1" in d.detail
    assert "stage 1: consumer" not in d.detail


def test_consumer_pair_stays_current_after_a_service_construction_change(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    assert env.status("3-2") == "current"
    env.edit(_construction(2))
    assert env.status("3-2") == "current"
    assert env.status("2-1").startswith("stale:")


def test_consumer_pair_goes_stale_after_a_service_interface_change(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    env.edit(_interface(2))
    assert env.status("3-2").startswith("stale:")


def test_walk_stales_a_pair_for_its_service_end_only_when_the_interface_moved(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    assert env.record("", "pass").ok
    env.edit(_construction(2))
    assert "3-2" not in _w(env)
    assert "2-1" in _w(env)
    env.edit(_interface(2))
    assert "3-2" in _w(env)


def test_walk_under_a_legacy_baseline_stales_the_service_end_of_a_moved_stage(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    _hand_baseline(env)
    env.edit(_construction(2))
    assert "3-2" in _w(env)


def test_a_pair_record_current_before_stays_current_under_a_legacy_baseline(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    _hand_baseline(env)
    before = env.statuses()
    assert set(before.values()) == {"current"}
    assert _w(env) == []
    env.edit(_construction(4))
    after = env.statuses()
    assert {p for p, s in after.items() if s != "current"} <= {"plan-4"}
    assert _w(env) == ["plan-4"]


def test_a_source_service_is_still_stale_by_its_construction(make_env):
    env = make_env(_bounded_data())
    env.record_all()
    env.edit(_construction(1))
    assert env.status("2-1").startswith("stale:")


def _effects_on_stage2(resolver):
    return lambda d: d["stage"][1].update(effects=_effects(resolver))


def test_effects_only_edit_of_a_relying_service_keeps_its_consumer_pair_current(make_env):
    data = _bounded_data()
    data["stage"][1]["effects"] = _effects("r1")
    env = make_env(data)
    env.record_all()
    env.edit(_effects_on_stage2("r2"))
    assert env.status("3-2") == "current"
    assert env.status("2-1").startswith("stale:")


def test_effects_only_edit_moves_no_review_key_so_nothing_is_in_scope(whole_pass):
    whole_pass.edit(_effects_on_stage2("r2"))
    assert _scope(whole_pass) == (False, set())


def test_moved_consumer_with_a_current_stage_pass_still_owes_the_new_interface(whole_pass):
    whole_pass.edit(_construction(2))
    assert _scope(whole_pass) == (False, {2})
    _stage_pass(whole_pass, 2)
    assert _scope(whole_pass) == (False, set())

    whole_pass.edit(_interface(1))
    assert _scope(whole_pass) == (False, {1, 2})
    _stage_pass(whole_pass, 1)
    assert _scope(whole_pass) == (False, {2})


def test_consumer_discharged_by_its_own_stage_pass_is_stale_again_when_the_interface_moves_again(whole_pass):
    whole_pass.edit(_interface(1))
    _stage_pass(whole_pass, 1)
    _stage_pass(whole_pass, 2)
    assert _scope(whole_pass) == (False, set())

    whole_pass.edit(lambda d: d["stage"][0].update(expected_result_image="img 1 edited again"))
    assert _scope(whole_pass) == (False, {1, 2})
    _stage_pass(whole_pass, 1)
    assert _scope(whole_pass) == (False, {2})


def test_consumer_linked_by_supplies_alone_is_in_scope(make_env):
    data = _bounded_data()
    data["stage"][1]["depends_on"] = []
    env = make_env(data)
    assert env.record("", "pass").ok
    env.edit(_interface(1))
    assert _scope(env) == (False, {1, 2})


def test_consumer_linked_by_depends_on_alone_is_in_scope(make_env):
    data = _bounded_data()
    data["stage"][1]["supplies"] = []
    env = make_env(data)
    assert env.record("", "pass").ok
    env.edit(_interface(1))
    assert _scope(env) == (False, {1, 2})


def test_an_added_stage_is_in_scope_and_widens_nothing_else(whole_pass):
    whole_pass.edit(lambda d: d["stage"].append(_stage(5, depends_on=[1], supplies=[{"on": 1}])))
    assert _scope(whole_pass) == (False, {5})


def test_interface_move_blocks_until_the_consumer_has_a_pass_that_saw_the_new_interface(whole_pass):
    whole_pass.edit(_interface(1))
    assert _blockers(whole_pass)
    _stage_pass(whole_pass, 1)
    owed = _blockers(whole_pass)
    assert owed
    assert any("stage 2" in b for b in owed)
    _stage_pass(whole_pass, 2)
    assert _blockers(whole_pass) == []


def test_construction_only_move_needs_no_consumer_pass(whole_pass):
    whole_pass.edit(_construction(1))
    assert _blockers(whole_pass)
    _stage_pass(whole_pass, 1)
    assert _blockers(whole_pass) == []


def test_blockers_and_delta_name_the_same_stages(whole_pass):
    whole_pass.edit(_interface(1))
    _stage_pass(whole_pass, 1)
    assert _scope(whole_pass) == (False, {2})
    text = " ".join(_blockers(whole_pass))
    assert "stage 2" in text or "stage:2" in text
    assert "stage 1" not in text and "stage:1" not in text


def test_a_moved_consumer_pass_recorded_before_the_interface_moved_does_not_discharge_it(whole_pass):
    whole_pass.edit(_construction(2))
    _stage_pass(whole_pass, 2)
    assert _blockers(whole_pass) == []
    whole_pass.edit(_interface(1))
    _stage_pass(whole_pass, 1)
    assert _blockers(whole_pass)


def test_plan_review_without_interface_or_pair_currency_fields_reads_as_a_legacy_record():
    review = PlanReview(plan_path="/plan.toml", verdict="pass", reviewer="thinker",
                        plan_sha256="0" * 64, reviewed_interface_keys={"1": "a" * 64},
                        reviewed_pair_currency={"2-1": "b" * 64})
    raw = dataclasses.asdict(review)
    assert PlanReview.from_dict(raw) == review
    del raw["reviewed_interface_keys"], raw["reviewed_pair_currency"]
    restored = PlanReview.from_dict(raw)
    assert restored.reviewed_interface_keys == {}
    assert restored.reviewed_pair_currency is None
