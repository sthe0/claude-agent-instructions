"""hook-ask-defer-timer.py: warn (Stop, non-blocking) when a turn promises to
ask via buttons "next message" but never arms the `sleep 2` background timer
that is supposed to open that next turn. Driven end-to-end via subprocess
with a synthetic transcript jsonl passed in the payload's transcript_path.
The pure should_warn function is also unit-tested directly via an importlib
load."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-ask-defer-timer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_ask_defer_timer", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(payload: dict, state_dir: Path | None = None) -> subprocess.CompletedProcess:
    # The one-shot marker dir is redirected next to the synthetic transcript
    # (inside tmp_path), so no test ever touches a real session's state and
    # each test starts with an unspent block.
    if state_dir is None:
        state_dir = Path(payload["transcript_path"]).parent / "askdefer-state"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "ASK_DEFER_TIMER_STATE_DIR": str(state_dir)},
    )


def block_decision(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)


def user_prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def assistant_tool_use(name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    }


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def payload_for(transcript: Path, session_id: str = "s1") -> dict:
    return {"session_id": session_id, "transcript_path": str(transcript), "hook_event_name": "Stop"}


PROMISE_TEXT = "Ладно, задам кнопками следующим сообщением."

# Verbatim from session a2b2e7c7 (2026-07-09) — the two phrasings the original
# pattern list missed, and the one it caught. Paraphrasing here would rebuild
# the very blind spot these cases exist to pin.
MISSED_PROMISE_1 = "Хорошо — переспрошу кнопками вместе с новыми развилками, когда план вернётся."
MISSED_PROMISE_2 = "План готов, я вынесу его тебе кнопками."
CAUGHT_PROMISE = "Вопросы никуда не денутся: они приедут кнопками следующим сообщением."


def test_warns_on_promise_without_timer_or_ask(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    assert "ask-defer-timer" in proc.stdout
    assert "timer" in proc.stdout


def test_silent_when_sleep_timer_armed(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
        assistant_tool_use("Bash", {"command": "sleep 2", "run_in_background": True}),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_silent_when_schedule_wakeup_armed(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
        assistant_tool_use("ScheduleWakeup", {"delay_seconds": 2}),
    ])
    proc = run_hook(payload_for(t))
    assert proc.stdout.strip() == ""


def test_foreground_sleep_does_not_count_as_timer(tmp_path):
    # A `sleep 2` NOT backgrounded doesn't open a next turn on its own — still
    # strands the ask, so the warning must still fire.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
        assistant_tool_use("Bash", {"command": "sleep 2", "run_in_background": False}),
    ])
    proc = run_hook(payload_for(t))
    assert "ask-defer-timer" in proc.stdout


def test_silent_when_ask_already_emitted_this_turn(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
        assistant_tool_use("AskUserQuestion", {"questions": []}),
    ])
    proc = run_hook(payload_for(t))
    assert proc.stdout.strip() == ""


def test_silent_when_no_promise(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("Вот план, готово."),
    ])
    proc = run_hook(payload_for(t))
    assert proc.stdout.strip() == ""


def test_fails_open_on_missing_transcript(tmp_path):
    proc = run_hook(payload_for(tmp_path / "absent.jsonl"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_fails_open_on_malformed_payload():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_fails_open_without_turn_boundary(tmp_path):
    # No real-user-prompt boundary anywhere -> observable unavailable -> silent.
    t = write_transcript(tmp_path / "t.jsonl", [assistant_text(PROMISE_TEXT)])
    proc = run_hook(payload_for(t))
    assert proc.stdout.strip() == ""


def test_only_current_turn_is_considered(tmp_path):
    # The promise + missing timer happened in a PRIOR turn (before the latest
    # boundary) -> the current turn carries neither -> silent.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("первый вопрос"),
        assistant_text(PROMISE_TEXT),
        user_prompt("второй вопрос"),
        assistant_text("Готово."),
    ])
    proc = run_hook(payload_for(t))
    assert proc.stdout.strip() == ""


def test_english_promise_pattern(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("show the plan"),
        assistant_text("I'll ask via buttons next message."),
    ])
    proc = run_hook(payload_for(t))
    assert "ask-defer-timer" in proc.stdout


def test_blocks_on_missed_phrasing_pereproshu(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("а вопросы?"),
        assistant_text(MISSED_PROMISE_1),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    assert block_decision(proc)["decision"] == "block"


def test_blocks_on_missed_phrasing_vynesu(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(MISSED_PROMISE_2),
    ])
    proc = run_hook(payload_for(t))
    assert block_decision(proc)["decision"] == "block"


def test_silent_on_compliant_turn_with_backgrounded_sleep(tmp_path):
    # The turn that COMPLIED: promises buttons and arms the timer in the same
    # turn -> nothing stranded, nothing to block.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(CAUGHT_PROMISE),
        assistant_tool_use("Bash", {"command": "sleep 2", "run_in_background": True}),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_quoted_promise_is_not_a_promise():
    mod = _load_module()
    fence = "Example of the phrasing:\n" + "`" * 3 + "\n" + MISSED_PROMISE_2 + "\n" + "`" * 3 + "\n"
    assert mod.has_deferral_promise(fence) is False
    quote = "> " + MISSED_PROMISE_2
    assert mod.has_deferral_promise(quote) is False
    inline = "The regex must catch `" + MISSED_PROMISE_2 + "` too."
    assert mod.has_deferral_promise(inline) is False


def test_promise_in_earlier_text_block_still_detected(tmp_path):
    # Pins that the scan covers ALL assistant text of the turn, not just the
    # last block: narrowing to last_assistant_message would miss this.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(MISSED_PROMISE_2),
        assistant_text("Готово."),
    ])
    proc = run_hook(payload_for(t))
    assert block_decision(proc)["decision"] == "block"


def test_block_payload_is_a_single_json_object(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    decision = block_decision(proc)
    assert isinstance(decision, dict)
    assert decision["decision"] == "block"
    assert "ask-defer-timer" in decision["reason"]


def test_one_shot_bound(tmp_path):
    state = tmp_path / "state"
    promising = write_transcript(tmp_path / "promise.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
    ])
    quiet = write_transcript(tmp_path / "quiet.jsonl", [
        user_prompt("покажи план"),
        assistant_text("Готово."),
    ])

    first = run_hook(payload_for(promising), state_dir=state)
    assert block_decision(first)["decision"] == "block"

    # Same session, still promising, no tool called: the bound consumes the
    # marker and lets the turn end rather than looping.
    second = run_hook(payload_for(promising), state_dir=state)
    assert second.returncode == 0
    assert second.stdout.strip() == ""

    # An intervening non-warning Stop clears the streak; the next promise blocks.
    assert run_hook(payload_for(quiet), state_dir=state).stdout.strip() == ""
    third = run_hook(payload_for(promising), state_dir=state)
    assert block_decision(third)["decision"] == "block"


def test_one_shot_marker_is_per_session(tmp_path):
    state = tmp_path / "state"
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text(PROMISE_TEXT),
    ])
    assert block_decision(run_hook(payload_for(t, "sA"), state_dir=state))["decision"] == "block"
    assert block_decision(run_hook(payload_for(t, "sB"), state_dir=state))["decision"] == "block"


def test_should_warn_pure():
    mod = _load_module()
    turn_no_timer = [user_prompt("q"), assistant_text(PROMISE_TEXT)]
    # user_prompt itself is the boundary entry, not part of "current turn"
    # entries in real usage, but should_warn only cares about the slice it's
    # given — build that slice directly here.
    current = [assistant_text(PROMISE_TEXT)]
    assert mod.should_warn(current) is True

    with_timer = [assistant_text(PROMISE_TEXT), assistant_tool_use("Bash", {"command": "sleep 2", "run_in_background": True})]
    assert mod.should_warn(with_timer) is False

    with_ask = [assistant_text(PROMISE_TEXT), assistant_tool_use("AskUserQuestion", {"questions": []})]
    assert mod.should_warn(with_ask) is False

    no_promise = [assistant_text("Готово.")]
    assert mod.should_warn(no_promise) is False
