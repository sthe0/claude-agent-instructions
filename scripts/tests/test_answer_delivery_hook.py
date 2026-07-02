"""hook-answer-delivery-reminder.py: nudge on AskUserQuestion timeout, silent otherwise."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-answer-delivery-reminder.py"


def _run(payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_timeout_result_nudges():
    r = _run({"tool_name": "AskUserQuestion",
              "tool_response": "No response after 60s — the user may be away from keyboard."})
    assert r.returncode == 0
    assert "final message" in r.stderr.lower()


def test_answered_result_silent():
    r = _run({"tool_name": "AskUserQuestion", "tool_response": "Your questions have been answered"})
    assert r.returncode == 0
    assert r.stderr == ""


def test_other_tool_silent():
    r = _run({"tool_name": "Bash", "tool_response": "No response after 60s"})
    assert r.returncode == 0
    assert r.stderr == ""


def test_dict_response_and_garbage_input_fail_open():
    r = _run({"tool_name": "AskUserQuestion", "tool_response": {"note": "No response after 60s"}})
    assert r.returncode == 0
    assert "final message" in r.stderr.lower()
    r2 = _run("not json")
    assert r2.returncode == 0
    assert r2.stderr == ""


def test_registered_in_installer_desired():
    installer = Path(__file__).resolve().parent.parent / "install-reminder-hooks.sh"
    text = installer.read_text(encoding="utf-8")
    assert '"PostToolUse",      "AskUserQuestion", "hook-answer-delivery-reminder.py"' in text
