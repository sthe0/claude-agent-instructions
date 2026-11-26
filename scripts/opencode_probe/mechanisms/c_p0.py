"""P0: the probe plugin loads in an isolated run and its hooks register.

pass when the plugin logged ``loaded`` and every hook registration it attempted succeeded;
fail when the plugin was not loaded or a registration raised.
"""
from opencode_probe import record, runner

CASE_ID = "P0"
K7 = False


def run(ctx):
    h = ctx.harness()
    run = h.run({"models": {"m1": [{"text": "hello"}]}}, label="plain")
    loaded = bool(run.probe_entries("loaded"))
    registrations = {e["hook"]: e["ok"] for e in run.probe_entries("register")}
    evidence = {"loaded": loaded, "registrations": registrations, "exit_code": run.exit_code,
                "stub_requests": len(run.requests)}
    ok = loaded and registrations and all(registrations.values())
    return runner.result(record.PASS if ok else record.FAIL, evidence)
