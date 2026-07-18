"""lib/transcript_turns.py: direct unit coverage for delivered_final_texts (and a
couple of latest_turn_start/iso_to_epoch smoke checks) — the block-level
"did this text actually render" primitive hook-plan-delivery-gate.py's Stage 3
extension depends on. See test_plan_delivery_gate_hook.py for the same-turn
check's end-to-end coverage and test_plan_delivery_gate_presentation.py for the
delivery-verification gate built on top of this primitive.

Non-qualifying block-shape fixtures below are copied VERBATIM (content-block
shape and text) from real, observed live-platform transcripts — not invented —
per this stage's requirement to cover both observed shapes with genuine
provenance. Provenance is cited at each fixture's definition.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lib.transcript_turns import delivered_final_texts, iso_to_epoch, latest_turn_start


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def user_prompt_entry(ts: float, text: str = "hi") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "timestamp": iso(ts),
    }


def queued_command_entry(ts: float) -> dict:
    return {
        "type": "attachment",
        "attachment": {"type": "queued_command", "prompt": "<task-notification>timer done</task-notification>"},
        "timestamp": iso(ts),
    }


# Real fixture 1 (delivered/terminal shape — ALLOW-eligible): transcript
# .claude/projects/-home-the0-task-mounts-main-robot-deepagent/
# 038d367a-a877-41d3-a732-746c1bdf2653.jsonl, assistant entry
# uuid=78c79ee9-594e-4887-9081-fd2c2ab2b0ed, timestamp=2026-06-30T18:21:13.208Z.
# Content is a single terminal text block with no tool_use — the shape that
# proves rendering.
REAL_TEXT_ONLY = "I'll investigate both tasks. Let me start by gathering facts about the current mount state and the trust-dialog config."


def text_only_entry(ts: float, text: str = REAL_TEXT_ONLY) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": iso(ts),
    }


# Real fixture 2 (minority pre-tool-call trap shape [text, tool_use]): same
# transcript as above, assistant entry
# uuid=63fbf772-276b-47b7-a917-56e39ee3eb5a, timestamp=2026-06-30T18:57:20.394Z.
# The text precedes a same-message tool_use — exactly the shape CLAUDE.md's
# turn-split defect describes: recorded, never rendered.
REAL_TEXT_THEN_TOOL_USE = "User chose rebase. Делаю ребейз `pr-gate-scope-clarify` на свежий trunk."


def text_then_tool_use_entry(ts: float, text: str = REAL_TEXT_THEN_TOOL_USE) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}},
            ],
        },
        "timestamp": iso(ts),
    }


# Real fixture 3 (majority pre-tool-call trap shape [thinking, text, tool_use]):
# transcript .claude/projects/-home-the0-arcadia-DEEPAGENT-433-qwen3-default-
# robot-deepagent/04c47a03-a673-4e99-a549-c9f2076a594f.jsonl, assistant entry
# uuid=50ea1627-b8e8-49c7-be34-edcb470368d8, version=2.1.178. The leading text
# block again precedes a same-message tool_use (a Skill call) — same trap,
# with a thinking block ahead of it (the majority live shape).
REAL_THINKING_TEXT_TOOL_USE = "Plain-URL ссылки перепощены в тикет (201)."


def thinking_text_tool_use_entry(ts: float, text: str = REAL_THINKING_TEXT_TOOL_USE) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": text},
                {"type": "tool_use", "id": "toolu_2", "name": "Skill", "input": {"skill": "self-improvement"}},
            ],
        },
        "timestamp": iso(ts),
    }


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


# --- delivered_final_texts ----------------------------------------------------

def test_terminal_text_only_message_qualifies(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_only_entry(105.0),
        user_prompt_entry(110.0),  # closes the segment
    ])
    result = delivered_final_texts(t)
    assert result == [(REAL_TEXT_ONLY, 105.0)]


def test_text_then_tool_use_message_never_qualifies(tmp_path):
    # Real minority trap shape: text precedes a same-message tool_use -> the
    # terminal assistant message HAS a tool_use block, so it must be excluded
    # even though it is the last (and only) assistant entry in the segment.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_then_tool_use_entry(105.0),
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == []


def test_thinking_text_tool_use_message_never_qualifies(tmp_path):
    # Real majority trap shape.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        thinking_text_tool_use_entry(105.0),
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == []


def test_mixed_shape_segment_only_terminal_message_counts(tmp_path):
    # A segment can carry MULTIPLE assistant entries: an early trap-shaped one
    # (tool call), then a later genuine terminal text reply. Block-granularity
    # means only the LAST assistant entry of the segment is examined at all —
    # the trap-shaped one earlier in the same segment must not leak through.
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_then_tool_use_entry(101.0),
        text_only_entry(105.0, "final reply"),
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == [("final reply", 105.0)]


def test_current_incomplete_turn_excluded(tmp_path):
    # No boundary follows the terminal text-only message -> it is the CURRENT,
    # still-open turn and must be excluded entirely (not even a candidate).
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_only_entry(105.0),
    ])
    assert delivered_final_texts(t) == []


def test_queued_command_boundary_closes_a_segment(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_only_entry(102.0, "delivered before timer"),
        queued_command_entry(110.0),
        user_prompt_entry(120.0),
    ])
    assert delivered_final_texts(t) == [("delivered before timer", 102.0)]


def test_no_boundary_at_all_returns_empty_list_not_none(tmp_path):
    # Readable transcript, zero turn boundaries -> an OBSERVED negative ([]),
    # not a missing observable (None) -> the caller must deny, not fail open.
    t = write_transcript(tmp_path / "t.jsonl", [text_only_entry(100.0)])
    assert delivered_final_texts(t) == []


def test_unreadable_transcript_returns_none(tmp_path):
    assert delivered_final_texts(tmp_path / "absent.jsonl") is None


def test_unparsable_timestamp_still_returned_with_none_epoch(tmp_path):
    entry = text_only_entry(105.0)
    entry["timestamp"] = "not-a-timestamp"
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        entry,
        user_prompt_entry(110.0),
    ])
    result = delivered_final_texts(t)
    assert result == [(REAL_TEXT_ONLY, None)]


def test_missing_timestamp_key_still_returned_with_none_epoch(tmp_path):
    entry = text_only_entry(105.0)
    del entry["timestamp"]
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        entry,
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == [(REAL_TEXT_ONLY, None)]


def test_empty_content_message_never_qualifies(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        {"type": "assistant", "message": {"role": "assistant", "content": []}, "timestamp": iso(105.0)},
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == []


def test_segment_with_no_assistant_entry_contributes_nothing(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        user_prompt_entry(110.0),
    ])
    assert delivered_final_texts(t) == []


# --- latest_turn_start / iso_to_epoch (smoke coverage; primary coverage lives
# in test_plan_delivery_gate_hook.py's end-to-end scenarios) -----------------

def test_latest_turn_start_finds_most_recent_boundary(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(100.0),
        text_only_entry(102.0),
        queued_command_entry(110.0),
    ])
    assert latest_turn_start(t) == 110.0


def test_latest_turn_start_no_boundary_returns_none(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [text_only_entry(100.0)])
    assert latest_turn_start(t) is None


def test_iso_to_epoch_round_trips():
    assert iso_to_epoch(iso(12345.0)) == 12345.0


def test_iso_to_epoch_unparsable_returns_none():
    assert iso_to_epoch("not-a-timestamp") is None
