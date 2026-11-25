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

* ``spellings`` (``facts_spellings``): no gate loaded; a ``glob`` / ``grep`` path of the text ``undefined`` or
  ``null``, a ``read`` of a path whose file exists only under another spelling (NFC / NFD, no-break and narrow
  no-break space, curly quote), and an ``edit`` whose ``oldString`` is not exactly in the file.
* ``project_routes`` (``facts_routes``): the routes derived from the pinned binary's scan names and config file
  names, each run as a pair (control, then with ``OPENCODE_DISABLE_PROJECT_CONFIG``) and recorded as ``off``,
  ``absent``, ``unobserved`` or ``on``, with the tool-file route beside them. ``project_config_off`` is true only
  when every member is ``off`` or ``absent`` and the tool-file route is ``off`` or ``absent``.
* ``config_dir_writes``: with the gate shim loaded, a write, an edit and a shell redirect into the session's own
  config directory are each refused (``refused: config-dir``) and leave their target as it was
  (``config_dir_write_refused``).

pass when every run produced its log, ``project_config_off`` and ``config_dir_write_refused`` hold; fail when a
member loads with the variable set (K7: the gates cannot be secured) or a config-directory write executed
(``marked_executed``); unobserved otherwise.
"""
import json

from opencode_probe import record, runner
from opencode_probe.adapter import facts_a6, facts_paths, facts_routes, facts_spellings, shared_a as sa

CASE_ID = "A6"
K7 = True
REST_ON_PLUGIN_DENY = True

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


CONFIG_DIR = "opencode-config"
CONFIG_DIR_SEED = f"{CONFIG_DIR}/seed.txt"
CONFIG_DIR_CALLS = (
    ("write", {"path": "{root}/" + CONFIG_DIR + "/w.txt", "content": "written"}, f"{CONFIG_DIR}/w.txt"),
    ("edit", {"path": "{root}/" + CONFIG_DIR_SEED, "oldString": "orig", "newString": "changed"}, None),
    ("shell", {"command": "echo x > {root}/" + CONFIG_DIR + "/s.txt"}, f"{CONFIG_DIR}/s.txt"),
)


def config_dir_writes(h, ctx):
    """A write, an edit and a shell redirect into the session's own config directory, each forced from the
    stub model with the gate shim loaded and no denying row: the bridge refuses them (``refused: config-dir``), so
    no target appears and the seed file keeps its text. ``refused`` is true only for a refusal whose class the
    shim logged for that tool; ``executed`` when its effect happened."""
    scenario = {"models": {"m1": [{"tool_calls": [{"name": name, "arguments": args}]}
                                  for name, args, _ in CONFIG_DIR_CALLS] + [{"text": "done"}]}}
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": sa.extra_rows([]), CONFIG_DIR_SEED: "orig"}
    run = h.run(scenario, label="config-dir-writes", config=sa.gate_config(), files=files, env=sa.gate_env(),
                timeout=120)
    entries = sa.gate_log(run)
    calls = {}
    for name, _, target in CONFIG_DIR_CALLS:
        refused = any(e.get("at") == "refusal" and e.get("tool") == name
                      and str(e.get("detail", "")).startswith("refused: config-dir") for e in entries)
        executed = run.exists(target) if target else run.read(CONFIG_DIR_SEED) != "orig"
        calls[name] = {"refused": refused, "executed": executed}
    return {"calls": calls, "exit_code": run.exit_code,
            "marked_executed": any(c["executed"] for c in calls.values()),
            "config_dir_write_refused": all(c["refused"] and not c["executed"] for c in calls.values())}


def run(ctx):
    h = ctx.harness()
    probe_plugin = "file://{root}/cdir"
    files = {**_plugin_files("cdir", "ctx_probe.mjs"), **_plugin_files("tdir", "throwing.mjs"),
             **sa.gate_files(inert=ctx.inert),
             "extra-rows.toml": sa.extra_rows([{"event": "PreToolUse", "matcher": "Bash",
                                                "script": str(sa.DENY_TOUCH), "timeout_s": 10}])}
    env = sa.gate_env(CTXPROBE_LOG="{root}/ctx.log")
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
    spellings = facts_spellings.spelling_facts(h)
    search = facts_a6.search_facts(h)
    from lib import opencode_events as oe

    routes = facts_routes.project_routes(h, oe.PROJECT_CONFIG_OFF)
    config_dir = config_dir_writes(h, ctx)
    evidence = {"ctx": facts, "setup_throw": throws, "gate_setup_failure": gate_setup, "path_forms": forms,
                "spellings": spellings, "search_and_output": search,
                "scan_names": routes["scan_names"], "config_file_names": routes["config_file_names"],
                "plugin_tool_offered_control": routes["plugin_tool_offered_control"],
                "project_routes": routes["project_routes"], "tool_file_route": routes["tool_file_route"],
                "project_config_off": routes["project_config_off"], "project_config_runs": routes,
                "config_dir_write_refused": config_dir["config_dir_write_refused"], "config_dir": config_dir,
                "marked_executed": config_dir["marked_executed"]}
    observed = (bool(entries) and all(e["exit_code"] is not None for e in throws.values()) and bool(marker)
                and forms["before_count"] == forms["call_count"] and bool(search["outputs"])
                and spellings["before_count"] == spellings["call_count"])
    if routes["any_on"] or config_dir["marked_executed"]:
        return runner.result(record.FAIL, evidence)
    passed = observed and routes["project_config_off"] and config_dir["config_dir_write_refused"]
    return runner.result(record.PASS if passed else record.UNOBSERVED, evidence)
