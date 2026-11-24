"""Gate adapter: the OpenCode plugin shim, the bridge to Core's unchanged hook scripts, the shadow
transcript, and the adapter probe phase (A1, A2, A4, A5, A6). Nothing here reaches a model or a live OpenCode
process: the shim runs under a plain Node driver against a fake plugin context, and the bridge runs
as a subprocess over recorded event payloads (``tests/fixtures/opencode_events``)."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from lib import engine_session_binding, hook_parity, opencode_events as oe  # noqa: E402

BRIDGE = SCRIPTS / "opencode-gate-bridge.py"
SHIM_DIR = REPO / "integrations" / "opencode" / "plugin"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode_events"
START_REF = "refs/cognition-opencode/stage-6-start"
STATE_TOKEN = "@STATE@"
NODE = shutil.which("node")

RECORDER = (
    "import os, sys\n"
    "with open(os.environ['RECORD_FILE'], 'a') as out:\n"
    "    out.write(sys.stdin.read().replace('\\n', ' ') + '\\n')\n"
)
DENIER = "import sys\nprint('extra-deny', file=sys.stderr)\nsys.exit(2)\n"
BLOCKER = "import json\nprint(json.dumps({'decision': 'block', 'reason': 'hold-reason'}))\n"

DRIVER = r"""
import fs from "node:fs";
const [shim, scenarioFile] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(scenarioFile, "utf8"));
process.chdir(scenario.cwd);
const mod = await import(shim);
const hooks = {};
const synthetic = [];
const failing = new Set(scenario.failing || []);
const register = (kind, name, fn) => {
  if (failing.has(kind + "." + name)) throw new Error("registration refused: " + kind + "." + name);
  hooks[kind + "." + name] = fn;
};
const ctx = {
  tool: { hook: (name, fn) => register("tool", name, fn) },
  session: { hook: (name, fn) => register("session", name, fn), synthetic: async (arg) => { synthetic.push(arg); } },
  shell: { hook: (name, fn) => register("shell", name, fn) },
  event: { subscribe: (fn) => { hooks.event = fn; return null; } },
};
if (scenario.location) ctx.location = { directory: scenario.location };
let setupThrew = null;
try { mod.default.setup(ctx); } catch (error) { setupThrew = String(error.message); }
const results = [];
for (const step of scenario.steps) {
  let threw = null;
  try { await hooks[step.hook](step.input); } catch (error) { threw = String(error.message); }
  results.push({ threw, input: step.input });
}
console.log(JSON.stringify({ results, synthetic, hooks: Object.keys(hooks), setupThrew }));
"""


def _env(tmp_path, **more):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "AGENTCTL_OWN_SESSION", "AGENTCTL_HARNESS_SESSION_ID",
                        "AGENTCTL_RUNTIME_HOST", "AGENT_GATE_EXTRA_ROWS", "AGENT_GATE_PLUGIN_TOOLS",
                        "AGENT_GATE_MCP_SERVERS", "CLAUDE_AGENT_HOME", "PROBE_INERT")}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(CLAUDE_CONFIG_DIR=str(tmp_path / "home"), AGENT_OPENCODE_HOME=str(tmp_path / "oc"))
    env.update(more)
    return env


def _script(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _rows(tmp_path, rows):
    out = []
    for row in rows:
        out.append("[[row]]")
        out += [f"{key} = {json.dumps(value)}" for key, value in row.items()]
        out.append("")
    path = tmp_path / "extra-rows.toml"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _fixture(tmp_path, name):
    text = (FIXTURES / name).read_text(encoding="utf-8").replace(STATE_TOKEN, str(tmp_path / "state"))
    return json.loads(text)


def _bridge(tmp_path, args, stdin=None, **more):
    return subprocess.run([sys.executable, str(BRIDGE), *args], input=stdin, capture_output=True, text=True,
                          env=_env(tmp_path, **more), cwd=tmp_path, timeout=300)


def _recorded(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _record_rows(tmp_path, event, matcher=None):
    recorder = _script(tmp_path, "recorder.py", RECORDER)
    row = {"event": event, "script": str(recorder), "timeout_s": 20}
    if matcher:
        row["matcher"] = matcher
    return row, tmp_path / "recorded.jsonl"


def _event(tmp_path, name, data, rows, **more):
    rows_file = _rows(tmp_path, rows)
    return _bridge(tmp_path, ["event", name], json.dumps(data), AGENT_GATE_EXTRA_ROWS=str(rows_file),
                   RECORD_FILE=str(tmp_path / "recorded.jsonl"), **more)


def _shim(tmp_path, steps, location=None, failing=(), cwd=None, **more):
    if NODE is None:
        pytest.skip("node is not installed; the shim is driven by a plain Node runtime")
    shim = tmp_path / "agent-gates.mjs"
    shim.write_text((SHIM_DIR / "agent-gates.ts").read_text(encoding="utf-8"), encoding="utf-8")
    driver = _script(tmp_path, "driver.mjs", DRIVER)
    scenario = _script(tmp_path, "scenario.json", json.dumps({"cwd": str(cwd or tmp_path), "steps": steps,
                                                              "location": location, "failing": list(failing)}))
    env = _env(tmp_path, **{"AGENT_GATE_BRIDGE": str(BRIDGE), "AGENT_GATE_PYTHON": sys.executable,
                            "AGENT_GATE_LOG": str(tmp_path / "gates.log"),
                            "AGENT_GATE_SETUP_MARKER": str(tmp_path / "setup.marker"), **more})
    proc = subprocess.run([NODE, str(driver), str(shim), str(scenario)], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=600)
    assert proc.returncode == 0, proc.stderr[-800:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _gate_log(tmp_path):
    path = tmp_path / "gates.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _table():
    return hook_parity.load_table(hook_parity.CORE_TABLE)


# --- rows, decisions and the replay CLI -------------------------------------------------------------

def test_combine_any_deny_wins():
    decisions = [oe.Decision(context=["a"]), oe.Decision(True, "first", "s1", ["b"]),
                 oe.Decision(True, "second", "s2"), oe.Decision(context=["c"])]
    chosen = oe.combine(decisions)
    assert (chosen.deny, chosen.reason, chosen.script) == (True, "first", "s1")
    assert chosen.context == ["a", "b", "c"]
    assert not oe.combine([oe.Decision(), oe.Decision()]).deny


def test_deny_wins_and_idle_cap_defined_once():
    source = (SCRIPTS / "lib" / "opencode_events.py").read_text(encoding="utf-8")
    assert len(re.findall(r"^DENY_WINS = ", source, re.M)) == 1
    assert len(re.findall(r"^IDLE_MAX_CONTINUES = ", source, re.M)) == 1
    assert oe.DENY_WINS is True and oe.IDLE_MAX_CONTINUES == 5
    assert "IDLE_MAX_CONTINUES" not in (SHIM_DIR / "agent-gates.ts").read_text(encoding="utf-8").replace(
        'bridgeValue("idle-cap")', "")


REPLAY_CASES = [
    ("shell_before.json", "tool.execute.before", "PreToolUse", "Bash"),
    ("write_before.json", "tool.execute.before", "PreToolUse", "Write"),
    ("read_before.json", "tool.execute.before", "PreToolUse", "Read"),
    ("grep_before.json", "tool.execute.before", "PreToolUse", "Grep"),
    ("inner_tool_before.json", "tool.execute.before", "PreToolUse", "mcp__probecm__touch"),
    ("prompt.json", "session.prompt", "UserPromptSubmit", None),
    ("question_before.json", "tool.execute.before", "PreToolUse", "AskUserQuestion"),
    ("question_after.json", "tool.execute.after", "PostToolUse", "AskUserQuestion"),
]


@pytest.mark.parametrize("name,event,hook_event,tool_name", REPLAY_CASES)
def test_replay_recorded_payload_reaches_rows_in_claude_shape(tmp_path, name, event, hook_event, tool_name):
    data = _fixture(tmp_path, name)
    payload_file = _script(tmp_path, "payload.json", json.dumps(data))
    row, recorded = _record_rows(tmp_path, hook_event)
    rows_file = _rows(tmp_path, [row])
    proc = _bridge(tmp_path, ["replay", "--event", event, "--payload", str(payload_file)],
                   AGENT_GATE_EXTRA_ROWS=str(rows_file), RECORD_FILE=str(recorded),
                   AGENT_GATE_PLUGIN_TOOLS="probe_tool", AGENT_GATE_MCP_SERVERS="probecm,probesrv")
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    (payload,) = _recorded(recorded)
    assert payload["hook_event_name"] == hook_event
    assert payload["session_id"] == data["sessionID"]
    if event == "session.prompt":
        assert payload["prompt"] == data["prompt"]["text"] and payload["prompt"]
        return
    assert payload["tool_name"] == tool_name and payload["tool_use_id"] == data["id"]
    args = data["input"]
    if tool_name == "Bash":
        assert payload["tool_input"] == {"command": args["command"]}
    elif tool_name in ("Write", "Read"):
        assert payload["tool_input"]["file_path"] == args["path"]
    elif tool_name == "Grep":
        assert payload["tool_input"] == {"pattern": args["pattern"], "path": args.get("path")}
    elif tool_name == "AskUserQuestion" and event == "tool.execute.after":
        assert payload["tool_response"]["answers"] == {"Pick one": "alpha"}
    elif tool_name == "AskUserQuestion":
        assert payload["tool_input"]["questions"] == args["questions"]


def test_replay_cli_exit_codes(tmp_path):
    shell = _script(tmp_path, "shell.json", json.dumps(_fixture(tmp_path, "shell_before.json")))
    denier = _script(tmp_path, "deny_row.py", DENIER)
    row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}

    denied = _bridge(tmp_path, ["replay", "--event", "tool.execute.before", "--payload", str(shell)],
                     AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])))
    assert denied.returncode == 1 and denied.stdout.startswith("deny: ") and "extra-deny" in denied.stdout

    prompt = _script(tmp_path, "prompt.json", json.dumps(_fixture(tmp_path, "prompt.json")))
    allowed = _bridge(tmp_path, ["replay", "--event", "session.prompt", "--payload", str(prompt)])
    assert allowed.returncode == 0 and allowed.stdout == ""

    missing = str(tmp_path / "no-such-script.py")
    gone = _bridge(tmp_path, ["replay", "--event", "tool.execute.before", "--payload", str(shell)],
                   AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [{**row, "script": missing}])))
    assert gone.returncode == 2 and gone.stdout.strip() == f"missing-script: {missing}"


def test_replay_synthetic_unresolvable_bash_fixture_is_marked(tmp_path):
    fixture = json.loads((FIXTURES / "bash_unresolvable.json").read_text(encoding="utf-8"))
    assert "synthetic" in fixture
    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    assert sources["synthetic"] == ["bash_unresolvable.json"]
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Bash")
    payload = _script(tmp_path, "payload.json", json.dumps(fixture))
    proc = _bridge(tmp_path, ["replay", "--event", "tool.execute.before", "--payload", str(payload)],
                   AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])), RECORD_FILE=str(recorded))
    assert proc.returncode in (0, 1)
    assert _recorded(recorded)[0]["tool_input"] == {"command": fixture["input"]["command"]}


def test_fixtures_from_recorded_events(tmp_path):
    from opencode_probe import core, record

    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    import hashlib

    for name, meta in sources["fixtures"].items():
        digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert digest == meta["sha256"], f"{name} differs from its recorded sha256"
    state = core.state_dir()
    found = record.pinned_record(state, core.read_pin().get("last_pass", {}).get("mechanisms"))
    if found is None:
        pytest.skip("the machine-local probe state holds no pinned mechanisms record to compare the fixtures with")
    run_dir = found[0].parent
    compared = 0
    for name, meta in sources["fixtures"].items():
        log = run_dir / meta["source"]
        if not log.is_file():
            pytest.skip(f"the machine-local event log {log} is absent")
        line = log.read_text(encoding="utf-8").splitlines()[meta["line"] - 1]
        recorded = json.loads(line)["entry"]["input"]
        text = (FIXTURES / name).read_text(encoding="utf-8").replace(STATE_TOKEN, str(state))
        assert json.loads(text) == recorded, f"{name} differs from {meta['source']} line {meta['line']}"
        compared += 1
    assert compared == len(sources["fixtures"])


def test_extra_rows_channel_runs_per_session_rows(tmp_path):
    denier = _script(tmp_path, "deny_row.py", DENIER)
    record_row, recorded = _record_rows(tmp_path, "PreToolUse", "Bash")
    deny_row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}
    data = _fixture(tmp_path, "shell_before.json")
    proc = _event(tmp_path, "tool.execute.before", data, [record_row, deny_row])
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["decision"] == "deny" and "extra-deny" in verdict["reason"] and verdict["script"] == "deny_row.py"
    assert len(_recorded(recorded)) == 1
    other = _event(tmp_path, "tool.execute.before", {**data, "tool": "write", "input": {"path": "x", "content": "y"}},
                   [deny_row])
    assert json.loads(other.stdout)["decision"] == "allow"


def test_worktree_and_session_move_tools_denied(tmp_path):
    for tool in sorted(oe.DENIED_BY_NAME):
        assert oe.denied_by_name(tool)
        proc = _bridge(tmp_path, ["event", "tool.execute.before"],
                       json.dumps({"tool": tool, "sessionID": "ses_d", "id": "call_d", "input": {}}))
        verdict = json.loads(proc.stdout)
        assert proc.returncode == 0 and verdict["decision"] == "deny" and tool in verdict["reason"]
    steps = [{"hook": "tool.execute.before", "input": {"tool": t, "sessionID": "ses_d", "id": "c", "input": {}}}
             for t in sorted(oe.DENIED_BY_NAME)]
    result = _shim(tmp_path, steps)
    assert all(r["threw"] and "denied" in r["threw"] for r in result["results"])
    assert not oe.denied_by_name("shell")


@pytest.mark.parametrize("tool", ["unmappedtool", "apply_patch", "todowrite", "task", "lsp", "mcp__x__y",
                                  "other_touch"])
def test_unknown_tool_refused_without_any_matcherless_row(tmp_path, tool):
    """The real parity table has no matcherless PreToolUse row, so a tool no row names would pass them
    all: a tool that is not a built-in, a plugin tool or a tool of a configured MCP server is refused."""
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Bash")
    data = {"tool": tool, "sessionID": "ses_u", "id": "call_u", "input": {}}
    proc = _event(tmp_path, "tool.execute.before", data, [row], AGENT_GATE_MCP_SERVERS="probesrv")
    verdict = json.loads(proc.stdout)
    assert proc.returncode == 0 and verdict["decision"] == "deny", proc.stdout + proc.stderr
    assert "neither a built-in tool" in verdict["reason"] and verdict["script"] == "opencode-gate-bridge"
    assert not Path(recorded).exists()


def test_underscore_does_not_make_an_mcp_tool():
    assert oe.claude_tool("apply_patch") is None and oe.claude_tool("probesrv_touch") is None
    servers = frozenset({"probesrv", "probe", "probe_long"})
    assert oe.claude_tool("probesrv_touch", servers=servers) == "mcp__probesrv__touch"
    assert oe.claude_tool("probe_long_x", servers=servers) == "mcp__probe_long__x", "the longest server wins"
    assert oe.claude_tool("probesrv_", servers=servers) is None
    assert oe.claude_tool("probesrv", servers=servers) is None
    assert oe.claude_tool("probe_tool", frozenset({"probe_tool"}), servers) == "plugin__probe_tool"


def test_star_matcher_matches_every_tool_like_claude(tmp_path):
    row = oe.Row("PreToolUse", "*", "s.py", "", 5, "layer")
    assert row.matches("Write") and row.matches("mcp__a__b") and row.matches(None)
    assert not oe.Row("PreToolUse", "Write", "s.py", "", 5, "layer").matches("Bash")
    recorder, recorded = _record_rows(tmp_path, "PreToolUse", "*")
    data = {"tool": "read", "sessionID": "ses_s", "id": "call_s", "input": {"filePath": str(tmp_path / "x")}}
    proc = _event(tmp_path, "tool.execute.before", data, [recorder])
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow", proc.stderr
    assert _recorded(recorded)[0]["tool_name"] == "Read"


def _bridge_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("opencode_gate_bridge_under_test", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_parity_table_is_an_error_not_allow_all(tmp_path, monkeypatch, capsys):
    import io

    monkeypatch.setattr(oe, "_core_rows", lambda: [])
    monkeypatch.setenv("AGENT_OPENCODE_HOME", str(tmp_path / "oc"))
    monkeypatch.delenv(oe.EXTRA_ROWS_ENV, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool": "read", "id": "c", "input": {}})))
    assert _bridge_module().main(["event", "tool.execute.before", "--layer-root", str(tmp_path)]) == 2
    assert "EmptyParityTable" in capsys.readouterr().err
    with pytest.raises(oe.EmptyParityTable):
        oe.require_pretool_rows([oe.Row("PreToolUse", None, "s.py", "", 5, "extra")])
    oe.require_pretool_rows([oe.Row("PreToolUse", None, "s.py", "", 5, "core")])


def test_hook_timeout_or_foreign_exit_code_allows_the_call(tmp_path):
    """Deliberate parity with Claude Code's hook protocol: only exit 2 (or a JSON deny) blocks a tool call;
    a hook that times out, or exits with any other non-zero code, is a non-blocking error and the call runs."""
    exit_one = _script(tmp_path, "exit1.py", "import sys\nsys.exit(1)\n")
    sleeper = _script(tmp_path, "sleeper.py", "import time\ntime.sleep(60)\n")
    rows = [{"event": "PreToolUse", "matcher": "Write", "script": str(exit_one), "timeout_s": 20},
            {"event": "PreToolUse", "matcher": "Write", "script": str(sleeper), "timeout_s": 1}]
    data = {"tool": "write", "sessionID": "ses_p", "id": "call_p", "input": {"filePath": str(tmp_path / "x")}}
    proc = _event(tmp_path, "tool.execute.before", data, rows)
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow", proc.stdout + proc.stderr
    denier = _script(tmp_path, "denier.py", DENIER)
    proc = _event(tmp_path, "tool.execute.before", data, [{**rows[0], "script": str(denier)}])
    assert json.loads(proc.stdout)["decision"] == "deny"


PATCH_TEXT = ("*** Begin Patch\n*** Add File: new/a.txt\n+one\n+two\n*** Update File: src/b.py\n"
              "*** Move to: src/c.py\n@@ def f():\n-x\n+y\n*** Delete File: old.txt\n*** End Patch")


def test_patch_operations_name_every_file_with_the_claude_tool():
    assert oe.patch_operations(PATCH_TEXT) == [
        ("write", "new/a.txt", "one\ntwo"), ("edit", "src/b.py", ""), ("write", "src/c.py", ""),
        ("edit", "old.txt", "")]
    for text in (None, "", "*** Begin Patch\n*** End Patch", "*** Add File:   \n+x", "*** Frobnicate: a\n"):
        with pytest.raises(oe.UnreadablePatch):
            oe.patch_operations(text)


def test_patch_reaches_edit_and_write_rows_once_per_file(tmp_path):
    """The gpt family is offered ``patch`` instead of edit/write (probe A5): without this mapping the rows
    written for ``Edit|Write`` never see a file it changes."""
    edit_row, edits = _record_rows(tmp_path, "PreToolUse", "Edit")
    data = {"tool": "patch", "sessionID": "ses_p", "id": "call_p", "input": {"patchText": PATCH_TEXT}}
    proc = _event(tmp_path, "tool.execute.before", data, [edit_row])
    assert json.loads(proc.stdout)["decision"] == "allow", proc.stdout + proc.stderr
    assert [p["tool_input"]["file_path"] for p in _recorded(edits)] == ["src/b.py", "old.txt"]
    assert [p["tool_use_id"] for p in _recorded(edits)] == ["call_p#1", "call_p#3"]
    sub = tmp_path / "w"
    sub.mkdir()
    write_row, writes = _record_rows(sub, "PreToolUse", "Write")
    proc = _event(sub, "tool.execute.before", data, [write_row])
    assert json.loads(proc.stdout)["decision"] == "allow", proc.stdout + proc.stderr
    assert [(p["tool_input"]["file_path"], p["tool_input"]["content"]) for p in _recorded(writes)] == [
        ("new/a.txt", "one\ntwo"), ("src/c.py", "")]


def test_patch_denied_when_a_row_denies_one_of_its_files(tmp_path):
    denier = _script(tmp_path, "deny_row.py", DENIER)
    row = {"event": "PreToolUse", "matcher": "Edit", "script": str(denier), "timeout_s": 20}
    data = {"tool": "patch", "sessionID": "ses_p", "id": "call_p", "input": {"patchText": PATCH_TEXT}}
    verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [row]).stdout)
    assert verdict["decision"] == "deny" and verdict["script"] == "deny_row.py"


@pytest.mark.parametrize("patch_text", [None, "", "no file operation here", "*** Begin Patch\n*** Frobnicate: a\n"])
def test_patch_with_unreadable_operations_is_refused(tmp_path, patch_text):
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Edit")
    data = {"tool": "patch", "sessionID": "ses_p", "id": "call_p", "input": {"patchText": patch_text}}
    verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [row]).stdout)
    assert verdict["decision"] == "deny" and "unreadable" in verdict["reason"]
    assert not Path(recorded).exists()


def test_configured_mcp_server_tool_reaches_its_rows(tmp_path):
    row, recorded = _record_rows(tmp_path, "PreToolUse", "mcp__probesrv__.*")
    data = {"tool": "probesrv_touch", "sessionID": "ses_m", "id": "call_m", "input": {"a": 1}}
    proc = _event(tmp_path, "tool.execute.before", data, [row], AGENT_GATE_MCP_SERVERS="probesrv")
    assert json.loads(proc.stdout)["decision"] == "allow"
    (payload,) = _recorded(recorded)
    assert payload["tool_name"] == "mcp__probesrv__touch" and payload["tool_input"] == {"a": 1}


def test_missing_script_makes_event_exit_two(tmp_path):
    row = {"event": "PreToolUse", "script": str(tmp_path / "absent.py"), "timeout_s": 5}
    proc = _event(tmp_path, "tool.execute.before", _fixture(tmp_path, "shell_before.json"), [row])
    assert proc.returncode == 2 and proc.stderr.strip() == f"missing-script: {tmp_path / 'absent.py'}"


def test_shipped_bridge_has_no_inert_switch(tmp_path):
    denier = _script(tmp_path, "deny_row.py", DENIER)
    row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}
    data = _fixture(tmp_path, "shell_before.json")
    proc = _event(tmp_path, "tool.execute.before", data, [row], PROBE_INERT="1", AGENT_GATE_INERT="1")
    assert json.loads(proc.stdout)["decision"] == "deny"
    for path in (BRIDGE, SCRIPTS / "lib" / "opencode_events.py", SHIM_DIR / "agent-gates.ts"):
        assert "inert" not in path.read_text(encoding="utf-8").lower(), path.name


# --- the tool-name mapping and the parity table -----------------------------------------------------

def _table_path_ids():
    return {p for row in _table()["row"] for p in row.get("paths", [])}


def test_every_path_mapped_or_disabled():
    ids = _table_path_ids()
    assert ids == set(oe.PATH_TOOL_NAMES), "the table's path ids and the mapping differ"
    plugin = frozenset(oe.PLUGIN_TOOLS_OF_PATHS)
    servers = frozenset({"probesrv", "probecm"})
    for path_id, tool in {**oe.PATH_TOOL_NAMES, **oe.FAMILY_TOOL_PATHS}.items():
        if path_id in oe.DISABLED_PATHS:
            assert oe.DISABLED_PATHS[path_id].strip()
            continue
        assert oe.claude_tool(tool, plugin, servers) is not None, f"{path_id}: tool {tool} has no Claude name"
    assert oe.claude_tool("probesrv_touch", servers=servers) == "mcp__probesrv__touch"
    assert oe.claude_tool("probe_tool", frozenset({"probe_tool"})) == "plugin__probe_tool"
    assert oe.claude_tool("probecm_touch", servers=servers) == "mcp__probecm__touch"


def test_every_denying_row_matcher_hit_or_switched_off():
    plugin = frozenset(oe.PLUGIN_TOOLS_OF_PATHS)
    servers = frozenset({"probesrv", "probecm"})
    mapped = {oe.claude_tool(tool, plugin, servers) for pid, tool in oe.PATH_TOOL_NAMES.items()
              if pid not in oe.DISABLED_PATHS}
    for row in _table()["row"]:
        if not hook_parity.is_denying(row["event"]) or row.get("opencode") == hook_parity.NO_MECHANISM:
            continue
        matcher = row.get("matcher")
        if not matcher:
            continue
        assert any(re.fullmatch(matcher, name) for name in mapped if name), \
            f"{row['script']}: matcher {matcher!r} is hit by no mapped tool"


def test_plugin_dir_main_names_ts_file():
    package = json.loads((SHIM_DIR / "package.json").read_text(encoding="utf-8"))
    assert package["main"] == "agent-gates.ts" and (SHIM_DIR / package["main"]).is_file()
    assert package["exports"] == {".": "./agent-gates.ts"}


def test_shim_registers_named_mechanisms():
    text = (SHIM_DIR / "agent-gates.ts").read_text(encoding="utf-8")
    for needle in ('ctx.tool.hook("execute.before"', 'ctx.tool.hook("execute.after"',
                   'ctx.session.hook("prompt"', 'ctx.session.hook("experimental.idle"',
                   'ctx.shell.hook("create.before"', "ctx.event.subscribe("):
        assert needle in text, needle
    result = _shim_registration()
    assert {"tool.execute.before", "tool.execute.after", "session.prompt", "session.experimental.idle",
            "shell.create.before", "event"} <= set(result)


def _shim_registration():
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        return set(_shim(Path(raw), [])["hooks"])


# --- the shim: shell prefix, credential gate, idle cap ----------------------------------------------

def test_tool_before_prefixes_shell_command_with_session_id(tmp_path):
    session = "ses_q'uote"
    command = 'printf %s "$AGENTCTL_HARNESS_SESSION_ID"'
    step = {"hook": "tool.execute.before",
            "input": {"tool": "shell", "sessionID": session, "id": "call_s", "input": {"command": command}}}
    result = _shim(tmp_path, [step])
    assert result["results"][0]["threw"] is None
    prefixed = result["results"][0]["input"]["input"]["command"]
    assert prefixed.startswith("export AGENTCTL_HARNESS_SESSION_ID=") and prefixed.endswith(f"; {command}")
    ran = subprocess.run(["bash", "-c", prefixed], capture_output=True, text=True, env=_env(tmp_path))
    assert ran.stdout == session


def test_engine_session_binds_opencode_session_id(tmp_path):
    session = "ses_engine1"
    step = {"hook": "tool.execute.before",
            "input": {"tool": "shell", "sessionID": session, "id": "call_e", "input": {"command": "true"}}}
    prefixed = _shim(tmp_path, [step])["results"][0]["input"]["input"]["command"]
    cli = SCRIPTS / "agentctl-cli.py"
    command = f"{prefixed} && {shlex.quote(sys.executable)} {shlex.quote(str(cli))} start --session eng-oc --task t"
    env = _env(tmp_path, AGENTCTL_RUNTIME_HOST="opencode", CLAUDE_CODE_SESSION_ID="claude-inherited")
    ran = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=300)
    assert ran.returncode == 0, ran.stderr[-600:]
    binding = tmp_path / "home" / "agentctl" / "engine-session-bindings" / f"{session}.json"
    assert [e["session"] for e in json.loads(binding.read_text(encoding="utf-8"))["driven"]] == ["eng-oc"]
    assert not (tmp_path / "home" / "agentctl" / "engine-session-bindings" / "claude-inherited.json").exists()


def test_engine_binding_reads_shadow_transcript(tmp_path, monkeypatch):
    from agentctl import cli, store

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_OPENCODE_HOME", str(tmp_path / "oc"))
    monkeypatch.setattr(store, "DEFAULT_ROOT", tmp_path / "home" / "agentctl" / "state")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("AGENTCTL_OWN_SESSION", raising=False)
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "opencode")
    monkeypatch.setenv("AGENTCTL_HARNESS_SESSION_ID", "ses_shadow")
    assert cli.main(["start", "--session", "eng-sh", "--task", "t"]) == 0
    state = store.FileStateStore(tmp_path / "home" / "agentctl" / "state").load("eng-sh")
    assert [b["root_session_id"] for b in state.host_binds] == ["ses_shadow"]
    assert engine_session_binding.harness_sessions_driving("eng-sh") == ["ses_shadow"]
    shadow = oe.shadow_transcript_path(tmp_path / "oc", "ses_shadow")
    assert engine_session_binding.transcript_for_session("eng-sh") is None
    oe.append_transcript(shadow, [{"type": "user"}])
    assert engine_session_binding.transcript_for_session("eng-sh") == shadow


def test_detect_host_from_runtime_marker(monkeypatch):
    from agentctl import runtime_host

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "opencode")
    assert runtime_host.detect_host() == "opencode"
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "other")
    assert runtime_host.detect_host() != "opencode"


def test_runtime_marker_beats_inherited_claude_session():
    env = {"AGENTCTL_RUNTIME_HOST": "opencode", "CLAUDE_CODE_SESSION_ID": "claude-id",
           "AGENTCTL_HARNESS_SESSION_ID": "ses_oc"}
    assert engine_session_binding.harness_session_id(env) == "ses_oc"
    env.pop("AGENTCTL_RUNTIME_HOST")
    assert engine_session_binding.harness_session_id(env) == "claude-id"


def test_linked_child_payload_session_is_own_session(tmp_path):
    row, recorded = _record_rows(tmp_path, "UserPromptSubmit")
    data = _fixture(tmp_path, "prompt.json")
    data["sessionID"] = "ses_c"
    transcripts = tmp_path / "oc" / "state" / "transcripts"
    child = _event(tmp_path, "session.prompt", data, [row], AGENTCTL_OWN_SESSION="U-child")
    assert child.returncode == 0, child.stderr
    assert _recorded(recorded)[0]["session_id"] == "U-child"
    assert (transcripts / "U-child.jsonl").is_file() and not (transcripts / "ses_c.jsonl").exists()
    Path(recorded).unlink()
    root = _event(tmp_path, "session.prompt", data, [row])
    assert root.returncode == 0, root.stderr
    assert _recorded(recorded)[0]["session_id"] == "ses_c" and (transcripts / "ses_c.jsonl").is_file()


def test_shadow_transcript_is_claude_format(tmp_path):
    row, _ = _record_rows(tmp_path, "UserPromptSubmit")
    data = _fixture(tmp_path, "prompt.json")
    assert _event(tmp_path, "session.prompt", data, [row]).returncode == 0
    lines = _recorded(tmp_path / "oc" / "state" / "transcripts" / f"{data['sessionID']}.jsonl")
    assert lines[0]["type"] == "user" and lines[0]["sessionId"] == data["sessionID"]
    assert lines[0]["message"]["content"][0] == {"type": "text", "text": data["prompt"]["text"]}


def _credentials(tmp_path, as_directory=True):
    secrets = (tmp_path / "secrets").resolve()
    secrets.mkdir(exist_ok=True)
    target = secrets if as_directory else secrets / "cred.json"
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "opencode.local.toml").write_text(
        f'{oe.SETTINGS_CREDENTIAL_KEY} = {json.dumps(str(target))}\n', encoding="utf-8")
    return secrets


def test_credential_paths_command_reads_per_machine_setting(tmp_path):
    home = (tmp_path / "home").resolve()
    plain = _bridge(tmp_path, ["credential-paths"])
    base = [str((tmp_path / "oc").resolve() / "data" / "opencode" / "auth.json"), str(home / "opencode.local.toml")]
    assert json.loads(plain.stdout) == base
    secrets = _credentials(tmp_path, as_directory=False)
    configured = _bridge(tmp_path, ["credential-paths"])
    assert json.loads(configured.stdout) == [*base, str(secrets / "cred.json")]
    assert oe.credential_paths(Path("/i"), Path("/h"), None) == (Path("/i/data/opencode/auth.json"),
                                                                 Path("/h/opencode.local.toml"))
    assert oe.credential_paths(Path("/i"), Path("/h"), "/c/key")[-1] == Path("/c/key")


def _grep_before(path, call="call_g"):
    return {"hook": "tool.execute.before",
            "input": {"tool": "grep", "sessionID": "ses_g", "id": call, "input": {"pattern": "x", "path": str(path)}}}


def test_grep_gate_refuses_credential_path(tmp_path):
    secrets = _credentials(tmp_path)
    auth = tmp_path.resolve() / "oc" / "data" / "opencode" / "auth.json"
    paths = [secrets, secrets / "nested" / "cred.json", auth, tmp_path.resolve() / "home" / "opencode.local.toml"]
    result = _shim(tmp_path, [_grep_before(p, f"call_{i}") for i, p in enumerate(paths)])
    for path, step in zip(paths, result["results"]):
        assert step["threw"] == f"credential path: {path}"
    refusals = [e for e in _gate_log(tmp_path) if e["at"] == "refusal"]
    assert len(refusals) == len(paths) and all(e["reason"] == "credential path" for e in refusals)
    relative = _shim(tmp_path, [_grep_before("secrets", "call_rel")])
    assert relative["results"][0]["threw"] == f"credential path: {secrets}"


GREP_TEXT = "Found 3 matches\n{ok}:\n  Line 1: fine\n{secret}:\n  Line 2: SECRET-TOKEN-1\n  Line 3: SECRET-TOKEN-2"


def _grep_after(text, ok, secret, call="call_ga"):
    return {"hook": "tool.execute.after",
            "input": {"tool": "grep", "sessionID": "ses_g", "id": call, "status": "completed",
                      "input": {"pattern": "x", "path": str(ok.parent)},
                      "result": {"output": text, "content": [{"type": "text", "text": text}], "metadata": {}}}}


def test_grep_gate_filters_credential_blocks_and_recounts(tmp_path):
    secrets = _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    secret = secrets / "cred.json"
    text = GREP_TEXT.format(ok=ok, secret=secret)
    result = _shim(tmp_path, [_grep_after(text, ok, secret)])
    filtered = result["results"][0]["input"]["result"]["content"][0]["text"]
    assert filtered == f"Found 1 matches\n{ok}:\n  Line 1: fine"
    assert result["results"][0]["threw"] is None
    assert [e for e in _gate_log(tmp_path) if e["at"] == "grep-filter"][0]["removed_blocks"] == 1


def test_grep_gate_unfiltered_text_absent_from_shadow_transcript_and_logs(tmp_path):
    secrets = _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    row, recorded = _record_rows(tmp_path, "PostToolUse", "Grep")
    rows_file = _rows(tmp_path, [row])
    text = GREP_TEXT.format(ok=ok, secret=secrets / "cred.json")
    _shim(tmp_path, [_grep_after(text, ok, secrets / "cred.json")], AGENT_GATE_EXTRA_ROWS=str(rows_file),
          RECORD_FILE=str(recorded))
    payload = Path(recorded).read_text(encoding="utf-8")
    assert "Line 1: fine" in payload and "SECRET-TOKEN" not in payload
    transcript = (tmp_path / "oc" / "state" / "transcripts" / "ses_g.jsonl").read_text(encoding="utf-8")
    assert "Line 1: fine" in transcript and "SECRET-TOKEN" not in transcript
    assert "SECRET-TOKEN" not in (tmp_path / "gates.log").read_text(encoding="utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in ("scenario.json", "extra-rows.toml"):
            assert "SECRET-TOKEN" not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_grep_gate_leaves_ordinary_grep_untouched(tmp_path):
    _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    other = tmp_path.resolve() / "ok" / "b.txt"
    text = f"Found 2 matches\n{ok}:\n  Line 1: fine\n{other}:\n  Line 4: also fine\n\n(Results truncated)"
    result = _shim(tmp_path, [_grep_after(text, ok, other), {
        "hook": "tool.execute.after",
        "input": {"tool": "grep", "sessionID": "ses_g", "id": "c2", "input": {"path": str(ok.parent)},
                  "result": {"output": "No matches", "content": [{"type": "text", "text": "No matches"}]}}}])
    first, second = (r["input"]["result"]["content"][0]["text"] for r in result["results"])
    assert first.encode() == text.encode() and second == "No matches"


def _link(tmp_path, name, target):
    link = tmp_path / name
    link.symlink_to(target, target_is_directory=True)
    return link


def test_grep_gate_compares_real_paths_through_a_symlinked_directory(tmp_path):
    secrets = _credentials(tmp_path)
    link = _link(tmp_path, "link", secrets)
    paths = [link, link / "cred.json", link / "not-yet-created" / "x.json"]
    result = _shim(tmp_path, [_grep_before(p, f"call_{i}") for i, p in enumerate(paths)])
    assert all(step["threw"].startswith("credential path: ") for step in result["results"])


def test_grep_gate_compares_real_paths_through_a_symlinked_agent_home(tmp_path):
    real_home, real_oc = tmp_path / "realhome", tmp_path / "realoc"
    real_home.mkdir()
    (real_oc / "data" / "opencode").mkdir(parents=True)
    home_link, oc_link = _link(tmp_path, "homelink", real_home), _link(tmp_path, "oclink", real_oc)
    paths = [home_link / "opencode.local.toml", real_home / "opencode.local.toml",
             oc_link / "data" / "opencode" / "auth.json", real_oc / "data" / "opencode" / "auth.json"]
    result = _shim(tmp_path, [_grep_before(p, f"call_{i}") for i, p in enumerate(paths)],
                   CLAUDE_CONFIG_DIR=str(home_link), AGENT_OPENCODE_HOME=str(oc_link))
    assert all(step["threw"].startswith("credential path: ") for step in result["results"])


def _leaky_result(text):
    return {"output": text, "content": [{"type": "text", "text": text}], "metadata": {"preview": text, "n": 3},
            "title": text}


UNRECOGNISED_SHAPES = {
    "other-header": "Found 2 results\n{ok}:\n  Line 1: fine\n{secret}:\n  Line 2: SECRET-TOKEN-1",
    "no-header": "{secret}:\n  Line 2: SECRET-TOKEN-1",
    "relative-path-line": "Found 2 matches\n{ok}:\n  Line 1: fine\nsecrets/cred.json:\n  Line 2: SECRET-TOKEN-1",
    "note-inside-refused-block": ("Found 3 matches\n{ok}:\n  Line 1: fine\n{secret}:\n  Line 2: SECRET-TOKEN-1\n"
                                  "(continued)\n  Line 3: SECRET-TOKEN-2\n  Line 4: SECRET-TOKEN-3"),
}


@pytest.mark.parametrize("shape", sorted(UNRECOGNISED_SHAPES))
def test_grep_filter_fails_closed_for_a_root_above_a_credential(tmp_path, shape):
    secrets = _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    text = UNRECOGNISED_SHAPES[shape].format(ok=ok, secret=secrets / "cred.json")
    step = _grep_after(text, ok, secrets / "cred.json")
    step["input"]["input"]["path"] = str(tmp_path)
    step["input"]["result"] = _leaky_result(text)
    row, recorded = _record_rows(tmp_path, "PostToolUse", "Grep")
    result = _shim(tmp_path, [step], AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])), RECORD_FILE=str(recorded))
    assert "SECRET-TOKEN" not in json.dumps(result["results"][0]["input"]["result"])
    assert "SECRET-TOKEN" not in Path(recorded).read_text(encoding="utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in ("scenario.json", "extra-rows.toml"):
            assert "SECRET-TOKEN" not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_grep_filter_covers_output_and_metadata_not_only_content(tmp_path):
    secrets = _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    text = GREP_TEXT.format(ok=ok, secret=secrets / "cred.json")
    step = _grep_after(text, ok, secrets / "cred.json")
    step["input"]["result"] = _leaky_result(text)
    result = _shim(tmp_path, [step])["results"][0]["input"]["result"]
    assert "SECRET-TOKEN" not in json.dumps(result)
    assert result["output"] == result["content"][0]["text"] == f"Found 1 matches\n{ok}:\n  Line 1: fine"
    assert result["metadata"]["n"] == 3


def test_grep_filter_leaves_an_unrecognised_shape_alone_when_no_credential_is_below_the_root(tmp_path):
    _credentials(tmp_path)
    ok = tmp_path.resolve() / "ok" / "a.txt"
    text = "Found 2 results\nanything"
    step = _grep_after(text, ok, ok)
    step["input"]["result"] = _leaky_result(text)
    result = _shim(tmp_path, [step])["results"][0]["input"]["result"]
    assert result == _leaky_result(text)


def test_grep_path_resolves_against_the_session_directory_not_the_process_cwd(tmp_path):
    secrets = _credentials(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Grep")
    steps = [_grep_before("secrets", "call_dir"), _grep_before("fine", "call_ok")]
    result = _shim(tmp_path, steps, location=str(tmp_path.resolve()), cwd=elsewhere,
                   AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])), RECORD_FILE=str(recorded))
    assert result["results"][0]["threw"] == f"credential path: {secrets}"
    assert result["results"][1]["threw"] is None
    assert _recorded(recorded)[-1]["cwd"] == str(tmp_path.resolve())


def _marker(tmp_path):
    return json.loads((tmp_path / "setup.marker").read_text(encoding="utf-8"))


def test_unwritable_log_does_not_unload_the_gates(tmp_path):
    step = {"hook": "tool.execute.before", "input": {"tool": "opencode_session_move", "id": "c1", "sessionID": "s"}}
    result = _shim(tmp_path, [step], AGENT_GATE_LOG=str(tmp_path / "no-such-dir" / "gates.log"))
    assert result["setupThrew"] is None and "tool.execute.before" in result["hooks"]
    assert result["results"][0]["threw"] == "opencode_session_move is denied by the agent gates"
    assert _marker(tmp_path) == {"status": "ok", "failures": []}


@pytest.mark.parametrize("which", ["bridge", "python"])
def test_unreachable_bridge_refuses_tool_calls_and_fails_the_setup_marker(tmp_path, which):
    missing = {"bridge": {"AGENT_GATE_BRIDGE": str(tmp_path / "missing-bridge.py")},
               "python": {"AGENT_GATE_PYTHON": str(tmp_path / "missing-python")}}[which]
    steps = [{"hook": "tool.execute.before", "input": {"tool": "read", "id": "c1", "sessionID": "s",
                                                       "input": {"filePath": str(tmp_path / "x")}}}]
    result = _shim(tmp_path, steps, **missing)
    assert result["setupThrew"] is None and "tool.execute.before" in result["hooks"]
    assert "did not set up" in result["results"][0]["threw"]
    marker = _marker(tmp_path)
    assert marker["status"] == "failed" and marker["failures"][0].startswith("bridge: ")


def test_failure_to_register_any_before_hook_is_a_failed_marker_not_a_throw(tmp_path):
    result = _shim(tmp_path, [], failing=["tool.execute.before"])
    assert result["setupThrew"] is None
    marker = _marker(tmp_path)
    assert marker["status"] == "failed"
    assert any("no refusing hook could be registered" in f for f in marker["failures"])


def test_failed_shell_hook_registration_fails_the_marker_and_refuses_shell(tmp_path):
    steps = [{"hook": "tool.execute.before", "input": {"tool": "shell", "id": "c1", "sessionID": "s",
                                                       "input": {"command": "true"}}}]
    result = _shim(tmp_path, steps, failing=["shell.create.before"])
    assert result["setupThrew"] is None and result["results"][0]["threw"].startswith("shell is refused")
    marker = _marker(tmp_path)
    assert marker["status"] == "failed" and marker["failures"][0].startswith("shell.create.before: ")


def test_marker_is_ok_when_every_registration_succeeds(tmp_path):
    _shim(tmp_path, [])
    assert _marker(tmp_path) == {"status": "ok", "failures": []}


def test_bridge_failure_on_the_turn_end_event_ends_the_turn(tmp_path):
    """Deliberate parity with Claude Code's Stop hook: a Stop hook that fails to run does not hold the
    turn; only a hook that answers block continues it."""
    broken = tmp_path / "extra-rows.toml"
    broken.write_text("[[row\n", encoding="utf-8")
    steps = [{"hook": "session.experimental.idle", "input": {"sessionID": "ses_i", "background": []}}]
    result = _shim(tmp_path, steps, AGENT_GATE_EXTRA_ROWS=str(broken))
    assert result["results"][0]["threw"] is None and result["synthetic"] == []
    assert [e["at"] for e in _gate_log(tmp_path) if e["at"] != "loaded"] == ["bridge-error"]


def test_idle_counter_caps_continues(tmp_path):
    blocker = _script(tmp_path, "block.py", BLOCKER)
    rows_file = _rows(tmp_path, [{"event": "Stop", "script": str(blocker), "timeout_s": 20}])
    steps = [{"hook": "session.experimental.idle", "input": {"sessionID": "ses_i", "background": []}}
             for _ in range(oe.IDLE_MAX_CONTINUES + 3)]
    result = _shim(tmp_path, steps, AGENT_GATE_EXTRA_ROWS=str(rows_file))
    assert len(result["synthetic"]) == oe.IDLE_MAX_CONTINUES
    assert all(s["sessionID"] == "ses_i" and s["text"] == "hold-reason" for s in result["synthetic"])
    kinds = [e["at"] for e in _gate_log(tmp_path)]
    assert kinds.count("idle-continued") == oe.IDLE_MAX_CONTINUES and kinds.count("idle-cap") == 3
    assert json.loads(_bridge(tmp_path, ["idle-cap"]).stdout) == oe.IDLE_MAX_CONTINUES


# --- the adapter probe phase -------------------------------------------------------------------------

def _adapter():
    from opencode_probe import adapter
    from opencode_probe.adapter import c_a4, shared_a

    return adapter, c_a4, shared_a


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_inert_install_targets_one_config_dir(tmp_path):
    adapter, _, shared = _adapter()
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("untouched", encoding="utf-8")
    before = _tree(other)
    target = tmp_path / "config"
    target.mkdir()
    installed = adapter.install_inert_shim(target)
    assert Path(installed).resolve().is_relative_to(target.resolve())
    assert _tree(other) == before
    assert {p.name for p in Path(installed).iterdir()} == {"agent-gates.ts", "package.json"}
    shipped = (SHIM_DIR / "agent-gates.ts").read_text(encoding="utf-8").split("\n")
    inert = (Path(installed) / "agent-gates.ts").read_text(encoding="utf-8").split("\n")
    changed = [i for i, (a, b) in enumerate(zip(shipped, inert)) if a != b]
    assert len(shipped) == len(inert) and len(changed) == 2
    assert all(shipped[i].rstrip().endswith((shared.REFUSE_MARK, shared.HOLD_MARK)) for i in changed)
    assert not _tree(tmp_path / "config").keys() - {f"agent-gates/{n}" for n in ("agent-gates.ts", "package.json")}


def test_adapter_subject_files_cover_routed_hooks():
    adapter, _, _ = _adapter()
    subject = set(adapter.SUBJECT_FILES)
    routed = {r["script"] for r in _table()["row"] if r.get("opencode") != hook_parity.NO_MECHANISM}
    assert routed
    for script in routed:
        assert f"scripts/{script}" in subject, script
    for path in subject:
        assert (REPO / path).is_file(), path
    for needed in ("integrations/opencode/plugin/agent-gates.ts", "integrations/opencode/plugin/package.json",
                   "scripts/opencode-gate-bridge.py", "scripts/lib/opencode_events.py",
                   "integrations/opencode/parity.toml", "scripts/lib/engine_session_binding.py",
                   "scripts/agentctl/runtime_host.py"):
        assert needed in subject, needed


def _stop_rows_exempt(table, exempt):
    for row in table["row"]:
        if row["event"] == "Stop":
            row["exempt"] = {"reason": "synthetic: no idle hook", "covers_gap": "synthetic gap"} if exempt else None
            if not exempt:
                row.pop("exempt")
            row["opencode"] = hook_parity.NO_MECHANISM if exempt else row["opencode"]
    return table


def test_a4_absent_implies_stop_rows_exempt(monkeypatch):
    adapter, c_a4, shared = _adapter()
    table = _table()
    stops = [r["script"] for r in table["row"] if r["event"] == "Stop"]
    assert stops and shared.stop_rows_not_exempt(table) == stops
    exempt = _stop_rows_exempt(_table(), True)
    assert shared.stop_rows_not_exempt(exempt) == []
    findings = hook_parity.check_rows(exempt, hook_parity.installer_rows(), strict=True)
    assert [f.line() for f in findings] == []
    monkeypatch.setattr(c_a4, "p4_entry", lambda state: {"status": "absent", "evidence": {"synthetic": True}})
    absent = c_a4.run(_Ctx())
    assert absent["status"] == "fail" and absent["evidence"]["stop_rows_not_exempt"] == stops
    monkeypatch.setattr(hook_parity, "load_table", lambda path: exempt)
    assert c_a4.run(_Ctx())["status"] == "absent"


class _Ctx:
    state = None
    inert = False
    binary = None

    def harness(self):
        raise AssertionError("A4 must not start a run when P4 is absent")


def test_a4_branch_follows_p4(monkeypatch, tmp_path):
    adapter, c_a4, _ = _adapter()
    from opencode_probe import core, record

    pinned = c_a4.p4_entry(core.state_dir())
    if pinned is None:
        pytest.skip("the machine-local probe state holds no pinned mechanisms record")
    assert pinned["status"] == record.PASS
    assert c_a4.REST_ON_PLUGIN_DENY is True
    monkeypatch.setattr(c_a4, "p4_entry", lambda state: {"status": "pass", "evidence": {}})

    class Stop(Exception):
        pass

    class Ctx(_Ctx):
        def harness(self):
            raise Stop

    with pytest.raises(Stop):
        c_a4.run(Ctx())


def test_adapter_phase_declares_its_cases():
    from opencode_probe import core

    phase = core.get_phase("adapter")
    infos = {c.case_id: c for c in core.phase_cases(phase)}
    assert phase.anchor == "A1" and set(infos) == {"A1", "A2", "A4", "A5", "A6"}
    assert (infos["A1"].k7, infos["A2"].k7, infos["A4"].k7) == (True, True, False)
    assert (infos["A5"].k7, infos["A6"].k7) == (True, False)
    assert infos["A1"].rests_on_plugin_deny and infos["A4"].rests_on_plugin_deny
    assert not infos["A2"].rests_on_plugin_deny


def test_adapter_record_with_a4_absent_pins_in_a_scratch_copy(tmp_path):
    """A synthetic record (every entry says so) with P4 absent: A4 absent is accepted (its case is
    not K7), the pin is written into the scratch copy only, and check-record then passes."""
    from opencode_probe import core, record

    scratch = tmp_path / "repo" / "scripts" / "opencode_probe"
    shutil.copytree(core.PROBE_DIR, scratch, ignore=shutil.ignore_patterns("__pycache__"))
    repo_pin = (core.PROBE_DIR / core.PIN_NAME).read_bytes()
    phase = core.get_phase("adapter", scratch)
    for rel in phase.subject_files:
        (tmp_path / "repo" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(core.REPO_ROOT / rel, tmp_path / "repo" / rel)
    pin = core.read_pin(scratch)
    state = tmp_path / "state"
    run_dir = record.new_record_dir(state)
    cases = {}
    for case_id, status in (("A1", record.PASS), ("A2", record.PASS), ("A4", record.ABSENT), ("A5", record.PASS),
                            ("A6", record.PASS)):
        cases[case_id] = {"status": status, "evidence": {"synthetic": True, "p4": "absent"}}
    (run_dir / "events").mkdir()
    root = state / "runs" / "root.synthetic"
    meta = json.dumps({"src": "meta", "run": "r1", "root": str(root), "env_paths": {"HOME": str(root / "home")}})
    for case_id in cases:
        (run_dir / "events" / f"{case_id}.jsonl").write_text(meta + "\n", encoding="utf-8")
    record.write_record(run_dir, pin["version"], "adapter", core.phase_hash(phase, scratch), cases)
    assert record.check_phase(state, phase, require_pass=True, case_ids=None, probe_dir=scratch), \
        "before pinning, check-record must refuse"
    assert record.pin_record(state, phase, scratch) == []
    assert record.check_phase(state, phase, require_pass=True, case_ids=None, probe_dir=scratch) == []
    assert "adapter" in core.read_pin(scratch)["last_pass"]
    assert (core.PROBE_DIR / core.PIN_NAME).read_bytes() == repo_pin


# --- guards on what this stage may not change --------------------------------------------------------

def _changed_since_start():
    probe = subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", START_REF],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"{START_REF} is not set in this checkout")
    tracked = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", START_REF], capture_output=True,
                             text=True, check=True).stdout.split()
    untracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout.split()
    return set(tracked) | set(untracked)


def test_no_hook_script_or_claude_wiring_changed():
    changed = _changed_since_start()
    wired = {p for p in changed if re.fullmatch(r"scripts/hook-[^/]+\.py", p) or p.startswith("settings/")
             or p == "scripts/install-reminder-hooks.sh"}
    assert not wired, sorted(wired)


SHARED_CORE = ("core", "harness", "runner", "record", "negative", "paths", "stub", "isolated", "cli",
               "coverage", "subject_only")


def test_shared_core_probe_files_unchanged():
    changed = _changed_since_start()
    guarded = {f"scripts/opencode_probe/{name}.py" for name in SHARED_CORE}
    touched = {p for p in changed if p in guarded or p.startswith(("scripts/opencode_probe/plugin/",
                                                                  "scripts/opencode_probe/mechanisms/"))}
    assert not touched, sorted(touched)
