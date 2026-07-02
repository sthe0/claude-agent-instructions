"""Tests for session_scope.registry — pure/testable session-scope store.

Mirrors scripts/tests/test_detect_backend.py's style: injected inputs (here,
scopes_dir via tmp_path and an explicit now_ts) instead of ambient state.
"""
from __future__ import annotations

import json
import os

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


# ── pid: schema back-compat, heartbeat, pid_alive, live_pid_check ──────────

def test_from_dict_defaults_pid_to_none_for_legacy_record():
    rec = ScopeRecord.from_dict({"session_id": "s1", "heartbeat_ts": 1.0})
    assert rec.pid is None


def test_to_dict_round_trips_pid():
    rec = ScopeRecord(session_id="s1", pid=4242)
    assert ScopeRecord.from_dict(rec.to_dict()).pid == 4242


def test_heartbeat_without_pid_leaves_existing_pid_untouched(tmp_path):
    registry.heartbeat("s1", 10.0, scopes_dir=tmp_path, pid=555)
    registry.heartbeat("s1", 20.0, scopes_dir=tmp_path)
    rec = registry.load(tmp_path, "s1")
    assert rec.heartbeat_ts == 20.0
    assert rec.pid == 555


def test_heartbeat_with_pid_records_it(tmp_path):
    registry.heartbeat("s1", 10.0, scopes_dir=tmp_path, pid=777)
    rec = registry.load(tmp_path, "s1")
    assert rec.pid == 777


def test_pid_alive_true_for_own_pid():
    assert registry.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead_pid(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(registry.os, "kill", fake_kill)
    assert registry.pid_alive(999999) is False


def test_pid_alive_true_on_permission_error(monkeypatch):
    def fake_kill(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(registry.os, "kill", fake_kill)
    assert registry.pid_alive(1) is True


def test_live_pid_check_excludes_dead_pid(monkeypatch):
    records = [_rec("a", 100.0), _rec("b", 100.0)]
    records[0].pid = 111
    records[1].pid = 222

    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 222)
    check = registry.live_pid_check(records)
    assert check("a") is False
    assert check("b") is True


def test_live_pid_check_treats_no_pid_as_alive(monkeypatch):
    records = [_rec("a", 100.0)]  # no pid set -> None

    monkeypatch.setattr(registry, "pid_alive", lambda pid: False)
    check = registry.live_pid_check(records)
    assert check("a") is True


def test_live_pid_check_unknown_session_is_alive():
    check = registry.live_pid_check([])
    assert check("nope") is True


def test_live_sessions_with_dead_pid_check_excludes_record(monkeypatch):
    records = [_rec("a", 100.0)]
    records[0].pid = 111
    monkeypatch.setattr(registry, "pid_alive", lambda pid: False)

    live = registry.live_sessions(
        records, now_ts=100.0, ttl_s=30.0, extra_live_check=registry.live_pid_check(records)
    )
    assert live == []


def test_live_sessions_legacy_no_pid_record_keeps_heartbeat_only_behavior():
    records = [_rec("a", 95.0), _rec("stale", 10.0)]  # neither has a pid
    live = registry.live_sessions(
        records, now_ts=100.0, ttl_s=30.0, extra_live_check=registry.live_pid_check(records)
    )
    assert [r.session_id for r in live] == ["a"]


# ── delete ────────────────────────────────────────────────────────────────

def test_delete_removes_existing_file(tmp_path):
    registry.heartbeat("s1", 1.0, scopes_dir=tmp_path)
    assert registry.scope_path(tmp_path, "s1").exists()
    registry.delete(tmp_path, "s1")
    assert not registry.scope_path(tmp_path, "s1").exists()


def test_delete_is_noop_on_missing_file(tmp_path):
    assert not registry.scope_path(tmp_path, "nope").exists()
    registry.delete(tmp_path, "nope")  # must not raise


def test_delete_does_not_disturb_other_sessions(tmp_path):
    registry.heartbeat("a", 1.0, scopes_dir=tmp_path)
    registry.heartbeat("b", 1.0, scopes_dir=tmp_path)
    registry.delete(tmp_path, "a")
    remaining = {r.session_id for r in registry.load_all(tmp_path)}
    assert remaining == {"b"}


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
