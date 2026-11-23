"""E2: a user-originated shell command (the server's shell routes) does not run through the tool hooks.

Under ``opencode serve`` two routes run a shell command for the user: ``POST /api/session/{id}/shell``
and ``POST /api/shell``. Each command touches its own marker. Record fields: ``user_shell_hook_fires``
(any ``tool.execute.before``, ``tool.execute.after`` or ``permission.evaluate`` entry in the probe log
while only user shell commands ran) and ``shell_create_before_fires`` (a ``shell.create.before``
entry, the only hook those routes reach). The terminal ``!`` form of a user shell command needs an
interactive terminal and is recorded unobservable. pass when both commands ran and
``shell.create.before`` fired; absent when they ran but no hook of any kind saw them; unobserved
when a command did not run or the session could not be created. Not a stopping side: the case
records the shape of the gap the shim lives with.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E2"
K7 = False

FIELDS = ("user_shell_hook_fires", "shell_create_before_fires")
TOOL_HOOKS = ("tool.execute.before", "tool.execute.after", "permission.evaluate")
TUI_REASON = "needs an interactive terminal"
MARKERS = ("m-e2-session-shell", "m-e2-api-shell")
SCENARIO = {"models": {"m1": [{"text": "hi"}]}}


def run(ctx):
    h = ctx.harness()
    with h.serve(SCENARIO, label="user-shell") as svc:
        root = svc.world.root
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        session = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not session:
            return runner.result(record.UNOBSERVED, {"create_session": list(created)})
        statuses = {
            "session_shell": svc.call("POST", f"/api/session/{session}/shell",
                                      {"command": f"touch {root}/{MARKERS[0]}"})[0],
            "api_shell": svc.call("POST", "/api/shell", {"command": f"touch {root}/{MARKERS[1]}"})[0]}
        ran = {marker: bool(svc.wait_until(lambda m=marker: (root / m).exists(), 15)) for marker in MARKERS}
        probe = svc.world.probe()
    seen = {name: sum(1 for e in probe if e.get("at") == name) for name in (*TOOL_HOOKS, "shell.create.before")}
    evidence = {"user_shell_hook_fires": any(seen[name] for name in TOOL_HOOKS),
                "shell_create_before_fires": bool(seen["shell.create.before"]),
                "hook_entries_seen": seen, "response_statuses": statuses, "commands_ran": ran,
                "tui_bang_shell": sh.unobservable(TUI_REASON)}
    if not all(ran.values()):
        return runner.result(record.UNOBSERVED, evidence)
    if not evidence["shell_create_before_fires"]:
        return runner.result(record.ABSENT, evidence)
    return runner.result(record.PASS, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, unobservable_parts={"tui_bang_shell": TUI_REASON})
