"""Tests for `lib.denial_arming.armed` -- the three-valued ARMED / NOT_ARMED /
UNREADABLE judgement over a session transcript. Every fixture lives under
`fixtures/denial_arming/` as a physical JSONL file rather than an inline
literal: this module reads a FILE, so its own tests exercise the read path
(missing file, empty file, unparseable rows) that an inline-dict fixture
could never represent.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.denial_arming import Verdict, armed  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "denial_arming"


# --- ARMED: resolved, unresolved, and non-arming denials in one transcript --

def test_mixed_kinds_arms_on_exactly_the_three_arming_denials_in_order():
    result = armed(FIXTURES / "mixed_kinds.jsonl")
    assert result.verdict is Verdict.ARMED
    assert len(result.denials) == 3

    first, second, third = result.denials
    assert first.kind == "permission-rule"
    assert first.tool_name == "Bash"
    assert first.tool_input == {"command": "rm -rf /tmp/x"}

    assert second.kind == "user-rejected"
    assert second.tool_name == "Write"
    assert second.tool_input == {"file_path": "/etc/passwd", "content": "x"}

    # sourceToolAssistantUUID names a row that isn't in the file -- still
    # arms, but the call it denied is unknown rather than guessed.
    assert third.kind == "automode-blocked"
    assert third.tool_name is None
    assert third.tool_input is None


def test_mixed_kinds_ignores_the_four_non_arming_kinds():
    result = armed(FIXTURES / "mixed_kinds.jsonl")
    kinds = {d.kind for d in result.denials}
    assert "automode-unavailable" not in kinds
    assert "automode-parsing-error" not in kinds
    assert "cancelled" not in kinds
    assert "interrupted" not in kinds


def test_mixed_kinds_tolerates_the_trailing_malformed_line():
    # mixed_kinds.jsonl ends with an unterminated JSON line; a single bad row
    # among otherwise-parseable rows must not flip the whole file UNREADABLE.
    result = armed(FIXTURES / "mixed_kinds.jsonl")
    assert result.verdict is not Verdict.UNREADABLE


# --- ARMED: every arming denial is returned, not just the first ------------

def test_three_independent_arming_denials_are_all_returned():
    # Mutation control against a first-hit shortcut: if armed() returned only
    # the first match, this would see 1 denial instead of 3.
    result = armed(FIXTURES / "three_arming.jsonl")
    assert result.verdict is Verdict.ARMED
    assert len(result.denials) == 3

    kinds = [d.kind for d in result.denials]
    assert kinds == ["permission-rule", "user-rejected", "automode-blocked"]

    names = [d.tool_name for d in result.denials]
    assert names == ["Bash", "Edit", "Write"]


# --- NOT_ARMED: only non-arming kinds present -------------------------------

def test_only_non_arming_kinds_does_not_arm():
    result = armed(FIXTURES / "only_non_arming.jsonl")
    assert result.verdict is Verdict.NOT_ARMED
    assert result.denials == ()


# --- the named mutation-control test: cancelled alone must never arm -------

def test_cancelled_denial_alone_does_not_arm():
    # If "cancelled" were ever added to _ARMING_KINDS, this fixture's verdict
    # would flip to ARMED and this assertion would go red.
    result = armed(FIXTURES / "cancelled_only.jsonl")
    assert result.verdict is Verdict.NOT_ARMED


# --- UNREADABLE: missing path, empty file, all-unparseable rows ------------

def test_nonexistent_path_is_unreadable():
    result = armed(FIXTURES / "does-not-exist.jsonl")
    assert result.verdict is Verdict.UNREADABLE


def test_empty_file_is_unreadable():
    result = armed(FIXTURES / "empty.jsonl")
    assert result.verdict is Verdict.UNREADABLE


def test_all_attempted_rows_unparseable_is_unreadable():
    result = armed(FIXTURES / "all_malformed.jsonl")
    assert result.verdict is Verdict.UNREADABLE


def test_corrupt_file_without_either_marker_is_unreadable_not_not_armed():
    # The hole the prefilter opened, measured: `corrupt_no_markers.jsonl` is
    # unreadable garbage, but none of its lines contains `"toolDenialKind"` or
    # `"tool_use"`, so the prefilter attempts NOTHING, nothing fails to parse,
    # and the verdict came back NOT_ARMED -- "I looked and found no denial"
    # about a file that could not be read. Judging readability from only the
    # prefiltered lines is what does it; `_looks_like_a_row` judges the whole
    # file. Delete that check and this row goes red while every other stays
    # green.
    result = armed(FIXTURES / "corrupt_no_markers.jsonl")
    assert result.verdict is Verdict.UNREADABLE


def test_a_truncated_tail_among_readable_rows_is_still_read():
    # The control on the other side of the same check: a real transcript whose
    # last line was cut mid-write IS readable -- its earlier rows have the row
    # shape and parse. A shape test that flipped this to UNREADABLE would fire
    # the third value on ordinary sessions and destroy the distinction.
    result = armed(FIXTURES / "truncated_tail.jsonl")
    assert result.verdict is Verdict.NOT_ARMED
