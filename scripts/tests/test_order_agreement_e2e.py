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
from agentctl.state import order_dismiss_marker
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


def _begin_substantive(cli: Cli) -> dict:
    cli.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")
    return cli.ok("classify", "--session", SESSION, "--architectural",
                  "--changed-lines", "400", "--wall-clock-min", "60")


def _present_order(cli: Cli, tmp_path: Path) -> str:
    classified = _begin_substantive(cli)
    assert "order_agreement_exemption" not in classified["data"], classified["detail"]
    return _present_draft(cli, tmp_path, _draft(tmp_path))


def _present_draft(cli: Cli, tmp_path: Path, draft: Path) -> str:
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


EDITED_CONSTRAINT = {"constraints": 'constraints = [{ id = "K1", text = "a new dependency is welcome" }]'}
NO_AGREEMENT = "no agreed order understanding"
DIVERGES = "diverges from the agreed"


def _agree(cli: Cli, tmp_path: Path, draft: Path | None = None) -> str:
    marker = _present_draft(cli, tmp_path, draft or _draft(tmp_path))
    cli.answer(marker, choose_marked=True)
    cli.ok("order-agree", "--session", SESSION, "--note", "as stated")
    return marker


def _said(proc_json: dict) -> str:
    return proc_json["detail"] + " | " + " | ".join(proc_json["data"].get("blockers", []))


def _present_essence(cli: Cli, tmp_path: Path) -> dict:
    rendering = tmp_path / "essence.txt"
    rendering.write_text("the plan, in essence", encoding="utf-8")
    proc = cli("present-plan", "--session", SESSION, "--kind", "essence",
               "--rendering-file", str(rendering))
    return json.loads(proc.stdout)


def test_an_edited_constraint_is_refused_at_every_act_that_takes_a_plan(cli, tmp_path):
    _begin_substantive(cli)
    _agree(cli, tmp_path)
    cli.ok("plan", "--session", SESSION)
    plan = Path(_plan(tmp_path))
    agreed_text = plan.read_text(encoding="utf-8")
    edited = Path(_plan(tmp_path, replace=EDITED_CONSTRAINT, name="edited.toml"))

    submit = cli.refused("submit-plan", "--session", SESSION, "--plan", str(edited))
    assert DIVERGES in _said(submit)
    cli.ok("submit-plan", "--session", SESSION, "--plan", str(plan))

    plan.write_text(edited.read_text(encoding="utf-8"), encoding="utf-8")
    approve = cli.refused("approve", "--session", SESSION, "--by", "user")
    assert DIVERGES in _said(approve)
    essence = _present_essence(cli, tmp_path)
    assert essence["ok"] is False and DIVERGES in _said(essence)
    replan = cli.refused("replan", "--session", SESSION, "--plan", str(edited))
    assert DIVERGES in _said(replan)

    plan.write_text(agreed_text, encoding="utf-8")
    assert DIVERGES not in _said(cli.refused("approve", "--session", SESSION, "--by", "user"))
    assert DIVERGES not in _said(_present_essence(cli, tmp_path))


def _challenge_the_agreed_order(cli: Cli, tmp_path: Path) -> str:
    cli.ok("plan", "--session", SESSION)
    cli.ok("submit-plan", "--session", SESSION, "--plan", _plan(tmp_path))
    reviewed = json.loads(cli("plan-review", "--session", SESSION, "--verdict", "revise",
                              "--reviewer", "thinker", "--concern",
                              "blocking: understanding: the second requirement is not what I ordered"
                              ).stdout)
    [challenge_id] = reviewed["data"]["concern_ids"]
    return challenge_id


def test_a_challenge_blocks_approval_until_the_user_dismisses_it(cli, tmp_path):
    _begin_substantive(cli)
    _agree(cli, tmp_path)
    challenge_id = _challenge_the_agreed_order(cli, tmp_path)

    held = cli.refused("approve", "--session", SESSION, "--by", "user")
    assert challenge_id in _said(held)
    unanswered = cli.refused("order-challenge-dismiss", "--session", SESSION, "--id", challenge_id,
                             "--note", "the user confirms R2 as written")
    assert "needs the user's own answer" in unanswered["detail"]

    cli.answer(order_dismiss_marker(challenge_id), choose_marked=True)
    cli.ok("order-challenge-dismiss", "--session", SESSION, "--id", challenge_id,
           "--note", "the user confirms R2 as written")

    assert challenge_id not in _said(cli.refused("approve", "--session", SESSION, "--by", "user"))


def test_a_new_agreement_closes_an_open_challenge(cli, tmp_path):
    _begin_substantive(cli)
    _agree(cli, tmp_path)
    challenge_id = _challenge_the_agreed_order(cli, tmp_path)
    assert challenge_id in _said(cli.refused("approve", "--session", SESSION, "--by", "user"))

    revised = _draft(tmp_path, name="revised.toml", replace={
        "difficulty": 'difficulty = "a release takes a day of hand-work and a review"'})
    _agree(cli, tmp_path, revised)

    approve = cli.refused("approve", "--session", SESSION, "--by", "user")
    assert challenge_id not in _said(approve)


def _classified(cli: Cli, *flags: str) -> dict:
    cli.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")
    return cli.ok("classify", "--session", SESSION, *flags)


def _unaffected_plan_cycle(cli: Cli, tmp_path: Path) -> None:
    """The gate would refuse this plan with no agreement; a session it does not govern takes it."""
    cli.ok("plan", "--session", SESSION)
    accepted = cli.ok("submit-plan", "--session", SESSION, "--plan", _plan(tmp_path))
    assert NO_AGREEMENT not in _said(accepted)
    assert cli.ok("status", "--session", SESSION)["data"]["order_agreement"] is None


@pytest.mark.parametrize("flags", [["--chat"], ["--changed-lines", "20"]], ids=["chat", "small-change"])
def test_chat_and_small_change_sessions_are_never_armed(cli, flags):
    classified = _classified(cli, *flags)

    assert "order_agreement_exemption" not in classified["data"]
    status = cli.ok("status", "--session", SESSION)["data"]
    assert status["order_agreement"] is None and status["order_agreement_exemption"] is None


def test_a_headless_session_is_exempt_and_takes_a_plan_without_agreement(root, cli, tmp_path):
    (root / "delivery-channel.toml").write_text('command = ["/bin/true"]\n', encoding="utf-8")

    classified = _begin_substantive(cli)

    assert "channel" in classified["data"]["order_agreement_exemption"]
    _unaffected_plan_cycle(cli, tmp_path)


def test_a_cursor_session_is_exempt_and_takes_a_plan_without_agreement(env, tmp_path):
    scrubbed = {k: v for k, v in env.items() if k != "CLAUDE_CODE_SESSION_ID" and "CURSOR" not in k}
    cursor = Cli(scrubbed, tmp_path)
    cursor.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc",
              "--host", "cursor")

    classified = cursor.ok("classify", "--session", SESSION, "--host", "cursor", "--architectural",
                           "--changed-lines", "400", "--wall-clock-min", "60")

    assert "host" in classified["data"]["order_agreement_exemption"]
    _unaffected_plan_cycle(cursor, tmp_path)


def _patch_state(root: Path, **fields) -> None:
    path = root / "agentctl" / "state" / f"{SESSION}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_linked_child_is_exempt_and_takes_a_plan_without_agreement(root, cli, tmp_path):
    cli.ok("start", "--session", SESSION, "--task", "od", "--goal", "g", "--done-criterion", "dc")
    _patch_state(root, parent_link={"parent_session": "p", "stage_index": 1, "depth": 1,
                                    "norm_digest": "x"})

    classified = cli.ok("classify", "--session", SESSION, "--architectural",
                        "--changed-lines", "400", "--wall-clock-min", "60")

    assert "linked" in classified["data"]["order_agreement_exemption"]
    _unaffected_plan_cycle(cli, tmp_path)


def test_a_sub_plan_is_not_held_to_the_parents_agreement(root, cli, tmp_path):
    _begin_substantive(cli)
    _agree(cli, tmp_path)
    cli.ok("plan", "--session", SESSION)
    cli.ok("submit-plan", "--session", SESSION, "--plan", _plan(tmp_path))
    state = json.loads((root / "agentctl" / "state" / f"{SESSION}.json").read_text(encoding="utf-8"))
    _patch_state(root, node="EXECUTING", route="IN_THREAD",
                 approval={**state["approval"], "armed": True, "passed": True, "by": "user"})

    cli.ok("push-subplan", "--session", SESSION, "--plan", _plan(tmp_path, name="child.toml"),
           "--originating-stage", "1")
    cli.ok("classify", "--session", SESSION, "--architectural",
           "--changed-lines", "400", "--wall-clock-min", "60")

    cli.ok("plan", "--session", SESSION)
    accepted = cli.ok("submit-plan", "--session", SESSION,
                      "--plan", _plan(tmp_path, replace=DIVERGED, name="child-diverged.toml"))
    assert DIVERGES not in _said(accepted) and NO_AGREEMENT not in _said(accepted)
    assert cli.ok("status", "--session", SESSION)["data"]["order_agreement"] is None
