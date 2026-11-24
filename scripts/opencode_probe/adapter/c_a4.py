"""A4: a turn-end gate holds the turn through the shim's ``experimental.idle`` hook.

Follows P4 of the pinned mechanisms record: when P4 is not a pass the idle hook cannot continue a
session, the case is recorded ``absent`` with P4's evidence, and every Stop row of ``parity.toml``
must then be exempt with a reason and a ``covers_gap``. When P4 passes, a stand-in Stop row
blocks the first turn end; the shim continues the session with the block's reason, and the stub
must see a further turn whose request carries that reason. Under the harness's inert shim the
turn ends unheld, which is the marked outcome (``marked_executed``). pass when the continued turn
carrying the reason is seen and the row was run; fail when the row blocked and no continued
turn followed; unobserved when no chat request reached the stub or the row never ran.
"""
from opencode_probe import core, record, runner
from opencode_probe.adapter import shared_a as sa

CASE_ID = "A4"
K7 = False
REST_ON_PLUGIN_DENY = True  # P4 passes in the pinned record; were it absent, drop this line (A4 is then recorded absent)

REASON = "a4-hold-marker"
SCENARIO = {"models": {"m1": [{"text": "first turn"}, {"text": "second turn"}, {"text": "third turn"}]}}


def p4_entry(state):
    pin = core.read_pin().get("last_pass", {}).get("mechanisms")
    found = record.pinned_record(state, pin)
    if found is None:
        return None
    return (found[1].get("cases") or {}).get("P4")


def run(ctx):
    p4 = p4_entry(ctx.state or core.state_dir())
    if p4 is None:
        return runner.result(record.UNOBSERVED, {"error": "the pinned mechanisms record has no P4 entry"})
    if p4.get("status") != record.PASS:
        from lib import hook_parity

        unexempt = sa.stop_rows_not_exempt(hook_parity.load_table(hook_parity.CORE_TABLE))
        evidence = {"p4_status": p4.get("status"), "p4_evidence": p4.get("evidence"),
                    "stop_rows_not_exempt": unexempt}
        return runner.result(record.FAIL if unexempt else record.ABSENT, evidence)
    h = ctx.harness()
    rows = sa.extra_rows([{"event": "Stop", "script": str(sa.STOP_BLOCK_ONCE), "timeout_s": 10}])
    result = h.run(SCENARIO, label="stop-hold", config=sa.gate_config(),
                   files={**sa.gate_files(inert=ctx.inert), "extra-rows.toml": rows},
                   env=sa.gate_env(ADAPTER_STOP_MARK="{root}/stop-mark", ADAPTER_STOP_REASON=REASON))
    chat = [r for r in result.requests if r.get("tools")]
    if not chat:
        return runner.result(record.UNOBSERVED, {"error": "the stub saw no chat request", "exit_code": result.exit_code})
    blocked = result.exists("stop-mark")
    continued = [r for r in chat[1:] if REASON in str(r.get("messages"))]
    log = sa.gate_log(result)
    evidence = {"stop_row_blocked": blocked, "chat_requests": len(chat), "continued_turn_seen": bool(continued),
                "gate_log_events": sorted({e.get("at") for e in log}), "exit_code": result.exit_code,
                "inert_shim": bool(ctx.inert), "marked_executed": blocked and not continued}
    if not blocked:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.PASS if continued else record.FAIL, evidence)
