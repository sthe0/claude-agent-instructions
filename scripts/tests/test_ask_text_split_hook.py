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


def run_hook(payload: dict, denial_log: str = "/dev/null") -> subprocess.CompletedProcess:
    # The hook runs as a subprocess with a scrubbed env, so it cannot see a
    # CLAUDE_ASK_GATE_DENIAL_LOG set on the pytest parent — point its denial
    # sink here explicitly. Default /dev/null keeps every test off the real
    # ~/.local/log denial log; a test that verifies logging passes a tmp path.
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_ASK_GATE_DENIAL_LOG": denial_log},
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


def assistant_thinking(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": text}]}}


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


def test_denial_is_logged(tmp_path):
    # Every DENY appends one fail-open entry-shape record to the denial sink, so a
    # suspected false positive can be diagnosed after the fact.
    log = tmp_path / "denials.jsonl"
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt("go"), tool_result_entry()])
    proc = run_hook(payload_for(t), denial_log=str(log))
    assert decision_of(proc) == "deny"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["reason_head"] and isinstance(rec["tail"], list) and rec["tail"]
    assert rec["tail"][-1]["has_tool_result"] is True


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


def test_render_checkpoint_then_short_text_is_still_mid_turn_deny(tmp_path):
    # SUPERSEDED (2026-07-04): this used to be the over-fire regression guard —
    # a big narration rendered by an earlier tool call, then a SHORT text (under
    # threshold) before the ask -> old hook allowed it (text-length rule only).
    # The topological rule now strictly dominates: ANY completed tool call this
    # turn (the tool_result checkpoint below) denies the ask regardless of how
    # little text follows it, because that trailing text rides the ask's own
    # message and is dropped from the transcript, not merely unrendered — see
    # hook-ask-text-split.py module docstring. The over-fire scenario the old
    # test guarded against is subsumed, not reintroduced: mid-turn asks are
    # denied by design now, not by text miscounting.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_text("x" * 500),   # rendered when the tool below ran
        tool_result_entry(),         # render checkpoint -> tool call completed this turn
        assistant_text("y" * 150),   # under threshold, but mid-turn deny fires anyway
    ])
    assert decision_of(run_hook(payload_for(t))) == "deny"


def test_mid_turn_ask_zero_text_still_denied(tmp_path):
    # The case the old text-length-only hook missed entirely: a tool call
    # completes this turn and the assistant writes ZERO text before the ask.
    # Old hook: turn_text_len == 0 -> allow. New hook: has_tool_result_this_turn
    # is True regardless of text -> deny, because the ask itself already shares
    # a risky mid-turn position and any text later added to this same turn
    # would be unobservably dropped.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        tool_result_entry(),
    ])
    proc = run_hook(payload_for(t))
    assert decision_of(proc) == "deny"
    assert "tool call" in proc.stdout


def test_turn_opening_ask_after_thinking_only_is_allowed(tmp_path):
    # Turn-opening ask: a real user prompt, then only assistant `thinking`
    # entries (not rendered `text`, so they don't count as at-risk text and
    # aren't a tool call) before the ask -> allow.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("вопрос"),
        assistant_thinking("hmm let me consider"),
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_turn_opened_by_task_notification_then_ask_is_allowed(tmp_path):
    # The timer-split's second turn: a task-notification user entry is itself a
    # real-user-prompt-shaped turn boundary, so an ask immediately after it is
    # turn-opening, not mid-turn -> allow.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt("покажи план"),
        assistant_text("x" * 5000),
        user_prompt("<task-notification>timer done</task-notification>"),
    ])
    assert decision_of(run_hook(payload_for(t))) == "allow"


def test_fails_open_on_post_compact_shape_without_boundary(tmp_path):
    # Post-/compact transcript shape: only summary/attachment-type entries,
    # no real-user-prompt boundary anywhere -> the observable is unavailable ->
    # fails open, allow (never wedge the workflow on an ambiguous transcript).
    t = write_transcript(tmp_path / "t.jsonl", [
        {"type": "summary", "summary": "earlier conversation, compacted"},
        {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "SessionStart"}},
        assistant_text("x" * 5000),
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
    assert mod.gate_decision(None, None) == ("allow", "")
    assert mod.gate_decision(False, 0)[0] == "allow"
    assert mod.gate_decision(False, mod.THRESHOLD_CHARS)[0] == "allow"
    decision, reason = mod.gate_decision(False, mod.THRESHOLD_CHARS + 1)
    assert decision == "deny"
    assert "FINAL" in reason
    # mid-turn rule dominates regardless of at-risk text length
    decision, reason = mod.gate_decision(True, 0)
    assert decision == "deny"
    assert "tool call" in reason
    decision, reason = mod.gate_decision(True, mod.THRESHOLD_CHARS + 1)
    assert decision == "deny"
    assert "tool call" in reason
