"""Tests for hook-turn-end-gate.py — the end-of-turn Stop gate.

Covers the migrated self-improvement decision matrix:
  - feedback signal present + no skill engaged -> block;
  - a Skill(self-improvement) call in the assistant turn -> pass;
  - a Skill(overcome-difficulty) call -> pass;
  - no feedback signal -> pass;
  - dedup marker prevents a second block for the same message;
  - stop_hook_active=True -> no block (loop guard);
  - malformed / empty / missing / unreadable transcript -> fail-open, no block.

Plus the two properties the generalization introduces:
  - multi-guardian aggregation: two guardians firing produce exactly ONE block
    whose reason names both, and the SAME message never blocks a second time
    even when only one of the two obligations was addressed;
  - BEHAVIORAL guardian purity: every guardian in TURN_GUARDIANS is invoked with
    subprocess.Popen/run, socket.socket and builtins.open monkeypatched to raise,
    and must still return its blocker list. A guardian that delegates its I/O one
    call deep passes a source-substring search but fails this test.

Transcript fixtures are small JSONL files built in tmp_path.
"""
from __future__ import annotations

import builtins
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-turn-end-gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_turn_end_gate", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: `from __future__ import annotations` makes the frozen
    # TurnContext's field annotations strings, and dataclass resolves them via
    # sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()

FEEDBACK = "почему ты не запустил тесты"


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


def _assistant_tool_use_line(name: str, tool_input: dict) -> dict:
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "input": tool_input},
    ]}}


def _assistant_bash_line(command: str, background: bool) -> dict:
    return _assistant_tool_use_line(
        "Bash", {"command": command, "run_in_background": background}
    )


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
        _user_line(FEEDBACK),
        _assistant_text_line("Sorry, here is the answer."),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    assert out is not None
    assert out["decision"] == "block"
    assert "self-improvement" in out["reason"]


def test_passes_when_self_improvement_engaged(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
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
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": True}) is None


def test_dedup_blocks_at_most_once(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    payload = {"transcript_path": str(t), "stop_hook_active": False}
    first = _mod.decide(payload)
    second = _mod.decide(payload)
    assert first is not None and first["decision"] == "block"
    assert second is None  # marker suppresses the repeat


def test_marker_lands_under_turn_gate(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    markers = list((isolated_state / "state" / "turn-gate").glob("*"))
    assert len(markers) == 1


def test_no_marker_written_when_nothing_fires(tmp_path, isolated_state):
    """A guardian that did not fire must get another chance on the next stop, so
    an all-clear turn writes no marker."""
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here you go"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None
    assert not (isolated_state / "state" / "turn-gate").exists()


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


# --- multi-guardian aggregation ---------------------------------------------

def _second_guardian(ctx) -> list[str]:
    return ["The resolution gate was not closed: confirm with the user."]


def test_two_guardians_produce_exactly_one_block(tmp_path, isolated_state, monkeypatch):
    monkeypatch.setitem(_mod.TURN_GUARDIANS, "resolution", _second_guardian)
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    out = _mod.decide({
        "transcript_path": str(t), "stop_hook_active": False, "session_id": "sess-1",
    })
    assert out is not None and out["decision"] == "block"
    # ONE emission, whose numbered reason names BOTH unmet obligations.
    assert "self-improvement" in out["reason"]
    assert "resolution gate" in out["reason"]
    assert "1." in out["reason"] and "2." in out["reason"]


def test_same_message_never_blocks_twice_with_one_obligation_addressed(
    tmp_path, isolated_state, monkeypatch
):
    """The trade aggregation buys: the marker keys on the message alone, so once
    the message has blocked, a stop that addressed only ONE of the two named
    obligations is allowed through. Turn-boundedness over per-obligation
    enforcement — stated, not hidden."""
    monkeypatch.setitem(_mod.TURN_GUARDIANS, "resolution", _second_guardian)
    payload = {"stop_hook_active": False, "session_id": "sess-1"}

    first = _mod.decide({**payload, "transcript_path": str(_write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ]))})
    assert first is not None and first["decision"] == "block"

    # Same session + same triggering message; self-improvement addressed, the
    # resolution obligation still unmet -> allowed anyway.
    second = _mod.decide({**payload, "transcript_path": str(_write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_skill_line("self-improvement"),
    ], name="t2.jsonl"))})
    assert second is None


def test_raising_guardian_contributes_no_blocker(tmp_path, isolated_state, monkeypatch):
    def _boom(ctx):
        raise RuntimeError("guardian bug")

    monkeypatch.setitem(_mod.TURN_GUARDIANS, "boom", _boom)
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    # The healthy guardian still fires; the broken one is simply absent.
    assert out is not None and out["decision"] == "block"
    assert "guardian bug" not in out["reason"]
    assert "1." not in out["reason"]  # a single blocker is not numbered


def test_raising_guardian_alone_never_wedges(tmp_path, isolated_state, monkeypatch):
    def _boom(ctx):
        raise RuntimeError("guardian bug")

    monkeypatch.setitem(_mod.TURN_GUARDIANS, "boom", _boom)
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here you go"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


# --- behavioral guardian purity ---------------------------------------------

def test_guardians_are_behaviorally_pure():
    """Every registered guardian must decide from the frozen TurnContext alone.

    Not a source-substring check: the I/O primitives themselves are replaced, so a
    guardian that delegates one call deep (`return _judge(ctx)`) still fails."""
    ctx = _mod.TurnContext(
        last_user_text=FEEDBACK,
        invocations=frozenset(),
        transcript_path="/nonexistent/t.jsonl",
        session_key="sess-pure",
        agentctl_state=None,
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("guardian performed I/O")

    saved = (builtins.open, subprocess.Popen, subprocess.run, socket.socket)
    results: dict[str, list[str]] = {}
    builtins.open = _forbidden
    subprocess.Popen = _forbidden
    subprocess.run = _forbidden
    socket.socket = _forbidden
    try:
        for name, guardian in _mod.TURN_GUARDIANS.items():
            results[name] = guardian(ctx)
    finally:
        builtins.open, subprocess.Popen, subprocess.run, socket.socket = saved

    assert set(results) == set(_mod.TURN_GUARDIANS)
    for name, blockers in results.items():
        assert isinstance(blockers, list), f"{name} did not return a list"
    # The context is a live feedback turn, so this guardian must have really
    # computed a blocker rather than short-circuiting to [].
    assert len(results["self_improvement"]) == 1


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


def test_malformed_transcript_lines(tmp_path, isolated_state):
    p = tmp_path / "bad.jsonl"
    p.write_text("{not json\nalso not json\n", encoding="utf-8")
    assert _mod.decide({"transcript_path": str(p), "stop_hook_active": False}) is None


def test_unreadable_transcript(tmp_path, isolated_state):
    # A directory `exists()` but raises IsADirectoryError (an OSError) on open.
    d = tmp_path / "a-directory"
    d.mkdir()
    assert _mod.decide({"transcript_path": str(d), "stop_hook_active": False}) is None


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
        _user_line(FEEDBACK),
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


# --- resolution guardian: unit matrix over a fabricated SessionState ---------

class _FakeGate:
    def __init__(self, passed: bool):
        self.passed = passed


class _FakeState:
    """A stand-in for agentctl SessionState exposing only what the resolution
    guardian reads: weight_class, all_stages_passed(), resolution.passed."""

    def __init__(self, weight_class="SUBSTANTIVE", all_passed=True, resolution_passed=False):
        self.weight_class = weight_class
        self._all_passed = all_passed
        self.resolution = _FakeGate(resolution_passed)

    def all_stages_passed(self) -> bool:
        return self._all_passed


def _res_ctx(state, closure=False, text="add a parser for the config file"):
    return _mod.TurnContext(
        last_user_text=text,
        invocations=frozenset(),
        transcript_path="/x.jsonl",
        session_key="s",
        agentctl_state=state,
        closure_sought=closure,
    )


def test_resolution_fires_when_all_passed_and_no_closure():
    out = _mod.resolution_turn_blockers(_res_ctx(_FakeState()))
    assert len(out) == 1
    assert "resolution gate" in out[0]
    assert "verify-final" in out[0]


def test_resolution_silent_when_closure_sought():
    assert _mod.resolution_turn_blockers(_res_ctx(_FakeState(), closure=True)) == []


def test_resolution_silent_for_chat_and_small_change():
    assert _mod.resolution_turn_blockers(_res_ctx(_FakeState(weight_class="CHAT"))) == []
    assert _mod.resolution_turn_blockers(_res_ctx(_FakeState(weight_class="SMALL_CHANGE"))) == []


def test_resolution_silent_with_an_unpassed_stage():
    assert _mod.resolution_turn_blockers(_res_ctx(_FakeState(all_passed=False))) == []


def test_resolution_silent_when_gate_already_passed():
    assert _mod.resolution_turn_blockers(_res_ctx(_FakeState(resolution_passed=True))) == []


def test_resolution_silent_when_no_state():
    assert _mod.resolution_turn_blockers(_res_ctx(None)) == []


# --- resolution guardian: integration through decide() ----------------------

def _patch_state(monkeypatch, state):
    monkeypatch.setattr(_mod, "_load_agentctl_state", lambda sid: state)


def test_resolution_blocks_via_decide(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, _FakeState())
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here you go"),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"})
    assert out is not None and out["decision"] == "block"
    assert "resolution gate" in out["reason"]
    # neutral user text -> the self-improvement obligation is NOT among the blockers
    assert "self-improvement" not in out["reason"]


def test_resolution_silent_when_ask_emitted_this_turn(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, _FakeState())
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_tool_use_line("AskUserQuestion", {"questions": []}),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


def test_resolution_silent_when_backgrounded_sleep_armed(tmp_path, isolated_state, monkeypatch):
    # C2 regression pin: a backgrounded `sleep` is the delivery-split timer, so the
    # turn IS seeking closure on the next turn — the guardian must stay silent.
    _patch_state(monkeypatch, _FakeState())
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_bash_line("sleep 2", True),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


def test_resolution_silent_when_no_session_state(tmp_path, isolated_state, monkeypatch):
    # State absent / unparseable -> _load returns None -> fail open (no block).
    _patch_state(monkeypatch, None)
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here you go"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


def test_resolution_and_self_improvement_cofire_one_block(tmp_path, isolated_state, monkeypatch):
    """Both obligations unmet -> ONE block naming both, resolution named LAST."""
    _patch_state(monkeypatch, _FakeState())
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"})
    assert out is not None and out["decision"] == "block"
    reason = out["reason"]
    assert "self-improvement" in reason and "resolution gate" in reason
    assert "1." in reason and "2." in reason
    # resolution is registered last, so it is numbered after self-improvement
    assert reason.index("self-improvement") < reason.index("resolution gate")


def test_resolution_self_heals_after_a_poller_turn(tmp_path, isolated_state, monkeypatch):
    """A backgrounded `sleep 60` poller reads as closure (accepted false negative),
    so the guardian is silent that turn; the FOLLOWING timer-less turn re-evaluates
    and fires. The miss self-heals rather than persisting."""
    _patch_state(monkeypatch, _FakeState())
    t1 = _write_transcript(tmp_path, [
        _user_line("keep monitoring the job"),
        _assistant_bash_line("sleep 60", True),
    ], name="t1.jsonl")
    assert _mod.decide({"transcript_path": str(t1), "stop_hook_active": False, "session_id": "s1"}) is None

    t2 = _write_transcript(tmp_path, [
        _user_line("is there anything else"),
        _assistant_text_line("all done"),
    ], name="t2.jsonl")
    out = _mod.decide({"transcript_path": str(t2), "stop_hook_active": False, "session_id": "s1"})
    assert out is not None and "resolution gate" in out["reason"]
