"""Agreement tests for the shared timer_arm_detect detector.

The whole point of extracting timer_arm_detect.py is that the ask-defer warn hook
(hook-ask-defer-timer.py) and the resolution guardian's shell (hook-turn-end-
gate.py) can never drift apart on "did this turn seek closure?". These tests pin
that:

  1. Both consumers reference the SAME function objects (import, not reimplement).
  2. On a table of fabricated transcripts spanning both entry shapes, the ask-
     defer view and the turn-end-gate's frozen `closure_sought` agree with the
     shared detector's verdict — divergence is what the test forbids.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


tad = _load("timer_arm_detect", "timer_arm_detect.py")
defer_mod = _load("hook_ask_defer_timer", "hook-ask-defer-timer.py")
turn_mod = _load("hook_turn_end_gate", "hook-turn-end-gate.py")


# --- 1. both consumers reference the shared definitions ----------------------

def test_ask_defer_imports_shared_predicates():
    assert defer_mod.timer_armed is tad.timer_armed
    assert defer_mod.ask_already_emitted is tad.ask_emitted


def test_turn_gate_imports_shared_closure():
    assert turn_mod._closure_sought is tad.closure_sought


# --- 2. behavioral parity on the same fabricated transcripts -----------------

def _assistant_bash(command: str, background: bool) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": command, "run_in_background": background}},
    ]}}


def _assistant_tool(name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "input": tool_input},
    ]}}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text},
    ]}}


# Each case: (label, assistant-slice entries, expected closure verdict).
CASES = [
    ("backgrounded sleep 2", [_assistant_bash("sleep 2", True)], True),
    ("backgrounded sleep 60 poller", [_assistant_bash("sleep 60", True)], True),
    ("foreground sleep", [_assistant_bash("sleep 2", False)], False),
    ("ScheduleWakeup", [_assistant_tool("ScheduleWakeup", {"delay_seconds": 2})], True),
    ("CronCreate", [_assistant_tool("CronCreate", {"schedule": "* * * * *"})], True),
    ("AskUserQuestion inline", [_assistant_tool("AskUserQuestion", {"questions": []})], True),
    ("plain text only", [_assistant_text("done, everything green")], False),
    ("no tool, no timer", [_assistant_text("a"), _assistant_text("b")], False),
]


def test_shared_detector_verdicts():
    for label, entries, expected in CASES:
        assert tad.closure_sought(entries) is expected, label


def test_ask_defer_and_shared_agree():
    for label, entries, expected in CASES:
        ask_defer_view = defer_mod.timer_armed(entries) or defer_mod.ask_already_emitted(entries)
        assert ask_defer_view is expected, label


def test_turn_gate_shell_freezes_the_same_verdict(tmp_path):
    """build_context (the impure shell) must freeze closure_sought equal to the
    shared detector's verdict over the same turn slice — proving the guardian
    reads the identical fact the warn hook does."""
    for i, (label, entries, expected) in enumerate(CASES):
        lines = [{"message": {"role": "user", "content": "please do the thing"}}]
        lines.extend(entries)
        p = tmp_path / f"t{i}.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        ctx = turn_mod.build_context({"transcript_path": str(p), "session_id": None})
        assert ctx is not None, label
        assert ctx.closure_sought is expected, label
