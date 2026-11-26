"""P4: the ``experimental.idle`` hook fires at the end of a turn and can continue the session.

The probe plugin, given ``idle_continue``, calls ``session.synthetic`` from that hook. pass
when the stub then sees a further turn whose request carries the synthetic text; absent when
the hook fired (or never fired) but no continued turn reached the model; unobserved when the
run never reached the stub.
"""
from opencode_probe import record, runner

CASE_ID = "P4"
K7 = False
CONTINUE_TEXT = "p4-continue-marker"


def run(ctx):
    h = ctx.harness()
    scenario = {"models": {"m1": [{"text": "first turn"}, {"text": "second turn"}]}}
    run = h.run(scenario, label="idle-continue", rules={"idle_continue": CONTINUE_TEXT})
    chat = [r for r in run.requests if r.get("tools")]
    if not chat:
        return runner.result(record.UNOBSERVED, {"error": "the stub saw no chat request",
                                                 "exit_code": run.exit_code})
    idle = run.probe_entries("session.experimental.idle")
    continued = [r for r in chat[1:] if CONTINUE_TEXT in str(r.get("messages"))]
    evidence = {"idle_fired": len(idle), "idle_continued_logged": len(run.probe_entries("idle.continued")),
                "chat_requests": len(chat), "continued_turn_seen": bool(continued),
                "idle_input_keys": sorted(idle[0]["input"]) if idle else [], "exit_code": run.exit_code}
    return runner.result(record.PASS if continued else record.ABSENT, evidence)
