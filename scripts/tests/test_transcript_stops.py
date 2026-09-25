"""Pins `lib.transcript_stops.parse_bash_tool_uses`'s classification of a Bash
tool_use/tool_result pair into one of the four stop kinds, and the specific
regression the module exists to guard against: `toolDenialKind` alone cannot tell a
`hook-block` from a `permission-denial` apart (both real, committed reference
transcripts under
`/home/the0/.claude-agent/plans/evidence/spawn-permission-grant-model/
transcript-fixtures/` carry `toolDenialKind: "permission-rule"`), so the parser must
fall back to the stop text's `hook error:` marker. The fixtures here are SYNTHETIC
reconstructions of those three real transcripts (see that directory's README.md for
the verbatim source) — no real machine path, email or UUID, so the leak-scan the
plan's verify_command runs over `scripts/tests/fixtures/transcript_stops/` stays
clean."""
import pathlib

import pytest

from lib.transcript_stops import STOP_KINDS, parse_bash_tool_uses

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "transcript_stops"


def test_stop_kinds_is_exactly_the_named_four():
    assert set(STOP_KINDS) == {"ran", "permission-denial", "hook-block", "user-rejected"}


def test_a_successful_bash_call_classifies_as_ran():
    [use] = parse_bash_tool_uses(_FIXTURES / "ran.jsonl")
    assert use.tool_use_id == "toolu_fixture_ran_1"
    assert use.command == "echo hello"
    assert use.stop_kind == "ran"
    assert use.stop_text is None


def test_a_permission_rule_denial_classifies_as_permission_denial():
    [use] = parse_bash_tool_uses(_FIXTURES / "permission-denial.jsonl")
    assert use.stop_kind == "permission-denial"
    assert use.stop_text is not None
    assert "has been denied" in use.stop_text


def test_a_hook_refusal_classifies_as_hook_block_despite_sharing_toolDenialKind():
    """THE regression this module exists to guard against, made concrete: read the
    fixture's raw `toolDenialKind` field directly and confirm it is the SAME
    "permission-rule" value the permission-denial fixture carries — so a classifier
    keyed on `toolDenialKind` alone would misclassify this as a `permission-denial`,
    silently routing a hook's own refusal into the wrong bucket downstream (a hook
    block is never a materialization defect the way an under-granted permission rule
    denial can be)."""
    import json

    path = _FIXTURES / "hook-block.jsonl"
    raw_denial_kinds = [
        json.loads(line)["toolDenialKind"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and "toolDenialKind" in line
    ]
    assert raw_denial_kinds == ["permission-rule"]

    [use] = parse_bash_tool_uses(path)
    assert use.stop_kind == "hook-block"
    assert "hook error:" in use.stop_text


def test_a_user_rejection_classifies_as_user_rejected():
    [use] = parse_bash_tool_uses(_FIXTURES / "user-rejected.jsonl")
    assert use.stop_kind == "user-rejected"
    assert "rejected" in use.stop_text


def test_edge_cases_skip_non_bash_and_dangling_calls_and_join_block_list_content():
    """One transcript file exercising four parser edge cases at once: a blank line
    and a malformed JSON line are tolerated (not raised on); a non-Bash tool_use
    (Read) is dropped even though it has a matching tool_result; a dangling Bash
    tool_use with no matching tool_result at all is dropped; and a tool_result whose
    `content` is a list of text blocks (rather than a bare string) is joined into one
    string before classification, still finding the `has been denied` marker split
    across two blocks."""
    [use] = parse_bash_tool_uses(_FIXTURES / "edge-cases.jsonl")
    assert use.tool_use_id == "toolu_fixture_edge_blocklist"
    assert use.stop_kind == "permission-denial"
    assert use.stop_text == (
        "Permission to use Bash with command python3 scripts/verify-all.py has been denied."
    )


def test_a_missing_transcript_file_raises_like_any_other_missing_path():
    with pytest.raises(FileNotFoundError):
        parse_bash_tool_uses(_FIXTURES / "does-not-exist.jsonl")
