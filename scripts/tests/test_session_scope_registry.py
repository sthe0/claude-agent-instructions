"""Tests for session_scope.registry — pure/testable session-scope store.

Mirrors scripts/tests/test_detect_backend.py's style: injected inputs (here,
scopes_dir via tmp_path and an explicit now_ts) instead of ambient state.
"""
from __future__ import annotations

import json

from session_scope import registry
from session_scope.registry import ScopeRecord


# ── round-trip: record_touch / set_context / heartbeat ─────────────────────

def test_record_touch_creates_and_accumulates(tmp_path):
    registry.record_touch("s1", "/repo/a.py", scopes_dir=tmp_path)
    registry.record_touch("s1", "/repo/b.py", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.touched_paths == ["/repo/a.py", "/repo/b.py"]


def test_record_touch_dedupes(tmp_path):
    registry.record_touch("s1", "/repo/a.py", scopes_dir=tmp_path)
    registry.record_touch("s1", "/repo/a.py", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.touched_paths == ["/repo/a.py"]


def test_record_touch_normalizes_path(tmp_path):
    registry.record_touch("s1", "/repo/./sub/../a.py", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.touched_paths == ["/repo/a.py"]


def test_record_touch_caps_fifo(tmp_path):
    for i in range(registry.MAX_TOUCHED_PATHS + 10):
        registry.record_touch("s1", f"/repo/f{i}.py", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert len(rec.touched_paths) == registry.MAX_TOUCHED_PATHS
    # oldest entries dropped first (FIFO), newest retained
    assert rec.touched_paths[-1] == f"/repo/f{registry.MAX_TOUCHED_PATHS + 9}.py"
    assert "/repo/f0.py" not in rec.touched_paths


def test_record_touch_does_not_touch_heartbeat(tmp_path):
    registry.heartbeat("s1", 100.0, scopes_dir=tmp_path)
    registry.record_touch("s1", "/repo/a.py", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.heartbeat_ts == 100.0


def test_set_context_round_trip(tmp_path):
    registry.set_context("s1", "/home/x/proj", "/home/x/proj", "git", scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.cwd == "/home/x/proj"
    assert rec.repo_root == "/home/x/proj"
    assert rec.vcs == "git"


def test_heartbeat_round_trip(tmp_path):
    registry.heartbeat("s1", 42.5, scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.heartbeat_ts == 42.5


def test_heartbeat_updates_existing_record_without_losing_paths(tmp_path):
    registry.record_touch("s1", "/repo/a.py", scopes_dir=tmp_path)
    registry.heartbeat("s1", 10.0, scopes_dir=tmp_path)
    registry.heartbeat("s1", 20.0, scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.heartbeat_ts == 20.0
    assert rec.touched_paths == ["/repo/a.py"]


def test_writes_are_atomic_no_leftover_tmp_file(tmp_path):
    registry.heartbeat("s1", 1.0, scopes_dir=tmp_path)
    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["s1.json"]


# ── session-id sanitization ─────────────────────────────────────────────────

def test_session_id_is_sanitized_in_filename(tmp_path):
    registry.heartbeat("weird/id with spaces!", 1.0, scopes_dir=tmp_path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert all(c.isalnum() or c in "-_." for c in files[0].name)


# ── load / load_all: missing and corrupt files ignored ──────────────────────

def test_load_missing_session_returns_none(tmp_path):
    assert registry.load(tmp_path, "nope") is None


def test_load_all_empty_dir_returns_empty_list(tmp_path):
    assert registry.load_all(tmp_path) == []


def test_load_all_nonexistent_dir_returns_empty_list(tmp_path):
    assert registry.load_all(tmp_path / "does-not-exist") == []


def test_load_all_skips_corrupt_file(tmp_path):
    registry.heartbeat("good", 1.0, scopes_dir=tmp_path)
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    records = registry.load_all(tmp_path)
    assert [r.session_id for r in records] == ["good"]


def test_load_all_skips_file_missing_required_field(tmp_path):
    registry.heartbeat("good", 1.0, scopes_dir=tmp_path)
    (tmp_path / "bad2.json").write_text(json.dumps({"heartbeat_ts": 1.0}), encoding="utf-8")
    records = registry.load_all(tmp_path)
    assert [r.session_id for r in records] == ["good"]


def test_load_returns_none_for_corrupt_file(tmp_path):
    path = registry.scope_path(tmp_path, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")
    assert registry.load(tmp_path, "s1") is None


# ── live_sessions: ttl filtering ─────────────────────────────────────────────

def _rec(session_id, heartbeat_ts):
    return ScopeRecord(session_id=session_id, heartbeat_ts=heartbeat_ts)


def test_live_sessions_filters_by_ttl():
    records = [_rec("fresh", 95.0), _rec("stale", 10.0)]
    live = registry.live_sessions(records, now_ts=100.0, ttl_s=30.0)
    assert [r.session_id for r in live] == ["fresh"]


def test_live_sessions_boundary_is_inclusive():
    records = [_rec("exact", 70.0)]
    live = registry.live_sessions(records, now_ts=100.0, ttl_s=30.0)
    assert [r.session_id for r in live] == ["exact"]


def test_live_sessions_empty_input():
    assert registry.live_sessions([], now_ts=100.0, ttl_s=30.0) == []


def test_live_sessions_extra_live_check_gates_further():
    records = [_rec("a", 100.0), _rec("b", 100.0)]
    live = registry.live_sessions(
        records, now_ts=100.0, ttl_s=30.0, extra_live_check=lambda sid: sid == "a"
    )
    assert [r.session_id for r in live] == ["a"]


# ── prune_stale ───────────────────────────────────────────────────────────

def test_prune_stale_removes_only_expired_files(tmp_path):
    registry.heartbeat("fresh", 95.0, scopes_dir=tmp_path)
    registry.heartbeat("stale", 10.0, scopes_dir=tmp_path)
    removed = registry.prune_stale(tmp_path, now_ts=100.0, ttl_s=30.0)
    assert removed == ["stale"]
    remaining = {r.session_id for r in registry.load_all(tmp_path)}
    assert remaining == {"fresh"}


def test_prune_stale_no_expired_records_returns_empty(tmp_path):
    registry.heartbeat("fresh", 95.0, scopes_dir=tmp_path)
    assert registry.prune_stale(tmp_path, now_ts=100.0, ttl_s=30.0) == []


def test_prune_stale_empty_dir(tmp_path):
    assert registry.prune_stale(tmp_path, now_ts=100.0, ttl_s=30.0) == []
