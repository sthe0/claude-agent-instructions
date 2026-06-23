import pytest

from agentctl.machine import TransitionError, legal_targets, transition
from agentctl.state import Node


def test_happy_path_edges():
    assert transition(Node.CLASSIFIED.value, "classify") == Node.ROUTED.value
    assert transition(Node.ROUTED.value, "plan") == Node.PLANNING.value
    assert transition(Node.PLANNING.value, "submit_plan") == Node.PLAN_READY.value
    assert transition(Node.PLAN_READY.value, "approve") == Node.APPROVED.value
    assert transition(Node.APPROVED.value, "execute_approved") == Node.EXECUTING.value
    assert transition(Node.EXECUTING.value, "verify") == Node.VERIFYING.value
    assert transition(Node.VERIFYING.value, "next_stage") == Node.EXECUTING.value
    assert transition(Node.VERIFYING.value, "final") == Node.RESOLUTION.value
    assert transition(Node.RESOLUTION.value, "resolve") == Node.RESOLVED.value


def test_small_change_entry_edge():
    assert transition(Node.ROUTED.value, "execute_small") == Node.EXECUTING.value


def test_wrong_source_node_raises():
    with pytest.raises(TransitionError):
        transition(Node.CLASSIFIED.value, "approve")


def test_unknown_event_raises():
    with pytest.raises(TransitionError):
        transition(Node.ROUTED.value, "teleport")


def test_legal_targets_from_routed():
    targets = set(legal_targets(Node.ROUTED.value))
    assert targets == {Node.PLANNING.value, Node.EXECUTING.value}
