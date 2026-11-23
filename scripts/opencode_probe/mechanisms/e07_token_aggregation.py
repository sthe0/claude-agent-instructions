"""E7: whether a parent session's stored token counts include its child sessions' usage.

One scripted ``subagent`` call: the parent session runs on model ``m1``, the child on ``m2``; the stub
reports a fixed usage per request, so each session's expected own total is known from the number of
requests the stub saw for its model. The usage store (table ``session_v2``) is read inside the case.
Record fields: ``aggregation`` (``parent-excludes-children`` when the parent row holds only its own
requests' usage, ``parent-includes-children`` when it also holds the child's, ``other`` otherwise),
``parent`` and ``child`` (stored tokens next to the stub-reported ones). pass when the aggregation is
one of the two named; absent when the stored figures match neither; unobserved when a row is missing.
"""
from opencode_probe import record, runner
from opencode_probe.harness import wait_until
from opencode_probe.mechanisms import c_p6
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E7"
K7 = False

FIELDS = ("aggregation", "parent", "child")
AGGREGATIONS = ("parent-excludes-children", "parent-includes-children")
STUB_INPUT, STUB_OUTPUT = c_p6.STUB_INPUT, c_p6.STUB_OUTPUT


def aggregation_of(parent, child, own_parent, own_child):
    """Name how ``parent`` (stored tokens) relates to the usage of its own requests and of the child's."""
    if parent == own_parent:
        return AGGREGATIONS[0]
    if parent == own_parent + own_child and own_child:
        return AGGREGATIONS[1]
    return "other"


def run(ctx):
    h = ctx.harness()
    result = h.run(c_p6.CHILD, label="parent-child", wait_for=("m-p6-child",), settle=12.0, timeout=100)
    root_id = (result.session_ids() or [None])[0]
    wait_until(lambda: [r for r in sh.db_rows(result.root, "select * from session_v2 where parent_id is not null")
                        or [] if r.get("tokens_input")], 15)
    parent = (sh.db_rows(result.root, "select * from session_v2 where id = ?", (root_id,)) or [{}])[0]
    children = sh.db_rows(result.root, "select * from session_v2 where parent_id = ?", (root_id,)) or []
    requests = {m: sum(1 for r in result.requests if r.get("model") == m) for m in ("m1", "m2")}
    expected = {name: {"input": STUB_INPUT * count, "output": STUB_OUTPUT * count}
                for name, count in (("parent", requests["m1"]), ("child", requests["m2"]))}
    child = children[0] if children else {}
    stored = {name: {"input": row.get("tokens_input"), "output": row.get("tokens_output")}
              for name, row in (("parent", parent), ("child", child))}
    evidence = {"parent": {"stored": stored["parent"], "stub_reported": expected["parent"]},
                "child": {"stored": stored["child"], "stub_reported": expected["child"]},
                "stub_requests_by_model": requests, "root_session": root_id,
                "child_rows": len(children)}
    if not parent or not child or not parent.get("tokens_input"):
        return runner.result(record.UNOBSERVED, {**evidence, "aggregation": None})
    named = {kind: aggregation_of(stored["parent"][kind], stored["child"][kind],
                                  expected["parent"][kind], expected["child"][kind])
             for kind in ("input", "output")}
    aggregation = named["input"] if named["input"] == named["output"] else "other"
    evidence["aggregation"] = aggregation
    evidence["per_kind"] = named
    return runner.result(record.PASS if aggregation in AGGREGATIONS else record.ABSENT, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS)
