"""The order-agreement gate through the real command line and the real answer hook, no
in-process shortcuts: `agentctl-cli.py` and `hook-ask-answer-record.py` run as subprocesses
against a throw-away config root whose settings.json registers the hook, which is what lets
classify arm the session (a claude host, the hook registered, a root process).

The scene is the one the user lives: the agent submits, reviews and presents the order; the
agent tries to agree on its own (no flag exists; no answer exists; a declined answer is not
consent); the user's marked answer reaches the ledger through the hook; only then does the
agreement land, and only a plan carrying the agreed understanding is accepted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl import delivery
from test_order_agreement import DIVERGED, _plan
from test_order_phase import RENDERING, _draft

SCRIPTS = Path(__file__).resolve().parent.parent
CLI = SCRIPTS / "agentctl-cli.py"
HOOK = SCRIPTS / "hook-ask-answer-record.py"
SESSION = "e2e"
HARNESS = "e2e-harness"


@pytest.fixture
def root(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "AskUserQuestion",
        "hooks": [{"type": "command", "command": f"python3 {HOOK}"}],
    }]}}), encoding="utf-8")
    return config


@pytest.fixture
def env(root, tmp_path):
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "AGENT", "AGENTCTL"))}
    return {
        **base, "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(root),
        "CLAUDE_CODE_SESSION_ID": HARNESS, "CLAUDE_PUBLISHED_TEXT_GATE": "0",
        "AGENTCTL_EDIT_LEDGER": str(tmp_path / "edit-log.jsonl"),
    }


class Cli:
    def __init__(self, env, cwd):
        self.env, self.cwd = env, cwd

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                              timeout=120, env=self.env, cwd=self.cwd)

    def ok(self, *args: str) -> dict:
        proc = self(*args)
        assert proc.returncode == 0, (args, proc.stdout[-600:], proc.stderr[-600:])
        return json.loads(proc.stdout)

    def refused(self, *args: str) -> dict:
        proc = self(*args)
        assert proc.returncode == 1, (args, proc.returncode, proc.stdout[-600:], proc.stderr[-600:])
        return json.loads(proc.stdout)

    def answer(self, marker: str, *, choose_marked: bool) -> None:
        question = "Is this the order you gave?"
        marked = f"Yes, agreed {marker}"
        payload = {
            "tool_name": "AskUserQuestion", "session_id": SESSION,
            "tool_input": {"questions": [{"question": question,
                                          "options": [{"label": marked}, {"label": "No"}]}]},
            "tool_response": {"answers": {question: marked if choose_marked else "No"}},
        }
        proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60, env=self.env)
        assert proc.returncode == 0, proc.stderr


@pytest.fixture
def cli(env, tmp_path):
    return Cli(env, tmp_path)


def _present_order(cli: Cli, tmp_path: Path) -> str:
    cli.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")
    classified = cli.ok("classify", "--session", SESSION, "--architectural",
                        "--changed-lines", "400", "--wall-clock-min", "60")
    assert "order_agreement_exemption" not in classified["data"], classified["detail"]
    draft = _draft(tmp_path)
    cli.ok("order-submit", "--session", SESSION, "--plan", str(draft))
    cli.ok("plan-review", "--session", SESSION, "--scope", "order", "--verdict", "pass",
           "--reviewer", "thinker")
    rendering = tmp_path / "order-rendering.txt"
    rendering.write_text(RENDERING, encoding="utf-8")
    presented = cli.ok("present-plan", "--session", SESSION, "--kind", "order",
                       "--rendering-file", str(rendering))
    cli.ok("confirm-delivery", "--session", SESSION, "--kind", "order", "--by", "fedor",
           "--note", "no delivery hook in this scratch root",
           "--escape-reason", delivery.DELIVERY_ESCAPE_REASONS[0])
    return presented["data"]["order_agree_marker"]


def test_agreement_rests_on_the_users_answer_and_gates_the_plan(cli, tmp_path):
    marker = _present_order(cli, tmp_path)

    forged = cli("order-agree", "--session", SESSION, "--by", "agent")
    assert forged.returncode == 2, forged.stderr[-300:]
    unanswered = cli.refused("order-agree", "--session", SESSION)
    assert "needs the user's own answer" in unanswered["detail"]

    cli.answer(marker, choose_marked=False)
    assert "needs the user's own answer" in cli.refused("order-agree", "--session", SESSION)["detail"]
    assert cli.ok("status", "--session", SESSION)["data"]["order_agreement"] is None

    cli.ok("plan", "--session", SESSION)
    early = cli.refused("submit-plan", "--session", SESSION, "--plan", _plan(tmp_path))
    assert "no agreed order understanding" in " | ".join(early["data"]["blockers"])

    cli.answer(marker, choose_marked=True)
    agreed = cli.ok("order-agree", "--session", SESSION, "--note", "as stated")
    assert agreed["ok"]
    summary = cli.ok("status", "--session", SESSION)["data"]["order_agreement"]
    assert summary["anchor"] == "confirmed"

    diverged = cli.refused("submit-plan", "--session", SESSION,
                           "--plan", _plan(tmp_path, replace=DIVERGED, name="diverged.toml"))
    assert "diverges from the agreed" in " | ".join(diverged["data"]["blockers"])
    accepted = cli.ok("submit-plan", "--session", SESSION, "--plan", _plan(tmp_path))
    assert accepted["ok"]


def test_a_session_whose_hook_is_not_registered_is_named_exempt(root, cli, tmp_path):
    (root / "settings.json").write_text("{}", encoding="utf-8")
    cli.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")

    classified = cli.ok("classify", "--session", SESSION, "--architectural",
                        "--changed-lines", "400", "--wall-clock-min", "60")

    assert classified["data"]["order_agreement_exemption"]
    assert classified["data"]["order_agreement_exemption"] in cli.ok(
        "status", "--session", SESSION)["data"]["order_agreement_exemption"]


def test_a_spawned_process_is_named_exempt(env, tmp_path):
    spawned = Cli({**env, "AGENT_RECURSION_DEPTH": "1"}, tmp_path)
    spawned.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")

    classified = spawned.ok("classify", "--session", SESSION, "--architectural",
                            "--changed-lines", "400", "--wall-clock-min", "60")

    assert "spawned" in classified["data"]["order_agreement_exemption"]
