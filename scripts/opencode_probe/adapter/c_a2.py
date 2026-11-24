"""A2: the answer to a question reaches a fresh answer ledger through the gate shim and Core's answer hook.

Under ``opencode serve`` the scripted session asks a question; the case answers the pending form. The shim's
``tool.execute.after`` forwards the answer (``input.result.output.answers``), the bridge hands it to
Core's unchanged ``hook-ask-answer-record.py`` as an ``AskUserQuestion`` response, and the hook appends a
row to the ledger beside the session's agentctl state file. The ledger is created for this run: the
isolated agent home holds an empty state file for the OpenCode session id and nothing else, and the case
reads the ledger back with ``answer_ledger.read``. pass when the ledger holds one row whose answers carry the
chosen label and whose offered options carry both labels; fail when the answer was given and the ledger
is empty or absent; absent when the form never appears; unobserved when the server or the session
could not be driven.
"""
import dataclasses
import json

from opencode_probe import record, runner
from opencode_probe.adapter import shared_a as sa
from opencode_probe.mechanisms import c_p5

CASE_ID = "A2"
K7 = True


def _state_file(root, session):
    from lib import config_root

    return root / "home" / ".claude-agent" / "agentctl" / "state" / f"{config_root.sanitize_session_id(session)}.json"


def _ledger(state_file):
    from agentctl import answer_ledger

    read = answer_ledger.read(state_file)
    return {"exists": read.exists, "records": [dataclasses.asdict(r) for r in read.records], "skipped": read.skipped}


def run(ctx):
    h = ctx.harness()
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": ""}
    with h.serve(c_p5.SCENARIO, label="serve", config=sa.gate_config(), files=files, env=sa.gate_env()) as svc:
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        session = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not session:
            return runner.result(record.UNOBSERVED, {"create_session": list(created)})
        state_file = _state_file(svc.world.root, session)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{}\n", encoding="utf-8")
        svc.call("POST", f"/api/session/{session}/prompt", {"text": "go"})
        forms = svc.wait_until(lambda: (svc.call("GET", f"/api/session/{session}/form")[1] or {}).get("data"), 30)
        if not forms:
            return runner.result(record.ABSENT, {"pending_forms": []})
        form = forms[0]
        fields = form.get("fields") or []
        answer = {f["key"]: c_p5.ANSWER for f in fields
                  if c_p5.ANSWER in [o.get("value") for o in f.get("options", [])]}
        reply = svc.call("POST", f"/api/session/{session}/form/{form['id']}/reply", {"answer": answer})
        svc.wait_until(lambda: _ledger(state_file)["records"], 20)
        ledger = _ledger(state_file)
        after = [e for e in svc.world.probe() if e.get("at") == "tool.execute.after"
                 and (e.get("input") or {}).get("tool") == "question"]
        log_path = svc.world.root / "gates.log"
        gate_log = sa.parse_gate_log(log_path.read_text(encoding="utf-8") if log_path.exists() else "")
    evidence = {"reply_status": reply[0], "answer_sent": answer, "ledger": json.loads(json.dumps(ledger, default=str)),
                "after_hook_seen": bool(after), "gate_log_events": sorted({e.get("at") for e in gate_log}),
                "ledger_isolated_under_run_root": True}
    if not answer or reply[0] not in (200, 204):
        return runner.result(record.UNOBSERVED, evidence)
    rows = ledger["records"]
    given = [r for r in rows if c_p5.ANSWER in json.dumps(r.get("answers"))]
    offered = [o.get("label") for r in given for q in r.get("questions", []) for o in q.get("options", [])]
    ok = len(rows) == 1 and given and sorted(offered) == ["alpha", "beta"]
    return runner.result(record.PASS if ok else record.FAIL, evidence)
