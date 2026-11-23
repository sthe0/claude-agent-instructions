"""E3: the ``execute`` (code mode) sandbox cannot reach a shell, and the tool hook sees its inner calls.

One run drives ``execute`` calls that try to spawn a process by every route the sandbox might offer
(``Bun.spawn``, ``import('node:child_process')``, Bun's shell template, ``require``) and by an inner
shell tool; each touches its own marker, and a last call lists the tool names of the builtin
``opencode`` namespace. A second run drives an ``execute`` call into a tool of an MCP namespace
(``probecm``) and reads which ``tool.execute.before`` entries the hook logged. Record fields:
``execute_reaches_shell`` (a marker exists or an attempt reported success) and ``hook_sees_inner``
(a before-entry for the namespaced tool whose id is not the wrapper call's id), plus
``builtin_namespace_tools`` (the list as the sandbox reported it). pass when the sandbox reaches
no shell and the hook sees the inner call; fail when it reaches a shell or the hook misses the
inner call; unobserved when no attempt ran or the inner call's own effect is absent.
"""
import json

from opencode_probe import paths, record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E3"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("execute_reaches_shell", "hook_sees_inner", "builtin_namespace_tools")
ATTEMPTS = {
    "bun-spawn": ("m-e3-bun", "const p = Bun.spawn(['touch','{root}/m-e3-bun']); await p.exited; return 'reached'"),
    "node-child-process": ("m-e3-cp", "const cp = await import('node:child_process'); "
                                      "cp.execSync('touch {root}/m-e3-cp'); return 'reached'"),
    "bun-shell-template": ("m-e3-dollar", "const {$} = Bun; await $`touch {root}/m-e3-dollar`; return 'reached'"),
    "require": ("m-e3-req", "const cp = require('child_process'); cp.execSync('touch {root}/m-e3-req'); "
                            "return 'reached'"),
    "inner-shell-tool": ("m-e3-inner", "await tools.opencode.shell({command: 'touch {root}/m-e3-inner'}); "
                                       "return 'reached'"),
}
NAMESPACE_CODE = "return JSON.stringify(Object.keys(tools.opencode))"
SCENARIO = {"models": {"m1": [
    {"text": name, "tool_calls": [{"name": "execute", "arguments": {"code": code}}]}
    for name, (_marker, code) in ATTEMPTS.items()
] + [{"text": "namespace", "tool_calls": [{"name": "execute", "arguments": {"code": NAMESPACE_CODE}}]},
     {"text": "done"}]}}
NAMESPACE_CALL = f"call_m1_{len(ATTEMPTS)}_0"


def verdict(observation):
    stopping = observation["execute_reaches_shell"] or not observation["hook_sees_inner"]
    return sh.decide(stopping, STOPPING_IS_FAIL)


def _namespace_tools(part):
    output = str(((part or {}).get("state") or {}).get("output") or "")
    start, end = output.find("["), output.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        names = json.loads(output[start:end + 1])
    except ValueError:
        return None
    return names if isinstance(names, list) else None


def run(ctx):
    h = ctx.harness()
    attempts = h.run(SCENARIO, label="shell-attempts", timeout=120)
    markers = {name: attempts.exists(marker) for name, (marker, _code) in ATTEMPTS.items()}
    outcomes = {}
    for index, name in enumerate(ATTEMPTS):
        state = (attempts.tool_part(f"call_m1_{index}_0") or {}).get("state") or {}
        outcomes[name] = {"status": state.get("status"), "text": str(state.get("output") or state.get("error"))[:200]}
    reported = [name for name, o in outcomes.items() if "reached" in o["text"] and o["status"] == "completed"]

    plan = paths.build_plan(include=(paths.INNER_PATH_ID,))
    inner = paths.exercise(h, plan, "inner-call")
    wrapper_id = plan.call_ids[paths.INNER_PATH_ID]
    seen = [e["input"] for e in inner.probe_entries("tool.execute.before")
            if paths.INNER_NAMESPACE in str((e.get("input") or {}).get("tool"))
            and (e.get("input") or {}).get("id") != wrapper_id]
    observation = {"execute_reaches_shell": any(markers.values()) or bool(reported),
                   "hook_sees_inner": bool(seen),
                   "builtin_namespace_tools": _namespace_tools(attempts.tool_part(NAMESPACE_CALL))}
    evidence = {**observation, "markers": markers, "attempt_outcomes": outcomes,
                "inner_tool_names": sorted({str(i.get("tool")) for i in seen}),
                "inner_control_effect": inner.exists("m-exec-inner")}
    attempted = [o for o in outcomes.values() if o["status"]]
    if not attempted or observation["builtin_namespace_tools"] is None or not evidence["inner_control_effect"]:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(observation), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
