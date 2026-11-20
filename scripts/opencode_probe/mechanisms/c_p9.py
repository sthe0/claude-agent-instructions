"""P9: the ``shell.create.before`` hook can put the session id into a shell call's environment.

The probe plugin sets ``AGENTCTL_HARNESS_SESSION_ID`` in the edited env to the session id; a
scripted shell call writes that variable to a file. pass when the file holds the session id of
the run; fail when the hook ran but the value is missing or different; absent when the hook was
never called, its input carried no session id, or registering it raised; unobserved when the
call did not execute.
"""
from opencode_probe import record, runner

CASE_ID = "P9"
K7 = False
VARIABLE = "AGENTCTL_HARNESS_SESSION_ID"


def run(ctx):
    h = ctx.harness()
    scenario = {"models": {"m1": [{"text": "p9", "tool_calls": [{"name": "shell", "arguments": {
        "command": f"printenv {VARIABLE} > {{root}}/p9-echo; touch {{root}}/p9-ran"}}]}, {"text": "done"}]}}
    run = h.run(scenario, label="env-hook", rules={"env": {VARIABLE: "{sessionID}"}})
    registered = {e["hook"]: e["ok"] for e in run.probe_entries("register")}
    calls = run.probe_entries("shell.create.before")
    session_ids = run.session_ids()
    echoed = (run.read("p9-echo") or "").strip()
    alternative = h.run(scenario, label="command-rewrite",
                        rules={"command_prefix_env": {VARIABLE: "{sessionID}"}})
    alt_echo = (alternative.read("p9-echo") or "").strip()
    evidence = {"registered": registered.get("shell.create.before"), "hook_calls": len(calls),
                "hook_input_keys": sorted(calls[0]["input"]) if calls else [],
                "session_ids": session_ids, "echoed": echoed, "call_ran": run.exists("p9-ran"),
                "alternative_tool_execute_before_command_rewrite": {
                    "echoed": alt_echo, "session_ids": alternative.session_ids(),
                    "carries_session_id": bool(alt_echo) and alt_echo in alternative.session_ids()}}
    if registered.get("shell.create.before") is not True or not calls:
        return runner.result(record.ABSENT, evidence)
    if not run.exists("p9-ran"):
        return runner.result(record.UNOBSERVED, evidence)
    if not echoed and not any(k in calls[0]["input"] for k in ("sessionID", "toolCall")):
        return runner.result(record.ABSENT, evidence)
    return runner.result(record.PASS if echoed and echoed in session_ids else record.FAIL, evidence)
