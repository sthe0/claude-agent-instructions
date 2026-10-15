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


def test_resolve_permission_acts_on_request_from_incomplete(store, venued_fixtures_dir):
    from agentctl import cli
    from test_marker_dispatch import ns

    _to_executing(store, "r3", venued_fixtures_dir)
    _dispatch_with(store, "r3", INCOMPLETE_WITH_REQUEST)

    resolved = cli.cmd_resolve_permission(
        ns(session="r3", decision="denied", scope="once"), store=store)

    assert resolved.action == "continue_spawn", (
        f"BEHAVIOUR: resolve-permission did not act on the request parked from an INCOMPLETE "
        f"(action={resolved.action!r}, message={resolved.message!r})"
    )
    assert resolved.data["decision"] == "denied"
    assert store.load("r3").permission_request is None, (
        "BEHAVIOUR: the answered request is still parked"
    )


def test_incomplete_permission_request_parity_with_bare_request(store, venued_fixtures_dir):
    block = (
        f"PERMISSION-REQUEST: {REQUEST_ACTION}\n"
        "Why: the stage's done criterion needs the branch on the remote\n"
        "Rule: Bash(git push origin feature/pretrial:*)\n"
    )
    _to_executing(store, "r4a", venued_fixtures_dir)
    bare = _dispatch_with(store, "r4a", block)
    _to_executing(store, "r4b", venued_fixtures_dir)
    wrapped = _dispatch_with(store, "r4b", "INCOMPLETE: committed, not pushed\n" + block)

    req_a, req_b = store.load("r4a").permission_request, store.load("r4b").permission_request
    assert req_a is not None, "setup: the bare request was not parked"
    assert req_b is not None, "BEHAVIOUR: the INCOMPLETE-wrapped request was not parked"
    assert (req_b.action, req_b.stage_index, req_b.raw) == (req_a.action, req_a.stage_index, req_a.raw), (
        "BEHAVIOUR: the parked request differs between bare and INCOMPLETE-wrapped"
    )
    assert (wrapped.action, wrapped.marker, wrapped.ok) == (bare.action, bare.marker, bare.ok), (
        "BEHAVIOUR: the directive differs between bare and INCOMPLETE-wrapped"
    )

    def miss_rows(sid):
        return [{k: v for k, v in row.items() if k != "ts"}
                for row in store.load(sid).planning_misses]

    assert miss_rows("r4b") == miss_rows("r4a"), (
        "BEHAVIOUR: the planning-miss rows differ between bare and INCOMPLETE-wrapped"
    )
    assert wrapped.data.get("incomplete_summary") == "committed, not pushed", (
        "BEHAVIOUR: the INCOMPLETE summary is lost from the directive data"
    )
    assert "incomplete_summary" not in bare.data
