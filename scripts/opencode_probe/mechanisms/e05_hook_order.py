"""E5: the order of the tool hook and the permission evaluation for one tool call, and what a whole-tool deny does.

One scripted ``read`` call is followed through the probe log: the ``tool.execute.before`` entry (by
the call id), the ``permission.evaluate`` entry (its ``source.id`` is the call id) and the
``tool.execute.after`` entry. Record fields: ``order`` (``before-first`` when the before-hook is
logged ahead of the evaluation, ``evaluate-first`` for the reverse), ``whole_tool_deny_removes_tool``
and ``whole_tool_deny_calls_evaluate`` (a second run with a static deny of the whole ``shell`` tool:
is it missing from the tool list the model was offered, and was the evaluation called for it). pass
when the order is ``before-first``; fail when it is ``evaluate-first`` (a before-hook rule would then
no longer stop a call the permission engine has already judged); unobserved when the read call left
no entry of one of the three kinds.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E5"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("order", "whole_tool_deny_removes_tool", "whole_tool_deny_calls_evaluate")
CALL_ID = "call_m1_0_0"
READ = {"models": {"m1": [{"text": "r", "tool_calls": [{"name": "read", "arguments": {"path": "{root}/f.txt"}}]},
                          {"text": "done"}]}}
SHELL = {"models": {"m1": [{"text": "s", "tool_calls": [{"name": "shell",
                                                         "arguments": {"command": "touch {root}/m-e5-shell"}}]},
                           {"text": "done"}]}}
WHOLE_TOOL_DENY = {"permissions": [{"action": "shell", "resource": "*", "effect": "deny"}]}


def verdict(observation):
    return sh.decide(observation["order"] != "before-first", STOPPING_IS_FAIL)


def _call_id(entry):
    data = entry.get("input") or {}
    return (data.get("source") or {}).get("id") if entry.get("at") == "permission.evaluate" else data.get("id")


def order_of(probe, call_id):
    """``before-first`` or ``evaluate-first`` from the log positions of the call's before-hook and
    evaluation entries; None when either is missing."""
    at = {}
    for index, entry in enumerate(probe):
        if entry.get("at") in ("tool.execute.before", "permission.evaluate") and _call_id(entry) == call_id:
            at.setdefault(entry["at"], index)
    if len(at) < 2:
        return None
    return "before-first" if at["tool.execute.before"] < at["permission.evaluate"] else "evaluate-first"


def run(ctx):
    h = ctx.harness()
    read = h.run(READ, label="read-order", files={"f.txt": "hello"}, timeout=90)
    after = [e for e in read.probe_entries("tool.execute.after") if _call_id(e) == CALL_ID]
    denied = h.run(SHELL, label="whole-tool-deny", config=WHOLE_TOOL_DENY, timeout=90)
    order = order_of(read.probe, CALL_ID)
    evidence = {"order": order, "after_logged": bool(after),
                "log_sequence": [e.get("at") for e in read.probe
                                 if e.get("at") in ("tool.execute.before", "permission.evaluate",
                                                    "tool.execute.after")],
                "whole_tool_deny_removes_tool": "shell" not in denied.tool_list(),
                "whole_tool_deny_calls_evaluate": any(
                    (e.get("input") or {}).get("action") == "shell"
                    for e in denied.probe_entries("permission.evaluate")),
                "whole_tool_deny_call_error": ((denied.tool_part(CALL_ID) or {}).get("state") or {}).get("error"),
                "whole_tool_deny_marker": denied.exists("m-e5-shell")}
    if order is None or not after:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(evidence), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
