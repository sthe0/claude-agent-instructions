"""E9: ``opencode run`` takes its prompt from standard input when no prompt argument is given.

The command line has no positional prompt and a distinctive token arrives on stdin; the stub's
request that carries tools must contain the token in a message. Record field: ``stdin_prompt``
(the token reached the model). pass when it did; absent when the run completed without the token
in any request; unobserved when the run timed out or the stub saw no model request.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E9"
K7 = False

FIELDS = ("stdin_prompt",)
TOKEN = "go-from-stdin"
SCENARIO = {"models": {"m1": [{"text": "hello"}]}}


def run(ctx):
    h = ctx.harness()
    with h.world(SCENARIO) as world:
        result = sh.launch_logged(h, world, sh.run_argv(world), "stdin-prompt", stdin=f"{TOKEN}\n")
    model_requests = [r for r in result.requests if r.get("tools")]
    evidence = {"stdin_prompt": any(TOKEN in str(r.get("messages")) for r in model_requests),
                "exit_code": result.exit_code, "model_requests": len(model_requests), "argv": result.argv[1:]}
    if result.timed_out or not model_requests:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.PASS if evidence["stdin_prompt"] else record.ABSENT, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS)
