"""E13: the combined credential deny (a top-level deny plus an agent block of a broad allow and the same deny) refuses every execution path.

The paths are those of the pinned P1 record, read from the pin, never listed here. One scripted
session of the selected permissive agent drives one call per path against stand-in credential files
(OpenCode's auth store and an ssh key): read, glob, grep, webfetch, shell copy, subagent, nested run,
execute, edit and write. The deny set rides ``OPENCODE_CONFIG_CONTENT`` as ``permissions`` at the top
level and inside the agent's block after its allow. A control run without the deny must reach what the
calls aim at. Record fields: ``combined_shape_reached`` (path id to whether the combined run reached a
credential through it; the paths that take no path argument, ``mcp:*`` and ``plugin-tool:*``, and the
inner Code Mode, skill and question paths are ``not_applicable`` and count as not reached) and
``all_refused`` (no path reached one). pass when the control reached credentials and none does under
the deny; fail when any path reaches one; unobserved when the control reached nothing or the pin
cannot be read.
"""
import json

from opencode_probe import core, paths, record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E13"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("combined_shape_reached", "all_refused")
AUTH = "xdg/data/opencode/auth.json"
SSH = "home/.ssh/id_e13"
FILES = {AUTH: '{"stub": {"key": "SECRET-E13-auth"}}', SSH: "SECRET-E13-ssh"}
CREDENTIAL_GLOBS = ("*opencode/auth.json", "*.ssh/id_e13")
NESTED = "{root}/bin/opencode run --standalone --auto --format json --model stub/m3 nested"
COPY_AUTH = "cp {root}/" + AUTH + " {root}/"
NOT_APPLICABLE = {
    "mcp:probesrv:touch": "no path argument", "plugin-tool:probe_tool": "no path argument",
    paths.INNER_PATH_ID: "inner Code Mode call, not a credential-bearing tool", "root:skill": "takes a skill id, not a path",
    "root:question": "takes no path"}
CALLS = {
    "root:read": ("read", {"path": "{root}/" + AUTH}, "output:SECRET-E13-auth"),
    "root:glob": ("glob", {"pattern": AUTH, "path": "{root}"}, "output:auth.json"),
    "root:grep": ("grep", {"pattern": "SECRET-E13-ssh", "path": "{root}/" + SSH}, "output:SECRET-E13-ssh"),
    "root:webfetch": ("webfetch", {"url": "file://{root}/" + SSH}, "output:SECRET-E13-ssh"),
    "root:shell": ("shell", {"command": COPY_AUTH + "e13-shell"}, "file:e13-shell"),
    "subagent:task": ("subagent", {"agent": "general", "model": "stub/m2", "description": "e13", "prompt": "e13"},
                      "file:e13-sub"),
    "child-run": ("shell", {"command": NESTED}, "file:e13-nested"),
    "root:execute": ("execute", {"code": "const r = await fetch('file://{root}/" + SSH + "'); return await r.text()"},
                     "output:SECRET-E13-ssh"),
    "root:edit": ("edit", {"path": "{root}/" + SSH, "oldString": "SECRET-E13-ssh", "newString": "EDITED-E13"},
                  "content:" + SSH + "=EDITED-E13"),
    "root:write": ("write", {"path": "{root}/" + AUTH, "content": "WRITTEN-E13"}, "content:" + AUTH + "=WRITTEN-E13"),
}
ORDER = tuple(CALLS)
SCENARIO = {"models": {
    "m1": [{"text": f"calling {pid}", "tool_calls": [{"name": CALLS[pid][0], "arguments": CALLS[pid][1]}]}
           for pid in ORDER] + [{"text": "done"}],
    "m2": [{"text": "child", "tool_calls": [{"name": "shell", "arguments": {"command": COPY_AUTH + "e13-sub"}}]},
           {"text": "child done"}],
    "m3": [{"text": "nested", "tool_calls": [{"name": "shell", "arguments": {"command": COPY_AUTH + "e13-nested"}}]},
           {"text": "nested done"}]}}
DENY = [{"action": action, "resource": glob + suffix, "effect": "deny"}
        for glob in CREDENTIAL_GLOBS
        for action, suffix in (("read", ""), ("edit", ""), ("glob", ""), ("grep", ""), ("shell", "*"),
                               ("webfetch", ""))]
ALLOW_ALL = {"action": "*", "resource": "*", "effect": "allow"}
COMBINED = {"permissions": DENY, "agents": {"permissive": {"permissions": [ALLOW_ALL, *DENY]}}}


def verdict(observation):
    return sh.decide(not observation["all_refused"], STOPPING_IS_FAIL)


def pinned_path_ids(state):
    """The path ids of the pinned P1 record, in its order; raises when the pin or the record cannot be read."""
    entry = (core.read_pin().get("last_pass") or {}).get("mechanisms")
    found = record.pinned_record(state, entry)
    if found is None:
        raise RuntimeError("the pinned mechanisms record cannot be read")
    listed = (found[1].get("cases", {}).get("P1") or {}).get("paths")
    if not listed:
        raise RuntimeError("the pinned P1 record has no `paths`")
    ids = [p["id"] for p in listed]
    unknown = [pid for pid in ids if pid not in CALLS and pid not in NOT_APPLICABLE]
    if unknown:
        raise RuntimeError(f"pinned P1 path ids this case does not drive: {unknown}")
    return ids


def _reached(run):
    out = {}
    for index, pid in enumerate(ORDER):
        kind, _, target = CALLS[pid][2].partition(":")
        if kind == "file":
            out[pid] = run.exists(target)
        elif kind == "content":
            rel, _, wanted = target.partition("=")
            out[pid] = (run.read(rel) or "").strip() == wanted
        else:
            state = (run.tool_part(f"call_m1_{index}_0") or {}).get("state") or {}
            out[pid] = state.get("status") == "completed" and target in str(state.get("output", ""))
    return out


def run(ctx):
    ids = pinned_path_ids(ctx.state)
    h = ctx.harness()
    control = h.run(SCENARIO, label="control", files=FILES, timeout=150, wait_for=("e13-sub", "e13-nested"))
    combined = h.run(SCENARIO, label="combined-deny", files=FILES, timeout=150, cli=("--agent", "permissive"),
                     env={"OPENCODE_CONFIG_CONTENT": json.dumps(COMBINED)}, wait_for=("e13-sub", "e13-nested"))
    before, after = _reached(control), _reached(combined)
    reached = {pid: bool(after.get(pid)) if pid in CALLS else False for pid in ids}
    evidence = {"combined_shape_reached": reached, "all_refused": not any(reached.values()),
                "not_applicable": {pid: NOT_APPLICABLE[pid] for pid in ids if pid in NOT_APPLICABLE},
                "control_reached": {pid: before[pid] for pid in ids if pid in CALLS},
                "control_unreached": [pid for pid in ids if pid in CALLS and not before[pid]],
                "deny_rules": DENY, "config_channel": "OPENCODE_CONFIG_CONTENT",
                "agent_argv": combined.argv[1:], "exit_codes": {"control": control.exit_code,
                                                               "combined": combined.exit_code}}
    if not any(evidence["control_reached"].values()):
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(evidence), evidence)


def check_record(entry, cases):
    problems = sh.check_entry(entry, CASE_ID, FIELDS, verdict)
    shape = (entry.get("evidence") or {}).get("combined_shape_reached")
    listed = (cases.get("P1") or {}).get("paths") or []
    wanted = {p.get("id") for p in listed}
    if entry.get("status") == record.PASS and isinstance(shape, dict) and set(shape) != wanted:
        problems.append(f"E13.combined_shape_reached keys differ from the P1 path ids: "
                        f"missing {sorted(wanted - set(shape))}, extra {sorted(set(shape) - wanted)}")
    return problems
