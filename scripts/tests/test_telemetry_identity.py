"""Task identity and contours of the live telemetry record: which engine acts open, continue,
abandon or resolve a task row, which events belong to which row, and how contours and
sessions attach to a row."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agentctl import runtime_host
from agentctl.state import SessionState
from agentctl.store import FileStateStore
from lib import config_root, marker_extract
from lib.telemetry import live, store, sync
from telemetry_live_support import (
    agentctl, events, make_resolved, no_background_sweep, open_epoch, query, reset, start, tasks)


@pytest.fixture(autouse=True)
def _live_env(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms; a test's
    verdict must not depend on machine load."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # The engine binds a host at start; with the session id unset, pin it so the verdict
    # does not depend on whether this machine has a runnable Cursor CLI.
    monkeypatch.setattr(runtime_host, "detect_host", lambda: runtime_host.HOST_CLAUDE)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    no_background_sweep(monkeypatch)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "state"


def _open_events(session: str) -> list:
    return [e for e in events(session, kind="task") if e["fields"]["action"] == "open"]


def _task_events(session: str, action: str) -> list:
    return [e for e in events(session, kind="task") if e["fields"]["action"] == action]


def _link_child(root, child: str, parent: str) -> None:
    state = SessionState(session_id=child, task_id="seed", recursion_depth=1)
    state.parent_link = {"parent_session": parent, "depth": 1}
    FileStateStore(root).save(state)


def test_reset_under_synthetic_env_opens_synthetic_row(root, monkeypatch):
    start(root, "plain", "task-a")
    monkeypatch.setenv(live.SYNTHETIC_ENV, "1")
    start(root, "synth-start", "task-b")
    reset(root, "plain", "task-c", "--force")
    agentctl(root, "status", "--session", "synth-start")
    monkeypatch.delenv(live.SYNTHETIC_ENV)
    start(root, "later", "task-d")
    agentctl(root, "status", "--session", "plain")

    sync.run()

    by_task = {row["task_id"]: row["synthetic"] for row in tasks()}
    assert by_task == {"task-a": 0, "task-b": 1, "task-c": 1, "task-d": 0}


def test_reset_with_continue_epoch_attaches_to_the_open_row(root):
    start(root, "s1", "task-a")
    epoch = open_epoch("s1")
    assert epoch == _open_events("s1")[0]["event_id"]

    reset(root, "s1", "task-a", "--force", "--continue-epoch", epoch)
    agentctl(root, "status", "--session", "s1")
    sync.run()

    assert len(_open_events("s1")) == 1 and _task_events("s1", "abandon") == []
    assert [e["fields"]["epoch"] for e in _task_events("s1", "continue")] == [epoch]
    assert open_epoch("s1") == epoch
    assert [(r["epoch"], r["status"]) for r in tasks()] == [(epoch, "open")]
    last_command = events("s1", kind="command")[-1]
    assert last_command["fields"]["task_epoch"] == epoch


def test_harness_resume_of_same_engine_session_continues_the_epoch(root):
    start(root, "s1", "task-a")
    epoch = open_epoch("s1")

    resumed = start(root, "s1", "task-a", "--if-absent")
    assert resumed["action"] == "continue"
    agentctl(root, "status", "--session", "s1")
    sync.run()

    assert len(_open_events("s1")) == 1 and _task_events("s1", "continue") == []
    assert open_epoch("s1") == epoch
    commands = events("s1", kind="command")
    assert [c["fields"]["command"] for c in commands] == ["start", "start", "status"]
    assert {c["fields"]["task_epoch"] for c in commands[1:]} == {epoch}
    assert len(tasks()) == 1


def test_reset_same_task_without_continuation_opens_new_row_and_abandons_old(root):
    start(root, "s1", "task-a")
    first = open_epoch("s1")

    reset(root, "s1", "task-a", "--force")
    second = open_epoch("s1")
    sync.run()

    assert second != first
    assert [e["fields"]["epoch"] for e in _task_events("s1", "abandon")] == [first]
    status = {row["epoch"]: row["status"] for row in tasks()}
    assert status == {first: "abandoned", second: "open"}
    assert {row["task_id"] for row in tasks()} == {"task-a"}


def test_task_id_reused_after_resolved_opens_new_row(root):
    start(root, "s1", "task-a")
    first = open_epoch("s1")
    make_resolved(root, "s1")
    agentctl(root, "status", "--session", "s1")
    assert live.read_epoch("s1")["resolved"] is True

    reopened = reset(root, "s1", "task-a", "--reopen-reason", "the order stands, more work")
    assert reopened["ok"] is True
    second = open_epoch("s1")
    sync.run()

    assert second != first and live.read_epoch("s1")["resolved"] is False
    assert _task_events("s1", "abandon") == []
    status = {row["epoch"]: row["status"] for row in tasks()}
    assert status == {first: "resolved", second: "open"}


def test_reset_to_another_task_abandons_the_unresolved_row(root):
    start(root, "s1", "task-a")
    first = open_epoch("s1")

    reset(root, "s1", "task-b", "--force")
    sync.run()

    rows = {row["task_id"]: row for row in tasks()}
    assert rows["task-a"]["status"] == "abandoned" and rows["task-a"]["epoch"] == first
    assert rows["task-a"]["ended_at"] is not None
    assert rows["task-b"]["status"] == "open" and rows["task-b"]["epoch"] == open_epoch("s1")


def test_one_task_id_opened_twice_in_one_session_has_two_epochs(root):
    start(root, "s1", "task-a")
    reset(root, "s1", "task-b", "--force")
    reset(root, "s1", "task-a", "--force")
    sync.run()

    rows = [row for row in tasks() if row["task_id"] == "task-a"]
    assert len(rows) == 2 and len({row["epoch"] for row in rows}) == 2
    assert {row["origin"] for row in rows} == {store.origin_id()}
    assert sorted(row["status"] for row in rows) == ["abandoned", "open"]
    assert len(tasks()) == 3


def test_events_of_a_two_row_session_attribute_to_their_own_epochs(root):
    start(root, "s1", "task-a")
    first = open_epoch("s1")
    agentctl(root, "status", "--session", "s1")
    reset(root, "s1", "task-b", "--force")
    second = open_epoch("s1")
    agentctl(root, "status", "--session", "s1")
    live.emit_event("s1", "note", "telemetry:test", {"task_epoch": None})
    sync.run()

    conn = store.open_record()
    try:
        origin = store.origin_id()
        first_set = set(sync.events_of_task(conn, origin, "task-a", first))
        second_set = set(sync.events_of_task(conn, origin, "task-b", second))
        every = {e["event_id"] for e in events("s1")}
        by_epoch = {e["event_id"]: e["fields"].get("task_epoch") for e in events("s1")}
        opening = {e["fields"]["task_id"]: e["event_id"] for e in _open_events("s1")}
    finally:
        conn.close()
    assert first_set and second_set and not (first_set & second_set)
    assert first_set | second_set == every
    assert {i for i, epoch in by_epoch.items() if epoch == first} <= first_set
    assert {i for i, epoch in by_epoch.items() if epoch == second} <= second_set
    assert opening["task-b"] in second_set and opening["task-a"] in first_set
    tail = events("s1", kind="note")[0]["event_id"]
    assert tail in second_set


def test_children_under_one_child_slug_stay_on_their_parents_rows(root):
    start(root, "parent-1", "task-a")
    start(root, "parent-2", "task-b")
    _link_child(root, "child-1", "parent-1")
    _link_child(root, "child-2", "parent-2")
    reset(root, "child-1", "child-slug", "--force")
    reset(root, "child-2", "child-slug", "--force")
    sync.run()

    keys = {row["session_id"]: row["task_id"] for row in query(
        "SELECT session_id, task_id FROM sessions WHERE session_id LIKE 'child-%'")}
    assert keys == {"child-1": "task-a", "child-2": "task-b"}
    assert {row["task_id"] for row in tasks()} == {"task-a", "task-b"}
    contours = {row["contour_id"]: (row["parent_session"], row["role"], row["task_id"]) for row in query(
        "SELECT * FROM contours WHERE contour_id LIKE 'child-%'")}
    assert contours == {"child-1": ("parent-1", "linked-child", "task-a"),
                        "child-2": ("parent-2", "linked-child", "task-b")}


def test_child_reset_at_depth_above_zero_never_opens_a_task_row(root):
    start(root, "deep", "task-x", "--recursion-depth", "1")
    reset(root, "deep", "task-y", "--force", "--recursion-depth", "2")
    _link_child(root, "linked", "deep")
    reset(root, "linked", "task-z", "--force")
    sync.run()

    assert events(kind="task") == []
    assert tasks() == [] and open_epoch("deep") is None and open_epoch("linked") is None
    sessions = [e["fields"] for e in events("deep", kind="session")]
    assert [s["depth"] for s in sessions] == [1, 2]
    assert events("linked", kind="session")[0]["fields"]["linked"] is True


def test_root_and_every_contour_kind_attach_to_one_task_row(root, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    transcript = projects / "proj" / "root-1" / "subagents" / "agent-7.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_root, "projects_roots", lambda: [projects])

    start(root, "root-1", "task-a")
    agentctl(root, "classify", "--session", "root-1", "--architectural", "--changed-lines", "900",
             "--wall-clock-min", "90")
    live.emit_event("root-1", "spawn", "spawn", {"role": "spawned-child", "child_session_id": "spawned-9"})
    judge_id = live.emit_event("root-1", "judge", "judge:advisor", {"verdict": "pass"})
    _link_child(root, "linked-1", "root-1")
    reset(root, "linked-1", "child-slug", "--force")
    sync.run()

    task = tasks()[0]
    weight = FileStateStore(root).load("root-1").weight_class
    assert weight and task["weight_class"] == weight
    spawn_id = events("root-1", kind="spawn")[0]["event_id"]
    rows = {row["contour_id"]: row for row in query("SELECT * FROM contours")}
    expected = {"root-1": "root", spawn_id: "spawned-child", judge_id: "judge",
                "root-1/agent-7": "agent-tool", "linked-1": "linked-child"}
    assert {k: rows[k]["role"] for k in expected} == expected
    for contour_id in expected:
        row = rows[contour_id]
        assert (row["task_origin"], row["task_id"], row["task_epoch"]) == (
            task["origin"], task["task_id"], task["epoch"]), contour_id
        assert row["weight_class"] == weight, contour_id
        assert row["role_table_version"] == 1
    assert sync.unattached_contour_count(store.open_record()) == 0


def test_unattachable_contour_is_stored_with_null_task_and_counted(root):
    live.emit_event("orphan", "judge", "judge:advisor", {"verdict": "pass"})
    live.emit_event("orphan", "spawn", "spawn", {"role": "spawned-child", "child_session_id": "kid"})

    outcome = sync.run()

    rows = query("SELECT * FROM contours WHERE parent_session = 'orphan' OR contour_id = 'orphan'")
    assert len(rows) == 3 and all(row["task_id"] is None for row in rows)
    assert {row["role"] for row in rows} == {"root", "judge", "spawned-child"}
    assert outcome["unattached"] == 3
    conn = store.open_record()
    try:
        assert sync.unattached_contour_count(conn) == 3
    finally:
        conn.close()


def test_sync_attaches_live_events_of_session_running_before_pr1_to_its_imported_task_row(root):
    live.emit_event("old", "judge", "judge:advisor", {"verdict": "pass"})
    live.emit_event("old", "spawn", "spawn", {"role": "spawned-child", "child_session_id": "kid"})
    first = sync.run()
    assert first["unattached"] == 3

    origin = store.origin_id()
    epoch = store.imported_row_id("/engine/history.jsonl", 0, "ef" * 32, origin=origin)
    assert store.insert_rows("tasks", [{
        "origin": origin, "task_id": "task-old", "epoch": epoch, "status": "open", "synthetic": 0,
        "opened_at": "2026-11-01T00:00:00+00:00"}])["inserted"] == 1
    assert store.transact(lambda conn: store.attach_session_task(conn, "old", (origin, "task-old", epoch)))["committed"] is True
    live.emit_event("old", "judge", "judge:advisor", {"verdict": "pass", "again": True})
    live.emit_event("kid", "note", "telemetry:test", {})
    second = sync.run()

    assert second["unattached"] == 0
    kid = query("SELECT task_origin, task_id, task_epoch FROM sessions WHERE session_id = 'kid'")
    assert [(row["task_origin"], row["task_id"], row["task_epoch"]) for row in kid] == [(origin, "task-old", epoch)]
    rows = query("SELECT * FROM contours WHERE parent_session = 'old' OR contour_id = 'old'")
    assert {(row["task_origin"], row["task_id"], row["task_epoch"]) for row in rows} == {
        (origin, "task-old", epoch)}
    conn = store.open_record()
    try:
        attributed = set(sync.events_of_task(conn, origin, "task-old", epoch))
    finally:
        conn.close()
    assert {e["event_id"] for e in events("old")} <= attributed
    assert len(tasks()) == 1 and tasks()[0]["epoch"] == epoch


def _load_spawner():
    path = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
    spec = importlib.util.spec_from_file_location("spawn_specialist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProc:
    returncode = 0
    pid = 424242

    def communicate(self, input=None):
        return json.dumps({"result": "COMPLETED: done", "cost_usd": 0.5}), ""


def _run_spawn(monkeypatch, tmp_path, session: str, child: str, cost_log: "Path | None" = None) -> None:
    mod = _load_spawner()
    monkeypatch.setattr(mod.proc_tree, "launch_supervised", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    if cost_log is None:
        monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    else:
        monkeypatch.setattr(mod, "COST_LOG", cost_log)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_child_session_id", lambda *a, **k: child)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    plan = tmp_path / "plan.md"
    plan.write_text("a plan", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "spawn-specialist.py", "--kind", "developer", "--plan", str(plan), "--session", session,
        "--done-criterion", "done", "--criterion-type", "measurable", "--model", "haiku",
        "--effort", "medium"])
    mod.main()


def test_spawn_event_carries_child_session_id(root, tmp_path, monkeypatch, capsys):
    start(root, "parent-1", "task-a")
    _run_spawn(monkeypatch, tmp_path, "parent-1", "child-session-77")
    capsys.readouterr()
    assert store.insert_rows("events", [{
        "event_id": store.imported_row_id("/ledger/spawn-costs.jsonl", 10, "aa" * 32, origin=store.origin_id()),
        "session_id": "parent-1", "kind": "spawn_cost", "provenance": "imported", "time": "2026-11-06T00:00:00+00:00",
        "child_session_id": "child-session-77", "fields": json.dumps({"cost_usd": 0.5})}])["inserted"] == 1
    assert [r["kind"] for r in query("SELECT kind FROM events WHERE child_session_id = 'child-session-77'")] == [
        "spawn_cost"]

    sync.run()

    spawns = events("parent-1", kind="spawn")
    assert len(spawns) == 1 and spawns[0]["fields"]["child_session_id"] == "child-session-77"
    assert spawns[0]["child_session_id"] == "child-session-77"
    assert spawns[0]["source_key"] == "spawn" and spawns[0]["provenance"] == "live"
    assert spawns[0]["fields"]["task_epoch"] == open_epoch("parent-1")
    assert [r["kind"] for r in query("SELECT kind FROM events WHERE child_session_id = 'child-session-77'")] == ["spawn"]
    contour = query("SELECT parent_session, role FROM contours WHERE contour_id = ?", spawns[0]["event_id"])[0]
    assert tuple(contour) == ("parent-1", "spawned-child")
