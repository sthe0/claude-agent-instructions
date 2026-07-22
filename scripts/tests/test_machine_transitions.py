import pytest

from agentctl.machine import TransitionError, legal_targets, transition
from agentctl.state import Node


def test_happy_path_edges():
    assert transition(Node.CLASSIFIED.value, "classify") == Node.ROUTED.value
    assert transition(Node.ROUTED.value, "plan") == Node.PLANNING.value
    assert transition(Node.PLANNING.value, "submit_plan") == Node.PLAN_READY.value
    assert transition(Node.PLAN_READY.value, "approve") == Node.APPROVED.value
    assert transition(Node.APPROVED.value, "partition") == Node.PARTITIONED.value
    assert transition(Node.PARTITIONED.value, "execute_approved") == Node.EXECUTING.value
    assert transition(Node.EXECUTING.value, "verify") == Node.VERIFYING.value
    assert transition(Node.VERIFYING.value, "next_stage") == Node.EXECUTING.value
    assert transition(Node.VERIFYING.value, "final") == Node.RESOLUTION.value
    assert transition(Node.RESOLUTION.value, "resolve") == Node.RESOLVED.value


def test_small_change_entry_edge():
    assert transition(Node.ROUTED.value, "execute_small") == Node.EXECUTING.value


def test_execute_approved_requires_partitioned_not_approved():
    # the spawn path must pass through PARTITIONED; skipping partition is illegal
    with pytest.raises(TransitionError):
        transition(Node.APPROVED.value, "execute_approved")


def test_partitioned_is_reachable_and_not_dead_end():
    from agentctl.machine import TRANSITIONS

    in_edges = [ev for ev, (frm, to) in TRANSITIONS.items() if to == Node.PARTITIONED.value]
    out_edges = [ev for ev, (frm, to) in TRANSITIONS.items() if frm == Node.PARTITIONED.value]
    assert in_edges == ["partition"]
    assert "execute_approved" in out_edges
    assert "finalize_partitioned" in out_edges


def test_finalize_partitioned_edge_goes_to_verifying():
    assert transition(Node.PARTITIONED.value, "finalize_partitioned") == Node.VERIFYING.value


def test_finalize_partitioned_illegal_outside_partitioned():
    for node in (Node.CLASSIFIED, Node.ROUTED, Node.PLANNING, Node.PLAN_READY,
                 Node.APPROVED, Node.EXECUTING, Node.VERIFYING, Node.RESOLUTION,
                 Node.RESOLVED, Node.DIAGNOSING, Node.BLOCKED):
        with pytest.raises(TransitionError):
            transition(node.value, "finalize_partitioned")


def test_wrong_source_node_raises():
    with pytest.raises(TransitionError):
        transition(Node.CLASSIFIED.value, "approve")


def test_unknown_event_raises():
    with pytest.raises(TransitionError):
        transition(Node.ROUTED.value, "teleport")


def test_legal_targets_from_routed():
    targets = set(legal_targets(Node.ROUTED.value))
    assert targets == {Node.PLANNING.value, Node.EXECUTING.value}


def test_reject_edge_resolution_to_diagnosing():
    assert transition(Node.RESOLUTION.value, "reject") == Node.DIAGNOSING.value


def test_reject_illegal_outside_resolution():
    with pytest.raises(TransitionError):
        transition(Node.EXECUTING.value, "reject")


def test_revise_plan_edge_is_a_self_loop_at_plan_ready():
    assert transition(Node.PLAN_READY.value, "revise_plan") == Node.PLAN_READY.value


def test_revise_plan_illegal_outside_plan_ready():
    with pytest.raises(TransitionError):
        transition(Node.PLANNING.value, "revise_plan")
