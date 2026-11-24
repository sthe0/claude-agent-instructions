"""Single-call launches follow the bound host, at one seam.

Every site of the registry (`docs/operations/llm-prompt-sites.md` § Single-call registry) is driven
through its own real runner against recording stand-in binaries on PATH. The stand-ins stand for
`claude`, `agent` and `opencode`; each call appends its binary name, argv, stdin and the
XDG_*/OPENCODE_*/AGENTCTL_RUNTIME_HOST* environment to a file. No model and no network is involved.

New production modules are imported inside the tests, so this module collects on a tree without them.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import inspect
import json
import os
import pwd
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from model_cli_path import path_with_arc_stub

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "host_launch"
GOLDEN = FIXTURES / "argv.json"
REGEN_ENV = "HOST_LAUNCH_REGEN"
LONG_ARG = 200
REPLY = "NO"
ENGINE = "eng-single-call"
HARNESS_SESSION = "ses_t"
OPENCODE_VERSION = "2.3.2"
PROVIDER_ID = "stub"
BASE_URL = "http://127.0.0.1:9/v1"
TIER_MODELS = {"low": "m-low", "medium": "m-medium", "high": "m-high"}
SITE_KEY = "scripts/agentctl/advisor.py:judge_binary_ask"

STAND_IN = '''#!{python}
import json, os, select, sys, time

name = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
if "--version" in argv:
    print(name + " v0")
    sys.exit(0)
data = ""
if select.select([sys.stdin], [], [], 0.5)[0]:
    data = sys.stdin.read()
env = {{k: v for k, v in os.environ.items()
        if k.startswith(("XDG_", "OPENCODE_", "AGENTCTL_RUNTIME_HOST"))}}
with open(os.environ["HOST_LAUNCH_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"binary": name, "argv": argv, "stdin": data, "env": env}}) + "\\n")
mode = os.environ.get("HOST_LAUNCH_MODE", "ok")
if mode == "timeout":
    time.sleep(60)
if mode == "error":
    print("stand-in failure", file=sys.stderr)
    sys.exit(3)
reply = os.environ.get("HOST_LAUNCH_REPLY", "{reply}")
if name == "opencode":
    print(json.dumps({{"type": "text", "part": {{"text": reply}}}}))
    print(json.dumps({{"type": "step_finish", "part": {{"tokens": {{"input": 1, "output": 1}}}}}}))
elif name == "claude" and "--output-format" in argv:
    print(json.dumps({{"result": reply, "usage": {{"input_tokens": 1, "output_tokens": 1}},
                      "total_cost_usd": 0.0}}))
else:
    print(reply)
'''


class Rig:
    """The recording stand-ins and the isolated environment they run in."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.bin = tmp_path / "standin-bin"
        self.bin.mkdir()
        self.record = tmp_path / "launches.jsonl"
        source = STAND_IN.format(python=sys.executable, reply=REPLY)
        for name in ("claude", "agent", "opencode"):
            path = self.bin / name
            path.write_text(source, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.home = tmp_path / "home"
        self.agent_home = tmp_path / "agent-home"
        self.home.mkdir()
        self.agent_home.mkdir()
        for var in ("CLAUDE_CONFIG_DIR", "AGENTCTL_OWN_SESSION", "AGENTCTL_HARNESS_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID", "AGENTCTL_RUNTIME_HOST", "AGENTCTL_RUNTIME_HOST_VERSION",
                    "AGENTCTL_LIVE_JUDGES", "HOST_LAUNCH_MODE", "HOST_LAUNCH_REPLY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(self.agent_home))
        monkeypatch.setenv("PATH", path_with_arc_stub(str(self.bin)))
        monkeypatch.setenv("HOST_LAUNCH_RECORD", str(self.record))
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        monkeypatch.setenv("CURSOR_API_KEY", "stand-in-key")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def calls(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def mode(self, value: str) -> None:
        self.monkeypatch.setenv("HOST_LAUNCH_MODE", value)

    def ledger(self) -> list:
        path = self.home / ".local" / "log" / "claude-single-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def ledger_text(self) -> str:
        path = self.home / ".local" / "log" / "claude-single-calls.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_state(self, host: str) -> None:
        directory = self.agent_home / "agentctl" / "state"
        directory.mkdir(parents=True, exist_ok=True)
        binds = [{"root_session_id": HARNESS_SESSION, "host": host, "harness_version": OPENCODE_VERSION,
                  "bound_at": "2026-11-24T00:00:00+00:00"}] if host == "opencode" else []
        state = {"session_id": ENGINE, "task_id": "t1", "runtime_host": host, "host_binds": binds}
        (directory / f"{ENGINE}.json").write_text(json.dumps(state), encoding="utf-8")

    def write_settings(self, *, provider: bool = True, models: "dict | None" = None) -> None:
        lines = []
        if provider:
            lines.append(f'opencode-provider = {{ id = "{PROVIDER_ID}", base_url = "{BASE_URL}" }}')
        for tier, model in (models or TIER_MODELS).items():
            lines.append(f'opencode-model-{tier} = "{model}"')
        (self.agent_home / "opencode.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def bind(self, host: str = "opencode", *, via: str = "own", provider: bool = True) -> None:
        """Bind this process to an engine session whose state names `host`: through the linked
        child's own-session variable, or through the harness conversation the engine last drove."""
        self.write_state(host)
        self.write_settings(provider=provider)
        self.monkeypatch.setenv("AGENTCTL_RUNTIME_HOST_VERSION", OPENCODE_VERSION)
        if via == "own":
            self.monkeypatch.setenv("AGENTCTL_OWN_SESSION", ENGINE)
        else:
            from lib import engine_session_binding

            engine_session_binding.record_drive(
                HARNESS_SESSION, ENGINE, dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc))
            self.monkeypatch.setenv("AGENTCTL_HARNESS_SESSION_ID", HARNESS_SESSION)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# --- the sites, each driven through its own real runner -----------------------------------------

GENERIC_POSITIONAL = {"name": "judge-site", "prompt_template": "{text}"}
SAMPLE_TEXT = "a sample text the judge reads, shall we go on?"

ADVISOR_RUNNER_JUDGES = (
    "judge_approval_ask", "judge_binary_ask", "judge_deferring_disposition", "judge_feedback_signal",
    "judge_landing_discipline_ask", "judge_outage_escalation", "judge_published_attachment",
    "judge_published_text_rules", "judge_same_difficulty", "judge_silent_closure",
    "_judge_user_text", "acceptance_judge", "judge_question_materiality",
)


def _rule_candidates() -> list:
    from lib import writer_rules

    rule = next(r for r in writer_rules.load_registry() if r.status == "judge")
    return [(rule.id, ["a hit line"])]


def _advisor_judge(name):
    def drive(host, _rig):
        from agentctl import advisor

        function = getattr(advisor, name)
        parameters = list(inspect.signature(function).parameters.values())
        args = []
        for parameter in parameters:
            if parameter.name == "runner":
                args.append(advisor.subprocess_runner)
                break
            if parameter.name == "candidates":
                args.append(_rule_candidates())
            else:
                args.append(GENERIC_POSITIONAL.get(parameter.name, SAMPLE_TEXT))
        values = {"enabled": True, "timeout": 20, "remaining": None, "ceiling": None,
                  "runtime_host": host}
        kwargs = {p.name: values[p.name] for p in parameters
                  if p.kind is p.KEYWORD_ONLY and p.name in values
                  and (p.default is p.empty or p.name in ("enabled", "runtime_host"))}
        function(*args, **kwargs)

    return drive


def _drive_enumerate_claims(host, _rig):
    from agentctl import advisor

    advisor.enumerate_claims(SAMPLE_TEXT, advisor.enumerate_subprocess_runner, runtime_host=host)


def _drive_enumerate_subprocess_runner(host, _rig):
    from agentctl import advisor

    advisor.enumerate_claims(SAMPLE_TEXT, runtime_host=host)


def _drive_judge(host, _rig):
    from agentctl import advisor

    payload = {"goal": "a goal", "weight_class": "substantive", "stages": ["one"]}
    advisor.judge(next(iter(advisor._PROMPTS)), payload, advisor.subprocess_runner,
                  enabled=True, runtime_host=host)


def _drive_judge_code_effects(host, rig):
    from agentctl import advisor

    advisor.judge_code_effects("echo hello", str(rig.tmp), timeout=20)


def _drive_invariants_coverage(host, _rig):
    from agentctl import advisor, gates

    gates._semantic_invariants_coverage(
        "an invariant nobody wrote down", "unrelated plan text", runner=advisor.subprocess_runner)


def _drive_judge_restatement(host, _rig):
    from agentctl import advisor, conditions

    conditions.judge_restatement("- the host is bound [probe: grep => x]", advisor.subprocess_runner,
                                 depends_on=[1])


def _drive_judge_collapse(host, _rig):
    from agentctl import advisor, procedure

    procedure.judge_collapse("a method of three steps", "1. read. 2. edit. 3. test.",
                             advisor.subprocess_runner)


def _drive_judge_echo(host, _rig):
    from agentctl import advisor, result_image

    result_image.judge_echo("the file exists", advisor.subprocess_runner,
                            verify_command="test -f x", done_criterion="the file exists")


def _drive_extract(host, _rig):
    from lib import marker_extract

    marker_extract.extract("The work is finished.\nCOMPLETED: done", runtime_host=host,
                           structural_review=False)


def _drive_grounding_ask(host, _rig):
    from agentctl import advisor, grounding_judge

    grounding_judge._ask("grounding-site", "a grounding prompt", advisor.subprocess_runner,
                         lambda text: text)


def _drive_real_judge(host, _rig):
    from mandate_cycle import driver

    driver.real_judge("a mandate prompt")


def site_drivers() -> dict:
    table = {f"scripts/agentctl/advisor.py:{name}": _advisor_judge(name)
             for name in ADVISOR_RUNNER_JUDGES}
    table.update({
        "scripts/agentctl/advisor.py:enumerate_claims": _drive_enumerate_claims,
        "scripts/agentctl/advisor.py:enumerate_subprocess_runner": _drive_enumerate_subprocess_runner,
        "scripts/agentctl/advisor.py:judge": _drive_judge,
        "scripts/agentctl/advisor.py:judge_code_effects": _drive_judge_code_effects,
        "scripts/agentctl/gates.py:_semantic_invariants_coverage": _drive_invariants_coverage,
        "scripts/agentctl/conditions.py:judge_restatement": _drive_judge_restatement,
        "scripts/agentctl/procedure.py:judge_collapse": _drive_judge_collapse,
        "scripts/agentctl/result_image.py:judge_echo": _drive_judge_echo,
        "scripts/lib/marker_extract.py:extract": _drive_extract,
        "scripts/agentctl/grounding_judge.py:_ask": _drive_grounding_ask,
        "scripts/mandate_cycle/driver.py:real_judge": _drive_real_judge,
    })
    return table


def drive_all(host: str, rig: Rig) -> dict:
    """Run every driver under `host`; the launches each one made, by site key."""
    seen: dict = {}
    for key, drive in sorted(site_drivers().items()):
        before = len(rig.calls())
        try:
            drive(host, rig)
        except Exception:  # a site fails open or raises its own error; only its launches matter here
            pass
        seen[key] = rig.calls()[before:]
    return seen


def _masked(argv: list) -> list:
    return [f"<arg>" if len(element) > LONG_ARG else element for element in argv]


def golden_view(seen: dict) -> dict:
    return {key: [{"binary": call["binary"], "argv": _masked(call["argv"])} for call in calls]
            for key, calls in seen.items()}


# --- goldens: Claude and Cursor argv do not move -------------------------------------------------


def test_golden_argv_claude_cursor(rig):
    captured = {}
    for host in ("claude", "cursor"):
        rig.record.unlink(missing_ok=True)
        captured[host] = golden_view(drive_all(host, rig))
    if os.environ.get(REGEN_ENV):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for host in ("claude", "cursor"):
        assert [key for key, calls in golden[host].items() if not calls] == []
        for key in golden[host]:
            assert captured[host].get(key) == golden[host][key], (host, key)


# --- OpenCode: one seam, one argv shape ----------------------------------------------------------


def drive_one(key: str, host: str, rig: Rig) -> None:
    site_drivers()[key](host, rig)


def all_calls(seen: dict) -> list:
    return [call for calls in seen.values() for call in calls]


def foreign_calls(calls: list, allowed: set) -> list:
    return [call for call in calls if call["binary"] not in allowed]


def describe(calls: list) -> str:
    return "; ".join(f"{call['binary']} {call['argv'][:4]}" for call in calls)


def model_of(call: dict) -> str:
    return call["argv"][call["argv"].index("--model") + 1]


def ask_binary(host: str, **kwargs):
    from agentctl import advisor

    return advisor.judge_binary_ask(SAMPLE_TEXT, advisor.subprocess_runner, runtime_host=host, **kwargs)


def test_opencode_argv_uses_run_subcommand(rig):
    rig.bind()
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    assert call["binary"] == "opencode"
    assert call["argv"][:5] == ["run", "--standalone", "--auto", "--format", "json"]
    assert "-p" not in call["argv"]
    assert model_of(call) in {f"{PROVIDER_ID}/{model}" for model in TIER_MODELS.values()}
    assert call["env"]["AGENTCTL_RUNTIME_HOST"] == "opencode"
    assert call["env"]["AGENTCTL_RUNTIME_HOST_VERSION"] == OPENCODE_VERSION


def test_toolless_single_call_agent(rig):
    from lib import opencode_env

    rig.bind()
    calls = all_calls(drive_all("claude", rig))
    assert calls
    for call in calls:
        assert call["argv"][call["argv"].index("--agent") + 1] == opencode_env.AGENT_NAME, describe([call])


def test_toolless_agent_defined_in_generated_config(rig):
    from lib import opencode_env

    rig.bind()
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    agent = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["agents"][opencode_env.AGENT_NAME]
    assert agent == opencode_env.agent_definition()
    assert agent["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]


def launch_opencode(prompt: str, *, trailing: bool) -> None:
    from lib import host_llm
    from lib.runtime_models import HOST_OPENCODE

    argv = host_llm.build_launch_argv(HOST_OPENCODE, f"{PROVIDER_ID}/m-low", structured_output=True)
    if trailing:
        host_llm.launch_single_call(argv + [prompt], timeout=30)
    else:
        host_llm.launch_single_call(argv, stdin=prompt, timeout=30)


def test_prompt_positional_up_to_bound(rig):
    from lib import host_llm

    rig.bind()
    prompts = ["short prompt", "x" * host_llm.OPENCODE_PROMPT_ARG_MAX_BYTES]
    for trailing in (True, False):
        for prompt in prompts:
            rig.record.unlink(missing_ok=True)
            launch_opencode(prompt, trailing=trailing)
            [call] = rig.calls()
            assert call["argv"][-1] == prompt and call["stdin"] == "", (trailing, len(prompt))
            assert call["argv"][-3:-1] == ["--agent", "agent-single-call"]


def test_prompt_by_stdin_above_bound(rig):
    from lib import host_llm

    rig.bind()
    bound = host_llm.OPENCODE_PROMPT_ARG_MAX_BYTES
    prompts = ["x" * (bound + 1), "é" * (bound // 2 + 1), "-leading option-like prompt"]
    for trailing in (True, False):
        for prompt in prompts:
            rig.record.unlink(missing_ok=True)
            launch_opencode(prompt, trailing=trailing)
            [call] = rig.calls()
            assert call["stdin"] == prompt, (trailing, prompt[:12], len(prompt))
            assert call["argv"][-2:] == ["--agent", "agent-single-call"]


# --- Claude and Cursor keep their fail-open defaults ---------------------------------------------


def test_fail_open_defaults_unchanged_under_claude(rig):
    for host in ("claude", "cursor"):
        for mode, outcome in (("error", "error"), ("timeout", "timeout")):
            rig.mode(mode)
            verdict, reason = ask_binary(host, timeout=1)
            assert verdict is False and "fail-open" in reason, (host, mode, reason)
            row = rig.ledger()[-1]
            assert (row["host"], row["outcome"]) == (host, outcome)


def test_fault_parity_timeout_and_fail_open(rig):
    seen = {}
    for label in ("claude", "opencode"):
        if label == "opencode":
            rig.bind()
        seen[label] = []
        for mode in ("error", "timeout"):
            rig.mode(mode)
            verdict, reason = ask_binary("claude", timeout=1)
            seen[label].append((verdict, "fail-open" in reason, rig.ledger()[-1]["outcome"]))
    assert seen["claude"] == seen["opencode"] == [(False, True, "error"), (False, True, "timeout")]


# --- every site, both hosts, and the tripwires ---------------------------------------------------


def test_every_site_exercised_under_both_hosts(rig):
    from lib import host_llm

    registry = {key for key, entry in host_llm.SINGLE_CALL_SITES.items() if entry["exempt"] is None}
    drivers = set(site_drivers())
    assert drivers == registry, (sorted(drivers - registry), sorted(registry - drivers))
    for label in ("unbound", "bound"):
        if label == "bound":
            rig.bind()
        seen = drive_all("claude", rig)
        assert [key for key, calls in seen.items() if not calls] == [], label


def test_tripwire_no_claude_under_opencode(rig):
    rig.bind()
    for requested in ("claude", "cursor", "opencode"):
        drive_all(requested, rig)
    assert rig.calls()
    stray = foreign_calls(rig.calls(), {"opencode"})
    assert stray == [], f"a host other than OpenCode launched under an OpenCode binding: {describe(stray)}"


def test_tripwire_no_opencode_under_claude_or_cursor(rig):
    for bound in (False, True):
        if bound:
            rig.bind("claude")
        for requested in ("claude", "cursor"):
            drive_all(requested, rig)
    assert rig.calls()
    stray = foreign_calls(rig.calls(), {"claude", "agent"})
    assert stray == [], f"OpenCode launched under a Claude or Cursor host: {describe(stray)}"


SEEDED_SOURCE = (
    "import subprocess\n\n\n"
    "def seeded():\n"
    "    return subprocess.run(['claude', '-p', 'a seeded hard-coded launch'], capture_output=True, text=True)\n"
)


def test_seeded_hard_coded_site_trips(rig, tmp_path):
    from lib import host_llm
    from test_llm_prompt_sites import launch_population

    root = tmp_path / "seeded-root"
    (root / "scripts").mkdir(parents=True)
    module = root / "scripts" / "seeded_site.py"
    module.write_text(SEEDED_SOURCE, encoding="utf-8")
    key = "scripts/seeded_site.py:seeded"
    assert key in launch_population(root)
    assert key not in host_llm.SINGLE_CALL_SITES
    spec = importlib.util.spec_from_file_location("seeded_site", module)
    seeded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seeded)
    rig.bind()
    seeded.seeded()
    stray = foreign_calls(rig.calls(), {"opencode"})
    assert [call["binary"] for call in stray] == ["claude"]


SWITCH_ALLOWED = {
    ("scripts/lib/marker_extract.py", "extractor_available"),
    ("scripts/lib/marker_extract.py", "model"),
}


def host_switches(rel: str) -> list:
    """(function, line) of every comparison that names OpenCode in the module `rel`."""
    found: list = []
    scope: list = []

    class Visitor(ast.NodeVisitor):
        def _enter(self, node):
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

        def visit_Compare(self, node):
            for operand in [node.left, *node.comparators]:
                for part in ast.walk(operand):
                    named = isinstance(part, ast.Name) and part.id == "HOST_OPENCODE"
                    attr = isinstance(part, ast.Attribute) and part.attr == "HOST_OPENCODE"
                    text = isinstance(part, ast.Constant) and part.value == "opencode"
                    if named or attr or text:
                        found.append((scope[-1] if scope else "<module>", node.lineno))
            self.generic_visit(node)

    Visitor().visit(ast.parse((REPO / rel).read_text(encoding="utf-8")))
    return found


def test_no_per_kind_host_switch():
    from lib import host_llm

    modules = sorted({key.split(":")[0] for key, entry in host_llm.SINGLE_CALL_SITES.items()
                      if entry["exempt"] is None})
    switches = [(rel, function, line) for rel in modules for function, line in host_switches(rel)
                if (rel, function) not in SWITCH_ALLOWED]
    assert switches == [], f"a site module decides on the OpenCode host itself: {switches}"


def test_fake_binary_is_recording_stand_in(rig):
    for name in ("claude", "agent", "opencode"):
        assert shutil.which(name) == str(rig.bin / name)
    assert "HOST_LAUNCH_RECORD" in (rig.bin / "opencode").read_text(encoding="utf-8")
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    assert call["binary"] == "claude" and call["argv"][0] == "-p"


def test_single_calls_share_toolless_seam(rig):
    from lib import host_llm, opencode_env

    rig.bind()
    calls = all_calls(drive_all("claude", rig))
    rows = rig.ledger()
    assert len(rows) == len(calls) > 0
    assert {row["site"] for row in rows} <= set(host_llm.SINGLE_CALL_SITES)
    assert {call["env"]["OPENCODE_CONFIG_CONTENT"] for call in calls} == {opencode_env.config_content()}
    assert len({call["env"]["XDG_CONFIG_HOME"] for call in calls}) == 1
    assert {call["env"]["XDG_CONFIG_HOME"] for call in calls} != {str(rig.home / ".config")}


# --- provider ------------------------------------------------------------------------------------


def generated_config(call: dict) -> dict:
    return json.loads(Path(call["env"]["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))


def test_provider_block_carries_base_url(rig):
    rig.bind()
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    config = generated_config(call)
    provider = config["providers"][PROVIDER_ID]
    assert provider["settings"]["baseURL"] == BASE_URL
    assert config["enabled_providers"] == [PROVIDER_ID]
    assert set(provider["models"]) == set(TIER_MODELS.values())


def test_provider_read_only_from_user_config(rig):
    user_dir = rig.home / ".config" / "opencode"
    user_dir.mkdir(parents=True)
    user_file = user_dir / "opencode.jsonc"
    user_file.write_text(
        '// the user own provider\n{"provider": {"acme": {"options": {"baseURL": "http://acme.invalid/v1"}}},'
        ' "theme": "dark"}\n', encoding="utf-8")
    before = (user_file.read_bytes(), user_file.stat().st_mtime_ns)
    rig.bind()
    rig.write_settings(provider=False, models={tier: f"acme/{model}" for tier, model in TIER_MODELS.items()})
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    assert generated_config(call) == {"provider": {"acme": {"options": {"baseURL": "http://acme.invalid/v1"}}}}
    assert model_of(call).startswith("acme/")
    assert (user_file.read_bytes(), user_file.stat().st_mtime_ns) == before
    assert call["env"]["XDG_CONFIG_HOME"] != str(rig.home / ".config")


# --- ledger --------------------------------------------------------------------------------------


def test_single_call_row_written_per_call(rig):
    rig.bind()
    drive_one(SITE_KEY, "claude", rig)
    drive_one(SITE_KEY, "claude", rig)
    rows = rig.ledger()
    assert len(rows) == len(rig.calls()) == 2
    for row in rows:
        assert set(row) >= {"ts", "site", "session", "host", "harness_version", "models",
                            "root_session_id", "outcome", "denying", "cost_usd"}
        assert (row["site"], row["host"], row["outcome"], row["denying"]) == (SITE_KEY, "opencode", "ok", False)
        assert row["session"] == ENGINE and row["harness_version"] == OPENCODE_VERSION
        assert row["cost_usd"] is None
    assert all('"cost_usd": null' in line for line in rig.ledger_text().splitlines())


def test_single_call_row_carries_task_key_and_root_session_id(rig):
    rig.bind(via="harness")
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    [row] = rig.ledger()
    assert row["session"] == ENGINE and row["root_session_id"] == HARNESS_SESSION
    assert row["models"] == [model_of(call)]
    assert row["models"] != [f"{PROVIDER_ID}/{TIER_MODELS['high']}"]


def test_denying_site_refuses_and_records(rig, monkeypatch):
    from agentctl import advisor
    from lib import host_llm

    refused: list = []

    def runner(argv, **kwargs):
        try:
            return advisor.subprocess_runner(argv, **kwargs)
        except host_llm.SingleCallRefused as exc:
            refused.append(exc)
            raise

    key = "scripts/agentctl/advisor.py:judge_deferring_disposition"
    assert host_llm.site_denying(key) and not host_llm.site_denying(SITE_KEY)
    rig.bind(provider=False)
    verdict, reason = advisor.judge_deferring_disposition(SAMPLE_TEXT, runner, timeout=20)
    assert verdict is False and "fail-open" in reason
    assert [exc.code for exc in refused] == ["provider_unconfigured"]
    assert rig.calls() == []
    [row] = rig.ledger()
    assert (row["site"], row["outcome"], row["denying"], row["host"]) == (key, "refused", True, "opencode")
    assert row["reason"]["code"] == "provider_unconfigured" and row["reason"]["detail"]

    verdict, reason = ask_binary("claude", timeout=20)
    assert verdict is False and "fail-open" in reason
    assert rig.calls() == []
    row = rig.ledger()[-1]
    assert (row["site"], row["outcome"], row["denying"]) == (SITE_KEY, "error", False)
    assert row["reason"]["code"] == "provider_unconfigured"

    rig.bind()
    (rig.bin / "opencode").unlink()
    monkeypatch.setenv("PATH", str(rig.bin))
    advisor.judge_deferring_disposition(SAMPLE_TEXT, runner, timeout=20)
    assert [exc.code for exc in refused][-1] == "binary_missing"
    assert rig.ledger()[-1]["outcome"] == "refused"


def load_synthetic_task():
    spec = importlib.util.spec_from_file_location(
        "telemetry_synthetic_task", REPO / "scripts" / "telemetry-synthetic-task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exempt_site_skipped_with_reason_under_opencode(rig):
    from lib import host_llm

    key = "scripts/telemetry-synthetic-task.py:run_task"
    reason = host_llm.SINGLE_CALL_SITES[key]["exempt"]
    assert reason
    task = load_synthetic_task()
    assert task.run_task("a prompt", dict(os.environ)) == 0
    assert [call["binary"] for call in rig.calls()] == ["claude"] and rig.ledger() == []
    rig.record.unlink()
    rig.bind()
    assert task.run_task("a prompt", dict(os.environ)) == 0
    assert rig.calls() == []
    [row] = rig.ledger()
    assert (row["site"], row["outcome"], row["host"]) == (key, "skipped", "opencode")
    assert row["reason"] == {"code": "exempt", "detail": reason}


# --- the P7 record -------------------------------------------------------------------------------


def p7_shape(argv: list) -> list:
    shape = list(argv)
    shape[0] = "<binary>"
    for flag in ("--model", "--agent"):
        shape[shape.index(flag) + 1] = "<value>"
    shape[-1] = "<prompt>"
    return shape


def test_argv_matches_latest_p7_record(rig, monkeypatch):
    import opencode_live
    from lib import opencode_env

    if "AGENT_OPENCODE_PROBE_STATE" not in os.environ:
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(real_home / ".local" / "state" / "opencode-probe"))
    _, record = opencode_live.latest_record("mechanisms")
    evidence = record["cases"]["P7"]["evidence"]
    rig.bind()
    drive_one(SITE_KEY, "claude", rig)
    [call] = rig.calls()
    mine = p7_shape(["opencode", *call["argv"]])
    assert evidence["paths"]
    assert evidence["agent_definition"]["toolless"] == opencode_env.agent_definition()
    assert evidence["config_channel"] == "OPENCODE_CONFIG_CONTENT"
    assert "OPENCODE_CONFIG_CONTENT" in call["env"]
    for path in evidence["paths"]:
        assert p7_shape(path["argv"]["toolless"]) == mine, path["id"]
        assert path["config_channel"] == evidence["config_channel"]
        assert set(path["env_keys"]) <= set(call["env"]), path["id"]
