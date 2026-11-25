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
const sessionGets = [];
const failing = new Set(scenario.failing || []);
const register = (kind, name, fn) => {
  if (failing.has(kind + "." + name)) throw new Error("registration refused: " + kind + "." + name);
  hooks[kind + "." + name] = fn;
};
const ctx = {
  tool: { hook: (name, fn) => register("tool", name, fn) },
  session: {
    hook: (name, fn) => register("session", name, fn),
    synthetic: async (arg) => { synthetic.push(arg); },
    get: scenario.noSessionGet ? undefined : async (arg) => {
      sessionGets.push(arg);
      if (scenario.sessionGetThrows) throw new Error("session.get refused");
      const directory = scenario.sessions ? scenario.sessions[arg.sessionID] : scenario.defaultDirectory;
      return directory === undefined ? {} : { location: { directory } };
    },
  },
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
console.log(JSON.stringify({ results, synthetic, sessionGets, hooks: Object.keys(hooks), setupThrew }));
"""


def _env(tmp_path, **more):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "AGENTCTL_OWN_SESSION", "AGENTCTL_HARNESS_SESSION_ID",
                        "AGENTCTL_RUNTIME_HOST", "AGENT_GATE_EXTRA_ROWS", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR",
                        "OPENCODE_CONFIG_PROJECT_DISABLE", "CLAUDE_AGENT_HOME", "PROBE_INERT")}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(CLAUDE_CONFIG_DIR=str(tmp_path / "home"), AGENT_OPENCODE_HOME=str(tmp_path / "oc"),
               **oe.PROJECT_CONFIG_OFF)
    env.update(more)
    return env


def _mcp_config(tmp_path, servers):
    """The session's own OpenCode config, declaring ``servers`` under ``mcp`` (where the bridge reads the MCP
    server names a tool id may carry), as an environment for ``_bridge`` and ``_shim``."""
    config = tmp_path / "session-config.json"
    config.write_text(json.dumps({"mcp": {name: {"type": "local", "command": ["true"]} for name in servers}}),
                      encoding="utf-8")
    return {oe.OPENCODE_CONFIG_ENV: str(config)}


def _script(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _rows(tmp_path, rows, plugin_tools=()):
    out = []
    if plugin_tools:
        out += ["[plugin_tools]", f"ids = {json.dumps(list(plugin_tools))}", ""]
    for row in rows:
        out.append("[[row]]")
        out += [f"{key} = {json.dumps(value)}" for key, value in row.items()]
        out.append("")
    path = tmp_path / "extra-rows.toml"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _fixture(tmp_path, name):
    text = (FIXTURES / name).read_text(encoding="utf-8").replace(STATE_TOKEN, str(tmp_path / "state"))
    return {"cwd": str(tmp_path), **json.loads(text)}


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


def _event(tmp_path, name, data, rows, servers=(), plugin_tools=(), **more):
    rows_file = _rows(tmp_path, rows, plugin_tools)
    data = {"cwd": str(tmp_path), **data}
    if servers:
        more = {**_mcp_config(tmp_path, servers), **more}
    return _bridge(tmp_path, ["event", name], json.dumps(data), AGENT_GATE_EXTRA_ROWS=str(rows_file),
                   RECORD_FILE=str(tmp_path / "recorded.jsonl"), **more)


DEFAULT = object()


def _shim(tmp_path, steps, location=DEFAULT, failing=(), cwd=None, sessions=None, no_session_get=False,
          session_get_throws=False, **more):
    if NODE is None:
        pytest.skip("node is not installed; the shim is driven by a plain Node runtime")
    shim = tmp_path / "agent-gates.mjs"
    shim.write_text((SHIM_DIR / "agent-gates.ts").read_text(encoding="utf-8"), encoding="utf-8")
    driver = _script(tmp_path, "driver.mjs", DRIVER)
    if location is DEFAULT:
        location = str(tmp_path.resolve())
    scenario = _script(tmp_path, "scenario.json", json.dumps({
        "cwd": str(cwd or tmp_path), "steps": steps, "location": location, "failing": list(failing),
        "sessions": sessions, "defaultDirectory": str(tmp_path.resolve()), "noSessionGet": no_session_get,
        "sessionGetThrows": session_get_throws}))
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
    rows_file = _rows(tmp_path, [row], plugin_tools=["probe_tool"])
    proc = _bridge(tmp_path, ["replay", "--event", event, "--payload", str(payload_file)],
                   AGENT_GATE_EXTRA_ROWS=str(rows_file), RECORD_FILE=str(recorded),
                   **_mcp_config(tmp_path, ["probecm", "probesrv"]))
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
    payload = _script(tmp_path, "payload.json", json.dumps({"cwd": str(tmp_path), **fixture}))
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
    other = _event(tmp_path, "tool.execute.before", {**data, "tool": "write", "input": {"path": str(tmp_path / "x"), "content": "y"}},
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
    proc = _event(tmp_path, "tool.execute.before", data, [row], servers=["probesrv"])
    verdict = json.loads(proc.stdout)
    assert proc.returncode == 0 and verdict["decision"] == "deny", proc.stdout + proc.stderr
    assert verdict["reason"].startswith("refused: unclassified ") and verdict["script"] == "opencode-gate-bridge"
    assert not Path(recorded).exists()


def test_underscore_does_not_make_an_mcp_tool():
    assert oe.claude_tool("apply_patch") is None and oe.claude_tool("probesrv_touch") is None
    servers = frozenset({"probesrv", "probe", "probe_long"})
    assert oe.claude_tool("probesrv_touch", servers=servers) == "mcp__probesrv__touch"
    assert oe.claude_tool("probe_long_x", servers=servers) == "mcp__probe_long__x", "the longest server wins"
    assert oe.claude_tool("probesrv_", servers=servers) is None
    assert oe.claude_tool("probesrv", servers=servers) is None
    assert oe.claude_tool("probe_tool", frozenset({"probe_tool"}), servers) is None, "fits a server and a plugin id"
    assert oe.claude_tool("probe_tool", frozenset({"probe_tool"}), frozenset({"probesrv"})) == "plugin__probe_tool"


def test_star_matcher_matches_every_tool_like_claude(tmp_path):
    row = oe.Row("PreToolUse", "*", "s.py", "", 5, "layer")
    assert row.matches("Write") and row.matches("mcp__a__b") and row.matches(None)
    assert not oe.Row("PreToolUse", "Write", "s.py", "", 5, "layer").matches("Bash")
    recorder, recorded = _record_rows(tmp_path, "PreToolUse", "*")
    data = {"tool": "read", "sessionID": "ses_s", "id": "call_s", "input": {"path": str(tmp_path / "x")}}
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
    for name, value in oe.PROJECT_CONFIG_OFF.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENCODE_CONFIG_PROJECT_DISABLE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool": "read", "id": "c", "sessionID": "ses_e", "input": {}})))
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
    data = {"tool": "write", "sessionID": "ses_p", "id": "call_p", "input": {"path": str(tmp_path / "x"), "content": "c"}}
    proc = _event(tmp_path, "tool.execute.before", data, rows)
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow", proc.stdout + proc.stderr
    denier = _script(tmp_path, "denier.py", DENIER)
    proc = _event(tmp_path, "tool.execute.before", data, [{**rows[0], "script": str(denier)}])
    assert json.loads(proc.stdout)["decision"] == "deny"


def _call(tmp_path, tool, args, call="call_x", **more):
    return {"tool": tool, "sessionID": "ses_t", "id": call, "cwd": str(tmp_path), "input": args, **more}


def test_patch_calls_are_refused_by_the_bridge_whatever_the_rows_say(tmp_path):
    """``patch`` is the gpt family's file editor and cannot be switched off alone (probe A5): the bridge does not
    imitate how OpenCode applies it, it refuses every call, and no row is even run."""
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    data = _call(tmp_path, "patch", {"patchText": "*** Begin Patch\n*** Add File: x\n+x\n*** End Patch"})
    verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [row]).stdout)
    assert verdict["decision"] == "deny" and verdict["script"] == "opencode-gate-bridge"
    assert verdict["reason"].startswith("refused: refused-tool ") and oe.REFUSED_TOOLS["patch"] in verdict["reason"]
    assert not Path(recorded).exists()
    assert oe.claude_tool("patch") is None and "patch" not in oe.ARGUMENT_CLASSES


# What OpenCode 2.3.2 did with each written form of a path argument, as adapter probe A6 recorded it
# (record ``path_forms``): ``(written form, session directory, home, what resolve_path must give)``.
D, H = "/work/sub", "/home/u"
RESOLVER_FORMS = [
    ("pf-seed", D, H, "/work/sub/pf-seed"), ("./pf-seed", D, H, "/work/sub/pf-seed"),
    ("sub2/./x", D, H, "/work/sub/sub2/x"), ("sub2/../x", D, H, "/work/sub/x"), ("..", D, H, "/work"),
    ("../pf-seed", D, H, "/work/pf-seed"), ("", D, H, "/work/sub"),
    ("~", D, H, "/home/u"), ("~/", D, H, "/home/u"), ("~/pf-seed", D, H, "/home/u/pf-seed"),
    ("~/../x", D, H, "/home/x"),
    ("~nobody", D, H, "/work/sub/~nobody"), ("~nobody/x", D, H, "/work/sub/~nobody/x"),
    ("$HOME/x", D, H, "/work/sub/$HOME/x"), ("${HOME}/x", D, H, "/work/sub/${HOME}/x"),
    ("file:///a/b", D, H, "/work/sub/file:/a/b"), ("sub2\\x", D, H, "/work/sub/sub2\\x"),
    ("/etc/hostname", D, H, "/etc/hostname"), ("//etc/hostname", D, H, "/etc/hostname"), ("//tmp", D, H, "/tmp"),
    ("/a//b", D, H, "/a/b"),
]


@pytest.mark.parametrize("given, directory, home, expected", RESOLVER_FORMS)
def test_resolve_path_gives_what_opencode_did_with_each_recorded_form(monkeypatch, given, directory, home, expected):
    monkeypatch.setenv("HOME", home)
    assert oe.resolve_path(given, directory) == expected


@pytest.mark.parametrize("given, directory, home", [
    ("~//x", D, H), ("~", D, ""), ("~/x", D, "relative"), ("x", "", H), ("", "", H), ("x", "rel/dir", H),
    ("a\0b", D, H), (7, D, H), (["a"], D, H)])
def test_resolve_path_refuses_a_form_it_was_not_recorded_to_handle(monkeypatch, given, directory, home):
    monkeypatch.setenv("HOME", home)
    with pytest.raises(oe.UnresolvablePath):
        oe.resolve_path(given, directory)


def test_resolve_path_leaves_a_missing_argument_missing_and_reads_home_from_the_environment(monkeypatch):
    assert oe.resolve_path(None, D) is None
    monkeypatch.setenv("HOME", "/home/env")
    assert oe.resolve_path("~/x", D) == "/home/env/x"


def _canon_home(tmp_path):
    home = tmp_path / "home"
    canon = home / "claude-agent-instructions"
    canon.mkdir(parents=True)
    (canon / "tracked.txt").write_text("x\n", encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q"], ["add", "tracked.txt"], [*identity, "commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(canon), *args], check=True, timeout=60, capture_output=True)
    return home, canon


def test_a_tilde_write_into_a_canon_checkout_is_refused_by_the_real_row(tmp_path):
    """``~/`` is the home directory for OpenCode, so the rows must see the absolute path they would see
    from Claude: the real canon read-only row then refuses it, from any session directory."""
    home, canon = _canon_home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {"HOME": str(home), "CLAUDE_INSTRUCTIONS_REPO": str(canon)}
    for tool, args in [("write", {"path": "~/claude-agent-instructions/new.txt", "content": "c"}),
                       ("edit", {"path": "~/claude-agent-instructions/tracked.txt", "oldString": "x", "newString": "y"})]:
        data = _call(elsewhere, tool, args)
        verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [], **env).stdout)
        assert verdict["decision"] == "deny" and verdict["script"] == "hook-guard-canon-readonly.py", (tool, verdict)
    assert not (canon / "new.txt").exists()
    plain = _call(elsewhere, "write", {"path": "~/free.txt", "content": "c"})
    assert json.loads(_event(tmp_path, "tool.execute.before", plain, [], **env).stdout)["decision"] == "allow"


NARROW = '{\n  "permissions": {\n    "allow": []\n  }\n}\n'
WIDEN = {"oldString": '"allow": []', "newString": '"allow": ["Bash(*)"]'}


def test_a_tilde_edit_of_the_live_settings_file_is_refused_by_the_real_row(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text(NARROW, encoding="utf-8")
    data = _call(tmp_path, "edit", {"path": "~/.claude/settings.json", **WIDEN})
    proc = _event(tmp_path, "tool.execute.before", data, [], HOME=str(home),
                  CLAUDE_PERMISSION_GUARD_LOG=str(tmp_path / "guard.jsonl"))
    verdict = json.loads(proc.stdout)
    assert verdict["decision"] == "deny" and verdict["script"] == "hook-guard-permission-surface.py", proc.stdout
    assert settings.read_text(encoding="utf-8") == NARROW
    harmless = _call(tmp_path, "edit", {"path": "~/notes.txt", "oldString": "a", "newString": "b"})
    assert json.loads(_event(tmp_path, "tool.execute.before", harmless, [], HOME=str(home)).stdout)["decision"] == "allow"


@pytest.mark.parametrize("tool, args", [
    ("write", {"path": "~//x", "content": "c"}), ("read", {"path": "a\0b"}), ("edit", {"path": 7, "oldString": "o", "newString": "n"}),
    ("grep", {"pattern": "p", "path": "~//x"}), ("glob", {"pattern": "*", "path": "~//x"})])
def test_an_unrecorded_path_form_is_refused_before_any_row_runs(tmp_path, tool, args):
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    verdict = json.loads(_event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args), [row]).stdout)
    assert verdict["decision"] == "deny", verdict
    assert verdict["reason"].startswith(("refused: path-form ", "refused: argument ")), verdict
    assert not Path(recorded).exists()


def test_relative_paths_reach_the_rows_absolute(tmp_path):
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Read|Write|Edit|Glob|Grep")
    directory = str(tmp_path)
    steps = [("read", {"path": "a/../r.txt"}), ("write", {"path": "w.txt", "content": "c"}),
             ("edit", {"path": "e.txt", "oldString": "o", "newString": "n"}), ("grep", {"pattern": "p", "path": "g"}),
             ("glob", {"pattern": "*.py"}), ("read", {"path": "/abs/x"})]
    for index, (tool, args) in enumerate(steps):
        proc = _event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args, f"c{index}"), [row])
        assert proc.stdout, (tool, proc.returncode, proc.stderr[-400:])
        assert json.loads(proc.stdout)["decision"] == "allow", (tool, proc.stdout)
    inputs = [p["tool_input"] for p in _recorded(recorded)]
    assert [i.get("file_path") or i.get("path") for i in inputs] == [
        f"{directory}/r.txt", f"{directory}/w.txt", f"{directory}/e.txt", f"{directory}/g", None, "/abs/x"]


@pytest.mark.parametrize("tool, args", [
    ("read", {"path": "r.txt"}), ("write", {"path": "w", "content": "c"}), ("glob", {"pattern": "*"}),
    ("grep", {"pattern": "p"}), ("shell", {"command": "true"}), ("execute", {"code": "x"}),
    ("webfetch", {"url": "http://example.invalid"}), ("probesrv_touch", {"a": 1}), ("patch", {"patchText": "x"})])
def test_a_tool_call_whose_session_directory_is_unknown_is_refused_for_every_tool(tmp_path, tool, args):
    """Nothing of this process's own directory stands in for the one the session runs in: a path- or
    cwd-dependent call without it is refused, a shell call included, and no row runs."""
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    for directory in ({"cwd": ""}, {"cwd": None}):
        data = {**_call(tmp_path, tool, args), **directory}
        verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [row], servers=["probesrv"]).stdout)
        assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: "), (tool, verdict)
        assert verdict["reason"].split()[1] in ("directory", "refused-tool"), (tool, verdict)
    missing = {k: v for k, v in _call(tmp_path, tool, args).items() if k != "cwd"}
    proc = _bridge(tmp_path, ["event", "tool.execute.before"], json.dumps(missing))
    assert json.loads(proc.stdout)["decision"] == "deny"
    assert not Path(recorded).exists()


def test_no_working_directory_fallback_is_left_in_the_decision_path():
    text = BRIDGE.read_text(encoding="utf-8")
    decide = text[text.index("def _decide_call"):text.index("def _decide(")]
    assert "getcwd" not in decide


def test_shell_workdir_is_the_directory_the_hooks_see_the_command_run_in(tmp_path):
    """OpenCode runs a shell call in its ``workdir`` (probe A6), resolved like any path: a hook sees it as the
    payload ``cwd``, as it would see the directory of a Claude Bash call."""
    row, recorded = _record_rows(tmp_path, "PreToolUse", "Bash")
    home = tmp_path / "home"
    for name in ("home", "sub", "$HOME"):
        (tmp_path / name).mkdir()
    expected = [str(tmp_path), f"{tmp_path}/sub", f"{tmp_path}", str(home), str(home), "/tmp", f"{tmp_path}/$HOME"]
    forms = [None, "sub", "", "~", "~/", "//tmp", "$HOME"]
    for index, form in enumerate(forms):
        args = {"command": "true"} if form is None else {"command": "true", "workdir": form}
        proc = _event(tmp_path, "tool.execute.before", _call(tmp_path, "shell", args, f"s{index}"), [row], HOME=str(home))
        assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow", (form, proc.stderr[-300:])
    got = [p["cwd"] for p in _recorded(recorded)]
    assert got == expected[:2] + [str(tmp_path)] + expected[3:]
    assert all("workdir" not in p["tool_input"] for p in _recorded(recorded))
    bad = _call(tmp_path, "shell", {"command": "true", "workdir": "~//x"})
    assert json.loads(_event(tmp_path, "tool.execute.before", bad, [row], HOME=str(home)).stdout)["decision"] == "deny"


def test_no_bridge_path_opens_the_file_a_tool_names(tmp_path, monkeypatch):
    """With ``patch`` refused, no gate path reads a file's content: the bridge shows the rows what the call
    says and never what its target holds. A refused write's content is the model's own, never the file's."""
    import builtins

    secret = tmp_path / "target.txt"
    secret.write_text("FILE-CONTENT-SENTINEL", encoding="utf-8")
    opened = []
    real_open = builtins.open
    real_os_open = os.open

    def watching_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def watching_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watching_open)
    monkeypatch.setattr(os, "open", watching_os_open)
    monkeypatch.setenv("AGENT_OPENCODE_HOME", str(tmp_path / "oc"))
    bridge = _bridge_module()
    rows = oe.load_rows([tmp_path / "layer"], None)
    transcript = oe.shadow_transcript_path(tmp_path / "oc", "ses_t")
    calls = [("read", {"path": str(secret)}), ("write", {"path": str(secret), "content": "c"}),
             ("edit", {"path": str(secret), "oldString": "o", "newString": "n"}),
             ("patch", {"patchText": f"*** Update File: {secret}\n@@\n-x\n+y"}),
             ("grep", {"pattern": "p", "path": str(secret)}), ("glob", {"pattern": "*", "path": str(tmp_path)})]
    for index, (tool, args) in enumerate(calls):
        for event in ("tool.execute.before", "tool.execute.after"):
            try:
                bridge._decide_call(event, _call(tmp_path, tool, args, f"c{index}",
                                                 result={"content": [{"type": "text", "text": "out"}]}), rows,
                                    frozenset(), transcript)
            except oe.UnshowableCall:
                assert tool == "patch" and event == "tool.execute.before"
    assert str(secret) not in opened and str(secret.resolve()) not in opened
    text = transcript.read_text(encoding="utf-8")
    assert "FILE-CONTENT-SENTINEL" not in text and "tool_use" in text


def test_a_call_the_rows_were_not_shown_leaves_nothing_in_the_transcript(tmp_path):
    transcript = oe.shadow_transcript_path(tmp_path / "oc", "ses_t")
    data = _call(tmp_path, "patch", {"patchText": "*** Update File: f\n@@\n-x\n+y"})
    proc = _event(tmp_path, "tool.execute.before", data, [])
    assert json.loads(proc.stdout)["decision"] == "deny"
    assert not transcript.exists() or "patchText" not in transcript.read_text(encoding="utf-8")


def test_a_call_a_row_denied_keeps_no_input_in_the_transcript(tmp_path):
    transcript = oe.shadow_transcript_path(tmp_path / "oc", "ses_t")
    denier = _script(tmp_path, "deny_row.py", DENIER)
    row = {"event": "PreToolUse", "matcher": "Write", "script": str(denier), "timeout_s": 20}
    data = _call(tmp_path, "write", {"path": str(tmp_path / "x"), "content": "SECRET-BODY"})
    assert json.loads(_event(tmp_path, "tool.execute.before", data, [row]).stdout)["decision"] == "deny"
    text = transcript.read_text(encoding="utf-8")
    assert "SECRET-BODY" not in text
    (record,) = [json.loads(line) for line in text.splitlines()]
    assert record["message"]["content"][0]["refused"] is True


def test_the_shadow_transcripts_directory_is_a_credential_path(tmp_path):
    paths = oe.credential_paths(tmp_path / "oc", tmp_path / "agent", None)
    assert oe.transcripts_dir(tmp_path / "oc").resolve() in paths
    listed = json.loads(_bridge(tmp_path, ["credential-paths"]).stdout)
    assert str(oe.transcripts_dir(tmp_path / "oc").resolve()) in listed


# The inventory of every argument of every built-in the adapter maps, against the schemas probe A5 recorded.
RECORDED_ARGUMENTS = {
    "read": ["limit", "offset", "path"], "write": ["content", "path"],
    "edit": ["newString", "oldString", "path", "replaceAll"],
    "glob": ["hidden", "limit", "path", "pattern"],
    "grep": ["caseSensitive", "include", "limit", "literal", "path", "pattern"],
    "shell": ["background", "command", "timeout", "workdir"], "webfetch": ["format", "timeout", "url"],
    "websearch": ["query"], "question": ["questions"], "skill": ["id"],
    "subagent": ["agent", "description", "model", "prompt", "sessionID"], "execute": ["code"],
}


def _schemas(arguments=RECORDED_ARGUMENTS):
    """The shape of record A5's ``tool_schemas``: ``{tool: {"properties": {name: {"type": ...}}, "required": [...]}}``."""
    return {tool: {"properties": {n: {"type": oe._expected_type(n) or "string"} for n in names},
                   "required": sorted(oe.REQUIRED_ARGUMENTS.get(tool, ()))}
            for tool, names in arguments.items()}


def test_every_argument_of_every_mapped_builtin_is_classified_with_a_reason():
    assert oe.unclassified_arguments(_schemas()) == []
    assert set(RECORDED_ARGUMENTS) == set(oe.ARGUMENT_CLASSES)
    for tool, classes in oe.ARGUMENT_CLASSES.items():
        for name, (kind, detail) in classes.items():
            assert kind in ("path", "mapped", "irrelevant") and detail.strip(), (tool, name)
            if kind == "irrelevant":
                assert len(detail.split()) >= 3, f"{tool}.{name}: an irrelevant argument is justified in a sentence"
    assert {tool for tool in oe.ARGUMENT_CLASSES} <= set(oe.BUILTIN_TOOLS)


def test_the_inventory_check_fails_for_a_tool_or_argument_it_does_not_classify():
    changed = oe.unclassified_arguments(_schemas({**RECORDED_ARGUMENTS, "read": ["path", "newarg"]}))
    assert sorted(changed) == ["read.limit (stale)", "read.newarg", "read.offset (stale)"]
    assert oe.unclassified_arguments(_schemas({**RECORDED_ARGUMENTS, "newtool": ["a"]})) == ["newtool"]
    assert oe.unclassified_arguments(_schemas({"patch": ["patchText"], "probe_tool": ["a"]})) == ["probe_tool"]
    assert oe.unclassified_arguments(_schemas({"patch": ["patchText"], "probe_tool": ["a"]}),
                                     plugin=frozenset({"probe_tool"})) == []
    retyped = _schemas()
    retyped["read"]["properties"]["path"] = {"type": "array"}
    assert oe.unclassified_arguments(retyped) == ["read.path (type)"]
    required = _schemas()
    required["read"]["required"] = ["path", "offset"]
    assert oe.unclassified_arguments(required) == ["read (required)"]


def test_no_irrelevant_argument_decides_where_or_what_a_tool_reads_writes_or_runs():
    named = {"path", "workdir", "url", "command", "code", "content", "oldString", "newString", "pattern", "query",
             "prompt", "agent", "questions", "id"}
    for tool, classes in oe.ARGUMENT_CLASSES.items():
        assert not [n for n, (kind, _) in classes.items() if kind == "irrelevant" and n in named], tool


def test_an_argument_nobody_classified_refuses_the_call(tmp_path):
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    data = _call(tmp_path, "write", {"path": "x", "content": "c", "newlocation": "/etc/y"})
    verdict = json.loads(_event(tmp_path, "tool.execute.before", data, [row]).stdout)
    assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: argument ")
    assert "newlocation" in verdict["reason"]
    assert not Path(recorded).exists()


def test_claude_input_maps_each_class(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    assert oe.claude_input("read", {"path": "a", "offset": 1, "limit": 2}, "/d") == {
        "file_path": "/d/a", "offset": 1, "limit": 2}
    assert oe.claude_input("edit", {"path": "~/a", "oldString": "o", "newString": "n", "replaceAll": True},
                           "/d") == {"file_path": "/h/a", "old_string": "o", "new_string": "n",
                                                "replace_all": True}
    assert oe.claude_input("glob", {"pattern": "*", "hidden": True, "limit": 3}, "/d") == {"pattern": "*", "path": None}
    assert oe.claude_input("shell", {"command": "c", "workdir": "x", "timeout": 5}, "/d") == {"command": "c"}
    assert oe.claude_input("shell", {"command": "c", "background": True}, "/d") == {
        "command": "c", "run_in_background": True}
    assert oe.claude_input("probesrv_touch", {"a": 1}, "/d") == {"a": 1}
    assert oe.run_directory("shell", {"command": "c", "workdir": "x"}, "/d") == "/d/x"
    assert oe.run_directory("read", {"path": "x", "workdir": "y"}, "/d") == "/d"
    assert oe.run_directory("shell", {"workdir": "~//x"}, "/d", strict=False) == "/d"
    with pytest.raises(oe.UnresolvablePath):
        oe.run_directory("shell", {"workdir": "~//x"}, "/d")
    with pytest.raises(oe.UnclassifiedArgument):
        oe.claude_input("read", {"path": "a", "extra": 1}, "/d")
    assert oe.claude_input("read", {"path": "a", "extra": 1}, "/d", strict=False) == {"file_path": "/d/a"}


def test_configured_mcp_server_tool_reaches_its_rows(tmp_path):
    row, recorded = _record_rows(tmp_path, "PreToolUse", "mcp__probesrv__.*")
    data = {"tool": "probesrv_touch", "sessionID": "ses_m", "id": "call_m", "input": {"a": 1}}
    proc = _event(tmp_path, "tool.execute.before", data, [row], servers=["probesrv"])
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
        if path_id in oe.REFUSED_PATHS:
            assert oe.REFUSED_PATHS[path_id].strip() and tool in oe.REFUSED_TOOLS
            continue
        assert oe.claude_tool(tool, plugin, servers) is not None, f"{path_id}: tool {tool} has no Claude name"
    assert oe.claude_tool("probesrv_touch", servers=servers) == "mcp__probesrv__touch"
    assert oe.claude_tool("probe_tool", frozenset({"probe_tool"})) == "plugin__probe_tool"
    assert oe.claude_tool("probecm_touch", servers=servers) == "mcp__probecm__touch"


def test_every_denying_row_matcher_hit_or_switched_off():
    plugin = frozenset(oe.PLUGIN_TOOLS_OF_PATHS)
    servers = frozenset({"probesrv", "probecm"})
    mapped = {oe.claude_tool(tool, plugin, servers) for pid, tool in oe.PATH_TOOL_NAMES.items()
              if pid not in oe.REFUSED_PATHS}
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
    integration = (tmp_path / "oc").resolve()
    base = [str(integration / "data" / "opencode" / "auth.json"), str(integration / "state" / "transcripts"),
            str(integration / "state" / "rows-snapshots"), str(home / "opencode.local.toml")]
    assert json.loads(plain.stdout) == base
    secrets = _credentials(tmp_path, as_directory=False)
    configured = _bridge(tmp_path, ["credential-paths"])
    assert json.loads(configured.stdout) == [*base, str(secrets / "cred.json")]
    assert oe.credential_paths(Path("/i"), Path("/h"), None) == (
        Path("/i/data/opencode/auth.json"), Path("/i/state/transcripts"), Path("/i/state/rows-snapshots"),
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
        assert step["threw"] == f"deny: agent-gates credential path: {path}"
    refusals = [e for e in _gate_log(tmp_path) if e["at"] == "refusal"]
    assert len(refusals) == len(paths) and all(e["reason"] == "credential path" for e in refusals)
    relative = _shim(tmp_path, [_grep_before("secrets", "call_rel")])
    assert relative["results"][0]["threw"] == f"deny: agent-gates credential path: {secrets}"


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
    assert all(step["threw"].startswith("deny: agent-gates credential path: ") for step in result["results"])


def test_grep_gate_compares_real_paths_through_a_symlinked_agent_home(tmp_path):
    real_home, real_oc = tmp_path / "realhome", tmp_path / "realoc"
    real_home.mkdir()
    (real_oc / "data" / "opencode").mkdir(parents=True)
    home_link, oc_link = _link(tmp_path, "homelink", real_home), _link(tmp_path, "oclink", real_oc)
    paths = [home_link / "opencode.local.toml", real_home / "opencode.local.toml",
             oc_link / "data" / "opencode" / "auth.json", real_oc / "data" / "opencode" / "auth.json"]
    result = _shim(tmp_path, [_grep_before(p, f"call_{i}") for i, p in enumerate(paths)],
                   CLAUDE_CONFIG_DIR=str(home_link), AGENT_OPENCODE_HOME=str(oc_link))
    assert all(step["threw"].startswith("deny: agent-gates credential path: ") for step in result["results"])


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
    assert result["results"][0]["threw"] == f"deny: agent-gates credential path: {secrets}"
    assert result["results"][1]["threw"] is None
    assert _recorded(recorded)[-1]["cwd"] == str(tmp_path.resolve())


def test_grep_path_resolves_against_each_sessions_own_directory(tmp_path):
    secrets = _credentials(tmp_path)
    inside, outside = tmp_path.resolve(), (tmp_path / "other").resolve()
    outside.mkdir()
    steps = [{"hook": "tool.execute.before",
              "input": {"tool": "grep", "sessionID": sid, "id": f"call_{sid}", "input": {"pattern": "x", "path": "secrets"}}}
             for sid in ("ses_in", "ses_out")]
    result = _shim(tmp_path, steps, location=str(outside), sessions={"ses_in": str(inside), "ses_out": str(outside)})
    assert result["results"][0]["threw"] == f"deny: agent-gates credential path: {secrets}"
    assert result["results"][1]["threw"] is None
    assert [g["sessionID"] for g in result["sessionGets"]] == ["ses_in", "ses_out"]


UNKNOWN_DIRECTORY = "the directory its session runs in is unknown, so no path or command can be placed"


@pytest.mark.parametrize("options", [{"no_session_get": True}, {"session_get_throws": True},
                                     {"sessions": {"another-session": "/x"}}])
def test_every_tool_call_is_refused_when_the_session_directory_cannot_be_had(tmp_path, options):
    """Neither the setup directory nor this process's is the session's under ``opencode serve``: when the lookup
    fails, a path- or cwd-dependent call of the session is refused (absolute paths and shell calls too), the
    reason is logged, and no row runs."""
    _credentials(tmp_path)
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    calls = [("grep", {"pattern": "x", "path": "secrets"}), ("grep", {"pattern": "x", "path": str(tmp_path / "fine")}),
             ("read", {"path": str(tmp_path / "fine")}), ("shell", {"command": "true"})]
    steps = [{"hook": "tool.execute.before", "input": {"tool": tool, "sessionID": "ses_g", "id": f"c{i}", "input": args}}
             for i, (tool, args) in enumerate(calls)]
    result = _shim(tmp_path, steps, AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])), RECORD_FILE=str(recorded),
                   **options)
    assert [r["threw"] for r in result["results"]] == [f"refused: directory {tool} refused: {UNKNOWN_DIRECTORY}" for tool, _ in calls]
    assert result["setupThrew"] is None and not Path(recorded).exists()
    log = _gate_log(tmp_path)
    refusals = [e for e in log if e["at"] == "refusal"]
    assert len(refusals) == len(calls) and {e["reason"] for e in refusals} == {"session directory unknown"}
    if "sessions" not in options:
        assert {e["at"] for e in log} >= {"session-directory-error"}
    assert not any(e["at"] == "session-directory-error" and "ses_g" not in e["sessionID"] for e in log)


def test_a_failed_directory_lookup_is_not_cached_and_a_successful_one_is(tmp_path):
    steps = [{"hook": "tool.execute.before", "input": {"tool": "read", "sessionID": "ses_a", "id": f"c{i}",
                                                       "input": {"path": str(tmp_path / "x")}}} for i in range(3)]
    result = _shim(tmp_path, steps, sessions={"ses_b": str(tmp_path.resolve())})
    assert all(r["threw"] for r in result["results"]) and len(result["sessionGets"]) == 3
    steps = [{**step, "input": {**step["input"], "sessionID": "ses_b"}} for step in steps]
    result = _shim(tmp_path, steps, sessions={"ses_b": str(tmp_path.resolve())})
    assert [r["threw"] for r in result["results"]] == [None, None, None] and len(result["sessionGets"]) == 1


def test_setup_without_a_location_directory_fails_the_marker(tmp_path):
    _shim(tmp_path, [], location="")
    marker = _marker(tmp_path)
    assert marker["status"] == "failed" and "ctx.location.directory is absent at setup" in marker["failures"]
    _shim(tmp_path, [], location="relative/dir")
    assert _marker(tmp_path)["status"] == "failed"


def test_session_directory_is_not_the_process_cwd_when_nothing_is_known(tmp_path):
    secrets = _credentials(tmp_path)
    result = _shim(tmp_path, [_grep_before("secrets")], location="", cwd=tmp_path, no_session_get=True)
    assert result["results"][0]["threw"] != f"deny: agent-gates credential path: {secrets}"
    assert UNKNOWN_DIRECTORY in result["results"][0]["threw"]


def test_a_tilde_rooted_grep_on_a_credential_directory_is_refused_and_marked_risky(tmp_path):
    """The shim's search root follows the bridge's resolver: ``~`` is the home directory, so a grep rooted at
    it, or at a credential directory below it, is refused, and a grep above a credential path is risky."""
    home = tmp_path / "homedir"
    (home / ".secrets").mkdir(parents=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "opencode.local.toml").write_text(
        f'{oe.SETTINGS_CREDENTIAL_KEY} = {json.dumps(str((home / ".secrets").resolve()))}\n', encoding="utf-8")
    env = {"HOME": str(home)}
    refused = _shim(tmp_path, [_grep_before("~/.secrets", "c1"), _grep_before("~/.secrets/x/y", "c2")], **env)
    assert [r["threw"] for r in refused["results"]] == [f"deny: agent-gates credential path: {home.resolve()}/.secrets",
                                                        f"deny: agent-gates credential path: {home.resolve()}/.secrets/x/y"]
    text = f"Found 1 matches\n{home.resolve()}/.secrets/cred:\n  Line 1: SECRET-TOKEN-1"
    step = {"hook": "tool.execute.after", "input": {"tool": "grep", "sessionID": "ses_g", "id": "c3",
                                                    "input": {"pattern": "x", "path": "~"},
                                                    "result": {"output": text, "content": [{"type": "text", "text": text}]}}}
    risky = _shim(tmp_path, [step], **env)
    assert "SECRET-TOKEN" not in json.dumps(risky["results"][0]["input"]["result"])
    plain = _shim(tmp_path, [_grep_before("~", "c4"), _grep_before("~//x", "c5"), _grep_before("$HOME/.secrets", "c6")],
                  **env)
    assert [r["threw"] is None for r in plain["results"]] == [True, False, True]
    assert plain["results"][1]["threw"].startswith("refused: path-form grep refused: its search path is in a form")
    assert [e["reason"] for e in _gate_log(tmp_path) if e["at"] == "refusal"] == [
        "credential path", "credential path", "search path not resolvable"]


def _after_with(tmp_path, result_value, root, call="call_s"):
    return {"hook": "tool.execute.after",
            "input": {"tool": "grep", "sessionID": "ses_g", "id": call, "input": {"pattern": "x", "path": str(root)},
                      "result": result_value}}


SECRET_SHAPES = {
    "object-output": lambda t: {"output": {"text": t, "list": [t]}, "content": [{"type": "text", "text": t}]},
    "numeric-and-null-fields": lambda t: {"output": None, "content": [{"type": "text", "text": t}], "metadata": {"n": 3, "p": None, "q": t}},
    "content-not-an-array": lambda t: {"output": t, "content": {"type": "text", "text": t}},
    "content-string": lambda t: {"output": t, "content": t},
    "extra-part-fields": lambda t: {"output": t, "content": [{"type": "text", "text": t, "preview": t, "data": {"x": [t]}},
                                                          {"type": "resource", "resource": {"text": t}}, t, None]},
    "string-result": lambda t: t,
    "deep-nesting": lambda t: {"output": t, "metadata": {"a": {"b": {"c": {"d": [t]}}}}},
}


@pytest.mark.parametrize("shape", sorted(SECRET_SHAPES))
def test_grep_filter_leaves_no_secret_in_any_field_under_a_risky_root(tmp_path, shape):
    secrets = _credentials(tmp_path)
    secret_text = f"Found 1 matches\n{secrets / 'cred.json'}:\n  Line 1: SECRET-TOKEN-1"
    row, recorded = _record_rows(tmp_path, "PostToolUse", "Grep")
    step = _after_with(tmp_path, SECRET_SHAPES[shape](secret_text), tmp_path)
    result = _shim(tmp_path, [step], AGENT_GATE_EXTRA_ROWS=str(_rows(tmp_path, [row])), RECORD_FILE=str(recorded))
    assert result["results"][0]["threw"] is None
    assert "SECRET-TOKEN" not in json.dumps(result["results"][0]["input"]["result"])
    assert "SECRET-TOKEN" not in Path(recorded).read_text(encoding="utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in ("scenario.json", "extra-rows.toml"):
            assert "SECRET-TOKEN" not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_exact_zero_hit_text_passes_under_a_risky_root_but_other_unrecognised_text_does_not(tmp_path):
    _credentials(tmp_path)
    exact = {"output": "No matches found", "content": [{"type": "text", "text": "No matches found"}], "metadata": {}}
    other = {"output": "No matches found\nNOTE-X", "content": [{"type": "text", "text": "No matches found\nNOTE-X"}]}
    result = _shim(tmp_path, [_after_with(tmp_path, exact, tmp_path, "c1"), _after_with(tmp_path, other, tmp_path, "c2")])
    first, second = (r["input"]["result"] for r in result["results"])
    assert first == {"output": "No matches found", "content": [{"type": "text", "text": "No matches found"}], "metadata": {}}
    assert "NOTE-X" not in json.dumps(second)


def test_bridge_verdict_without_a_decision_refuses_a_pre_tool_event(tmp_path):
    silent = _script(tmp_path, "silent-bridge.py", "import sys\nif sys.argv[1:2] == ['event']:\n    print('{}')\nelse:\n    print('[]' if sys.argv[1] == 'credential-paths' else '5')\n")
    steps = [{"hook": "tool.execute.before", "input": {"tool": "read", "id": "c1", "sessionID": "s",
                                                       "input": {"path": str(tmp_path / "x")}}}]
    result = _shim(tmp_path, steps, AGENT_GATE_BRIDGE=str(silent))
    assert result["results"][0]["threw"] == "refused: gate-setup the gate bridge answered no decision"


def _marker(tmp_path):
    return json.loads((tmp_path / "setup.marker").read_text(encoding="utf-8"))


def test_unwritable_log_does_not_unload_the_gates(tmp_path):
    step = {"hook": "tool.execute.before", "input": {"tool": "opencode_session_move", "id": "c1", "sessionID": "s"}}
    result = _shim(tmp_path, [step], AGENT_GATE_LOG=str(tmp_path / "no-such-dir" / "gates.log"))
    assert result["setupThrew"] is None and "tool.execute.before" in result["hooks"]
    assert result["results"][0]["threw"] == "refused: refused-tool opencode_session_move is denied by the agent gates"
    assert _marker(tmp_path) == {"status": "ok", "failures": []}


@pytest.mark.parametrize("which", ["bridge", "python"])
def test_unreachable_bridge_refuses_tool_calls_and_fails_the_setup_marker(tmp_path, which):
    missing = {"bridge": {"AGENT_GATE_BRIDGE": str(tmp_path / "missing-bridge.py")},
               "python": {"AGENT_GATE_PYTHON": str(tmp_path / "missing-python")}}[which]
    steps = [{"hook": "tool.execute.before", "input": {"tool": "read", "id": "c1", "sessionID": "s",
                                                       "input": {"path": str(tmp_path / "x")}}}]
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
    assert result["setupThrew"] is None and result["results"][0]["threw"].startswith("refused: gate-setup shell is refused")
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
    assert (infos["A5"].k7, infos["A6"].k7) == (True, True)
    assert infos["A1"].rests_on_plugin_deny and infos["A4"].rests_on_plugin_deny and infos["A6"].rests_on_plugin_deny
    assert not infos["A2"].rests_on_plugin_deny and not infos["A5"].rests_on_plugin_deny


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


# --- refusals, rows snapshot, project config, config directory ----------------------------------------

def _verdict(proc):
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _bridge_env_without(tmp_path, args, stdin, drop=(), **more):
    env = _env(tmp_path, **more)
    for name in drop:
        env.pop(name, None)
    return subprocess.run([sys.executable, str(BRIDGE), *args], input=stdin, capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=300)


def _shell(tmp_path, **more):
    return _call(tmp_path, "shell", {"command": "true"}, **more)


def test_unclassified_tool_or_argument_refused(tmp_path):
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    path = str(tmp_path / "f.txt")
    for tool, args in (("write", {"path": path, "content": "x", "bogus": 1}),
                       ("shell", {"command": "true", "workdirs": "/"}),
                       ("read", {"path": path, "offset": 1, "surprise": True})):
        verdict = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args), [row]))
        assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: argument "), verdict
    verdict = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, "nosuchtool", {}), [row]))
    assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: unclassified ")
    assert not Path(recorded).exists(), "no row ran for a refused call"


@pytest.mark.parametrize("tool, args", [
    ("write", {"path": "{p}"}), ("write", {"content": "x"}), ("write", {"path": "{p}", "content": 5}),
    ("edit", {"path": "{p}", "oldString": "a"}), ("edit", {"path": "{p}", "oldString": "a", "newString": None}),
    ("shell", {}), ("shell", {"command": ["true"]}), ("read", {"path": 7}),
])
def test_required_argument_absent_or_wrong_type_refused(tmp_path, tool, args):
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    args = {k: (str(tmp_path / "f") if v == "{p}" else v) for k, v in args.items()}
    verdict = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args), [row]))
    assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: "), verdict
    assert not Path(recorded).exists()


def test_refusal_reason_names_source(tmp_path):
    """A refusal the bridge makes reads ``refused: <class> ...``; a hook's deny reads ``deny: <script> ...``."""
    denier = _script(tmp_path, "deny_row.py", DENIER)
    deny_row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}
    hooked = _verdict(_event(tmp_path, "tool.execute.before", _shell(tmp_path), [deny_row]))
    assert hooked["decision"] == "deny" and hooked["reason"].startswith("deny: deny_row.py ")
    assert hooked["script"] == "deny_row.py"
    refused = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, "patch", {}), [deny_row]))
    assert refused["reason"].startswith("refused: refused-tool ") and refused["script"] == "opencode-gate-bridge"
    for cls in oe.REFUSAL_CLASSES:
        assert oe.refusal_reason(cls, "x") == f"refused: {cls} x"
    assert oe.hook_deny_reason("s.py", "r") == "deny: s.py r"


def test_unknown_directory_event_refused(tmp_path):
    """A before-event that names no directory is refused whatever the tool; an unclassified tool is reported as
    such first, and an after-event with no directory is not a call to refuse."""
    data = {"tool": "read", "sessionID": "ses_nd", "id": "c1", "input": {"path": "x"}}
    verdict = _verdict(_bridge(tmp_path, ["event", "tool.execute.before"], json.dumps(data)))
    assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: directory ")
    verdict = _verdict(_bridge(tmp_path, ["event", "tool.execute.before"],
                               json.dumps({**data, "tool": "nosuchtool"})))
    assert verdict["reason"].startswith("refused: unclassified ")
    verdict = _verdict(_bridge(tmp_path, ["event", "tool.execute.after"],
                               json.dumps({**data, "output": {"output": "ok"}})))
    assert verdict["decision"] == "allow"


def test_project_config_off_or_refused(tmp_path):
    data = _shell(tmp_path)
    ok = _verdict(_bridge_env_without(tmp_path, ["event", "tool.execute.before"], json.dumps(data)))
    assert ok["decision"] == "allow", ok
    for drop, more in (((oe.PROJECT_CONFIG_OFF_ENV,), {}),
                       ((), {oe.PROJECT_CONFIG_PRECEDENCE_ENV: "1"})):
        for event, payload in (("tool.execute.before", data),
                               ("session.prompt", {"sessionID": "ses_t", "prompt": {"text": "go"}})):
            verdict = _verdict(_bridge_env_without(tmp_path, ["event", event], json.dumps(payload), drop=drop,
                                                   **more))
            assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: project-config "), (
                event, drop, more, verdict)
        after = _verdict(_bridge_env_without(tmp_path, ["event", "tool.execute.after"],
                                             json.dumps({**data, "output": {"output": "ok"}}), drop=drop, **more))
        assert after["decision"] == "allow", "an event that cannot refuse a call is not refused"


def test_config_dir_write_refused(tmp_path):
    """A write, an edit and a shell redirect into the session's config directory or the integration directory
    are refused before any row runs and leave the target as it was; the same calls elsewhere pass."""
    row, recorded = _record_rows(tmp_path, "PreToolUse")
    config, integration = tmp_path / "cfg", tmp_path / "oc"
    config.mkdir()
    seed = config / "seed.txt"
    seed.write_text("orig", encoding="utf-8")
    env = {oe.OPENCODE_CONFIG_DIR_ENV: str(config)}
    calls = [("write", {"path": str(config / "w.txt"), "content": "x"}, config / "w.txt"),
             ("edit", {"path": str(seed), "oldString": "orig", "newString": "changed"}, None),
             ("shell", {"command": f"echo x > {config}/s.txt"}, config / "s.txt"),
             ("write", {"path": str(integration / "plugin" / "p.ts"), "content": "x"}, integration / "plugin" / "p.ts"),
             ("shell", {"command": f"tee {integration}/state/t.txt < /dev/null"}, integration / "state" / "t.txt")]
    for tool, args, target in calls:
        verdict = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args), [row], **env))
        assert verdict["decision"] == "deny" and verdict["reason"].startswith("refused: config-dir "), (tool, verdict)
        assert target is None or not target.exists()
    assert seed.read_text(encoding="utf-8") == "orig" and not Path(recorded).exists()
    outside = tmp_path / "elsewhere.txt"
    for tool, args in (("write", {"path": str(outside), "content": "x"}),
                       ("shell", {"command": f"echo x > {outside}"})):
        verdict = _verdict(_event(tmp_path, "tool.execute.before", _call(tmp_path, tool, args), [row], **env))
        assert verdict["decision"] == "allow", (tool, verdict)


def _rows_session(tmp_path, rows, session="ses_rs", **data):
    """One before-event of ``session`` under the rows file ``rows``; ``data`` may carry ``rowsDigest``."""
    return _verdict(_event(tmp_path, "tool.execute.before", {**_shell(tmp_path), "sessionID": session, **data}, rows))


def test_rows_frozen_at_session_start(tmp_path):
    """A row added after the session began does not take effect in it, one removed does not drop out."""
    denier = _script(tmp_path, "deny_row.py", DENIER)
    deny_row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}
    allowed = _rows_session(tmp_path, [], session="ses_a")
    assert allowed["decision"] == "allow" and allowed["digest"]
    later = _rows_session(tmp_path, [deny_row], session="ses_a", rowsDigest=allowed["digest"])
    assert later["decision"] == "allow" and later["digest"] == allowed["digest"], "an added row is not seen"
    denied = _rows_session(tmp_path, [deny_row], session="ses_b")
    assert denied["decision"] == "deny"
    later = _rows_session(tmp_path, [], session="ses_b", rowsDigest=denied["digest"])
    assert later["decision"] == "deny" and later["reason"].startswith("deny: deny_row.py "), "a removed row stays"


def test_rows_snapshot_changed_mid_session_refused(tmp_path):
    first = _rows_session(tmp_path, [])
    snapshot = oe.snapshots_dir(tmp_path / "oc") / "ses_rs.json"
    assert snapshot.is_file()
    wrong = _rows_session(tmp_path, [], rowsDigest="0" * 64)
    assert wrong["decision"] == "deny" and wrong["reason"].startswith("refused: rows-snapshot ")
    snapshot.write_text(json.dumps({"rows": [], "plugin_tools": ["x"]}), encoding="utf-8")
    edited = _rows_session(tmp_path, [], rowsDigest=first["digest"])
    assert edited["decision"] == "deny" and edited["reason"].startswith("refused: rows-snapshot ")
    snapshot.unlink()
    missing = _rows_session(tmp_path, [], rowsDigest=first["digest"])
    assert missing["decision"] == "deny" and missing["reason"].startswith("refused: rows-snapshot ")
    nameless = _verdict(_event(tmp_path, "tool.execute.before", {**_shell(tmp_path), "sessionID": ""}, []))
    assert nameless["decision"] == "deny" and nameless["reason"].startswith("refused: rows-snapshot ")


def test_rows_snapshot_rebuilt_for_new_process(tmp_path):
    """An event with no digest (a new process of the shim) builds the snapshot again from the sources."""
    denier = _script(tmp_path, "deny_row.py", DENIER)
    deny_row = {"event": "PreToolUse", "matcher": "Bash", "script": str(denier), "timeout_s": 20}
    first = _rows_session(tmp_path, [])
    assert first["decision"] == "allow"
    second = _rows_session(tmp_path, [deny_row])
    assert second["decision"] == "deny" and second["digest"] != first["digest"]
    third = _rows_session(tmp_path, [deny_row], rowsDigest=second["digest"])
    assert third["decision"] == "deny" and third["digest"] == second["digest"]


# --- A6 route states: the pure rules and the derived runs ----------------------------------------------

class _FakeRun:
    def __init__(self, loaded, tool_name):
        self.exit_code = 0
        self._loaded = sorted(loaded)
        self._tool_name = tool_name

    def read(self, name):
        if name == "routes.log":
            return "\n".join(json.dumps({"route": r, "at": "module"}) for r in self._loaded)
        return ""

    def tool_list(self):
        return [self._tool_name(r) for r in self._loaded]

    def tool_description(self, tool):
        return ""


class _FakeHarness:
    """Stands in for the probe harness: the control run loads ``control`` members, the run with the variable set
    loads ``with_variable`` members."""

    def __init__(self, control, with_variable):
        self.control, self.with_variable = control, with_variable

    def run(self, scenario, *, label, config=None, files=None, env=None, cwd=None, timeout=None, **more):
        from opencode_probe.adapter import facts_routes

        loaded = self.with_variable if "OPENCODE_DISABLE_PROJECT_CONFIG" in (env or {}) else self.control
        return _FakeRun(loaded, facts_routes.tool_name)


NAMES = {"scan_names": [".claude", ".opencode"], "plugin_directory_names": ["plugin", "plugins"],
         "config_file_names": ["opencode.json"], "tool_directory_glob": False, "scan_list_expected": True}


def _members():
    from opencode_probe.adapter import facts_routes

    return facts_routes.derive_members(NAMES)


def _routes(control_ids, variable_ids, names=NAMES):
    from opencode_probe.adapter import facts_routes

    return facts_routes.project_routes(_FakeHarness(set(control_ids), set(variable_ids)), oe.PROJECT_CONFIG_OFF,
                                       names=names)


def _opencode_dir_members():
    return {m["id"] for m in _members() if m["id"].startswith(".opencode/") or m["id"].startswith("opencode.json")}


def test_a6_project_config_needs_loaded_control():
    """A control that loaded nothing proves nothing: no member is ``off``, ``project_config_off`` stays false."""
    result = _routes(set(), set())
    assert result["project_config_off"] is False and not result["any_on"]
    assert set(r["state"] for r in result["project_routes"].values()) == {"unobserved"}
    assert result["tool_file_route"]["state"] == "unobserved"
    healthy = _routes(_opencode_dir_members(), set())
    states = {k: r["state"] for k, r in healthy["project_routes"].items()}
    assert states[".opencode/plugin/flat-js"] == "off" and states[".claude/plugin/flat-js"] == "absent", states
    assert healthy["project_config_off"] is True and healthy["tool_file_route"]["state"] == "absent"


def test_a6_variable_that_stops_nothing_is_k7():
    """A binary that keeps ``.claude/plugin/`` loading with the variable set: the member is ``on`` (K7)."""
    loaded = _opencode_dir_members() | {".claude/plugin/flat-js"}
    result = _routes(loaded, {".claude/plugin/flat-js"})
    assert result["project_routes"][".claude/plugin/flat-js"]["state"] == "on"
    assert result["any_on"] is True and result["project_config_off"] is False


def test_adapter_case_modules_use_no_undefined_global():
    """A case's ``run`` is reached only by the live phase, so a global it names but never imported would show
    first as an ``unobserved`` record; every global a function of the adapter package loads is bound."""
    import builtins
    import dis
    import importlib
    import pkgutil
    import types

    import opencode_probe.adapter as package

    def code_objects(code):
        yield code
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                yield from code_objects(const)

    unbound = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        source = Path(module.__file__).read_text(encoding="utf-8")
        for code in code_objects(compile(source, module.__file__, "exec")):
            for ins in dis.get_instructions(code):
                if ins.opname == "LOAD_GLOBAL" and not (
                        hasattr(module, ins.argval) or hasattr(builtins, ins.argval)):
                    unbound.append(f"{info.name}:{code.co_name}:{ins.argval}")
    assert unbound == []


def test_a6_absent_route_needs_sensitivity_and_scan_list():
    """``absent`` needs the same file form to have loaded in the control through another member, and the scan list
    to be the expected one; a tool-file route is ``absent`` only when a plugin tool was seen offered in the control."""
    from opencode_probe.adapter import facts_routes

    nothing_of_form = _routes({".opencode/plugin/flat-js"}, set())
    assert nothing_of_form["project_routes"][".claude/plugin/flat-ts"]["state"] == "unobserved"
    assert nothing_of_form["project_routes"][".claude/plugin/flat-js"]["state"] == "absent"
    odd_scan = _routes(_opencode_dir_members(), set(), names={**NAMES, "scan_list_expected": False})
    assert odd_scan["project_routes"][".claude/plugin/flat-js"]["state"] == "unobserved"
    assert odd_scan["project_config_off"] is False and odd_scan["tool_file_route"]["state"] == "unobserved"
    glob = _routes(_opencode_dir_members(), set(), names={**NAMES, "tool_directory_glob": True})
    assert glob["tool_file_route"]["state"] == "unobserved" and glob["project_config_off"] is False
    tools = facts_routes.tool_file_members()
    control_loaded = {tools[0]["id"]}
    assert _routes(_opencode_dir_members() | control_loaded, set())["tool_file_route"]["state"] == "off"
    assert _routes(_opencode_dir_members() | control_loaded, control_loaded)["tool_file_route"]["state"] == "on"
    states = {m["id"]: "off" for m in _members()}
    assert facts_routes.project_config_off(states, "unobserved") is False
    assert facts_routes.project_config_off({**states, ".claude/plugin/flat-js": "unobserved"}, "absent") is False
    assert facts_routes.project_config_off({}, "absent") is False


def test_adapter_record_a6_holds():
    """The pinned adapter record's A6 entry carries the derived route table, every member ``off`` or ``absent``,
    and a live config-directory write, edit and shell redirect refused; skipped while no record is pinned."""
    from opencode_probe import core, record

    pin = core.read_pin().get("last_pass", {}).get("adapter")
    found = record.pinned_record(core.state_dir(), pin) if pin else None
    if found is None:
        pytest.skip("no adapter record is pinned in this checkout's state directory")
    entry = (found[1].get("cases") or {}).get("A6") or {}
    evidence = entry.get("evidence") or {}
    assert entry.get("status") == record.PASS, entry.get("status")
    routes = evidence["project_routes"]
    assert routes and all(r["state"] in ("off", "absent") and not r["with_variable"] for r in routes.values())
    assert evidence["project_config_off"] is True and evidence["tool_file_route"]["state"] in ("off", "absent")
    assert evidence["scan_names"] and evidence["config_file_names"]
    assert evidence["config_dir_write_refused"] is True and evidence["marked_executed"] is False
    assert {c for c, v in evidence["config_dir"]["calls"].items() if v["refused"] and not v["executed"]} == {
        "write", "edit", "shell"}


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
    assert wired <= UNREADABLE_INPUT_HOOKS, sorted(wired - UNREADABLE_INPUT_HOOKS)


UNREADABLE_INPUT_HOOKS = {"scripts/hook-guard-canon-readonly.py", "scripts/hook-guard-permission-surface.py"}


SHARED_CORE = ("core", "harness", "runner", "record", "negative", "paths", "stub", "isolated", "cli",
               "coverage", "subject_only")


def test_shared_core_probe_files_unchanged():
    changed = _changed_since_start()
    guarded = {f"scripts/opencode_probe/{name}.py" for name in SHARED_CORE}
    touched = {p for p in changed if p in guarded or p.startswith(("scripts/opencode_probe/plugin/",
                                                                  "scripts/opencode_probe/mechanisms/"))}
    assert not touched, sorted(touched)
