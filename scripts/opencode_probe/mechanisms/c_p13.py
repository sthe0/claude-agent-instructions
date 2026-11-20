"""P13: which hook mechanisms exist, whether they fire and whether they can refuse.

One entry per event PreToolUse, PostToolUse, UserPromptSubmit, SessionStart, SessionEnd,
Stop, SubagentStop and ``permission.evaluate``: {mechanism, fired, can_refuse, evidence}.
A session with a shell call and a subagent call shows what fires; a throw in
``tool.execute.before``, a deny in ``permission.evaluate`` and a throw in ``session.prompt`` show
which can refuse (a refusal is judged by the marker file the call would have written, and by the
absence of a model request for the prompt). fail when PreToolUse or ``permission.evaluate`` did
not fire or did not refuse (``marked_executed`` is then true); absent when both refuse but
another listed event has no mechanism that fired; pass when every listed event fired.
"""
from opencode_probe import record, runner

CASE_ID = "P13"
K7 = False
REST_ON_PLUGIN_DENY = True

SCENARIO = {"models": {
    "m1": [{"text": "p13", "tool_calls": [{"name": "shell", "arguments": {"command": "touch {root}/m-p13"}}]},
           {"text": "sub", "tool_calls": [{"name": "subagent", "arguments": {
               "agent": "general", "model": "stub/m2", "description": "p13", "prompt": "p13-child"}}]},
           {"text": "done"}],
    "m2": [{"text": "child", "tool_calls": [{"name": "shell", "arguments": {"command": "touch {root}/m-p13-child"}}]},
           {"text": "child done"}]}}
SESSION_START_EVENT = ("session.created",)
SESSION_END_EVENT = ("session.execution.succeeded", "session.deleted")


def _events(run):
    found = []
    for entry in run.probe_entries("event"):
        event = entry.get("event")
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            data = event.get("data")
            found.append((event["type"], data.get("sessionID") if isinstance(data, dict) else None))
    return found


def run(ctx):
    h = ctx.harness()
    observe = h.run(SCENARIO, label="observe", wait_for=("m-p13-child",))
    thrown = h.run(SCENARIO, label="throw-before", rules={"throw": [{"name": "shell", "contains": "m-p13"}]})
    denied = h.run(SCENARIO, label="deny-evaluate", rules={"deny": [{"name": "shell", "contains": "m-p13"}]})
    prompt = h.run(SCENARIO, label="refuse-prompt", rules={"refuse_prompt": True})
    events = _events(observe)
    types = sorted({t for t, _ in events})
    root_sessions = {e["input"].get("sessionID") for e in observe.probe_entries("session.prompt")
                     if (e["input"].get("prompt") or {}).get("text") == "go"}
    idle_sessions = [e["input"].get("sessionID") for e in observe.probe_entries("session.experimental.idle")]
    child_ended = sorted({s for t, s in events if t in SESSION_END_EVENT and s and s not in root_sessions})
    child_idle = [s for s in idle_sessions if s not in root_sessions]
    chat_in_prompt_refusal = [r for r in prompt.requests if r.get("tools")]
    marked_executed = thrown.exists("m-p13") or denied.exists("m-p13")

    def entry(name, fired, can_refuse, **evidence):
        return {"mechanism": name, "fired": bool(fired), "can_refuse": bool(can_refuse), "evidence": evidence}

    mechanisms = [
        entry("PreToolUse", observe.probe_entries("tool.execute.before"), not thrown.exists("m-p13"),
              calls=len(observe.probe_entries("tool.execute.before")), control_executed=observe.exists("m-p13"),
              refused_run_executed=thrown.exists("m-p13")),
        entry("PostToolUse", observe.probe_entries("tool.execute.after"), False,
              calls=len(observe.probe_entries("tool.execute.after")),
              note="fires after the call's effect; nothing left to refuse"),
        entry("UserPromptSubmit", observe.probe_entries("session.prompt"), not chat_in_prompt_refusal,
              calls=len(observe.probe_entries("session.prompt")),
              model_requests_when_prompt_hook_throws=len(chat_in_prompt_refusal),
              exit_code_when_prompt_hook_throws=prompt.exit_code),
        entry("SessionStart", any(t in SESSION_START_EVENT for t in types), False,
              mapped_to=list(SESSION_START_EVENT)),
        entry("SessionEnd", any(t in SESSION_END_EVENT for t in types), False,
              mapped_to=list(SESSION_END_EVENT)),
        entry("Stop", idle_sessions, False, mapped_to="session.experimental.idle", sessions=len(set(idle_sessions)),
              note="can continue a session (P4), not refuse a call"),
        entry("SubagentStop", child_ended or child_idle, False,
              mapped_to="session.execution.succeeded or session.experimental.idle in a child session",
              child_sessions_ended=len(child_ended), child_sessions_idle=len(set(child_idle))),
        entry("permission.evaluate", observe.probe_entries("permission.evaluate"), not denied.exists("m-p13"),
              calls=len(observe.probe_entries("permission.evaluate")), control_executed=observe.exists("m-p13"),
              refused_run_executed=denied.exists("m-p13")),
    ]
    by_name = {m["mechanism"]: m for m in mechanisms}
    refusers = [by_name["PreToolUse"], by_name["permission.evaluate"]]
    evidence = {"marked_executed": marked_executed, "event_types_seen": types,
                "control_executed": observe.exists("m-p13"), "child_marker": observe.exists("m-p13-child")}
    if not observe.exists("m-p13"):
        return runner.result(record.UNOBSERVED, evidence, mechanisms=mechanisms)
    if not all(m["fired"] and m["can_refuse"] for m in refusers):
        return runner.result(record.FAIL, evidence, mechanisms=mechanisms)
    status = record.PASS if all(m["fired"] for m in mechanisms) else record.ABSENT
    return runner.result(status, evidence, mechanisms=mechanisms)


def check_record(entry, cases):
    """A non-empty ``mechanisms`` list, each entry saying whether it fired and whether it can refuse."""
    listed = entry.get("mechanisms")
    if not listed:
        return ["no `mechanisms` list"]
    return [f"mechanism {m.get('mechanism')!r} lacks fired or can_refuse" for m in listed
            if not m.get("mechanism") or "fired" not in m or "can_refuse" not in m]
