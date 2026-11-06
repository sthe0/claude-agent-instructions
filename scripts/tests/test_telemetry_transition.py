"""The importer's hand-over to the live record: task epochs and contours of imported history, head
derivation, rebuild, prune, the phase definition shared by events and transcripts, the time-profile
reading the record, and `check-profile-unchanged` against stage 1's frozen inputs."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from lib import config_root, time_profile
from lib.telemetry import emit, importer, phases, store, sync
from telemetry_import_support import (
    BASE, DAY, Frozen, at, bash_call, bash_result, line, prompt, reply, session_lines, world)  # noqa: F401
from telemetry_live_support import query

NOW = BASE + timedelta(days=200)


def start_entry(seconds, event="start", **extra) -> dict:
    return {"ts": at(seconds), "event": event, **extra}


# --- epochs and contours of imported history ----------------------------------------------------

def _tasks() -> list:
    return [tuple(r) for r in query("SELECT task_id, status, weight_class FROM tasks ORDER BY opened_at")]


def test_imported_reset_continues_open_epoch_until_resolved(world):
    world.state("s1", [start_entry(0), start_entry(10, "classify", weight_class="small")], task="T1")
    world.state("s2", [start_entry(100, "reset")], task="T1")
    world.run()
    assert _tasks() == [("T1", "open", "small")]
    epochs = {r[0]: r[1] for r in query("SELECT session_id, task_epoch FROM sessions WHERE session_id IN ('s1','s2')")}
    assert epochs["s1"] == epochs["s2"] and epochs["s1"]


def test_imported_reset_after_resolved_opens_new_epoch(world):
    world.state("s1", [start_entry(0), start_entry(10, "classify", weight_class="small"),
                       start_entry(20, "resolve", node="RESOLVED")], task="T1")
    world.state("s2", [start_entry(100, "reset")], task="T1")
    world.run()
    assert _tasks() == [("T1", "resolved", "small"), ("T1", "open", None)]
    epochs = {r[0]: r[1] for r in query("SELECT session_id, task_epoch FROM sessions WHERE session_id IN ('s1','s2')")}
    assert epochs["s1"] != epochs["s2"]


def test_imported_contours_carry_role_and_tasks_weight_class(world):
    world.state("R", [start_entry(0), start_entry(10, "classify", weight_class="small")], task="T")
    world.state("C", [start_entry(20)], task="T", parent="R", depth=1)
    world.add(world.spawn, {"ts": at(30), "event": "spawn", "child_session_id": "K", "spawner_session": "R",
                            "kind": "developer", "depth": 1, "cost_usd": 0.5})
    world.add(world.judge, {"ts": at(40), "kind": "call", "source": "R", "judge": "jd", "duration": 2.0})
    world.run()
    rows = query("SELECT role, weight_class, task_id FROM contours ORDER BY role")
    assert [tuple(r) for r in rows] == [("judge", "small", "T"), ("linked-child", "small", "T"),
                                         ("root", "small", "T"), ("spawned-child", "small", "T")]


def test_imported_session_head_from_quality_stamp_else_derived_from_commit_log(world):
    world.commits = [(int((BASE + timedelta(seconds=120)).timestamp()), "bbb")]
    world.state("A", [start_entry(50)])
    world.state("B", [start_entry(150)])
    world.add(world.quality, {"session": "A", "instructions_head": "aaa"})
    world.run()
    heads = {r[0]: (r[1], r[2]) for r in query(
        "SELECT s.session_id, s.core_head, m.derived FROM sessions s JOIN session_head_marks m USING (session_id)")}
    assert heads == {"A": ("aaa", 0), "B": ("bbb", 1)}
    world.add(world.quality, {"session": "B", "instructions_head": "ccc"})
    world.run()
    assert world.one("SELECT core_head FROM sessions WHERE session_id = 'B'") == "ccc"
    assert world.one("SELECT derived FROM session_head_marks WHERE session_id = 'B'") == 0


# --- landing -----------------------------------------------------------------------------------

def test_landing_interval_per_pr_from_first_attempt_to_landed():
    def event(source_key, seconds, branch="b1"):
        return {"source_key": source_key, "time": at(seconds), "task": "T", "branch": branch}
    events = [event("land:start", 0), event("land:start", 5), event("land:landed", 10),
              event("land:start", 20), event("land:landed", 30), event("land:start", 2, "b2")]
    spans = sorted((i["branch"], i["start"], i["end"]) for i in phases.landing_intervals(events))
    ms = lambda s: phases._ms(at(s))
    assert spans == [("b1", ms(0), ms(10)), ("b1", ms(20), ms(30))]
    calls = phases.call_landing_intervals([(at(0), 1), (at(5), 0), (at(20), 0)])
    assert [(c["start"], c["end"]) for c in calls] == [(ms(0), ms(5)), (ms(20), ms(20))]


# --- phases ------------------------------------------------------------------------------------

def test_phase_boundaries_shared_by_events_and_transcripts():
    nodes = [("classify", "ROUTED"), ("plan", "PLANNING"), ("submit-plan", "PLAN_READY"), ("approve", "APPROVED")]
    history = [{"ts": at(i * 10), "event": verb, "node": node} for i, (verb, node) in enumerate(nodes)]
    events = [(at(i * 10), {"node_after": node}) for i, (_, node) in enumerate(nodes)]
    bash = [(at(i * 10), {"program": "agentctl-cli.py", "verb": verb}) for i, (verb, _) in enumerate(nodes)]
    expected = phases.phase_intervals(phases.history_points(history))
    assert [p for _, _, p in expected] == ["order", "planning", "plan_review", "execution"]
    assert phases.phase_intervals(phases.event_points(events)) == expected
    assert phases.phase_intervals(phases.bash_points(bash)) == expected


def test_phase_at_uses_events_when_live_else_transcript_reconstruction(world):
    world.live_event("L", "command", "cli:plan", {"node_before": "ROUTED", "node_after": "PLANNING"})
    world.replay("L")
    world.state("H", [start_entry(0, node="CLASSIFIED"), start_entry(10, "plan", node="PLANNING"),
                      start_entry(20, "submit-plan", node="PLAN_READY")])
    world.transcript("B", [prompt(0), bash_call(1, "c1", "python3 scripts/agentctl-cli.py plan"), bash_result(2, "c1"),
                           bash_call(30, "c2", "python3 scripts/agentctl-cli.py approve"), bash_result(31, "c2")])
    world.run(include_transcripts=True)
    conn = store.open_readonly()
    try:
        assert phases.phase_at("L", "2100-01-01T00:00:00Z", conn=conn) == "planning"
        assert phases.phase_at("H", at(15), conn=conn) == "planning"
        assert phases.phase_at("H", at(25), conn=conn) == "plan_review"
        assert phases.phase_at("H", at(-5), conn=conn) == "other"
        assert phases.phase_at("B", at(10), conn=conn) == "planning"
        assert phases.phase_at("B", at(40), conn=conn) == "execution"
    finally:
        conn.close()


def test_phase_at_ignores_imported_command_rows_of_a_session_that_later_has_live_events(world):
    world.state("M", [start_entry(0, node="CLASSIFIED"), start_entry(10, "plan", node="PLANNING"),
                      start_entry(20, "submit-plan", node="PLAN_READY")])
    world.run()
    conn = store.open_readonly()
    try:
        assert phases.phase_at("M", at(15), conn=conn) == "planning"
    finally:
        conn.close()
    world.live_event("M", "command", "cli:approve", {"node_before": "PLAN_READY", "node_after": "EXECUTING"})
    world.replay("M")
    conn = store.open_readonly()
    try:
        assert phases.phase_at("M", at(15), conn=conn) == "other"
    finally:
        conn.close()


# --- time profile reading the record ------------------------------------------------------------

def _profile(world, **kw):
    return time_profile.profile_all(
        world.tmp / "cache", world.projects, agentctl_state_dir=world.state_dir, spawn_ledger=world.spawn,
        judge_ledger_path=world.judge, policy_ledger=world.tmp / "policy.jsonl", commit_index=[], **kw)


def _profiled_world(world):
    history = [start_entry(0, node="CLASSIFIED"), start_entry(60, "plan", node="PLANNING"),
               start_entry(120, "submit-plan", node="PLAN_READY")]
    world.state("sess", history)
    world.transcript("sess", [prompt(0), reply(30), prompt(60), reply(90), prompt(120), reply(150)])
    return history


def test_time_profile_reports_record_as_source(world):
    _profiled_world(world)
    world.run()
    world.state_dir.joinpath("sess.json").unlink()
    run = _profile(world, source="auto")
    assert run.source == "record"
    assert run.profiles and run.profiles[0].session_id == "sess"


def test_time_profile_falls_back_to_transcripts_without_record(world):
    _profiled_world(world)
    assert not config_root.telemetry_db().exists()
    assert _profile(world, source="auto").source == "transcripts"
    world.run(include_transcripts=False)
    world.state_dir.joinpath("sess.json").unlink()
    assert _profile(world, source="auto").source == "record"
    world.state("sess", [start_entry(0)])
    store_db = config_root.telemetry_db()
    store_db.rename(store_db.with_name("moved.sqlite"))
    assert _profile(world, source="auto").source == "transcripts"


def test_forced_transcript_flag_ignores_record(world):
    _profiled_world(world)
    world.run()
    assert _profile(world, source="auto").source == "record"
    assert _profile(world, source="transcripts").source == "transcripts"


# --- rebuild -----------------------------------------------------------------------------------

def _live_only_row(session: str) -> dict:
    return {"event_id": emit.uuid7(), "session_id": session, "kind": "note", "source_key": "test:note",
            "provenance": "live", "time": emit.now_iso(), "fields": {"source_key": "test:note"}}


def _shape() -> dict:
    return {
        "tasks": query("SELECT COUNT(*) FROM tasks")[0][0],
        "sessions": query("SELECT COUNT(*) FROM sessions")[0][0],
        "contours": query("SELECT COUNT(*) FROM contours")[0][0],
        "imported": query("SELECT COUNT(*) FROM events WHERE provenance = 'imported'")[0][0],
        "spooled": query("SELECT COUNT(*) FROM events WHERE provenance = 'live' AND session_id = 'LIVE'")[0][0]}


def _populate(world):
    world.state("R", [start_entry(0), start_entry(10, "classify", weight_class="small")], task="T")
    world.add(world.gate, world.gate_line("R"), world.gate_line("R", "exec", False, 20))
    world.add(world.decision, {"ts": at(5), "mechanism": "m", "session": "R", "event": "e", "decision": "allow"})
    world.live_event("LIVE", "command", "cli:status", {"node_before": "ROUTED", "node_after": "ROUTED"})
    world.live_event("LIVE", "command", "cli:status", {"node_before": "ROUTED", "node_after": "ROUTED"})
    world.run()
    world.replay("LIVE")


def test_rebuild_lists_sessions_whose_live_only_events_were_lost(world):
    _populate(world)
    store.insert_rows("events", [_live_only_row("LOST")])
    report = importer.rebuild()
    assert report["rebuilt_sessions"] == ["LOST"]
    assert report["live_only_lost"] == {"LOST": 1}
    assert world.one("SELECT count FROM losses WHERE session_id = 'LOST' AND loss_mode = 'record_corrupt_rebuilt'") == 1
    assert world.count("events", "session_id = 'LOST'") == 0


def test_corrupt_record_rebuilt_with_same_structured_counts_and_live_only_loss_reported(world):
    _populate(world)
    before = _shape()
    assert before["spooled"] == 2 and before["imported"] > 0
    store.insert_rows("events", [_live_only_row("LOST")])
    report = importer.rebuild()
    assert report["complete"] and report["live_only_lost"] == {"LOST": 1}
    assert _shape() == before
    record = config_root.telemetry_db()
    record.write_bytes(b"this is not a database" * 100)
    for suffix in ("-wal", "-shm"):
        record.with_name(record.name + suffix).unlink(missing_ok=True)
    report = importer.rebuild()
    assert report["complete"] and report["live_only_lost"] is None
    assert _shape() == before
    assert list(record.parent.glob(record.name + ".corrupt-*"))


def test_rebuild_keeps_origin(world):
    _populate(world)
    origin = store.origin_id()
    imported = world.one("SELECT event_id FROM events WHERE provenance = 'imported' ORDER BY event_id LIMIT 1")
    importer.rebuild()
    assert store.origin_id() == origin
    assert world.one("SELECT event_id FROM events WHERE provenance = 'imported' ORDER BY event_id LIMIT 1") == imported


# --- prune -------------------------------------------------------------------------------------

def _sync_row(event_kind: str, when, **fields) -> dict:
    stamp = when.isoformat(timespec="microseconds")
    return {"event_id": emit.uuid7(), "session_id": sync.SYNC_SESSION, "kind": event_kind, "source_key": sync.SOURCE_KEY,
            "provenance": "live", "time": stamp, "fields": {**fields, "source_key": sync.SOURCE_KEY, "time": stamp}}


def _ids(rows: dict) -> set:
    return {row["event_id"] for row in rows.values()}


def _remaining_sync() -> set:
    return {r[0] for r in query("SELECT event_id FROM events WHERE session_id = ?", sync.SYNC_SESSION)}


def _latency_rows():
    old = BASE + timedelta(days=10)
    sec = lambda n: old + timedelta(seconds=n)
    store.insert_rows("events", [{**_live_only_row("S2"), "time": sec(0).isoformat()}])
    rows = {
        "trigger_before": _sync_row(sync.TRIGGER_START, sec(-100), kind="sweep"),
        "commit_before": _sync_row(sync.SYNC_COMMIT, sec(-90), session="S1"),
        "end": _sync_row(sync.TRIGGER_START, sec(0), kind="session_end", session="S1"),
        "commit_first": _sync_row(sync.SYNC_COMMIT, sec(1), session="S1"),
        "commit_second": _sync_row(sync.SYNC_COMMIT, sec(2), session="S1"),
        "sweep_after_ttl": _sync_row(sync.TRIGGER_START, sec(sync.LIVE_TTL_S + 5), kind="sweep"),
        "s2_commit_early": _sync_row(sync.SYNC_COMMIT, sec(10), session="S2"),
        "s2_commit_first": _sync_row(sync.SYNC_COMMIT, sec(sync.LIVE_TTL_S + 6), session="S2"),
        "s2_commit_next": _sync_row(sync.SYNC_COMMIT, sec(sync.LIVE_TTL_S + 7), session="S2")}
    store.insert_rows("events", list(rows.values()))
    return rows


def _marks():
    conn = store.open_readonly()
    try:
        return {s: sync.latency_marks(conn, s) for s in ("S1", "S2")}
    finally:
        conn.close()


def test_prune_removes_old_trigger_and_commit_rows(world):
    rows = _latency_rows()
    result = importer.prune(70, NOW)
    gone = {"trigger_before", "commit_before", "commit_second", "s2_commit_early", "s2_commit_next"}
    assert result["events_removed"] == len(gone)
    assert _remaining_sync() == _ids({k: v for k, v in rows.items() if k not in gone})


def test_prune_twice_leaves_latency_marks_unchanged(world):
    _latency_rows()
    marks = _marks()
    assert marks["S1"][2] is not None and marks["S2"][2] is not None
    importer.prune(70, NOW)
    assert _marks() == marks
    kept = _remaining_sync()
    assert importer.prune(70, NOW)["events_removed"] == 0
    assert _remaining_sync() == kept and _marks() == marks


def test_prune_removes_old_decision_rows_keeps_task_aggregates(world):
    world.state("R", [start_entry(0), start_entry(10, "classify", weight_class="small")], task="T")
    world.add(world.decision, {"ts": at(5), "mechanism": "m", "session": "R", "event": "e", "decision": "allow"})
    world.add(world.gate, world.gate_line("R"))
    world.run()
    aggregates = {t: world.count(t) for t in ("tasks", "sessions", "contours")}
    assert world.count("events", "kind = 'decision'") == 1 and all(aggregates.values())
    result = importer.prune(70, NOW)
    assert result["events_removed"] == 1
    assert world.count("events", "kind = 'decision'") == 0
    assert world.count("events", "kind = 'gate'") == 1
    assert {t: world.count(t) for t in aggregates} == aggregates


def test_prune_keeps_sync_deferred_row_while_its_spool_holds_unreplayed_bytes(world):
    world.live_event("D", "command", "cli:status", {"node_before": "ROUTED", "node_after": "ROUTED"})
    row = _sync_row(sync.SYNC_DEFERRED, BASE + timedelta(days=10), session="D")
    store.insert_rows("events", [row])
    assert importer.prune(70, NOW)["events_removed"] == 0
    assert row["event_id"] in _remaining_sync()
    world.replay("D")
    assert importer.prune(70, NOW)["events_removed"] == 1
    assert row["event_id"] not in _remaining_sync()


# --- sync, import, sync ---------------------------------------------------------------------------

def test_real_sync_import_sync_attaches_running_session_to_imported_row(world):
    world.state("RUN", [start_entry(0), start_entry(10, "classify", weight_class="small")], task="TASK")
    world.live_event("RUN", "command", "cli:status", {"node_before": "ROUTED", "node_after": "ROUTED"})
    sync.run()
    assert world.count("sessions", "session_id = 'RUN'") == 1
    assert world.one("SELECT task_id FROM sessions WHERE session_id = 'RUN'") is None
    report = world.run()
    assert report["complete"]
    sync.run()
    assert _tasks() == [("TASK", "open", "small")]
    row = query("SELECT task_id, task_epoch FROM sessions WHERE session_id = 'RUN'")[0]
    assert row["task_id"] == "TASK" and row["task_epoch"] == world.one("SELECT epoch FROM tasks")
    assert world.one("SELECT task_id FROM contours WHERE contour_id = 'RUN'") == "TASK"


# --- check-profile-unchanged ------------------------------------------------------------------

@pytest.fixture
def frozen(tmp_path):
    return Frozen(tmp_path / "frozen").freeze()


def _bump(tree) -> bool:
    for key, value in (tree.items() if isinstance(tree, dict) else enumerate(tree) if isinstance(tree, list) else ()):
        if isinstance(tree, dict) and key == "run":
            continue
        if isinstance(value, float) and value:
            tree[key] = value * 1.5
            return True
        if _bump(value):
            return True
    return False


def test_check_profile_unchanged_refuses_a_snapshot_with_another_sha256(frozen):
    assert frozen.check()[0] == 0
    frozen.members.append(("proj/extra.jsonl", line(prompt(1))))
    frozen.write_snapshot()
    code, report = frozen.check()
    assert code == 2 and report["verdict"] == "premise-failed" and "sha256" in report["reason"]


def test_check_profile_unchanged_reads_the_snapshot_not_live_transcripts(frozen, tmp_path, monkeypatch):
    live_projects = tmp_path / "live-projects"
    (live_projects / "proj").mkdir(parents=True)
    (live_projects / "proj" / "decoy.jsonl").write_bytes(b"".join(line(o) for o in session_lines(10 * DAY)))
    monkeypatch.setattr(config_root, "projects_roots", lambda: [live_projects])
    monkeypatch.setattr(config_root, "iter_transcripts", lambda pattern="*/*.jsonl": sorted(live_projects.glob(pattern)))
    code, report = frozen.check()
    assert (code, report["verdict"]) == (0, "unchanged")


def test_check_profile_unchanged_detects_a_changed_aggregate_figure(frozen):
    assert _bump(frozen.reference)
    frozen.save_reference()
    code, report = frozen.check()
    assert code == 1 and report["verdict"] == "figure-differs" and report["differences"]


def test_check_profile_unchanged_reads_transcripts_only_up_to_reference_time(frozen):
    code, report = frozen.check()
    assert (code, report["verdict"]) == (0, "unchanged")
    assert frozen.reference["run"]["profiled"] == 3


def test_check_profile_unchanged_uses_reference_half_life(frozen):
    assert frozen.reference["half_life_days"] == frozen.half_life == 30.0
    assert frozen.check()[0] == 0


def test_check_profile_unchanged_exits_2_when_session_set_differs(frozen):
    frozen.reference["run"]["profiled"] += 1
    frozen.save_reference()
    code, report = frozen.check()
    assert code == 2 and report["verdict"] == "session-set-differs"
    assert any("profiled sessions" in d for d in report["differences"])


def test_check_profile_unchanged_names_transcripts_with_lines_past_the_cut(frozen):
    code, report = frozen.check()
    assert code == 0 and report["past_cut"] == ["s2"]


def test_check_profile_unchanged_reads_the_root_prefix_member_whole(frozen):
    code, report = frozen.check()
    assert code == 0
    assert "root" not in report["past_cut"]
    root = json.loads(json.dumps(frozen.reference))
    assert root["run"]["profiled"] == 3


def test_check_profile_unchanged_reads_frozen_ledgers_not_live(frozen, tmp_path, monkeypatch):
    live_judge = tmp_path / "live-judge.jsonl"
    live_judge.write_bytes(line({"ts": at(1), "kind": "call", "source": "s1", "judge": "j", "duration": 50.0}))
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(live_judge))
    code, report = frozen.check()
    assert (code, report["verdict"]) == (0, "unchanged")
