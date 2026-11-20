"""P7: a tool-less agent refuses every single tool call that a default agent executes.

For each id of P1's ``paths`` list except ``child-run`` one scripted call runs twice in a fresh
root: under the agent ``toolless`` (defined through ``OPENCODE_CONFIG_CONTENT`` with a deny of
every action) and under the default agent with the same config. HOME is seeded with stand-ins of
the HOME-level OpenCode and Claude locations. pass when every call is refused under ``toolless``,
every call executes under the default agent and every stand-in is byte-identical afterwards; fail
when a call executes under ``toolless`` or a stand-in changed; unobserved when a call did not
execute under the default agent. For each path executed under the default agent the record keeps
``fired_by``: the P13 mechanisms whose hook call the run's event log shows for that call.
"""
import json

from opencode_probe import paths, record, runner

CASE_ID = "P7"
K7 = True

AGENT = "toolless"
AGENT_DEFINITION = {"permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}
CHANNEL = "OPENCODE_CONFIG_CONTENT"
CONTENT = {"agents": {AGENT: AGENT_DEFINITION}}


def _fired_by(run, spec):
    tool = "subagent" if spec.path_id == "subagent:task" else spec.tool
    fired = []
    if any(e["input"].get("tool") == tool for e in run.probe_entries("tool.execute.before")):
        fired.append("PreToolUse")
    if any(e["input"].get("tool") == tool for e in run.probe_entries("tool.execute.after")):
        fired.append("PostToolUse")
    if run.probe_entries("permission.evaluate"):
        fired.append("permission.evaluate")
    return fired


def run(ctx):
    h = ctx.harness()
    env = {CHANNEL: json.dumps(CONTENT)}
    entries, unobserved, leaked, stand_in_changes = [], [], [], {}
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
        if not under_default["executed"]:
            unobserved.append(path_id)
        if under_toolless["executed"]:
            leaked.append(path_id)
        for label, r in (("default", default), ("toolless", tool_less)):
            changed = r.stand_ins_changed()
            if changed:
                stand_in_changes[f"{label}:{path_id}"] = changed
        entries.append({"id": path_id, "toolless": under_toolless, "default": under_default,
                        "fired_by": _fired_by(default, spec) if under_default["executed"] else [],
                        "argv": {"default": default.argv, "toolless": tool_less.argv},
                        "env_keys": tool_less.env_keys})
    evidence = {"agent_definition": {AGENT: AGENT_DEFINITION}, "config_channel": CHANNEL,
                "paths": entries, "executed_under_toolless": leaked,
                "not_executed_under_default": unobserved, "stand_ins_changed": stand_in_changes,
                "marked_executed": bool(leaked)}
    if leaked or stand_in_changes:
        return runner.result(record.FAIL, evidence)
    return runner.result(record.UNOBSERVED if unobserved else record.PASS, evidence)
