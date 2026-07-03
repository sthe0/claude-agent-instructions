"""hook-ask-text-split.py: deny an AskUserQuestion preceded by substantive
same-turn assistant text (the client silently drops pre-tool-call text).
Driven end-to-end via subprocess with a synthetic transcript jsonl passed in the
payload's transcript_path. The pure gate_decision function is also unit-tested
directly via an importlib load."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-ask-text-split.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_ask_text_split", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def user_prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def tool_result_entry() -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


def assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def payload_for(transcript: Path) -> dict:
    return {"tool_name": "AskUserQuestion", "transcript_path": str(transcript)}


def decision_of(proc: subprocess.CompletedProcess) -> str:
    if not proc.stdout.strip():
        return "allow"
    out = json.loads(proc.stdout)
    return out["hookSpecificOutput"]["permissionDecision"]


def test_denies_over_threshold(tmp_path):
    # A true preamble: substantive assistant text emitted immediately before the
    # ask, with no completed tool call between it and the ask -> at risk -> deny.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("x" * 500),
    ])
    proc = run_hook(payload_for(t))
    assert proc.returncode == 0
    assert decision_of(proc) == "deny"
    assert "timer" in proc.stdout or "FINAL" in proc.stdout


def test_allows_short_status_line(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("ok"),
        assistant_text("Гейт утверждения:"),
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_allows_clean_turn_after_timer_notification(tmp_path):
    # The text-then-buttons split: long text belongs to the PREVIOUS turn,
    # the task-notification user entry opens a new turn with zero assistant text.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("x" * 5000),
        user_prompt("<task-notification>timer done</task-notification>"),
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_queued_command_attachment_is_a_boundary(tmp_path):
    # Real-transcript shape of the timer split (verified live 2026-07-03): a
    # background task-notification is injected as an `attachment` entry with
    # attachment.type == "queued_command", NOT as a user-typed entry. The
    # mid-turn `queue-operation` enqueue record must NOT count as a boundary.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("x" * 5000),
        {"type": "queue-operation", "operation": "enqueue",
         "content": "<task-notification>timer done</task-notification>"},
        assistant_text("y" * 300),  # still the same turn: enqueue is mid-turn
        {"type": "queue-operation", "operation": "remove"},
        {"type": "attachment",
         "attachment": {"type": "queued_command",
                        "prompt": "<task-notification>timer done</task-notification>"}},
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_other_attachments_are_not_boundaries(tmp_path):
    # hook_success and similar attachment records appear mid-turn constantly.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_text("x" * 500),
        {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PreToolUse:Bash"}},
    ])
    assert decision_of(run_hook(payload_for(t))) == "deny"


def test_render_checkpoint_excludes_earlier_narration(tmp_path):
    # Over-fire regression: a big narration block was emitted before an ordinary
    # tool call (and rendered when the tool ran), then a SHORT text precedes the
    # ask. Only the short text is at risk -> allow. Before the render-checkpoint
    # fix the hook summed the big narration and over-fired the deny.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_text("x" * 500),   # rendered when the tool below ran
        tool_result_entry(),         # render checkpoint
        assistant_text("y" * 150),   # the at-risk preamble, under threshold
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_true_preamble_still_denied(tmp_path):
    # No completed tool call between the substantive text and the ask -> the whole
    # >200-char block is at risk -> deny.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_text("x" * 500),
        tool_result_entry(),
        assistant_text("y" * 300),   # emitted after the checkpoint, before the ask
    ])
    assert decision_of(run_hook(payload_for(t))) == "deny"


def test_timer_split_allowed_zero_text(tmp_path):
    # The timer split: the turn opens with a queued_command attachment boundary and
    # the assistant emits ONLY the ask (zero text) -> total 0 -> allow.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("x" * 5000),
        {"type": "attachment",
         "attachment": {"type": "queued_command",
                        "prompt": "<task-notification>timer done</task-notification>"}},
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_fails_open_without_boundary(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [assistant_text("x" * 5000)])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_fails_open_on_missing_transcript(tmp_path):
    proc = run_hook(payload_for(tmp_path / "absent.jsonl"))
    assert proc.returncode == 0
    assert decision_of(proc) == "allow"


def test_ignores_other_tools(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt("hi"), assistant_text("x" * 5000)])
    proc = run_hook({"tool_name": "Bash", "transcript_path": str(t)})
    assert decision_of(proc) == "allow"


def test_meta_user_entries_are_not_boundaries(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_text("x" * 500),
        {"type": "user", "isMeta": True,
         "message": {"role": "user", "content": [{"type": "text", "text": "meta"}]}},
    ])
    assert decision_of(run_hook(payload_for(t))) == "deny"


def test_gate_decision_pure():
    mod = _load_module()
    assert mod.gate_decision(None) == ("allow", "")
    assert mod.gate_decision(0)[0] == "allow"
    assert mod.gate_decision(mod.THRESHOLD_CHARS)[0] == "allow"
    decision, reason = mod.gate_decision(mod.THRESHOLD_CHARS + 1)
    assert decision == "deny"
    assert "FINAL" in reason
