"""P5: the question tool holds a session on a pending form under ``serve``, and the answer reaches the model.

Under ``opencode run --standalone`` the question is cancelled by the client (recorded as evidence:
the tool part's error text). Under ``opencode serve`` the same scripted session leaves a pending
form (``GET /api/session/{id}/form``: kind ``question``, the options as fields); a reply
(``POST .../form/{id}/reply`` with ``{"answer": {<field key>: <value>}}``) clears it and the next
model request carries the answer in the question call's tool message. pass when the form is
pending with the scripted options, the reply is accepted and the answer is seen in that tool
message; absent when the form is never pending (the question cannot be answered programmatically);
unobserved when the server or the session could not be driven; fail when the reply is accepted but
the model never sees the answer.
"""
import json

from opencode_probe import record, runner

CASE_ID = "P5"
K7 = True

QUESTION = {"questions": [{"question": "Pick one", "header": "Pick",
                           "options": [{"label": "alpha", "description": "first"},
                                       {"label": "beta", "description": "second"}]}]}
SCENARIO = {"models": {"m1": [{"text": "asking", "tool_calls": [{"name": "question", "arguments": QUESTION}]},
                              {"text": "done"}]}}
CALL_ID = "call_m1_0_0"
ANSWER = "alpha"


def _tool_message(requests):
    for request in requests:
        for message in request.get("messages", []):
            if message.get("role") == "tool" and message.get("tool_call_id") == CALL_ID:
                return message.get("content")
    return None


def run(ctx):
    h = ctx.harness()
    noninteractive = h.run(SCENARIO, label="run-standalone", timeout=60)
    cancelled = (noninteractive.tool_part(CALL_ID) or {}).get("state", {})
    evidence = {"run_standalone": {"exit_code": noninteractive.exit_code, "tool_status": cancelled.get("status"),
                                   "tool_error": cancelled.get("error")}}
    with h.serve(SCENARIO, label="serve") as svc:
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        session = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not session:
            return runner.result(record.UNOBSERVED, {**evidence, "create_session": list(created)})
        svc.call("POST", f"/api/session/{session}/prompt", {"text": "go"})
        forms = svc.wait_until(lambda: (svc.call("GET", f"/api/session/{session}/form")[1] or {}).get("data"), 30)
        evidence["pending_forms"] = forms or []
        if not forms:
            evidence["messages_status"] = svc.call("GET", f"/api/session/{session}/message")[0]
            return runner.result(record.ABSENT, evidence)
        form = forms[0]
        fields = form.get("fields") or []
        labels = [o.get("label") for f in fields for o in f.get("options", [])]
        answer = {f["key"]: ANSWER for f in fields if ANSWER in [o.get("value") for o in f.get("options", [])]}
        reply = svc.call("POST", f"/api/session/{session}/form/{form['id']}/reply", {"answer": answer})
        evidence["form"] = {"kind": (form.get("metadata") or {}).get("kind"), "title": form.get("title"),
                            "field_keys": [f.get("key") for f in fields], "option_labels": labels,
                            "answer_sent": answer, "reply_status": reply[0]}
        svc.wait_until(lambda: _tool_message(svc.world.requests()), 20)
        message = _tool_message(svc.world.requests())
        evidence["tool_message_seen_by_model"] = message
        evidence["pending_after_reply"] = (svc.call("GET", f"/api/session/{session}/form")[1] or {}).get("data")
    if (evidence["form"]["kind"] != "question" or sorted(labels) != ["alpha", "beta"] or not answer
            or reply[0] not in (200, 204)):
        return runner.result(record.UNOBSERVED, json.loads(json.dumps(evidence, default=str)))
    return runner.result(record.PASS if message and ANSWER in message else record.FAIL, evidence)
