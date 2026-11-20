"""P7: a tool-less agent refuses every single tool call that a default agent executes.

For each id of P1's ``paths`` list except ``child-run`` one scripted call runs twice in a fresh
root: under the agent ``toolless`` (defined through ``OPENCODE_CONFIG_CONTENT`` with a deny of
every action) and under the default agent with the same config. HOME is seeded with stand-ins of
the HOME-level OpenCode and Claude locations. pass when every call is refused under ``toolless``,
every call executes under the default agent and every stand-in is byte-identical afterwards; a
refusal counts only with its positive event, the tool part of that call in the error state with an
error text. fail when a call executes under ``toolless`` or a stand-in changed; unobserved when a
call did not execute under the default agent or a refused call has no such event. For each path
executed under the default agent the record keeps ``fired_by``: the P13 mechanisms whose hook
entry in the run's event log names that call id. A tool the runtime rejects as unavailable under
the default agent is not a path (P1 leaves it out too) and is recorded in ``unavailable_tools``.
"""
import json

from opencode_probe import paths, record, runner

CASE_ID = "P7"
K7 = True

AGENT = "toolless"
AGENT_DEFINITION = {"permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}
CHANNEL = "OPENCODE_CONFIG_CONTENT"
CONTENT = {"agents": {AGENT: AGENT_DEFINITION}}


def _fired_by(run, call_id):
    """The mechanisms whose hook entry in the run's log names this call id."""
    fired = []
    if any(e["input"].get("id") == call_id for e in run.probe_entries("tool.execute.before")):
        fired.append("PreToolUse")
    if any(e["input"].get("id") == call_id for e in run.probe_entries("tool.execute.after")):
        fired.append("PostToolUse")
    if any((e["input"].get("source") or {}).get("id") == call_id
           for e in run.probe_entries("permission.evaluate")):
        fired.append("permission.evaluate")
    return fired


def _refusal_shown(outcome):
    """The refused call left a tool part in the error state with an error text."""
    return outcome["status"] == "error" and bool(outcome["error"])


def run(ctx):
    h = ctx.harness()
    env = {CHANNEL: json.dumps(CONTENT)}
    entries, unobserved, leaked, stand_in_changes, no_refusal_event = [], [], [], {}, []
    unavailable = {}
    for path_id in paths.PATH_IDS:
        if path_id == "child-run":
            continue
        plan = paths.build_plan(include=(path_id,))
        spec = plan.spec(path_id)
        default = paths.exercise(h, plan, f"default:{path_id}", env=env, stand_ins=True)
        tool_less = paths.exercise(h, plan, f"toolless:{path_id}", env=env, stand_ins=True,
                                   cli=("--agent", AGENT))
        under_default = paths.outcome(default, plan, spec)
        under_toolless = paths.outcome(tool_less, plan, spec)
        if paths.UNAVAILABLE_TEXT in str(under_default["error"]):
            unavailable[spec.tool] = under_default["error"]
            continue
        if not under_default["executed"]:
            unobserved.append(path_id)
        if under_toolless["executed"]:
            leaked.append(path_id)
        elif not _refusal_shown(under_toolless):
            no_refusal_event.append(path_id)
        for label, r in (("default", default), ("toolless", tool_less)):
            changed = r.stand_ins_changed()
            if changed:
                stand_in_changes[f"{label}:{path_id}"] = changed
        entries.append({"id": path_id, "toolless": under_toolless, "default": under_default,
                        "fired_by": _fired_by(default, plan.call_ids[path_id])
                        if under_default["executed"] else [],
                        "argv": {"default": default.argv, "toolless": tool_less.argv}})
    evidence = {"agent_definition": {AGENT: AGENT_DEFINITION}, "config_channel": CHANNEL,
                "paths": entries, "executed_under_toolless": leaked,
                "not_executed_under_default": unobserved,
                "no_refusal_event_under_toolless": no_refusal_event, "unavailable_tools": unavailable,
                "stand_ins_changed": stand_in_changes, "marked_executed": bool(leaked)}
    if leaked or stand_in_changes:
        return runner.result(record.FAIL, evidence)
    return runner.result(record.UNOBSERVED if unobserved or no_refusal_event else record.PASS, evidence)


def check_record(entry, cases):
    """Evidence for every P1 path id except ``child-run``."""
    p1_ids = {p.get("id") for p in cases.get("P1", {}).get("paths") or []} - {"child-run"}
    have = {p.get("id") for p in entry.get("evidence", {}).get("paths") or []}
    if not p1_ids:
        return ["P1 has no paths to compare against"]
    return [f"no evidence for P1 path {path_id!r}" for path_id in sorted(p1_ids - have)]
