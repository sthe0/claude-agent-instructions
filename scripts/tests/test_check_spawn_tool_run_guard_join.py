"""Issue #268: `check_outcome`'s `--expect-guard-block` branch must join a
guard-log fire to the resolved Bash call by id, not by a bare substring search
over the log's raw text — a substring match credits ANY row whose free-text
happens to contain the command, even one logged for a DIFFERENT call that
shares a command substring (two Bash tool_uses commonly do, e.g. the same
script invoked with different args).

Join order (see check-spawn-tool-run.py's module docstring): by
(session_id, tool_use_id) when both the guard-log row and the transcript
supply a session_id; by tool_use_id alone when either is missing; the
historical substring fallback applies only when NO row in the guard log
carries a tool_use_id at all.

These tests build their own synthetic transcript JSONL in tmp_path (the
shared fixtures under fixtures/transcript_stops/ carry no `sessionId` field at
all, confirmed by grep — see test_transcript_stops.py), matching the exact
assistant/tool_use + user/tool_result shape `hook-block.jsonl` uses, plus a
top-level `sessionId` on each line (the harness's own real transcripts always
carry one; the fixture corpus predates any test needing it)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_spawn_tool_run", ROOT / "scripts" / "check-spawn-tool-run.py"
)
check_spawn_tool_run = importlib.util.module_from_spec(_SPEC)
sys.modules["check_spawn_tool_run"] = check_spawn_tool_run
_SPEC.loader.exec_module(check_spawn_tool_run)

transcript_stops = check_spawn_tool_run.transcript_stops


def _args(**overrides):
    ns = check_spawn_tool_run.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _hook_block_transcript(tmp_path: Path, *, tool_use_id: str, command: str, session_id: str) -> Path:
    path = tmp_path / f"transcript-{tool_use_id}.jsonl"
    assistant_line = {
        "type": "assistant",
        "sessionId": session_id,
        "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}},
        ]},
    }
    user_line = {
        "type": "user",
        "sessionId": session_id,
        "toolDenialKind": "permission-rule",
        "message": {"content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "PreToolUse:Bash hook error: blocked by guard",
                "is_error": True,
            },
        ]},
    }
    path.write_text(json.dumps(assistant_line) + "\n" + json.dumps(user_line) + "\n", encoding="utf-8")
    return path


def _guard_row(*, tool_use_id: str | None, session_id: str | None, target: str) -> str:
    row = {"decision": "deny", "branch": "G1-bash", "target": target}
    if tool_use_id is not None:
        row["tool_use_id"] = tool_use_id
    if session_id is not None:
        row["session_id"] = session_id
    return json.dumps(row)


def test_join_credits_only_id_matched_fire(tmp_path):
    """Two rows share the SAME command substring; only the row carrying the
    resolved call's own tool_use_id must credit the block. A bare substring
    match would credit either row indiscriminately -- this pins that it
    doesn't."""
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_target", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.jsonl"
    guard_log.write_text(
        "\n".join([
            # A DIFFERENT call, same command substring, wrong tool_use_id.
            _guard_row(tool_use_id="toolu_other", session_id="sess-A", target=command),
            # The actual fire for the resolved call.
            _guard_row(tool_use_id="toolu_target", session_id="sess-A", target=command),
        ]) + "\n",
        encoding="utf-8",
    )

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is None


def test_join_fails_when_only_a_different_tool_use_id_fired(tmp_path):
    """The negative twin: a guard log that records fires with tool_use_id, but
    never the resolved call's own id, must FAIL -- even though its command
    substring is present (logged under the wrong id)."""
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_target", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.jsonl"
    guard_log.write_text(
        _guard_row(tool_use_id="toolu_other", session_id="sess-A", target=command) + "\n",
        encoding="utf-8",
    )

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is not None
    assert "does not record a fire for tool_use_id" in err


def test_join_respects_session_id_when_both_sides_carry_one(tmp_path):
    """Same tool_use_id, but the guard-log row belongs to a DIFFERENT session
    -- the id alone must not be enough when both sides can disambiguate by
    session_id too."""
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_shared", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.jsonl"
    guard_log.write_text(
        _guard_row(tool_use_id="toolu_shared", session_id="sess-B", target=command) + "\n",
        encoding="utf-8",
    )

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is not None
    assert "does not record a fire for tool_use_id" in err


def test_join_by_tool_use_id_alone_when_session_id_missing_on_guard_row(tmp_path):
    """The guard-log row omits session_id entirely (an older log format) --
    the join must still succeed by tool_use_id alone rather than refusing for
    want of a session_id to compare."""
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_target", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.jsonl"
    guard_log.write_text(
        _guard_row(tool_use_id="toolu_target", session_id=None, target=command) + "\n",
        encoding="utf-8",
    )

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is None


def test_substring_fallback_still_applies_when_no_row_carries_tool_use_id(tmp_path):
    """A guard log whose rows are plain text (or JSON with no tool_use_id
    field anywhere) has nothing to join on -- the historical substring match
    against the raw text is the fallback, unchanged from before #268."""
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_target", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.log"
    guard_log.write_text(f"blocked: {command}\n", encoding="utf-8")

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is None


def test_substring_fallback_still_fails_when_command_absent(tmp_path):
    command = "cat foo.txt > out.json"
    transcript = _hook_block_transcript(
        tmp_path, tool_use_id="toolu_target", command=command, session_id="sess-A",
    )
    [target] = transcript_stops.parse_bash_tool_uses(transcript)

    guard_log = tmp_path / "guard.log"
    guard_log.write_text("some other unrelated line\n", encoding="utf-8")

    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log, transcript=transcript),
    )
    assert err is not None
    assert "does not record the blocked command" in err
