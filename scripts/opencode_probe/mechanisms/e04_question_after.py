"""E4: the answer to a question call reaches the ``tool.execute.after`` hook.

Under ``opencode serve`` the scripted question call leaves a pending form; the reply picks the first
option and the after-hook's entry for the question call is read from the probe log. Record fields:
``after_carries_answer`` (that entry holds the answer) and ``field`` (the dotted path of the
answers inside the hook input, ``input.result.output.answers``) with the answers found there.
pass when the answer is in the entry; fail when the entry is missing or has no answer there;
unobserved when the form never became pending or the reply was not accepted.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import c_p5
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E4"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("after_carries_answer", "field")
FIELD = "input.result.output.answers"


def verdict(observation):
    return sh.decide(not observation["after_carries_answer"] or observation["field"] != FIELD,
                     STOPPING_IS_FAIL)


def run(ctx):
    h = ctx.harness()
    with h.serve(c_p5.SCENARIO, label="question-after") as svc:
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        session = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not session:
            return runner.result(record.UNOBSERVED, {"create_session": list(created)})
        svc.call("POST", f"/api/session/{session}/prompt", {"text": "go"})
        forms = svc.wait_until(lambda: (svc.call("GET", f"/api/session/{session}/form")[1] or {}).get("data"), 30)
        if not forms:
            return runner.result(record.UNOBSERVED, {"pending_forms": []})
        form = forms[0]
        answer = {f["key"]: c_p5.ANSWER for f in form.get("fields") or []}
        reply = svc.call("POST", f"/api/session/{session}/form/{form['id']}/reply", {"answer": answer})
        entry = svc.wait_until(lambda: next((e for e in svc.world.probe() if e.get("at") == "tool.execute.after"
                                             and (e.get("input") or {}).get("id") == c_p5.CALL_ID), None), 20)
    answers = sh.get_path(entry, FIELD) if entry else None
    evidence = {"after_carries_answer": bool(answers) and c_p5.ANSWER in str(answers),
                "field": FIELD if answers is not None else None, "answers": answers,
                "answer_sent": answer, "reply_status": reply[0],
                "after_entry_input_keys": sorted((entry or {}).get("input") or [])}
    if reply[0] not in (200, 204):
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(evidence), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
