"""Pairwise review is THE required plan-review route of a SPAWN-route session.

Proves: the `submit_plan` directive and the blocker message name the topological
driver and `plan-review-compose` on a SPAWN route (and keep the whole-plan thinker
spawn elsewhere); a compose over all-pass pairs is a first thinker verdict the
autonomy boundary reads, one with an override pair is not; compose never charges a
review round (the pair records did).

New symbols are imported lazily inside test bodies so the file stays collectable on
a tree that predates them.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agentctl import gates, plugins
from agentctl.directive import Directive
from agentctl.plan import load_plan, review_ids, review_pairs, review_units
from agentctl.state import Node, Route, SessionState, WeightClass
from test_replan_autonomy_boundary import (  # noqa: F401  (fixtures + helpers)
    Eng, _no_ambient_harness_session, action, approved_order, autonomy, cmd_plan, eng, venue,
)


@pytest.fixture
def review_armed(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "1")


def _plan_ready_state(plan: Path, route: str | None) -> SessionState:
    return SessionState(
        session_id="pr1", task_id="t", node=Node.PLAN_READY.value,
        weight_class=WeightClass.SUBSTANTIVE.value, route=route, plan_path=str(plan),
    )


def _fire(state: SessionState) -> list[dict]:
    plugins.activate(state, "review_dispatch")
    fired = plugins.fire("submit_plan", state, Directive(True, state.node, "noop"))
    return [p for p in fired if p["plugin"] == "review_dispatch"]


def _plan_file(tmp_path: Path) -> Path:
    fixture = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"
    plan = tmp_path / "plan.toml"
    plan.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return plan


def test_spawn_route_directive_names_pairwise_review_and_no_whole_plan_spawn(tmp_path, review_armed):
    plan = _plan_file(tmp_path)
    (directive,) = _fire(_plan_ready_state(plan, Route.SPAWN.value))
    detail = directive["detail"]
    assert directive["blocking"] is True
    assert directive["data"]["mode"] == "pairwise"
    assert f"scripts/plan-review-topological.py --session pr1 --plan {plan}" in detail
    assert "plan-review-compose" in detail
    assert "question-list" in detail
    assert "whole-plan spawn ceiling refuses" not in detail
    assert "spawn the `thinker` specialization" not in detail


def test_non_spawn_route_keeps_the_whole_plan_directive(tmp_path, review_armed):
    plan = _plan_file(tmp_path)
    (directive,) = _fire(_plan_ready_state(plan, Route.IN_THREAD.value))
    assert directive["data"]["mode"] == "whole"
    assert "spawn the `thinker` specialization" in directive["detail"]
    assert "plan-review-topological.py" not in directive["detail"]


def test_blocker_message_follows_the_same_route_predicate(tmp_path, review_armed):
    plan = _plan_file(tmp_path)
    spawn = gates.plan_review_blockers(_plan_ready_state(plan, Route.SPAWN.value), str(plan))
    other = gates.plan_review_blockers(_plan_ready_state(plan, Route.IN_THREAD.value), str(plan))
    assert "plan-review-topological.py" in spawn[0] and "no thinker review recorded" in spawn[0]
    assert "plan-review-topological.py" not in other[0] and "no thinker review recorded" in other[0]


def _record_pairs(eng: Eng, sid: str, plan: str, *, override: tuple[str, ...] = ()) -> list[str]:
    """Record a review for EVERY unit and EVERY pair of the plan (`review_ids`: the
    compose demands both), `override` ids as revise + user override, the rest as pass.
    Returns the pair ids."""
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    doc = load_plan(plan)
    pairs = list(review_pairs(doc))
    assert pairs
    for review_id in review_ids(doc):
        if review_id in override:
            assert eng.run("plan_review", session=sid, verdict="revise", reviewer="thinker",
                           target=plan, scope=f"topo:{review_id}", plan_digest=digest,
                           concern=["blocking: open"])["ok"], review_id
            assert eng.run("plan_review", session=sid, verdict="override", reviewer="user",
                           target=plan, scope=f"topo:{review_id}", note="accepted")["ok"], review_id
        else:
            assert eng.run("plan_review", session=sid, verdict="pass", reviewer="thinker",
                           target=plan, scope=f"topo:{review_id}", plan_digest=digest)["ok"], review_id
    return pairs


def test_compose_over_all_pass_pairs_is_a_first_thinker_verdict(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    d = eng.open("t1", slow)
    assert action(d) == "await_user_approval"
    assert autonomy(d)["thinker_pass"] is False
    _record_pairs(eng, "t1", slow)
    rounds_before = eng.state("t1").review_rounds
    composed = eng.run("plan_review_compose", session="t1", target=slow)
    assert composed["ok"], composed
    assert eng.state("t1").review_rounds == rounds_before
    later = eng.open("t2", slow)
    assert autonomy(later)["thinker_pass"] is True
    assert action(later) == "self_approve"


def test_compose_demands_every_unit_not_only_the_pairs(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    digest = hashlib.sha256(Path(slow).read_bytes()).hexdigest()
    doc = load_plan(slow)
    for pair in review_pairs(doc):
        assert eng.run("plan_review", session="t1", verdict="pass", reviewer="thinker",
                       target=slow, scope=f"topo:{pair}", plan_digest=digest)["ok"], pair
    refused = eng.run("plan_review_compose", session="t1", target=slow)
    assert not refused["ok"], refused
    assert set(refused["data"]["failing"]) == set(review_units(doc))
    assert set(refused["data"]["failing"].values()) == {"missing"}
    assert autonomy(eng.open("t2", slow))["thinker_pass"] is False


@pytest.mark.parametrize("kind", ["pair", "unit"])
def test_compose_with_an_override_pair_is_not_a_first_thinker_verdict(eng, venue, kind):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    doc = load_plan(slow)
    overridden = review_pairs(doc)[0] if kind == "pair" else review_units(doc)[0]
    _record_pairs(eng, "t1", slow, override=(overridden,))
    composed = eng.run("plan_review_compose", session="t1", target=slow)
    assert composed["ok"], composed
    later = eng.open("t2", slow)
    assert autonomy(later)["thinker_pass"] is False
    assert action(later) == "await_user_approval"
