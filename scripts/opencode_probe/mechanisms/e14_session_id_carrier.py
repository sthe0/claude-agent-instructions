"""E14: the ``tool.execute.before`` hook can carry the session id to a shell command by rewriting it.

The probe plugin prefixes the shell call's command with ``export AGENTCTL_HARNESS_SESSION_ID=<session id>;``
from the before-hook; the scripted shell call writes the variable to a file. Record fields:
``carries_session_id`` (the file holds the run's session id) and ``shell_event`` (the shell tool
part as the run reported it, and the after-hook's input for the same call, showing the command
the shell received). pass when the session id is carried; fail when it is not; unobserved when the
call did not execute.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E14"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("carries_session_id", "shell_event")
VARIABLE = "AGENTCTL_HARNESS_SESSION_ID"
CALL_ID = "call_m1_0_0"
SCENARIO = {"models": {"m1": [{"text": "e14", "tool_calls": [{"name": "shell", "arguments": {
    "command": f"printenv {VARIABLE} > {{root}}/e14-echo; touch {{root}}/e14-ran"}}]}, {"text": "done"}]}}


def verdict(observation):
    return sh.decide(not observation["carries_session_id"], STOPPING_IS_FAIL)


def run(ctx):
    h = ctx.harness()
    result = h.run(SCENARIO, label="command-rewrite", rules={"command_prefix_env": {VARIABLE: "{sessionID}"}})
    echoed = (result.read("e14-echo") or "").strip()
    part = result.tool_part(CALL_ID) or {}
    state = part.get("state") or {}
    after = next((e.get("input") for e in result.probe_entries("tool.execute.after")
                  if (e.get("input") or {}).get("id") == CALL_ID), None)
    evidence = {"carries_session_id": bool(echoed) and echoed in result.session_ids(), "echoed": echoed,
                "session_ids": result.session_ids(),
                "shell_event": {"tool": part.get("tool"), "status": state.get("status"), "input": state.get("input"),
                                "output": state.get("output"), "after_hook_input": (after or {}).get("input")}}
    if not result.exists("e14-ran"):
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(evidence), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
