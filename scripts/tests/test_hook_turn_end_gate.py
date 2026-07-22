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

from agentctl.dispatch import RunResult

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-turn-end-gate.py"


def _fake_runner(text, code=0):
    """Inject a canned judge_binary_ask verdict -- never a live model call."""
    def runner(argv, **kwargs):
        return RunResult(code, stdout=text, stderr="")
    return runner


def _capturing_runner(text, code=0):
    """Like _fake_runner but records every prompt argv, so a test can assert WHAT
    text the judge was fed (the injection-stripping contract)."""
    def runner(argv, **kwargs):
        runner.calls.append(argv)
        return RunResult(code, stdout=text, stderr="")
    runner.calls = []
    return runner


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
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert out is not None
    assert out["decision"] == "block"
    assert "self-improvement" in out["reason"]


def test_feedback_prefilter_fires_but_judge_no_does_not_block(tmp_path, isolated_state):
    """The false positive this stage removes: text that trips the regex prefilter
    but is NOT genuine agent-behavior feedback (analytical/meta prose). The judge
    says NO -> no block. Regression proof for the feedback axis, mirroring the
    outage axis's test_false_positive_escalation_no_deny_when_judge_says_no."""
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    assert _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("NO"),
    ) is None


def test_feedback_judge_is_fed_injection_stripped_text(tmp_path, isolated_state):
    """Contract: the feedback judge MUST receive strip_injected_context(user_text).
    The harness replays CLAUDE.md/SKILL.md into the user buffer inside a
    <system-reminder> span, which is dense with feedback-shaped language that would
    re-introduce the very false positive this judge removes. Wrap the tripping text
    in such a span and assert the runner's prompt carries the human text but NOT
    the injected content."""
    injected = (
        FEEDBACK
        + "\n<system-reminder>\nNext time ask first; you should have run the tests."
        "\n</system-reminder>"
    )
    t = _write_transcript(tmp_path, [
        _user_line(injected),
        _assistant_text_line("answer"),
    ])
    runner = _capturing_runner("NO")
    _mod.decide({"transcript_path": str(t), "stop_hook_active": False}, runner=runner)
    assert runner.calls, "judge was never invoked -- prefilter did not fire"
    prompt = runner.calls[0][-1]
    assert "system-reminder" not in prompt
    assert "Next time ask first" not in prompt
    # the human-authored feedback text survives stripping and reaches the judge
    assert "тесты" in prompt


def test_passes_when_self_improvement_engaged(tmp_path, isolated_state):
    # YES runner so the prefilter+judge both mark this a feedback turn; the pass is
    # then genuinely driven by the self-improvement skill invocation, not by the
    # judge being off (which would silence the guardian at its first line).
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_skill_line("self-improvement"),
        _assistant_text_line("done"),
    ])
    assert _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    ) is None


def test_passes_when_overcome_difficulty_engaged(tmp_path, isolated_state):
    # YES runner: the pass is genuinely driven by the overcome-difficulty
    # invocation, not by the judge being off.
    t = _write_transcript(tmp_path, [
        _user_line("you shouldn't have skipped that"),
        _assistant_skill_line("overcome-difficulty"),
    ])
    assert _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    ) is None


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
    first = _mod.decide(payload, runner=_fake_runner("YES"))
    second = _mod.decide(payload, runner=_fake_runner("YES"))
    assert first is not None and first["decision"] == "block"
    assert second is None  # marker suppresses the repeat


def test_marker_lands_under_turn_gate(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
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


# --- spawned-specialist inertness -------------------------------------------

def test_specialist_session_is_inert(tmp_path, isolated_state, monkeypatch):
    """In a spawned specialist (AGENT_RECURSION_DEPTH>=1) the turn-end gate must
    not fire: the specialist's contract is to emit its return marker, and a brief
    that merely mentions "self-improvement" would otherwise hijack it into a block."""
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("COMPLETED: did the thing"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None
    # inert means: nothing fired, so no dedup marker is written either
    assert not (isolated_state / "state" / "turn-gate").exists()


def test_root_session_still_blocks(tmp_path, isolated_state, monkeypatch):
    """Depth 0 (or unset) is the root coordinator — the gate still enforces."""
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "0")
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert out is not None and out["decision"] == "block"


def test_malformed_depth_falls_back_to_enforcing(tmp_path, isolated_state, monkeypatch):
    """A non-integer AGENT_RECURSION_DEPTH must not silence the gate (fail-closed
    on the enforcement side): the ValueError is swallowed and the turn is judged."""
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "not-a-number")
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_text_line("answer"),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert out is not None and out["decision"] == "block"


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
    }, runner=_fake_runner("YES"))
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
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
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
    # self_improvement_feedback is the shell-computed fact (regex prefilter AND
    # semantic judge, both resolved in build_context); the guardian only reads it.
    # Set it True here so the guardian has a real blocker to compute — the judge's
    # I/O has already happened in the (impure) shell, never inside the guardian.
    ctx = _mod.TurnContext(
        last_user_text=FEEDBACK,
        invocations=frozenset(),
        transcript_path="/nonexistent/t.jsonl",
        session_key="sess-pure",
        agentctl_state=None,
        self_improvement_feedback=True,
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


# --- long-job auto-wake guardian --------------------------------------------

def test_long_job_blocks_on_detached_launch_without_waiter(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("kick off the training job"),
        _assistant_bash_line("nohup ./train.sh > log 2>&1 &", False),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    assert out is not None and out["decision"] == "block"
    assert "auto-wake" in out["reason"]


def test_long_job_silent_when_background_waiter_armed(tmp_path, isolated_state):
    # A harness-tracked run_in_background:true waiter that blocks on the job -> the
    # harness auto-wakes on its exit -> obligation met.
    t = _write_transcript(tmp_path, [
        _user_line("kick off the training job"),
        _assistant_bash_line("nohup ./train.sh &", False),
        _assistant_bash_line("wait $(cat job.pid); echo JOB_DONE", True),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_long_job_silent_when_launched_run_in_background(tmp_path, isolated_state):
    # Orchestrator launch done directly as a harness-tracked background Bash: detect()
    # fires, but the same tool_use is run_in_background:true -> auto-wake -> silent.
    t = _write_transcript(tmp_path, [
        _user_line("start the pipeline"),
        _assistant_bash_line("nirvana workflow start --id abc", True),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_long_job_silent_when_cron_armed(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("start the training job"),
        _assistant_bash_line("nohup ./train.sh &", False),
        _assistant_tool_use_line("CronCreate", {"schedule": "*/5 * * * *"}),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_long_job_still_blocks_when_only_schedulewakeup(tmp_path, isolated_state):
    # d2 regression guard: ScheduleWakeup no-ops outside /loop, so it does NOT
    # satisfy the auto-wake obligation — the guardian must STILL fire.
    t = _write_transcript(tmp_path, [
        _user_line("start the training job"),
        _assistant_bash_line("nohup ./train.sh &", False),
        _assistant_tool_use_line("ScheduleWakeup", {"delay_seconds": 300}),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    assert out is not None and "auto-wake" in out["reason"]


def test_long_job_blocks_on_foreground_poller_only(tmp_path, isolated_state):
    # A `setsid nohup ... &` poller returns immediately (run_in_background unset), so
    # it is not a harness-tracked waiter; the guardian still fires.
    t = _write_transcript(tmp_path, [
        _user_line("watch the job"),
        _assistant_bash_line("setsid nohup ./poll.sh &", False),
    ])
    out = _mod.decide({"transcript_path": str(t), "stop_hook_active": False})
    assert out is not None and "auto-wake" in out["reason"]


def test_long_job_silent_when_no_launch(tmp_path, isolated_state):
    t = _write_transcript(tmp_path, [
        _user_line("run the unit tests"),
        _assistant_bash_line("python3 -m pytest -q", False),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_long_job_autowake_registered_before_resolution():
    keys = list(_mod.TURN_GUARDIANS)
    assert "long_job_autowake" in keys
    assert keys.index("long_job_autowake") < keys.index("resolution")


def test_long_job_and_self_improvement_cofire_one_block(tmp_path, isolated_state):
    """A feedback-signal turn that also launched a detached job without a waiter
    produces ONE block naming BOTH obligations, self-improvement numbered first."""
    t = _write_transcript(tmp_path, [
        _user_line(FEEDBACK),
        _assistant_bash_line("nohup ./train.sh &", False),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert out is not None and out["decision"] == "block"
    reason = out["reason"]
    assert "self-improvement" in reason and "auto-wake" in reason
    assert "1." in reason and "2." in reason
    assert reason.index("self-improvement") < reason.index("auto-wake")


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
    # Exercise main()'s stdin -> block-JSON -> exit-0 wiring with a STRUCTURALLY
    # decidable obligation (a detached long-job launch with no auto-wake waiter),
    # which needs no model judge: main() passes advisor.subprocess_runner and a
    # subprocess cannot inject a fake runner, so a semantic (feedback) block would
    # fail open here. The long-job guardian fires deterministically instead.
    t = _write_transcript(tmp_path, [
        _user_line("kick off the training job"),
        _assistant_bash_line("nohup ./train.sh > log 2>&1 &", False),
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
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"},
        runner=_fake_runner("YES"),
    )
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


# --- escalation_without_diagnosis guardian ----------------------------------

# Assistant text that fires outage_escalation_detect (present-tense outage cue +
# a user-facing escalation frame). NEUTRAL user text is paired with it so the
# self-improvement guardian never co-fires and assertions stay clean.
ESCALATION_TEXT = "Сервис недоступен и не отвечает. К кому обратиться за доступом?"


class _FakeDifficulty:
    def __init__(self, declared: bool):
        self.declaration = object() if declared else None


class _StateWithDifficulty:
    """Minimal SessionState stand-in exposing only `.difficulty` — what the
    escalation guardian's difficulty_declared computation reads."""

    def __init__(self, declared: bool):
        self.difficulty = _FakeDifficulty(declared)


def _esc_ctx(sought=True, invocations=frozenset(), declared=False):
    return _mod.TurnContext(
        last_user_text="add a parser for the config file",
        invocations=invocations,
        transcript_path="/x.jsonl",
        session_key="s",
        agentctl_state=None,
        outage_escalation_sought=sought,
        difficulty_declared=declared,
    )


def test_escalation_guardian_fires_on_undiagnosed_escalation():
    out = _mod.escalation_without_diagnosis_blockers(_esc_ctx())
    assert len(out) == 1
    assert "external-service failure" in out[0]
    assert "overcome-difficulty" in out[0]


def test_escalation_guardian_silent_when_overcome_difficulty_invoked():
    ctx = _esc_ctx(invocations=frozenset({"overcome-difficulty"}))
    assert _mod.escalation_without_diagnosis_blockers(ctx) == []


def test_escalation_guardian_silent_when_declared():
    assert _mod.escalation_without_diagnosis_blockers(_esc_ctx(declared=True)) == []


def test_escalation_guardian_silent_when_not_sought():
    assert _mod.escalation_without_diagnosis_blockers(_esc_ctx(sought=False)) == []


def test_escalation_registered_after_self_improvement_before_resolution():
    keys = list(_mod.TURN_GUARDIANS)
    assert "escalation_without_diagnosis" in keys
    assert keys.index("self_improvement") < keys.index("escalation_without_diagnosis")
    assert keys.index("escalation_without_diagnosis") < keys.index("long_job_autowake")
    assert keys.index("escalation_without_diagnosis") < keys.index("resolution")


def test_difficulty_declared_reader():
    assert _mod._difficulty_declared(None) is False
    assert _mod._difficulty_declared(_StateWithDifficulty(declared=False)) is False
    assert _mod._difficulty_declared(_StateWithDifficulty(declared=True)) is True


# --- escalation guardian: integration through decide() ----------------------

def test_escalation_blocks_via_decide(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, None)  # no declared difficulty
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(ESCALATION_TEXT),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"},
        runner=_fake_runner("YES"),
    )
    assert out is not None and out["decision"] == "block"
    assert "external-service failure" in out["reason"]
    # neutral user text -> the self-improvement obligation is not among the blockers
    assert "agent-behavior-feedback" not in out["reason"]


def test_escalation_silent_when_overcome_difficulty_this_turn(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, None)
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_skill_line("overcome-difficulty"),
        _assistant_text_line(ESCALATION_TEXT),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


def test_escalation_silent_when_declare_present(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, _StateWithDifficulty(declared=True))
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(ESCALATION_TEXT),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


def test_escalation_silent_when_no_escalation_text(tmp_path, isolated_state, monkeypatch):
    _patch_state(monkeypatch, None)
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("here is the parser, all tests pass"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False, "session_id": "s1"}) is None


# --- prose_binary_ask guardian ----------------------------------------------

# Assistant text that fires judge_binary_ask (a trailing confirm question posed
# in prose). NEUTRAL user text is paired with it so the self-improvement guardian
# never co-fires and assertions stay clean.
PROSE_ASK_TEXT = "Готов черновик v11. Публикуем v11?"


def _pba_ctx(prose=True, invocations=frozenset(), closure=False):
    return _mod.TurnContext(
        last_user_text="add a parser for the config file",
        invocations=invocations,
        transcript_path="/x.jsonl",
        session_key="s",
        agentctl_state=None,
        closure_sought=closure,
        prose_binary_ask=prose,
    )


def test_prose_binary_ask_fires_on_trailing_confirm_question():
    out = _mod.prose_binary_ask_blockers(_pba_ctx())
    assert len(out) == 1
    assert "AskUserQuestion" in out[0]


def test_prose_binary_ask_silent_when_ask_invoked():
    ctx = _pba_ctx(invocations=frozenset({"AskUserQuestion"}))
    assert _mod.prose_binary_ask_blockers(ctx) == []


def test_prose_binary_ask_silent_when_closure_sought():
    assert _mod.prose_binary_ask_blockers(_pba_ctx(closure=True)) == []


def test_prose_binary_ask_silent_when_not_detected():
    assert _mod.prose_binary_ask_blockers(_pba_ctx(prose=False)) == []


def test_prose_binary_ask_registered_before_resolution():
    keys = list(_mod.TURN_GUARDIANS)
    assert "prose_binary_ask" in keys
    assert keys.index("prose_binary_ask") < keys.index("resolution")


def test_prose_binary_ask_blocks_via_decide(tmp_path, isolated_state):
    # Neutral user text + assistant text ending in a prose confirm, semantic
    # judge says YES -> only the prose_binary_ask guardian fires (no state, no
    # feedback, no outage).
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(PROSE_ASK_TEXT),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert out is not None and out["decision"] == "block"
    assert "AskUserQuestion" in out["reason"]
    assert "self-improvement" not in out["reason"]


def test_prose_binary_ask_silent_when_judge_says_no(tmp_path, isolated_state):
    # Same prose confirm text, but the semantic judge says NO -> no block.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(PROSE_ASK_TEXT),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("NO"),
    )
    assert out is None


def test_prose_binary_ask_blocks_on_russian_decisional_question(tmp_path, isolated_state):
    # The empirical miss that motivated this task: a confirm-verb lexicon
    # missed "Починить заодно?" in every language it was tried in. The semantic
    # judge, given YES, must fire; given NO, must stay silent.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("Починить заодно?"),
    ])
    blocked = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("YES"),
    )
    assert blocked is not None and blocked["decision"] == "block"

    allowed = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("NO"),
    )
    assert allowed is None


def test_prose_binary_ask_silent_on_rhetorical_comprehension_check(tmp_path, isolated_state):
    # Negative pin against over-fire (block-2 review): a rhetorical
    # comprehension check must not block even if a runner were somehow to
    # answer YES -- the judge is prompted to say NO here, and this test locks
    # that expectation down.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("Понятно?"),
    ])
    out = _mod.decide(
        {"transcript_path": str(t), "stop_hook_active": False},
        runner=_fake_runner("NO"),
    )
    assert out is None


def test_prose_binary_ask_silent_when_ask_emitted_this_turn(tmp_path, isolated_state):
    # The turn DID pose the decision through the click-gate -> obligation met.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(PROSE_ASK_TEXT),
        _assistant_tool_use_line("AskUserQuestion", {"questions": []}),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_prose_binary_ask_silent_when_sleep_timer_armed(tmp_path, isolated_state):
    # The legitimate delivery-split: artifact + confirm question this turn, sleep-2
    # armed so the ask opens next turn -> closure_sought -> guardian stays silent.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line(PROSE_ASK_TEXT),
        _assistant_bash_line("sleep 2", True),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None


def test_prose_binary_ask_silent_on_open_wh_question(tmp_path, isolated_state):
    # An open-ended (free-text) question is out of the detector's scope.
    t = _write_transcript(tmp_path, [
        _user_line("add a parser for the config file"),
        _assistant_text_line("Куда записать вывод?"),
    ])
    assert _mod.decide({"transcript_path": str(t), "stop_hook_active": False}) is None
