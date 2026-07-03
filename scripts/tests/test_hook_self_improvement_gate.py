"""Tests for hook-self-improvement-gate.py — the end-of-turn Stop gate.

Covers the decision matrix:
  - feedback signal present + no skill engaged -> block;
  - a Skill(self-improvement) call in the assistant turn -> pass;
  - a Skill(overcome-difficulty) call -> pass;
  - no feedback signal -> pass;
  - dedup marker prevents a second block for the same message;
  - stop_hook_active=True -> no block (loop guard);
  - malformed / empty / missing-transcript input -> fail-open, no block.

Transcript fixtures are small JSONL files built in tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-self-improvement-gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_si_gate", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# --- transcript fixture builders --------------------------------------------

def _user_line(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}


def _tool_result_line() -> dict:
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "content": "ok"},
    ]}}


def _assistant_text_line(text: str) -> dict:
    return {"message": {"role": "assistant", "content": [
        {"type": "text", "text": text},
    ]}}


def _assistant_skill_line(skill: str) -> dict:
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": skill}},
    ]}}


def _write_transcript(tmp_path: Path, lines: list[dict], name="t.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the gate's state dir at a throwaway agent-home."""
    home = tmp_path / "agent-home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


# --- decide() matrix --------------------------------------------------------

def test_blocks_on_feedback_without_skill(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("почему ты не запустил тесты"),
        _assistant_text_line("Sorry, here is the answer."),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    assert out is not None
    assert out["decision"] == "block"
    assert "self-improvement" in out["reason"]


def test_passes_when_self_improvement_engaged(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("почему ты не запустил тесты"),
        _assistant_skill_line("self-improvement"),
        _assistant_text_line("done"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_passes_when_overcome_difficulty_engaged(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("you shouldn't have skipped that"),
        _assistant_skill_line("overcome-difficulty"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_passes_when_no_feedback_signal(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here you go"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_stop_hook_active_never_blocks(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("почему ты не запустил тесты"),
        _assistant_text_line("answer"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": True}) is None


def test_dedup_blocks_at_most_once(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("почему ты не запустил тесты"),
        _assistant_text_line("answer"),
    ])
    payload = {"transcript_path": str(t), "stop_hook_active": False}
    first = _mod.decide(payload)
    second = _mod.decide(payload)
    assert first is not None and first["decision"] == "block"
    assert second is None  # marker suppresses the repeat


def test_tool_result_user_turn_is_not_the_trigger(tmp_path, isolated_state):
    # The last *human-text* user message is neutral; a later tool_result user
    # message must not be mistaken for a new prompt.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_skill_line("some-other-skill"),
        _tool_result_line(),
        _assistant_text_line("done"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


# --- fail-open robustness ---------------------------------------------------

def test_missing_transcript_path(isolated_state):
    assert _mod.decide({"stop_hook_active": False}) is None


def test_nonexistent_transcript(tmp_path, isolated_state):
    assert _mod.decide(
        {"transcript_path": str(tmp_path / "nope.jsonl"), "stop_hook_active": False}
    ) is None


def test_empty_transcript(tmp_path, isolated_state):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert _mod.decide({"transcript_path": str(p), "stop_hook_active": False}) is None


# --- main() via subprocess: exit 0 always, block JSON on stdout -------------

def _run(stdin_bytes: bytes, env=None):
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_bytes,
        capture_output=True,
        env=env,
    )


def test_main_emits_block_json(tmp_path, monkeypatch):
    import os
    t = _write_transcript(tmp_path, [
        _user_line("почему ты не запустил тесты"),
        _assistant_text_line("answer"),
    ])
    env = dict(os.environ)
    env["CLAUDE_AGENT_HOME"] = str(tmp_path / "home")
    env.pop("CLAUDE_CONFIG_DIR", None)
    p = _run(json.dumps({"transcript_path": str(t), "stop_hook_active": False}).encode(), env=env)
    assert p.returncode == 0
    directive = json.loads(p.stdout.decode())
    assert directive["decision"] == "block"


def test_main_malformed_stdin_exit_0():
    p = _run(b"not json at all")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


def test_main_empty_stdin_exit_0():
    p = _run(b"")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""
