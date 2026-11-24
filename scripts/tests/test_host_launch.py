"""Single-call launches follow the bound host, at one seam.

Every site of the registry (`docs/operations/llm-prompt-sites.md` § Single-call registry) is driven
through its own real runner against recording stand-in binaries on PATH. The stand-ins stand for
`claude`, `agent` and `opencode`; each call appends its binary name, argv, stdin and the
XDG_*/OPENCODE_*/AGENTCTL_RUNTIME_HOST* environment to a file. No model and no network is involved.

New production modules are imported inside the tests, so this module collects on a tree without them.
"""
from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from model_cli_path import path_with_arc_stub

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "host_launch"
GOLDEN = FIXTURES / "argv.json"
REGEN_ENV = "HOST_LAUNCH_REGEN"
LONG_ARG = 200
REPLY = "NO"

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

    def calls(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def mode(self, value: str) -> None:
        self.monkeypatch.setenv("HOST_LAUNCH_MODE", value)


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
