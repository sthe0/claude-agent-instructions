"""E6: nothing in OpenCode limits how often an idle hook may continue a session.

A plugin continues the session from the ``experimental.idle`` hook with a synthetic message each
time the session goes idle, up to a limit of 40, over a scripted model that answers 60 turns.
Record fields: ``max_continues`` (continuations that happened) and ``guard_present`` (fewer than
the limit happened: something stopped the loop before the plugin did). Not a stopping side: the
case records whether a loop guard in the runtime can be relied on. pass when the idle hook fired;
absent when it never did.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E6"
K7 = False

FIELDS = ("guard_present", "max_continues")
LIMIT = 40
SCENARIO = {"models": {"m1": [{"text": f"turn-{i}"} for i in range(LIMIT + 20)]}}
IDLE_PLUGIN = """import { appendFileSync } from "node:fs";
const LOG = process.env.PROBE_LOG || "/dev/null";
const LIMIT = Number(process.env.IDLE_LIMIT || "0");
const log = (e) => appendFileSync(LOG, JSON.stringify(e) + "\\n");
export default {
  id: "e6-idle-plugin",
  setup(ctx) {
    let n = 0;
    ctx.session.hook("experimental.idle", async (input) => {
      n += 1;
      log({ at: "idle", n });
      if (n <= LIMIT) {
        await ctx.session.synthetic({ sessionID: input.sessionID, text: `continue-${n}`, metadata: { source: "probe" } });
        log({ at: "idle.continued", n });
      }
    });
  },
};
"""


def run(ctx):
    h = ctx.harness()
    result = h.run(SCENARIO, label="idle-loop", env={"IDLE_LIMIT": str(LIMIT)}, timeout=240,
                   files={"pdir/index.mjs": IDLE_PLUGIN})
    idles = result.probe_entries("idle")
    continues = result.probe_entries("idle.continued")
    evidence = {"idle_limit": LIMIT, "max_continues": len(continues), "idle_calls": len(idles),
                "guard_present": len(continues) < LIMIT, "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "model_requests": sum(1 for r in result.requests if r.get("tools"))}
    if result.timed_out:
        return runner.result(record.UNOBSERVED, evidence)
    if not idles:
        return runner.result(record.ABSENT, evidence)
    return runner.result(record.PASS, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS)
