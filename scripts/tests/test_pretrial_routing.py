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
        f"(action={resolved.action!r}, detail={resolved.detail!r})"
    )
    assert resolved.data["decision"] == "denied"
    assert store.load("r3").permission_request is None, (
        "BEHAVIOUR: the answered request is still parked"
    )


_BLOCK_TAIL = (
    "Why: the stage's done criterion needs the branch on the remote\n"
    "Rule: Bash(git push origin feature/pretrial:*)\n"
)
_BLOCK_FORMATS = {
    "label-with-inline-text": f"PERMISSION-REQUEST: {REQUEST_ACTION}\n" + _BLOCK_TAIL,
    "label-alone-with-action-line": f"PERMISSION-REQUEST:\nAction: {REQUEST_ACTION}\n" + _BLOCK_TAIL,
}


def test_incomplete_permission_request_parity_with_bare_request(store, venued_fixtures_dir):
    def comparable(directive):
        return {k: v for k, v in directive.data.items()
                if k not in ("incomplete_summary", "stdout", "evidence_dir")}

    def miss_rows(sid):
        return [{k: v for k, v in row.items() if k != "ts"}
                for row in store.load(sid).planning_misses]

    for name, block in _BLOCK_FORMATS.items():
        a, b = f"r4a-{name}", f"r4b-{name}"
        _to_executing(store, a, venued_fixtures_dir)
        bare = _dispatch_with(store, a, block)
        _to_executing(store, b, venued_fixtures_dir)
        wrapped = _dispatch_with(store, b, "INCOMPLETE: committed, not pushed\n" + block)

        req_a, req_b = store.load(a).permission_request, store.load(b).permission_request
        assert req_a is not None, f"setup: the bare request was not parked ({name})"
        assert req_b is not None, f"BEHAVIOUR: the INCOMPLETE-wrapped request was not parked ({name})"
        assert req_a.action == REQUEST_ACTION, (
            f"BEHAVIOUR: the bare request parked action {req_a.action!r} ({name})"
        )
        assert (req_b.action, req_b.stage_index, req_b.raw) == (req_a.action, req_a.stage_index, req_a.raw), (
            f"BEHAVIOUR: the parked request differs between bare and INCOMPLETE-wrapped ({name})"
        )
        assert (wrapped.action, wrapped.marker, wrapped.ok, wrapped.detail) == (
            bare.action, bare.marker, bare.ok, bare.detail), (
            f"BEHAVIOUR: the directive differs between bare and INCOMPLETE-wrapped ({name})"
        )
        assert comparable(wrapped) == comparable(bare), (
            f"BEHAVIOUR: the directive data differs between bare and INCOMPLETE-wrapped ({name})"
        )
        assert miss_rows(b) == miss_rows(a), (
            f"BEHAVIOUR: the planning-miss rows differ between bare and INCOMPLETE-wrapped ({name})"
        )
        assert wrapped.data.get("incomplete_summary") == "committed, not pushed", (
            f"BEHAVIOUR: the INCOMPLETE summary is lost from the directive data ({name})"
        )
        assert "incomplete_summary" not in bare.data


def test_decorated_permission_request_after_incomplete_parks_request(store, venued_fixtures_dir):
    _to_executing(store, "r5", venued_fixtures_dir)
    output = (
        "INCOMPLETE: committed, not pushed\n"
        "**PERMISSION-REQUEST:**\n"
        f"- **Action:** `{REQUEST_ACTION}`\n"
        + _BLOCK_TAIL
    )

    _dispatch_with(store, "r5", output)

    parked = store.load("r5").permission_request
    assert parked is not None and parked.action == REQUEST_ACTION, (
        f"BEHAVIOUR: a markdown-decorated PERMISSION-REQUEST after an INCOMPLETE parked {parked!r}"
    )


def test_resolve_permission_stage_scope_on_request_from_incomplete(store, venued_fixtures_dir):
    from agentctl import cli
    from test_marker_dispatch import ns

    _to_executing(store, "r6", venued_fixtures_dir)
    _dispatch_with(store, "r6", INCOMPLETE_WITH_REQUEST)

    resolved = cli.cmd_resolve_permission(
        ns(session="r6", decision="granted", scope="stage"), store=store)

    assert resolved.action == "continue_spawn", (
        f"BEHAVIOUR: a stage-scope grant on a request parked from an INCOMPLETE was not applied "
        f"(action={resolved.action!r}, detail={resolved.detail!r})"
    )
    assert resolved.data["decision"] == "granted"
    assert store.load("r6").permission_request is None
