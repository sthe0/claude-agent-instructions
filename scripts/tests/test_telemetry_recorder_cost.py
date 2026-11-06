"""The cost of the live recorder and the self-filling trigger: the command-path replay stays inside
one deadline, a SessionEnd writes the end marker and starts a detached sync, and every sync (or
sweep) delivers spools, records its own work, and answers the latency rule."""
from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_land_branch as lands
from lib import config_root
from lib.telemetry import emit, live, recorder, store, sync
from telemetry_live_support import (
    REAL_MAYBE_SWEEP, agentctl, db, events, no_background_sweep, query, spool_events, start)

SCRIPTS = Path(__file__).resolve().parent.parent
REAL_CANON_STATE = sync._canon_state
BASE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


@pytest.fixture(autouse=True)
def _live_env(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms; a test's verdict
    must not depend on machine load, on the real canon checkout or on the real transcripts."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    monkeypatch.delenv(sync.HARNESS_VERSION_ENV, raising=False)
    monkeypatch.setattr(sync, "_canon_state", lambda: ("canon-head", 0))
    monkeypatch.setattr(config_root, "projects_roots", lambda: [])
    no_background_sweep(monkeypatch)


def freeze(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(sync, "_now", lambda: when)


def emit_at(monkeypatch, session: str, when: datetime, count: int = 1) -> None:
    monkeypatch.setattr(emit, "now_iso", lambda: iso(when))
    for _ in range(count):
        assert live.emit_event(session, "note", "telemetry:test", {}) is not None


def age(session: str, now: datetime, seconds: float) -> None:
    stamp = (now - timedelta(seconds=seconds)).timestamp()
    os.utime(emit.spool_path(session), (stamp, stamp))


def end_marker(session: str) -> None:
    live.append_line(live.end_marker_path(session), b"ended\n")


def commits(session: "str | None" = None) -> "list[dict]":
    rows = [e for e in events(kind=sync.SYNC_COMMIT)]
    return [e for e in rows if session is None or e["fields"]["session"] == session]


def triggers() -> "list[dict]":
    return events(kind=sync.TRIGGER_START)


def deferred_rows() -> "list[dict]":
    return events(kind=sync.SYNC_DEFERRED)


@contextmanager
def held(filename: str):
    """Hold an flock in the spool dir (a spool's `<stem>.lock`, the sweep lock), as a competing process would."""
    fd = os.open(live.spool_dir() / filename, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield fd
    finally:
        os.close(fd)


def marks(session: str):
    conn = store.open_record()
    try:
        return sync.latency_marks(conn, session)
    finally:
        conn.close()


def _warm() -> None:
    store.replay("warm-up")


def test_replay_on_command_path_within_one_deadline(monkeypatch):
    _warm()
    live.emit_event("s1", "note", "telemetry:test", {})
    monkeypatch.setattr(store, "DEADLINE_S", 0.3)

    with held("s1.lock"):
        began = time.monotonic()
        locked = recorder.replay_on_command_path("s1")
        elapsed = time.monotonic() - began
    assert elapsed < 0.3 + 2.0
    assert locked["deferred"] is True and locked["drained"] is False
    assert [e["fields"]["deferred"] for e in spool_events("s1") if e["kind"] == "recorder"] == [True]

    free = recorder.replay_on_command_path("s1")
    assert free["drained"] is True and free["inserted"] >= 1 and free["deferred"] is False


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "hook_telemetry_session_end", SCRIPTS / "hook-telemetry-session-end.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook(monkeypatch, hook, payload) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    return hook.main()


def test_session_end_hook_writes_end_marker(monkeypatch, capsys):
    hook = _load_hook()
    started = []
    monkeypatch.setattr(hook, "_start_sync", started.append)

    assert _run_hook(monkeypatch, hook, {"session_id": "s-end"}) == 0
    marker = live.end_marker_path("s-end")
    assert marker == config_root.telemetry_spool_dir() / "s-end.end" and marker.is_file()
    datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    assert started == ["s-end"]
    assert capsys.readouterr().out == ""

    for payload in ("not json", {"no": "session"}, {"session_id": "a/b"}):
        assert _run_hook(monkeypatch, hook, payload) == 0
    assert sorted(p.name for p in config_root.telemetry_spool_dir().glob("*.end")) == ["s-end.end"]
    monkeypatch.setenv(live.SWITCH_ENV, "off")
    assert _run_hook(monkeypatch, hook, {"session_id": "s-off"}) == 0
    assert not live.end_marker_path("s-off").exists() and started == ["s-end"]


def test_session_end_hook_starts_detached_sync_and_returns(monkeypatch):
    hook = _load_hook()
    launched = []

    class _NeverFinishes:
        def __init__(self, argv, **kwargs):
            launched.append((argv, kwargs))

    monkeypatch.setattr(hook.subprocess, "Popen", _NeverFinishes)
    assert _run_hook(monkeypatch, hook, {"session_id": "s-end"}) == 0
    (argv, kwargs), = launched
    assert argv[1:] == [str(SCRIPTS / "telemetry.py"), "sync", "--session", "s-end"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == kwargs["stderr"] == kwargs["stdin"] == subprocess.DEVNULL


def test_session_end_hook_process_returns_while_its_detached_sync_delivers():
    live.emit_event("s-real", "note", "telemetry:test", {})
    began = time.monotonic()
    done = subprocess.run([sys.executable, str(SCRIPTS / "hook-telemetry-session-end.py")],
                          input=json.dumps({"session_id": "s-real"}), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert time.monotonic() - began < 30
    assert live.end_marker_path("s-real").is_file()

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not _committed("s-real"):
        time.sleep(0.3)
    assert [t["fields"]["session"] for t in triggers()] == ["s-real"]
    assert len(commits("s-real")) == 1 and len(events("s-real", kind="note")) == 1


def _committed(session: str) -> bool:
    try:
        return bool(commits(session))
    except sqlite3.OperationalError:
        return False


def test_any_sync_delivers_idle_spool_without_end_marker_past_ttl(monkeypatch):
    now = at(50_000)
    freeze(monkeypatch, now)
    emit_at(monkeypatch, "idle", at(40_000), 3)
    age("idle", now, sync.LIVE_TTL_S + 60)
    assert not live.end_marker_path("idle").exists() and sync.is_eligible("idle", now)

    outcome = sync.run()

    assert outcome["visited"] == ["idle"] and outcome["committed"] == ["idle"]
    assert len(events("idle", kind="note")) == 3
    assert [c["fields"]["rows_inserted"] for c in commits("idle")] == [3]


def test_sync_records_trigger_start_and_sync_commit_events(monkeypatch):
    now = at(1_000)
    freeze(monkeypatch, now)
    emit_at(monkeypatch, "a", at(990), 2)
    emit_at(monkeypatch, "b", at(995), 3)

    sync.run()

    (trigger,) = triggers()
    assert trigger["fields"]["kind"] == "sweep" and "session" not in trigger["fields"]
    assert trigger["fields"]["time"] == trigger["time"] == iso(now)
    assert trigger["source_key"] == sync.SOURCE_KEY == "telemetry:sync" and trigger["session_id"] == sync.SYNC_SESSION
    rows = {c["fields"]["session"]: c for c in commits()}
    assert {s: c["fields"]["rows_inserted"] for s, c in rows.items()} == {"a": 2, "b": 3}
    assert all(c["source_key"] == "telemetry:sync" and c["fields"]["time"] == iso(now) for c in rows.values())


def _git_repo(path: Path) -> str:
    path.mkdir()
    lands.git("init", "--quiet", "-b", "main", cwd=path)
    return _commit(path, "first")


def _commit(path: Path, name: str) -> str:
    (path / name).write_text(name)
    lands.git("add", name, cwd=path)
    lands.git("commit", "--quiet", "-m", name, cwd=path)
    return lands.git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _session_row(session: str):
    return dict(query("SELECT * FROM sessions WHERE session_id = ?", session)[0])


def test_session_row_carries_core_head_dirty_flag_and_harness_version(tmp_path, monkeypatch):
    canon = tmp_path / "canon"
    first_head = _git_repo(canon)
    monkeypatch.setattr(sync, "CORE_ROOT", canon)
    monkeypatch.setattr(sync, "_canon_state", REAL_CANON_STATE)
    monkeypatch.setenv(sync.HARNESS_VERSION_ENV, "1.2.3")
    root = tmp_path / "state"
    start(root, "s1", "task-a")
    sync.run()
    row = _session_row("s1")
    assert (row["core_head"], row["dirty"], row["harness_version"]) == (first_head, 0, "1.2.3")

    second_head = _commit(canon, "second")
    (canon / "untracked.txt").write_text("dirty")
    monkeypatch.setenv(sync.HARNESS_VERSION_ENV, "9.9.9")
    agentctl(root, "status", "--session", "s1")
    start(root, "s2", "task-b")
    sync.run()

    assert second_head != first_head
    first = _session_row("s1")
    assert (first["core_head"], first["dirty"], first["harness_version"]) == (first_head, 0, "1.2.3")
    later = _session_row("s2")
    assert (later["core_head"], later["dirty"], later["harness_version"]) == (second_head, 1, "9.9.9")


def test_killed_session_spool_replayed_at_next_session_end(monkeypatch):
    now = at(10_000)
    freeze(monkeypatch, now)
    emit_at(monkeypatch, "killed", at(9_970), 2)
    age("killed", now, 30)
    emit_at(monkeypatch, "next", at(9_990))
    end_marker("next")
    assert not sync.is_eligible("killed", now)

    outcome = sync.run("next")

    assert sorted(outcome["visited"]) == ["killed", "next"]
    assert len(events("killed", kind="note")) == 2
    assert {c["fields"]["session"] for c in commits()} == {"killed", "next"}


def test_next_command_sweeps_stale_spools_of_ended_sessions(tmp_path, monkeypatch):
    root = tmp_path / "state"
    started = []
    for _ in range(2):
        live.emit_event("old", "note", "telemetry:test", {})
    end_marker("old")
    monkeypatch.setattr(recorder, "maybe_sweep", REAL_MAYBE_SWEEP)
    monkeypatch.setattr(recorder, "_spawn_sweep", lambda fd: started.append(fd))

    start(root, "s1", "task-a")
    assert len(started) == 1
    agentctl(root, "status", "--session", "s1")
    assert len(started) == 1

    stamp = live.spool_dir() / recorder.SWEEP_STAMP
    newer = emit.spool_path("newer")
    live.emit_event("newer", "note", "telemetry:test", {})
    later = stamp.stat().st_mtime + 5
    os.utime(newer, (later, later))
    with held(recorder.SWEEP_LOCK):  # a sweep still running
        agentctl(root, "status", "--session", "s1")
        assert len(started) == 1
    agentctl(root, "status", "--session", "s1")
    assert len(started) == 2

    sync.run()
    assert len(events("old", kind="note")) == 2 and len(events("newer", kind="note")) == 1


def test_live_ttl_used_only_without_end_marker(monkeypatch):
    now = at(100_000)
    emit_at(monkeypatch, "fresh", at(99_990))
    emit_at(monkeypatch, "stale", at(1))
    emit_at(monkeypatch, "marked", at(99_990))
    end_marker("marked")
    age("fresh", now, 10)
    age("stale", now, sync.LIVE_TTL_S + 1)
    age("marked", now, 10)

    assert sync.is_eligible("fresh", now) is False
    assert sync.is_eligible("stale", now) is True
    assert sync.is_eligible("marked", now) is True
    assert sync.is_eligible("absent", now) is False
    age("fresh", now, sync.LIVE_TTL_S - 1)
    assert sync.is_eligible("fresh", now) is False


def test_every_sync_replays_unreplayed_bytes_of_every_spool_whatever_its_age(monkeypatch):
    now = at(60_000)
    freeze(monkeypatch, now)
    for name, seconds in (("young", 1), ("middle", 600), ("ancient", sync.LIVE_TTL_S + 10)):
        emit_at(monkeypatch, name, now - timedelta(seconds=seconds + 5))
        age(name, now, seconds)

    sweep = sync.run()
    assert sorted(sweep["visited"]) == ["ancient", "middle", "young"]
    assert all(len(events(name, kind="note")) == 1 for name in ("young", "middle", "ancient"))

    emit_at(monkeypatch, "young-too", now)
    emit_at(monkeypatch, "ender", now)
    end_marker("ender")
    ended = sync.run("ender")
    assert sorted(ended["visited"]) == ["ender", "young-too"]
    assert len(events("young-too", kind="note")) == 1


def test_session_end_trigger_start_names_its_session(monkeypatch):
    freeze(monkeypatch, at(2_000))
    emit_at(monkeypatch, "s-end", at(1_990))
    end_marker("s-end")
    sync.run("s-end")
    freeze(monkeypatch, at(2_100))
    sync.run()

    ended, swept = triggers()
    assert ended["fields"]["kind"] == "session_end" and ended["fields"]["session"] == "s-end"
    assert swept["fields"]["kind"] == "sweep" and "session" not in swept["fields"]
    assert marks("s-end")[0] == at(2_000)


def test_eligible_spool_without_unreplayed_bytes_gets_one_sync_commit_with_zero_rows(monkeypatch):
    emit_at(monkeypatch, "done", at(0), 2)
    end_marker("done")
    emit_at(monkeypatch, "live2", at(1_990))
    assert store.replay("done")["drained"] and store.replay("live2")["drained"]
    now = at(2_000)
    freeze(monkeypatch, now)
    age("live2", now, 10)

    outcome = sync.run()

    assert outcome["visited"] == ["done"] and outcome["committed"] == ["done"]
    assert [c["fields"]["rows_inserted"] for c in commits("done")] == [0]
    assert commits("live2") == []
    assert marks("done")[2] is not None


def test_sync_events_written_through_insert_rows_never_to_a_visited_spool(monkeypatch):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "visited", at(490), 2)
    before = emit.spool_path("visited").read_bytes()
    real = store.insert_rows
    calls = []

    def spy(table, rows, *args, **kwargs):
        calls.append((table, [row["kind"] for row in rows], [row["fields"].get("source_key") for row in rows]))
        return real(table, rows, *args, **kwargs)

    monkeypatch.setattr(store, "insert_rows", spy)
    sync.run()

    assert ("events", [sync.TRIGGER_START], ["telemetry:sync"]) in calls
    assert ("events", [sync.SYNC_COMMIT], ["telemetry:sync"]) in calls
    assert emit.spool_path("visited").read_bytes() == before
    assert not emit.spool_path(sync.SYNC_SESSION).exists()
    assert events("visited", kind=sync.SYNC_COMMIT) == [] and events("visited", kind=sync.TRIGGER_START) == []
    assert len(commits("visited")) == 1


def test_second_sweep_over_a_drained_spool_adds_no_sync_commit(monkeypatch):
    emit_at(monkeypatch, "done", at(0), 2)
    end_marker("done")
    freeze(monkeypatch, at(2_000))
    first = sync.run()
    assert first["committed"] == ["done"] and len(commits("done")) == 1

    freeze(monkeypatch, at(2_100))
    second = sync.run()
    assert second["visited"] == [] and second["committed"] == [] and len(commits("done")) == 1

    monkeypatch.setattr(sync, "unreplayed_bytes", lambda conn, stem: 1)
    freeze(monkeypatch, at(2_200))
    forced = sync.run()
    assert forced["visited"] == ["done"] and forced["committed"] == [] and len(commits("done")) == 1


def test_deferred_replay_visit_writes_no_sync_commit(monkeypatch):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "locked", at(490), 2)
    monkeypatch.setattr(store, "DEADLINE_S", 0.2)
    monkeypatch.setattr(sync, "BUDGET_S", 0.0)

    with held("locked.lock"):
        outcome = sync.run()

    assert outcome["visited"] == ["locked"] and outcome["deferred"] == ["locked"] and outcome["committed"] == []
    assert commits("locked") == [] and events("locked", kind="note") == []


def test_session_end_sync_commits_own_spool_already_committed_before_E(monkeypatch):
    emit_at(monkeypatch, "s", at(0), 3)
    end_marker("s")
    freeze(monkeypatch, at(10))
    sync.run()
    assert [c["fields"]["rows_inserted"] for c in commits("s")] == [3]

    freeze(monkeypatch, at(100))
    sync.run("s")

    assert [c["fields"]["rows_inserted"] for c in commits("s")] == [3, 0]
    ended, first, appearance = marks("s")
    assert (ended, first, appearance) == (at(100), at(100), at(100))
    assert commits("s")[1]["time"] == iso(at(100))


def test_repeated_sweeps_over_a_live_drained_spool_add_no_sync_commit(monkeypatch):
    emit_at(monkeypatch, "live", at(0), 2)
    for second in (10, 20, 30):
        now = at(second)
        freeze(monkeypatch, now)
        age("live", now, 5)
        outcome = sync.run()
        assert (outcome["visited"], len(commits("live"))) == (["live"] if second == 10 else [], 1)

    monkeypatch.setattr(sync, "unreplayed_bytes", lambda conn, stem: 1)
    freeze(monkeypatch, at(40))
    forced = sync.run()
    assert forced["visited"] == ["live"] and forced["committed"] == [] and len(commits("live")) == 1


def test_deferred_sync_rows_kept_in_reserved_spool_and_replayed_by_next_sync(monkeypatch):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "v", at(490), 2)
    real = store.insert_rows

    def refuse_own(table, rows, *args, **kwargs):
        if table == "events" and rows and all(
                row.get("fields", {}).get("source_key") == sync.SOURCE_KEY for row in rows):
            return {"inserted": 0, "deferred": True, "failed": False}
        return real(table, rows, *args, **kwargs)

    monkeypatch.setattr(store, "insert_rows", refuse_own)
    sync.run()
    monkeypatch.setattr(store, "insert_rows", real)

    kept = spool_events(sync.SYNC_SESSION)
    assert [e["kind"] for e in kept] == [sync.TRIGGER_START, sync.SYNC_COMMIT]
    assert all(e["fields"]["time"] == e["time"] == iso(at(500)) for e in kept)
    assert triggers() == [] and commits() == []

    freeze(monkeypatch, at(900))
    outcome = sync.run()

    assert sync.SYNC_SESSION not in outcome["visited"]
    assert sorted((t["time"] for t in triggers())) == [iso(at(500)), iso(at(900))]
    assert [c["time"] for c in commits("v")] == [iso(at(500))]
    assert commits(sync.SYNC_SESSION) == []
    conn = store.open_record()
    try:
        assert sync.unreplayed_bytes(conn, sync.SYNC_SESSION) == 0
    finally:
        conn.close()


def test_lock_held_past_one_deadline_then_released_still_commits_within_s(monkeypatch):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "slow", at(490), 2)
    monkeypatch.setattr(store, "DEADLINE_S", 0.2)
    waits = []

    with held("slow.lock") as fd:
        def release_on_third_wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                fcntl.flock(fd, fcntl.LOCK_UN)

        monkeypatch.setattr(sync, "_sleep", release_on_third_wait)
        outcome = sync.run()

    assert waits == [1.0, 2.0, 4.0] and sum(waits) <= sync.LATENCY_S
    assert outcome["committed"] == ["slow"] and outcome["deferred"] == []
    assert [c["fields"]["rows_inserted"] for c in commits("slow")] == [2]
    assert deferred_rows() == []


def test_lock_held_past_budget_writes_one_sync_deferred_row(monkeypatch):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "stuck", at(490), 2)
    monkeypatch.setattr(store, "DEADLINE_S", 0.2)
    monkeypatch.setattr(sync, "BUDGET_S", 2.5)
    waits = []
    monkeypatch.setattr(sync, "_sleep", waits.append)
    size = emit.spool_path("stuck").stat().st_size

    with held("stuck.lock"):
        outcome = sync.run()

    assert waits == [1.0] and outcome["deferred"] == ["stuck"] and commits("stuck") == []
    (row,) = deferred_rows()
    assert row["fields"]["session"] == "stuck" and row["fields"]["unreplayed_bytes"] == size
    assert row["source_key"] == "telemetry:sync" and row["fields"]["time"] == iso(at(500))

    freeze(monkeypatch, at(3_000))
    sync.run()
    assert marks("stuck")[0] == at(490) + timedelta(seconds=sync.LIVE_TTL_S)
    assert len(deferred_rows()) == 1


@pytest.mark.parametrize("bad", [
    {"inserted": 3, "deferred": False, "torn": 0, "superseded": 0, "drained": False, "failed": False},
    {"inserted": 3, "deferred": False, "torn": 0, "superseded": 0, "drained": True, "failed": True},
    {"inserted": 3, "deferred": True, "torn": 0, "superseded": 0, "drained": True, "failed": False},
], ids=["not-drained", "failed", "deferred"])
def test_undrained_or_failed_replay_visit_writes_no_sync_commit(monkeypatch, bad):
    freeze(monkeypatch, at(500))
    emit_at(monkeypatch, "victim", at(490), 3)
    monkeypatch.setattr(sync, "BUDGET_S", 0.0)
    real = store.replay
    monkeypatch.setattr(store, "replay", lambda stem, *a, **k: bad if stem == "victim" else real(stem, *a, **k))

    outcome = sync.run()

    assert outcome["visited"] == ["victim"] and outcome["deferred"] == ["victim"] and outcome["committed"] == []
    assert commits("victim") == [] and len(deferred_rows()) == 1


def _row(session: str, kind: str, when: datetime, fields: dict) -> dict:
    return {"event_id": emit.uuid7(), "session_id": session, "kind": kind, "source_key": "telemetry:test",
            "provenance": "live", "time": iso(when), "fields": fields}


def _own(event_kind: str, when: datetime, **fields) -> dict:
    return sync._own_event(event_kind, fields, when)


def test_zero_row_commit_keyed_on_latency_E_after_end_marker_without_trigger_start(monkeypatch):
    emit_at(monkeypatch, "late-end", at(0), 2)
    end_marker("late-end")
    freeze(monkeypatch, at(10))
    early = sync.run()
    assert early["committed"] == ["late-end"] and marks("late-end")[2] is None

    freeze(monkeypatch, at(2_000))
    sweep = sync.run()

    assert sweep["committed"] == ["late-end"]
    assert [(c["fields"]["rows_inserted"], c["time"]) for c in commits("late-end")] == [
        (2, iso(at(10))), (0, iso(at(2_000)))]
    ended, first, appearance = marks("late-end")
    assert ended == at(1_800) and first == at(2_000) and appearance == at(2_000)


def test_latency_marks_returns_E_T1_A_by_the_latency_rule():
    rows = [
        _row("m", "note", at(0), {}), _row("m", "note", at(100), {}),
        _own("trigger.start", at(4_900), kind="sweep"),
        _own("sync.commit", at(4_980), session="m", rows_inserted=2),
        _own("trigger.start", at(5_000), kind="session_end", session="m"),
        _own("sync.commit", at(5_005), session="m", rows_inserted=0),
        _own("trigger.start", at(5_050), kind="sweep"),
        _own("sync.commit", at(5_060), session="m", rows_inserted=0),
        _row("n", "note", at(0), {}), _row("n", "note", at(100), {}),
        _own("trigger.start", at(1_800), kind="sweep"),
        _own("sync.commit", at(1_850), session="n", rows_inserted=2),
        _own("trigger.start", at(2_000), kind="sweep"),
        _own("sync.commit", at(2_010), session="n", rows_inserted=0),
        _own("sync.commit", at(2_100), session="n", rows_inserted=0),
        _own("sync.deferred", at(9_000), session="n", unreplayed_bytes=5),
    ]
    assert store.insert_rows("events", rows)["inserted"] == len(rows)

    assert marks("m") == (at(5_000), at(5_000), at(5_005))
    assert marks("n") == (at(1_900), at(2_000), at(2_010))
    assert marks("nobody") == (None, None, None)
    conn = store.open_record()
    try:
        verdict = sync.latency(conn, "m", now=at(5_100))
    finally:
        conn.close()
    assert verdict["latency_s"] == 5.0 and verdict["late"] is False
