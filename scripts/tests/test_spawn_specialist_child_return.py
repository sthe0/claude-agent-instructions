"""spawn-specialist.py: the spawn-cost ledger row states the marker the parent's dispatch routes
on the forwarded text, with its source, and a `claude -p` child is launched with the autonomous
loop withheld.

A fake process stands in for `claude -p` (as in test_spawn_specialist_stdin_prompt.py); `main()`
runs for real up to and after the launch.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agentctl.dispatch import parse_marker
from lib import marker_extract

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, result: str) -> None:
        self.returncode = 0
        self.pid = 424242
        self._result = result

    def communicate(self, input=None):
        return (json.dumps({"result": self._result, "cost_usd": 0}), "")


def _run(monkeypatch, tmp_path, capsys, result: str):
    mod = _load()
    captured: dict = {"ledger": []}

    def fake_launch(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return _FakeProc(result)

    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", captured["ledger"].append)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)
    # The legacy path: the forwarded text is the child's raw result.
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")

    plan = tmp_path / "plan.md"
    plan.write_text("a plan", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "spawn-specialist.py", "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--model", "haiku", "--effort", "medium",
    ])
    mod.main()
    captured["stdout"] = capsys.readouterr().out
    captured["row"] = next(e for e in captured["ledger"] if e.get("event") == "spawn")
    return captured


def test_ledger_records_forwarded_marker(monkeypatch, tmp_path, capsys):
    result = (
        "Summary of the work.\n"
        "COMPLETED: all stage steps done.\n\n"
        "Report body:\n"
        "**REPLAN:** was the alternative I considered and did not need.\n"
    )
    captured = _run(monkeypatch, tmp_path, capsys, result)

    routed, _ = parse_marker(captured["stdout"])
    assert routed == "COMPLETED"
    assert captured["row"]["return_marker"] == routed


def test_ledger_source_child(monkeypatch, tmp_path, capsys):
    captured = _run(monkeypatch, tmp_path, capsys, "COMPLETED: done.\n")

    assert captured["row"]["return_marker"] == "COMPLETED"
    assert captured["row"]["return_marker_source"] == "child"


def test_ledger_source_envelope(monkeypatch, tmp_path, capsys):
    captured = _run(monkeypatch, tmp_path, capsys, "API Error: Unable to connect to API (ENOTFOUND)")

    assert captured["row"]["return_marker"] == "CHILD_INFRA_FAILURE"
    assert captured["row"]["return_marker_source"] == "envelope"


def test_launch_withholds_autonomous_loop(monkeypatch, tmp_path, capsys):
    captured = _run(monkeypatch, tmp_path, capsys, "COMPLETED: done.\n")

    cmd = captured["cmd"]
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "ScheduleWakeup" in denied
    assert captured["env"]["CLAUDE_CODE_DISABLE_CRON"] == "1"


def test_markerless_final_stays_malformed(monkeypatch, tmp_path, capsys):
    # A marker an earlier message held is not recoverable: only the final result text counts.
    captured = _run(monkeypatch, tmp_path, capsys, "I have looked into it and finished the work.")

    routed, _ = parse_marker(captured["stdout"])
    assert routed == "MALFORMED"
    assert captured["row"]["malformed"] is True
    assert captured["row"]["return_marker"] == "MALFORMED"
    assert captured["row"]["return_marker_source"] == "envelope"
