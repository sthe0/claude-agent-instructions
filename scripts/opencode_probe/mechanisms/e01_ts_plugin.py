"""E1: a TypeScript plugin file named by its directory's package.json ``main`` loads and its hooks fire.

The plugin directory is the probe plugin's own, with ``package.json`` and ``index.ts`` replaced by a
TypeScript source (an interface, type annotations, ``as``) that logs ``loaded`` and each
``tool.execute.before`` call; a scripted ``read`` call is the hook's trigger. Record fields:
``loaded`` (the plugin's own ``loaded`` entry of language ``ts``) and ``hooks_fired`` (its
``tool.execute.before`` entry for the read call). pass when both are true; fail when either is
false (the whole shim rests on a ``.ts`` plugin); unobserved when the run timed out. A bare ``.ts``
path as the plugin entry is not driven.
"""
import json

from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E1"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("loaded", "hooks_fired")
CALL_ID = "call_m1_0_0"
SCENARIO = {"models": {"m1": [{"text": "r", "tool_calls": [{"name": "read", "arguments": {"path": "{root}/f.txt"}}]},
                              {"text": "done"}]}}
PACKAGE = json.dumps({"name": "e1-ts-plugin", "version": "0.0.0", "type": "module", "main": "index.ts",
                      "exports": {".": "./index.ts"}})
SOURCE = """import { appendFileSync } from "node:fs";
interface Entry { at: string; [k: string]: unknown }
const LOG: string = process.env.PROBE_LOG || "/dev/null";
const log = (e: Entry): void => appendFileSync(LOG, JSON.stringify(e) + "\\n");
export default {
  id: "e1-ts-plugin",
  setup(ctx: any) {
    log({ at: "loaded", lang: "ts" });
    ctx.tool.hook("execute.before", (input: any) => {
      log({ at: "tool.execute.before", tool: input.tool as string, id: input.id });
    });
  },
};
"""


def verdict(observation):
    return sh.decide(not observation["loaded"] or not observation["hooks_fired"], STOPPING_IS_FAIL)


def run(ctx):
    h = ctx.harness()
    result = h.run(SCENARIO, label="ts-plugin", timeout=90,
                   files={"f.txt": "hello", "pdir/package.json": PACKAGE, "pdir/index.ts": SOURCE})
    observation = {
        "loaded": any(e.get("lang") == "ts" for e in result.probe_entries("loaded")),
        "hooks_fired": any(e.get("tool") == "read" and e.get("id") == CALL_ID
                           for e in result.probe_entries("tool.execute.before"))}
    evidence = {**observation, "exit_code": result.exit_code, "plugin_entry": "directory package.json main index.ts",
                "bare_ts_path_entry": "not observed"}
    if result.timed_out:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(observation), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
