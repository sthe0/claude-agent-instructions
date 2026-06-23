import pytest

from agentctl.machine import TransitionError, legal_targets, transition
from agentctl.state import Node


def test_happy_path_edges():
    assert transition(Node.CLASSIFIED.value, "classify") == Node.ROUTED.value
    assert transition(Node.ROUTED.value, "plan") == Node.PLANNING.value
    assert transition(Node.PLANNING.value, "submit_plan") == Node.PLAN_READY.value
    assert transition(Node.PLAN_READY.value, "approve") == Node.APPROVED.value
    assert transition(Node.APPROVED.value, "decompose") == Node.DECOMPOSED.value
    assert transition(Node.DECOMPOSED.value, "execute_approved") == Node.EXECUTING.value
    assert transition(Node.EXECUTING.value, "verify") == Node.VERIFYING.value
    assert transition(Node.VERIFYING.value, "next_stage") == Node.EXECUTING.value
    assert transition(Node.VERIFYING.value, "final") == Node.RESOLUTION.value
    assert transition(Node.RESOLUTION.value, "resolve") == Node.RESOLVED.value


def test_small_change_entry_edge():
    assert transition(Node.ROUTED.value, "execute_small") == Node.EXECUTING.value


def test_execute_approved_requires_decomposed_not_approved():
    # the spawn path must pass through DECOMPOSED; skipping decompose is illegal
    with pytest.raises(TransitionError):
        transition(Node.APPROVED.value, "execute_approved")


def test_decomposed_is_reachable_and_not_dead_end():
    from agentctl.machine import TRANSITIONS

    in_edges = [ev for ev, (frm, to) in TRANSITIONS.items() if to == Node.DECOMPOSED.value]
    out_edges = [ev for ev, (frm, to) in TRANSITIONS.items() if frm == Node.DECOMPOSED.value]
    assert in_edges == ["decompose"]
    assert "execute_approved" in out_edges


def test_wrong_source_node_raises():
    with pytest.raises(TransitionError):
        transition(Node.CLASSIFIED.value, "approve")


def test_unknown_event_raises():
    with pytest.raises(TransitionError):
        transition(Node.ROUTED.value, "teleport")


def test_legal_targets_from_routed():
    targets = set(legal_targets(Node.ROUTED.value))
    assert targets == {Node.PLANNING.value, Node.EXECUTING.value}
