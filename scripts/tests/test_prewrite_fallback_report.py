"""Tests for prewrite-fallback-report.py aggregate/load logic."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Load the hyphenated-filename module directly
_MOD_PATH = Path(__file__).resolve().parent.parent / "prewrite-fallback-report.py"
_spec = importlib.util.spec_from_file_location("prewrite_fallback_report", _MOD_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_ledger = _mod.load_ledger
aggregate = _mod.aggregate


def _write_ledger(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _ts(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestLoadLedger:
    def test_missing_file_returns_empty(self, tmp_path):
        result = load_ledger(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_malformed_lines_skipped(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        p.write_text('not json\n{"ts": "x", "session_id": "s1"}\n', encoding="utf-8")
        rows = load_ledger(p)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        p.write_text("", encoding="utf-8")
        assert load_ledger(p) == []


class TestAggregate:
    def test_empty_rows_zero_totals(self):
        agg = aggregate([], None)
        assert agg["total"] == 0
        assert agg["unique_sessions"] == 0
        assert agg["by_cwd"] == {}

    def test_correct_totals_and_unique_sessions(self, tmp_path):
        rows = [
            {"ts": _ts(0), "session_id": "s1", "cwd": "/proj/a"},
            {"ts": _ts(0), "session_id": "s1", "cwd": "/proj/a"},
            {"ts": _ts(0), "session_id": "s2", "cwd": "/proj/b"},
        ]
        agg = aggregate(rows, None)
        assert agg["total"] == 3
        assert agg["unique_sessions"] == 2
        assert agg["by_cwd"]["/proj/a"] == 2
        assert agg["by_cwd"]["/proj/b"] == 1

    def test_days_filter_excludes_old_rows(self, tmp_path):
        rows = [
            {"ts": _ts(0), "session_id": "s1", "cwd": "/proj/a"},
            {"ts": _ts(10), "session_id": "s2", "cwd": "/proj/b"},  # too old
        ]
        agg = aggregate(rows, days=5)
        assert agg["total"] == 1
        assert agg["unique_sessions"] == 1
        assert "/proj/b" not in agg["by_cwd"]

    def test_days_none_includes_all(self, tmp_path):
        rows = [
            {"ts": _ts(0), "session_id": "s1", "cwd": "/proj/a"},
            {"ts": _ts(100), "session_id": "s2", "cwd": "/proj/b"},
        ]
        agg = aggregate(rows, days=None)
        assert agg["total"] == 2

    def test_malformed_ts_skipped_in_window(self):
        rows = [
            {"ts": "not-a-date", "session_id": "s1", "cwd": "/proj"},
            {"ts": _ts(0), "session_id": "s2", "cwd": "/proj"},
        ]
        agg = aggregate(rows, days=5)
        # malformed ts row is excluded when filtering by days
        assert agg["total"] == 1
        assert agg["unique_sessions"] == 1


class TestLoadAndAggregate:
    def test_full_roundtrip(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        _write_ledger(p, [
            {"ts": _ts(0), "session_id": "s1", "edit_count": 3, "cwd": "/proj/x"},
            {"ts": _ts(0), "session_id": "s2", "edit_count": 5, "cwd": "/proj/y"},
            {"ts": _ts(30), "session_id": "s3", "edit_count": 4, "cwd": "/proj/x"},
        ])
        rows = load_ledger(p)
        agg = aggregate(rows, days=None)
        assert agg["total"] == 3
        assert agg["unique_sessions"] == 3
        assert agg["by_cwd"]["/proj/x"] == 2

        agg7 = aggregate(rows, days=7)
        assert agg7["total"] == 2
        assert "/proj/x" in agg7["by_cwd"]
