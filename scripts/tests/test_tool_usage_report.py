"""tool-usage-report.py's transcript reading, after it was folded onto the
shared `transcript_read` module.

The report was the third private copy of the JSONL reader and the `tool_use`
extraction; migrating it left no test behind it at all, so nothing pinned the
two behaviours the migration could plausibly have dropped: that a whole project
dir keeps scanning past ONE unreadable file (the report is handed a glob, unlike
the hooks, which the harness hands a single transcript), and that malformed
content shapes yield nothing rather than raising.
"""
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "tool_usage_report", SCRIPTS / "tool-usage-report.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _write(path: Path, entries) -> Path:
    path.write_text(
        "\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries),
        encoding="utf-8",
    )
    return path


def _assistant(blocks, timestamp="2026-07-20T10:00:00Z"):
    return {"timestamp": timestamp,
            "message": {"role": "assistant", "content": blocks}}


def _skill_use(skill, args="do the thing"):
    return {"type": "tool_use", "name": "Skill",
            "input": {"skill": skill, "args": args}}


def _agent_use(subagent, description="explore"):
    return {"type": "tool_use", "name": "Agent",
            "input": {"subagent_type": subagent, "description": description}}


# --- iter_transcript_lines ----------------------------------------------------

def test_entries_are_read_across_several_transcripts(tmp_path):
    a = _write(tmp_path / "a.jsonl", [{"n": 1}, {"n": 2}])
    b = _write(tmp_path / "b.jsonl", [{"n": 3}])
    assert [e["n"] for e in mod.iter_transcript_lines([a, b])] == [1, 2, 3]


def test_malformed_and_blank_lines_are_skipped(tmp_path):
    path = _write(tmp_path / "a.jsonl", [{"n": 1}, "", "{not json", {"n": 2}])
    assert [e["n"] for e in mod.iter_transcript_lines([path])] == [1, 2]


def test_an_unreadable_file_is_warned_about_and_the_scan_continues(tmp_path, capsys):
    """The report globs a whole project dir, so one vanished or unreadable
    transcript must not abort the other N — the reason this wrapper keeps its own
    try/except around the shared reader."""
    missing = tmp_path / "gone.jsonl"
    present = _write(tmp_path / "b.jsonl", [{"n": 7}])

    assert [e["n"] for e in mod.iter_transcript_lines([missing, present])] == [7]
    assert "cannot read" in capsys.readouterr().err


# --- collect_invocations ------------------------------------------------------

def test_skill_and_agent_tool_uses_are_bucketed_with_their_purposes(tmp_path):
    path = _write(tmp_path / "a.jsonl", [
        _assistant([_skill_use("developer", "implement stage 2")]),
        _assistant([_agent_use("Explore", "find the callers")]),
        _assistant([_skill_use("developer", "implement stage 3")]),
    ])
    buckets = mod.collect_invocations([path], None)

    assert buckets[("Skill", "developer")] == ["implement stage 2",
                                               "implement stage 3"]
    assert buckets[("Agent", "Explore")] == ["find the callers"]


def test_non_assistant_messages_and_non_tool_use_blocks_are_ignored(tmp_path):
    path = _write(tmp_path / "a.jsonl", [
        {"timestamp": "2026-07-20T10:00:00Z",
         "message": {"role": "user", "content": [_skill_use("developer")]}},
        _assistant([{"type": "text", "text": "prose, not a call"}]),
        {"timestamp": "2026-07-20T10:00:00Z", "message": "not-a-dict"},
    ])
    assert mod.collect_invocations([path], None) == {}


def test_a_string_content_message_yields_nothing_rather_than_raising(tmp_path):
    """Totality of the shared block reader: `content` is a bare string on some
    transcript paths, and the report must not crash a whole scan over one."""
    path = _write(tmp_path / "a.jsonl", [
        _assistant("a plain string content"),
        _assistant([_skill_use("planner", "draft the plan")]),
    ])
    assert mod.collect_invocations([path], None) == {("Skill", "planner"):
                                                     ["draft the plan"]}


def test_entries_before_the_window_start_are_dropped(tmp_path):
    import datetime as dt

    path = _write(tmp_path / "a.jsonl", [
        _assistant([_skill_use("planner", "old")], timestamp="2026-07-01T10:00:00Z"),
        _assistant([_skill_use("planner", "new")], timestamp="2026-07-20T10:00:00Z"),
    ])
    since = dt.datetime(2026, 7, 10, tzinfo=dt.timezone.utc)
    assert mod.collect_invocations([path], since) == {("Skill", "planner"): ["new"]}
