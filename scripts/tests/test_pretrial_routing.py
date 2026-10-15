"""Pre-trial defect #434: a developer spawn that returns INCOMPLETE and carries a
PERMISSION-REQUEST block must park the stage awaiting a decision.

The marker scan takes the first marker line of the child's stdout, so an INCOMPLETE that
precedes a PERMISSION-REQUEST routes as a plain INCOMPLETE and the request is dropped: the
engine records no pending request and `resolve-permission` has nothing to answer.

A test asserts the repaired behaviour with a message that starts `BEHAVIOUR:`; every setup
assertion lacks that marker.
"""
from __future__ import annotations

from agentctl.state import Node
from test_marker_dispatch import _dispatch_with, _to_executing

REQUEST_ACTION = "git push origin feature/pretrial"
INCOMPLETE_WITH_REQUEST = (
    "INCOMPLETE: the change is committed but not pushed\n"
    "PERMISSION-REQUEST:\n"
    f"Action: {REQUEST_ACTION}\n"
    "Why: the stage's done criterion needs the branch on the remote\n"
    "Fallback if denied: stop the step\n"
    "Rule: Bash(git push origin feature/pretrial:*)\n"
)


def test_i434_incomplete_with_permission_request_parks_request(store, venued_fixtures_dir):
    _to_executing(store, "r1", venued_fixtures_dir)
    assert store.load("r1").permission_request is None, "setup: a request is pending before the dispatch"

    directive = _dispatch_with(store, "r1", INCOMPLETE_WITH_REQUEST)

    parked = store.load("r1").permission_request
    assert parked is not None and REQUEST_ACTION in parked.action, (
        f"BEHAVIOUR: an INCOMPLETE carrying a PERMISSION-REQUEST parked no request "
        f"(directive action={directive.action!r}, pending request={parked!r})"
    )
    assert directive.action != "decide_incomplete", (
        f"BEHAVIOUR: the INCOMPLETE-with-request dispatch routed as a plain INCOMPLETE "
        f"({directive.action!r}) instead of asking for a decision on the request"
    )


def test_plain_incomplete_keeps_base_directive(store, venued_fixtures_dir):
    _to_executing(store, "r2", venued_fixtures_dir)

    directive = _dispatch_with(store, "r2", "INCOMPLETE: half of the edits are done\n")

    assert directive.action == "decide_incomplete"
    assert directive.marker == "INCOMPLETE"
    assert directive.node == Node.EXECUTING.value
    assert store.load("r2").permission_request is None
