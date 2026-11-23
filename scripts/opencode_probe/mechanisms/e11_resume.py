"""E11: ``opencode run -s <session id>`` resumes a session with its history.

A first run says a distinctive token; its session id is read from the output events; a second run
names that session and sends a new prompt. Record field: ``resume_works`` (the second run continues
the same session and the stub's request carries the first run's token). pass when it does; absent
when the second run completed without the history; unobserved when the first run gave no session id
or a run timed out.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E11"
K7 = False

FIELDS = ("resume_works",)
TOKEN = "remember-token-ZEBRA"
SCENARIO = {"models": {"m1": [{"text": "first answer"}, {"text": "second answer"}]}}


def run(ctx):
    h = ctx.harness()
    with h.world(SCENARIO) as world:
        first = sh.launch_logged(h, world, sh.run_argv(world, prompt=TOKEN), "first-run")
        session = next(iter(first.session_ids()), None)
        if not session:
            return runner.result(record.UNOBSERVED, {"first_exit_code": first.exit_code, "session_id": None})
        seen_before = len(first.requests)
        second = sh.launch_logged(h, world, sh.run_argv(world, extra=("-s", session), prompt="what-did-i-say"),
                                  "second-run")
    later = [r for r in second.requests[seen_before:] if r.get("tools")]
    same_session = second.session_ids() == [session]
    evidence = {"resume_works": same_session and any(TOKEN in str(r.get("messages")) for r in later),
                "same_session": same_session, "history_in_second_request": any(
                    TOKEN in str(r.get("messages")) for r in later),
                "exit_codes": [first.exit_code, second.exit_code], "second_run_requests": len(later)}
    if first.timed_out or second.timed_out or not later:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.PASS if evidence["resume_works"] else record.ABSENT, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS)
