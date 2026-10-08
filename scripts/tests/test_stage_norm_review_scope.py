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

import pytest

from agentctl import gates

from test_plan_review_topo import (  # noqa: F401  (fixtures + helpers)
    _bounded_data,
    _delta,
    _hand_baseline,
    _w,
    make_env,
)


def _construction(index):
    return lambda d: d["stage"][index - 1].update(method=f"method-{index} edited")


def _interface(index):
    return lambda d: d["stage"][index - 1].update(expected_result_image=f"img {index} edited")


def _scope(env):
    return gates.plan_review_delta(env.state(), env.doc())


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
