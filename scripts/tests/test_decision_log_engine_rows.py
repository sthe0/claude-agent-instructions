"""cli._log_gate writes one engine row to the decision log per gate evaluation."""
import json
from argparse import Namespace

import pytest

from agentctl import cli
from lib import config_root


def _rows():
    path = config_root.decision_log()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def state(store):
    cli.cmd_start(Namespace(session="engine-rows", task="t", goal="g", done_criterion="d",
                            criterion_type="measurable", recursion_depth=0), store=store)
    return store.load("engine-rows")


def test_a_refusal_is_a_deny_row_and_a_pass_is_a_silent_row(state):
    cli._log_gate(state, "plan_approval", ["blocked"], passed=False)
    cli._log_gate(state, "plan_approval", [], passed=True)
    refused, passed = [r for r in _rows() if r["mechanism"] == "engine:plan_approval"]
    assert (refused["decision"], passed["decision"]) == ("deny", "silent")
    assert refused["event"] == "engine" and refused["layer"] == "engine"
    assert refused["session"] == "engine-rows" and refused["test"] is True


def test_every_gate_log_line_has_one_engine_row(state):
    for label, ok in (("resolution", False), ("renorm_final", True), ("escalation_orphans", True),
                      ("frozen_stage", True)):
        cli._log_gate(state, label, [] if ok else ["x"], passed=ok)
    gate_lines = cli.GATE_LOG.read_text(encoding="utf-8").splitlines()
    assert len(_rows()) == len(gate_lines)
    assert [r["mechanism"] for r in _rows()][-4:] == [
        "engine:resolution", "engine:renorm_final", "engine:escalation_orphans", "engine:frozen_stage"]


def test_the_engine_row_does_not_pass_through_the_hook_writer(state, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("engine row went through the hook writer")
    monkeypatch.setattr(cli.hook_telemetry, "record", refuse)
    cli._log_gate(state, "plan_approval", [], passed=True)
    assert [r["mechanism"] for r in _rows()] == ["engine:plan_approval"]


def test_a_failing_path_resolver_leaves_the_gate_log_row_intact(state, monkeypatch):
    def broken():
        raise RuntimeError("no log")
    monkeypatch.setattr(config_root, "decision_log", broken)
    cli._log_gate(state, "plan_approval", [], passed=True)
    assert cli.GATE_LOG.read_text(encoding="utf-8").strip()


def test_an_uncreatable_log_changes_nothing(state, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("AGENTCTL_DECISION_LOG", str(blocker / "sub" / "log.jsonl"))
    cli._log_gate(state, "plan_approval", [], passed=True)
    assert cli.GATE_LOG.read_text(encoding="utf-8").strip()
