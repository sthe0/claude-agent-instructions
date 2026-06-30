"""Tests for the shared temporal-frontmatter validator (memory_dates.py)."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import memory_dates as md  # noqa: E402


def test_parse_iso_valid():
    assert md.parse_iso("2026-06-29") is not None


def test_parse_iso_rejects_garbage():
    assert md.parse_iso("2026/06/29") is None
    assert md.parse_iso("2026-13-01") is None
    assert md.parse_iso("") is None
    assert md.parse_iso("nope") is None


def test_top_level_value_ignores_nested():
    fm = "name: x\nmetadata:\n  created: 2020-01-01\ncreated: 2026-06-29\n"
    assert md.top_level_value(fm, "created") == "2026-06-29"


def test_require_flags_both_missing():
    issues = md.validate_temporal("name: x\ntype: reference\n", require=True)
    assert any("created" in i for i in issues)
    assert any("last_verified" in i for i in issues)


def test_require_false_silent_when_absent():
    assert md.validate_temporal("name: x\n", require=False) == []


def test_valid_dates_pass():
    fm = "created: 2026-06-01\nlast_verified: 2026-06-29\n"
    assert md.validate_temporal(fm, require=True) == []


def test_last_verified_before_created_rejected():
    fm = "created: 2026-06-29\nlast_verified: 2026-06-01\n"
    issues = md.validate_temporal(fm, require=True)
    assert any("before" in i for i in issues)


def test_last_accessed_present_rejected_as_retired():
    # any present last_accessed (even valid ISO) is now a retired-field error
    fm = "created: 2026-06-01\nlast_verified: 2026-06-01\nlast_accessed: 2026-06-29\n"
    issues = md.validate_temporal(fm, require=True)
    assert any("last_accessed" in i and "RETIRED" in i for i in issues)


def test_last_accessed_only_still_requires_the_two():
    fm = "last_accessed: 2026-06-29\n"
    issues = md.validate_temporal(fm, require=True)
    assert any("created" in i for i in issues)
    assert any("last_verified" in i for i in issues)
    # also flags last_accessed as retired
    assert any("last_accessed" in i and "RETIRED" in i for i in issues)


def test_malformed_created_rejected():
    fm = "created: 2026-6-1\nlast_verified: 2026-06-29\n"
    assert any("created" in i and "valid" in i for i in md.validate_temporal(fm, require=True))
