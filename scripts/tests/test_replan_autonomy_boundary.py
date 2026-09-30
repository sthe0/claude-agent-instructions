"""Unit 2: the autonomy boundary letting the coordinator self-approve/self-grant
certain permission requests and replans for an order the user already approved.

This module is being built incrementally (stage 5 of
effort-replan-absolute-5-autonomous-r2.toml). It currently covers the
order-approvals ledger primitives that (b) adds to `order_approvals.py`:
the stamped effort estimate, the per-order `effort_since_user_approval`
window and its reset on a fresh user approval, the `open_effort_fires`
marker, and the `first_thinker_verdicts` key/write-once semantics. The pure
`gates.autonomy_boundary` function (c), the submission/replan routing (d),
the agent-acknowledge paths (e)/(f), and the plan-review override disclosure
(g) are NOT yet implemented — see the stage's own verify_command for the
full named-test list this file is meant to grow into.
"""
from __future__ import annotations

from agentctl import order_approvals as oa
from agentctl.resources import FileResource


def test_threshold_value_is_five():
    from agentctl.config import Thresholds

    assert Thresholds().effort_replan_absolute() == 5


def test_customer_approval_stamps_effort_estimate(tmp_path):
    order = "order-1"
    data = oa.record_approval(
        order,
        plan_sha256="plan-a",
        resources=[FileResource(path=str(tmp_path / "f.txt"), mode="write")],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:00:00Z",
        effort_estimate={"spend": 3.0, "wall_clock": 25},
        root=tmp_path,
    )
    assert data["records"][-1]["effort_estimate"] == {"spend": 3.0, "wall_clock": 25}
    stored = oa.get(order, root=tmp_path)
    assert stored["records"][-1]["effort_estimate"] == {"spend": 3.0, "wall_clock": 25}


def test_fresh_order_initial_approval_goes_to_user():
    assert oa.latest_user_approved_record("never-approved-order", root=None) is None


def test_effort_accumulates_across_agent_approved_sessions(tmp_path):
    order = "order-2"
    oa.record_approval(
        order,
        plan_sha256="plan-a",
        resources=[],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:00:00Z",
        effort_estimate={"spend": 3.0, "wall_clock": 25},
        root=tmp_path,
    )
    oa.flush_effort(order, spend_delta=0.5, wall_clock_delta=4, root=tmp_path)
    oa.flush_effort(order, spend_delta=0.25, wall_clock_delta=2, root=tmp_path)
    totals = oa.get(order, root=tmp_path)["effort_since_user_approval"]
    assert totals == {"spend": 0.75, "wall_clock": 6}


def test_self_approval_does_not_restamp_user_ledger(tmp_path):
    order = "order-3"
    oa.record_approval(
        order,
        plan_sha256="plan-a",
        resources=[],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:00:00Z",
        effort_estimate={"spend": 3.0, "wall_clock": 25},
        root=tmp_path,
    )
    before = oa.get(order, root=tmp_path)
    assert len(before["records"]) == 1
    # An agent approval never calls record_approval at all (cmd_approve --by
    # agent "never stamps the ledger" per the stage brief) -- there is
    # nothing else to call here; this test pins that record_approval itself
    # still refuses AGENT_ACTOR as a last-resort guard.
    import pytest

    from agentctl.state import AGENT_ACTOR

    with pytest.raises(ValueError):
        oa.record_approval(
            order,
            plan_sha256="plan-b",
            resources=[],
            unresolved_identities=[],
            stage_effects=[],
            by=AGENT_ACTOR,
            at="2026-09-30T00:01:00Z",
            root=tmp_path,
        )
    after = oa.get(order, root=tmp_path)
    assert len(after["records"]) == 1


def test_open_spend_fire_blocks_self_approval_in_new_session(tmp_path):
    order = "order-4"
    oa.record_approval(
        order,
        plan_sha256="plan-a",
        resources=[],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:00:00Z",
        effort_estimate={"spend": 1.0, "wall_clock": 10},
        root=tmp_path,
    )
    oa.record_effort_fire(
        order,
        scale="spend",
        detail={"ratio": 6.0},
        at="2026-09-30T00:05:00Z",
        root=tmp_path,
    )
    assert oa.get(order, root=tmp_path)["open_effort_fires"]
    oa.clear_effort_fires(order, root=tmp_path)
    assert oa.get(order, root=tmp_path)["open_effort_fires"] == []


def test_approve_restarts_the_order_window(tmp_path):
    order = "order-5"
    oa.record_approval(
        order,
        plan_sha256="plan-a",
        resources=[],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:00:00Z",
        effort_estimate={"spend": 1.0, "wall_clock": 10},
        root=tmp_path,
    )
    oa.flush_effort(order, spend_delta=0.9, wall_clock_delta=9, root=tmp_path)
    oa.record_effort_fire(order, scale="spend", detail={}, at="2026-09-30T00:05:00Z", root=tmp_path)
    oa.record_approval(
        order,
        plan_sha256="plan-b",
        resources=[],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-30T00:10:00Z",
        effort_estimate={"spend": 2.0, "wall_clock": 15},
        root=tmp_path,
    )
    data = oa.get(order, root=tmp_path)
    assert data["effort_since_user_approval"] == {"spend": 0.0, "wall_clock": 0.0}
    assert data["open_effort_fires"] == []


def test_first_thinker_verdict_per_identity_set_survives_byte_edit(tmp_path):
    order = "order-6"
    key_a = oa.first_thinker_verdict_key("plan-a", [("bash", "some command")])
    key_b = oa.first_thinker_verdict_key("plan-a", [("bash", "some command")])
    assert key_a == key_b
    oa.record_first_thinker_verdict(order, key_a, {"verdict": "pass"}, root=tmp_path)
    assert oa.get_first_thinker_verdict(order, key_b, root=tmp_path) == {"verdict": "pass"}


def test_first_thinker_verdict_not_carried_across_user_reapproval(tmp_path):
    order = "order-7"
    identities = [("bash", "some command")]
    key_before = oa.first_thinker_verdict_key("plan-a", identities)
    oa.record_first_thinker_verdict(order, key_before, {"verdict": "pass"}, root=tmp_path)
    key_after = oa.first_thinker_verdict_key("plan-b", identities)
    assert key_after != key_before
    assert oa.get_first_thinker_verdict(order, key_after, root=tmp_path) is None


def test_first_thinker_verdict_written_only_when_absent(tmp_path):
    order = "order-8"
    key = oa.first_thinker_verdict_key("plan-a", [])
    oa.record_first_thinker_verdict(order, key, {"verdict": "revise"}, root=tmp_path)
    oa.record_first_thinker_verdict(order, key, {"verdict": "pass"}, root=tmp_path)
    assert oa.get_first_thinker_verdict(order, key, root=tmp_path) == {"verdict": "revise"}
