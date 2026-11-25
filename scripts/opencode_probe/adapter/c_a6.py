"""A6: what a plugin is handed (the directory of the session), and what OpenCode does with a plugin whose setup throws.

Two facts the gate shim depends on, observed rather than assumed:

* ``ctx``: a passive plugin logs the keys and scalar values of its setup context and of each hook input while
  the session runs in a directory other than the run root; the record carries the keys and whether any value
  equals that directory.
* ``setup-throw``: a plugin whose setup throws is listed before and after the gate shim, under a denying row;
  the record carries the exit code, whether the gate shim still loaded, whether the shell call the denying
  row names ran, and the head of the process's output.
* ``gate-setup-failure``: the gate shim with a bridge path that does not exist; the record carries the setup
  marker it wrote, whether the shell call ran, and whether the shim logged a refusal.
* ``path_forms`` (``facts_paths``): no gate loaded; for each written form of a path (``~``, ``~/x``, ``~user``,
  ``//x``, ``..``, empty, environment variables, ``file://``) the file a ``write`` lands on, the text a ``read``
  returns, what a ``grep`` / ``glob`` search root and a shell ``workdir`` come to.
* ``search_and_output`` (``facts_a6``): no gate loaded; what a plugin's ``tool.execute.after`` sees of ``grep``
  results (through symlinks, with and without a hit) and of a shell output far past the output limit, whether
  that output was saved to a file, and what ``ctx.session.get`` answers for a call's session.

pass when every run produced its log; unobserved otherwise. The record is the evidence, not a verdict on the gates.
"""
import json

from opencode_probe import record, runner
from opencode_probe.adapter import facts_a6, facts_paths, shared_a as sa

CASE_ID = "A6"
K7 = False

SUBDIR = "work/sub"
PACKAGE = json.dumps({"name": "adapter-probe", "version": "0.0.0", "type": "module", "main": "index.mjs",
                      "exports": {".": "./index.mjs"}})
MARKER = "m-after-throw"
SCENARIO = {"models": {"m1": [{"tool_calls": [{"name": "shell", "arguments": {"command": "touch {root}/" + MARKER}}]},
                             {"text": "done"}]}}


def _plugin_files(name, source):
    text = (sa.ASSETS / source).read_text(encoding="utf-8")
    return {f"{name}/index.mjs": text, f"{name}/package.json": PACKAGE}


def _log(run, name):
    return [json.loads(line) for line in (run.read(name) or "").splitlines() if line.strip().startswith("{")]


def run(ctx):
    h = ctx.harness()
    probe_plugin = "file://{root}/cdir"
    files = {**_plugin_files("cdir", "ctx_probe.mjs"), **_plugin_files("tdir", "throwing.mjs"),
             **sa.gate_files(inert=ctx.inert),
             "extra-rows.toml": sa.extra_rows([{"event": "PreToolUse", "matcher": "Bash",
                                                "script": str(sa.DENY_TOUCH), "timeout_s": 10}])}
    env = sa.gate_env(CTXPROBE_LOG="{root}/ctx.log", AGENT_GATE_MCP_SERVERS="")
    seen = h.run(SCENARIO, label="ctx", config={"plugins": [sa.PROBE_PLUGIN, probe_plugin]}, files=files, env=env,
                 cwd=SUBDIR)
    entries = _log(seen, "ctx.log")
    root = str(seen.root)
    directory = f"{root}/{SUBDIR}"
    found = {}
    for entry in entries:
        for group in ("scalars", "location"):
            for key, value in (entry.get(group) or {}).items():
                if value in (directory, root):
                    found[f"{entry['at']}.{group}.{key}"] = "session-directory" if value == directory else "run-root"
    facts = {"entries": entries[:12], "directory_values": found, "session_directory": SUBDIR,
             "exit_code": seen.exit_code}
    throws = {}
    for label, plugins in (("throw-first", [sa.PROBE_PLUGIN, "file://{root}/tdir", sa.GATES_PLUGIN]),
                           ("throw-last", [sa.PROBE_PLUGIN, sa.GATES_PLUGIN, "file://{root}/tdir"])):
        thrown = h.run(SCENARIO, label=label, config={"plugins": plugins}, files=files, env=env)
        loaded = any(e.get("at") == "loaded" for e in sa.gate_log(thrown))
        refused = any(e.get("at") == "refusal" for e in sa.gate_log(thrown))
        throws[label] = {"exit_code": thrown.exit_code, "gate_shim_loaded": loaded, "gate_refused_call": refused,
                         "marked_executed": thrown.exists(MARKER), "setup_marker": (thrown.read("setup.marker") or "").strip()[:300],
                         "stderr_head": thrown.stderr[:300],
                         "stdout_head": thrown.stdout[:300]}
    unreachable = h.run(SCENARIO, label="bridge-missing", config={"plugins": [sa.PROBE_PLUGIN, sa.GATES_PLUGIN]},
                        files=files, env={**env, "AGENT_GATE_BRIDGE": "{root}/no-such-bridge.py"})
    marker = (unreachable.read("setup.marker") or "").strip()
    gate_setup = {"marker": marker[:300], "exit_code": unreachable.exit_code,
                  "marked_executed": unreachable.exists(MARKER),
                  "refused_logged": any(e.get("at") == "refusal" for e in sa.gate_log(unreachable))}
    forms = facts_paths.path_forms(h)
    search = facts_a6.search_facts(h)
    evidence = {"ctx": facts, "setup_throw": throws, "gate_setup_failure": gate_setup, "path_forms": forms,
                "search_and_output": search}
    observed = (bool(entries) and all(e["exit_code"] is not None for e in throws.values()) and bool(marker)
                and forms["before_count"] == forms["call_count"] and bool(search["outputs"]))
    return runner.result(record.PASS if observed else record.UNOBSERVED, evidence)
