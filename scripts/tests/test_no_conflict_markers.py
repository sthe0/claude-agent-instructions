"""Tests for verify-no-conflict-markers.py.

Verifies that:
  - Clean content passes.
  - ours / theirs / base markers at column 0 are flagged.
  - A separator line (seven '=') is flagged ONLY when an ours marker is also
    present (so a 7-char markdown rule / setext underline alone is fine).
  - The PreToolUse --hook mode blocks (exit 2) a Write carrying a marker and
    allows a clean Write.

Marker literals are built via char multiplication so this test file itself
carries no column-0 conflict marker (which the verifier would otherwise flag
when it scans the whole tracked tree).
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_no_conflict_markers",
    Path(__file__).resolve().parents[1] / "verify-no-conflict-markers.py",
)
ncm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ncm)

OURS = "<" * 7
THEIRS = ">" * 7
BASE = "|" * 7
SEP = "=" * 7


def test_clean_content_passes():
    assert ncm.check_content("# Title\n\nordinary text\n") is None


def test_ours_marker_flagged():
    content = "a\n" + OURS + " HEAD\nb\n"
    err = ncm.check_content(content)
    assert err is not None and "ours marker" in err


def test_theirs_marker_flagged():
    content = "a\n" + THEIRS + " feature-branch\n"
    err = ncm.check_content(content)
    assert err is not None and "theirs marker" in err


def test_base_marker_flagged():
    content = "a\n" + BASE + " merged common ancestor\n"
    err = ncm.check_content(content)
    assert err is not None and "base marker" in err


def test_full_conflict_block_flagged():
    content = (
        "x\n" + OURS + " HEAD\nmine\n" + SEP + "\ntheirs\n" + THEIRS + " other\n"
    )
    err = ncm.check_content(content)
    assert err is not None
    assert "ours marker" in err and "conflict separator" in err and "theirs marker" in err


def test_lone_separator_not_flagged():
    # A bare 7-char '=' line is a valid markdown setext underline / rule when
    # no ours marker is present — must NOT be flagged.
    content = "Heading\n" + SEP + "\n\nbody\n"
    assert ncm.check_content(content) is None


def test_marker_not_at_column_zero_ignored():
    # Indented marker-looking text (e.g. inside a code sample) is not a marker.
    content = "    " + OURS + " HEAD\n"
    assert ncm.check_content(content) is None


def test_hook_blocks_write_with_marker(monkeypatch):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x.md", "content": "a\n" + OURS + " HEAD\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert ncm.cmd_hook() == 2


def test_hook_allows_clean_write(monkeypatch):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x.md", "content": "clean text\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert ncm.cmd_hook() == 0


def test_hook_ignores_non_write(monkeypatch):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "x.md"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert ncm.cmd_hook() == 0


def test_cmd_file_roundtrip(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("fine\n", encoding="utf-8")
    assert ncm.cmd_file(str(good)) == 0
    bad = tmp_path / "bad.txt"
    bad.write_text("a\n" + THEIRS + " z\n", encoding="utf-8")
    assert ncm.cmd_file(str(bad)) == 1
