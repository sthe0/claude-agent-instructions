"""The importer: what each source becomes, that a second run adds nothing, how a changed or shortened
source is read again, deferral and failure bookkeeping, and the live-wins rule against spools."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import telemetry as telemetry_cli
from lib import config_root, judge_ledger
from lib.telemetry import align, importer, live, store, sync
from telemetry_import_support import (
    BASE, at, bash_call, bash_result, line, prompt, reply, world)  # noqa: F401
from telemetry_live_support import query

SCRIPTS = Path(__file__).resolve().parents[1]
STATUS = {"node_before": "ROUTED", "node_after": "ROUTED"}


@pytest.fixture(autouse=True)
def no_fixture_causes(monkeypatch, tmp_path):
    """The exact row totals below are those of the generated sources; the committed causes fixture
    adds its own 235 rows to every import and is covered by test_telemetry_fixture.py."""
    monkeypatch.setattr(align, "FIXTURES", tmp_path / "no-fixtures")


def history(*seconds) -> list:
    return [{"ts": at(s), "event": "start" if i == 0 else "plan"} for i, s in enumerate(seconds)]


def gate_lines(world, count: int = 4) -> list:
    return world.add(world.gate, *[world.gate_line(f"s{i}", seconds=i) for i in range(count)])


def gate_rows() -> list:
    return [r[0] for r in query("SELECT event_id FROM events WHERE kind = 'gate' ORDER BY event_id")]


def defer_batches(monkeypatch, times: int, failed: bool = False) -> dict:
    real = store.transact
    state = {"left": times, "calls": 0}

    def fake(work, deadline_s=None):
        if "_batch_work" in work.__qualname__ and state["left"] > 0:
            state["left"] -= 1
            state["calls"] += 1
            return {"committed": False, "deferred": not failed, "failed": failed, "value": None}
        return real(work, deadline_s)
    monkeypatch.setattr(store, "transact", fake)
    return state


# --- one source, one row ---------------------------------------------------------------------

def test_each_source_imported_with_expected_row_count(world):
    world.add(world.gate, world.gate_line("R"), world.gate_line("R", "exec", False, 20))
    world.add(world.decision, *[{"ts": at(i), "mechanism": "m", "session": "R", "event": "e", "decision": "allow"}
                                for i in range(3)])
    world.add(world.judge, {"ts": at(1), "kind": "call", "source": "R", "judge": "j", "duration": 1.0},
              {"ts": at(2), "kind": "call", "source": "R", "judge": "j", "duration": 2.0}, {"ts": at(3), "kind": "summary"})
    world.add(world.spawn, {"ts": at(4), "event": "spawn", "child_session_id": "K", "spawner_session": "R",
                            "kind": "developer", "depth": 1, "cost_usd": 0.5})
    world.add(world.quality, {"session": "Q", "instructions_head": "h1"})
    world.state("R", history(0, 10, 20))
    report = world.run()
    rows = {name: report["per_source"][name]["rows"] for name in ("gate", "decision", "judge", "spawn", "quality", "history")}
    assert rows == {"gate": 2, "decision": 3, "judge": 2, "spawn": 1, "quality": 1, "history": 3}
    kinds = {r[0]: r[1] for r in query("SELECT kind, COUNT(*) FROM events GROUP BY kind")}
    assert {k: kinds[k] for k in ("gate", "decision", "judge", "spawn_cost", "command")} == {
        "gate": 2, "decision": 3, "judge": 2, "spawn_cost": 1, "command": 3}
    assert report["errors"] == 0 and report["complete"]


def test_second_import_adds_no_row(world):
    gate_lines(world)
    world.add(world.judge, {"ts": at(1), "kind": "call", "source": "R", "judge": "j", "duration": 1.0})
    world.add(world.spawn, {"ts": at(4), "event": "spawn", "child_session_id": "K", "spawner_session": "R"})
    world.state("R", history(0, 10))
    first = world.run()
    assert first["rows_added"] > 0
    snapshot = query("SELECT COUNT(*) FROM events WHERE provenance = 'imported'")[0][0]
    second = world.run()
    assert second["rows_added"] == 0 and all(s["rows"] == 0 for s in second["per_source"].values())
    assert query("SELECT COUNT(*) FROM events WHERE provenance = 'imported'")[0][0] == snapshot


def test_imported_gate_rows_hold_r2_fields_and_back_reference_without_payload(world):
    [(offset, sha, _)] = world.add(world.gate, {**world.gate_line("R"), "payload": "SECRET", "blockers": ["x"]})
    world.run()
    fields = json.loads(world.one("SELECT fields FROM events WHERE kind = 'gate'"))
    assert set(fields) == {"session", "verb", "gate", "outcome", "time", "source_key", "backref"}
    assert fields["backref"] == {"path": str(world.gate), "offset": offset, "sha256": sha}
    assert fields["outcome"] == "pass" and "SECRET" not in json.dumps(fields)


def test_imported_ids_are_uuid5_over_origin_path_offset(world):
    [(o0, s0, _), (o1, s1, _)] = gate_lines(world, 2)
    world.run()
    ids = gate_rows()
    origin = store.origin_id()
    expected = {store.imported_row_id(str(world.gate), o0, s0, origin), store.imported_row_id(str(world.gate), o1, s1, origin)}
    assert set(ids) == expected and len(expected) == 2
    assert all(uuid.UUID(i).version == 5 for i in ids)
    assert store.imported_row_id(str(world.gate), o0, s0, str(uuid.uuid4())) not in expected
    assert store.imported_row_id(str(world.gate) + "x", o0, s0, origin) not in expected


# --- live wins ---------------------------------------------------------------------------------

def test_live_event_after_import_supersedes_imported_row(world):
    [(offset, sha, _)] = world.add(world.gate, world.gate_line("R"))
    world.run()
    imported = world.id_of(world.gate, offset, sha)
    assert world.count("events", "event_id = ?", imported) == 1
    live_id = world.live_event("R", "gate", "gate:plan", {"session": "R", "gate": "plan", "outcome": "pass"},
                               backref=(str(world.gate), offset, sha))
    world.replay("R")
    assert world.count("events", "event_id = ?", imported) == 0
    assert world.one("SELECT backref_id FROM events WHERE event_id = ?", live_id) == imported
    world.run()
    assert world.count("events", "kind = 'gate'") == 1


def test_file_line_matching_a_live_back_reference_not_inserted(world):
    [(offset, sha, _)] = world.add(world.gate, world.gate_line("R"))
    world.live_event("R", "gate", "gate:plan", {"session": "R", "gate": "plan", "outcome": "pass"},
                     backref=(str(world.gate), offset, sha))
    world.replay("R")
    world.run()
    assert world.count("events", "kind = 'gate' AND provenance = 'imported'") == 0
    assert world.count("events", "kind = 'gate' AND provenance = 'live'") == 1


def _spawn_line(child="K", spawner="R") -> dict:
    return {"ts": at(4), "event": "spawn", "child_session_id": child, "spawner_session": spawner, "kind": "developer"}


def test_spawn_cost_line_of_a_live_child_session_not_inserted(world):
    world.live_event("K", "command", "cli:status", STATUS)
    world.replay("K")
    world.add(world.spawn, _spawn_line())
    world.run()
    assert world.count("events", "kind = 'spawn_cost'") == 0
    assert world.count("contours", "parent_session = 'R'") == 0


def test_spawn_cost_line_after_live_spawn_event_not_inserted(world):
    world.live_event("R", "spawn", "cli:spawn", {"child_session_id": "K"})
    world.replay("R")
    world.add(world.spawn, _spawn_line())
    world.run()
    assert world.count("events", "kind = 'spawn_cost'") == 0
    assert world.count("events", "kind = 'spawn' AND provenance = 'live'") == 1


def test_history_of_session_with_live_events_not_imported(world):
    world.state("S", history(0, 10), task="T")
    world.live_event("S", "command", "cli:start", STATUS)
    world.replay("S")
    world.run()
    assert world.count("events", "session_id = 'S' AND provenance = 'imported'") == 0


def test_spool_only_session_counts_as_live(world):
    world.state("S", history(0, 10), task="T")
    world.transcript("S", [prompt(0), bash_call(1, "c1", "python3 scripts/agentctl-cli.py classify"), bash_result(2, "c1")])
    world.live_event("S", "command", "cli:status", STATUS)
    world.run(include_transcripts=True)
    assert world.count("events", "session_id = 'S' AND provenance = 'imported'") == 0
    assert world.count("events", "session_id = 'S'") == 0


def test_session_with_live_events_gets_no_transcript_event_rows(world):
    world.transcript("L", [prompt(0), bash_call(1, "c1", "python3 scripts/agentctl-cli.py classify"), bash_result(2, "c1")])
    world.live_event("L", "command", "cli:status", STATUS)
    world.replay("L")
    world.run(include_transcripts=True)
    assert world.count("events", "kind = 'bash'") == 0


def test_session_without_live_events_gets_transcript_events(world):
    world.transcript("B", [prompt(0), bash_call(1, "c1", "python3 scripts/agentctl-cli.py classify"), bash_result(2, "c1"),
                           bash_call(3, "c2", "git status"), bash_result(4, "c2", "Exit code 1\nfatal")])
    world.run(include_transcripts=True)
    rows = [json.loads(r[0]) for r in query("SELECT fields FROM events WHERE kind = 'bash' AND session_id = 'B' ORDER BY time")]
    assert [(r["program"], r["verb"], r["exit"]) for r in rows] == [("agentctl-cli.py", "classify", 0), ("git", "status", 1)]
    assert world.run(include_transcripts=True)["rows_added"] == 0


def test_transcript_with_oversized_first_line_gets_transcript_events(world):
    oversized = {"type": "summary", "sessionId": "O", "summary": "x" * (importer.FIRST_LINE_MAX + 10)}
    world.transcript("O", [oversized, prompt(0), bash_call(1, "c1", "git status"), bash_result(2, "c1")])
    world.run(include_transcripts=True)
    assert world.count("events", "kind = 'bash' AND session_id = 'O'") == 1
    assert world.run(include_transcripts=True)["rows_added"] == 0


# --- identity of partially recorded and child sessions ------------------------------------------------

def _root_with_live_event(world, session="S", opening=None) -> None:
    world.state(session, [{"ts": at(0), "event": "start"}, {"ts": at(10), "event": "classify", "weight_class": "small"},
                          {"ts": at(20), "event": "plan"}, {"ts": at(30), "event": "submit-plan"}], task="T")
    world.live_event(session, "command", opening or "cli:status", STATUS)
    world.replay(session)


def test_partially_recorded_session_counts_early_acts_as_loss(world):
    _root_with_live_event(world)
    report = world.run()
    assert world.one("SELECT count FROM losses WHERE session_id = 'S' AND loss_mode = "
                     "'partial_session_before_first_live_event'") == 4
    assert report["losses_by_session"]["S"] == {"partial_session_before_first_live_event": 4}
    assert world.count("events", "session_id = 'S' AND provenance = 'imported'") == 0


def test_partially_recorded_session_whose_first_live_event_follows_its_opening_act_gets_identity_from_history(world):
    _root_with_live_event(world)
    world.run()
    row = query("SELECT task_id, task_epoch, role FROM sessions WHERE session_id = 'S'")[0]
    assert row["task_id"] == "T" and row["role"] == "root" and row["task_epoch"] == world.one("SELECT epoch FROM tasks")
    assert world.one("SELECT weight_class FROM tasks") == "small"
    assert world.one("SELECT task_id FROM contours WHERE contour_id = 'S'") == "T"


@pytest.mark.parametrize("replayed", [True, False])
def test_session_with_live_opening_event_gets_no_imported_task_row(world, replayed):
    world.state("S", history(0, 10), task="T")
    world.live_event("S", "command", "cli:start", STATUS)
    if replayed:
        world.replay("S")
    report = world.run()
    assert world.count("tasks") == 0
    assert "S" not in report["losses_by_session"]


def test_partially_recorded_child_session_takes_no_task_row_from_its_own_history(world):
    world.state("C", history(0, 10), task="TC", parent="R", depth=1)
    world.live_event("C", "command", "cli:status", STATUS)
    world.replay("C")
    report = world.run()
    assert world.count("tasks") == 0
    assert "C" not in report["losses_by_session"]


def test_imported_child_history_opens_no_task_row(world):
    world.state("R", history(0, 10), task="T")
    world.state("C", history(20, 30), task="TC", parent="R", depth=1)
    world.run()
    assert [r[0] for r in query("SELECT task_id FROM tasks")] == ["T"]
    assert world.one("SELECT task_id FROM sessions WHERE session_id = 'C'") == "T"


@pytest.mark.parametrize("kind", ["state-file", "spawn-cost", "judge", "subagent"])
def test_imported_child_contour_attaches_to_parent_task_row(world, kind):
    world.state("R", history(0, 10), task="T")
    if kind == "state-file":
        world.state("C", history(20), task="TC", parent="R", depth=1)
        tables, ident = ("sessions", "contours"), "C"
    elif kind == "spawn-cost":
        [(offset, sha, _)] = world.add(world.spawn, _spawn_line())
        tables, ident = ("sessions", "contours"), ("K", world.id_of(world.spawn, offset, sha))
    elif kind == "judge":
        [(offset, sha, _)] = world.add(world.judge, {"ts": at(40), "kind": "call", "source": "R", "judge": "jd", "duration": 2.0})
        tables, ident = ("contours",), world.id_of(world.judge, offset, sha)
    else:
        sub = world.projects / "p" / "R" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_bytes(line(prompt(0)))
        tables, ident = ("contours",), "R/agent-1"
    world.run()
    epoch = world.one("SELECT epoch FROM tasks")
    ids = ident if isinstance(ident, tuple) else (ident, ident)
    for table, key in zip(("sessions", "contours"), ids):
        if table not in tables:
            continue
        column = "session_id" if table == "sessions" else "contour_id"
        row = query(f"SELECT task_id, task_epoch FROM {table} WHERE {column} = ?", key)
        assert [tuple(r) for r in row] == [("T", epoch)], (table, key)
    if kind == "judge":
        assert world.one("SELECT role FROM contours WHERE contour_id = ?", ident) == "judge"
    if kind == "subagent":
        assert world.one("SELECT role FROM contours WHERE contour_id = 'R/agent-1'") == "agent-tool"


def test_offsets_kept_per_file_and_consumer(world):
    gate_lines(world, 2)
    [(offset, sha, raw)] = world.add(world.judge, {"ts": at(1), "kind": "call", "source": "R", "judge": "j", "duration": 1.0})
    world.run()
    assert [tuple(r) for r in world.offsets(world.judge)] == [("contours", len(raw)), ("events", len(raw))]
    assert [r[0] for r in world.offsets(world.gate)] == ["events"]
    assert world.count("contours", "role = 'judge'") == 1 and world.count("events", "kind = 'judge'") == 1


# --- reading a source again ----------------------------------------------------------------------

def test_same_content_under_new_inode_adds_no_row(world):
    gate_lines(world)
    world.run()
    copy = world.gate.with_name("copy")
    shutil.copy(world.gate, copy)
    os.replace(copy, world.gate)
    second = world.run()
    assert second["rows_added"] == 0 and second["per_source"]["gate"]["rereads"] == 0
    assert len(world.offsets(world.gate)) == 1


def test_truncation_keeping_first_line_rereads_from_zero_without_duplicates(world):
    written = gate_lines(world)
    world.run()
    os.truncate(world.gate, written[2][0])
    second = world.run()
    cut = len(written[2][2]) + len(written[3][2])
    assert second["per_source"]["gate"]["rereads"] == 1 and second["per_source"]["gate"]["truncated_bytes"] == cut
    assert second["rows_added"] == 0 and world.count("events", "kind = 'gate'") == 4
    assert world.one("SELECT count FROM losses WHERE session_id = 'import:gate' AND loss_mode = 'source_truncated'") == 1


def test_new_content_after_truncation_is_stored(world):
    written = gate_lines(world)
    world.run()
    os.truncate(world.gate, written[1][0])
    [(offset, sha, _)] = world.add(world.gate, world.gate_line("zz"))
    report = world.run()
    assert world.count("events", "event_id = ?", world.id_of(world.gate, offset, sha)) == 1
    assert report["per_source"]["gate"]["rows"] == 1


def test_tail_cut_then_append_imports_the_new_lines(world):
    written = gate_lines(world)
    world.run()
    os.truncate(world.gate, written[3][0])
    [(offset, sha, _)] = world.add(world.gate, world.gate_line("a-much-longer-session-name"))
    report = world.run()
    assert report["per_source"]["gate"]["rereads"] == 1
    assert world.count("events", "event_id = ?", world.id_of(world.gate, offset, sha)) == 1


def test_growth_past_old_offset_with_changed_tail_rereads_from_zero(world):
    written = gate_lines(world)
    world.run()
    changed = world.gate_line("s9", seconds=3)
    assert len(line(changed)) == len(written[3][2])
    world.gate.write_bytes(b"".join(w[2] for w in written[:3]) + line(changed))
    [(extra, extra_sha, _)] = world.add(world.gate, world.gate_line("s8", seconds=5))
    report = world.run()
    assert report["per_source"]["gate"]["rereads"] == 1
    changed_id = world.id_of(world.gate, written[3][0], __import__("hashlib").sha256(line(changed)).hexdigest())
    assert world.count("events", "event_id IN (?, ?)", changed_id, world.id_of(world.gate, extra, extra_sha)) == 2


def test_replaced_source_read_again_from_start(world):
    first = world.add(world.gate, world.gate_line("a1"), world.gate_line("a2"))
    world.run()
    world.gate.unlink()
    second = world.add(world.gate, world.gate_line("b1", seconds=5), world.gate_line("b2", seconds=6))
    assert [w[0] for w in first] == [w[0] for w in second]
    report = world.run()
    assert report["rows_added"] == 2 and world.count("events", "kind = 'gate'") == 4
    assert {world.id_of(world.gate, o, s) for o, s, _ in second} <= set(gate_rows())
    assert len(world.offsets(world.gate)) == 2


# --- losses and counters -----------------------------------------------------------------------------

def test_malformed_line_counted_per_session_not_as_error(world):
    world.add(world.gate, world.gate_line("R"), b"not json\n", b"[1, 2]\n")
    report = world.run()
    assert report["errors"] == 0 and report["complete"] and report["malformed_lines"] == 2
    assert world.one("SELECT count FROM losses WHERE session_id = 'import:gate' AND loss_mode = 'malformed_source_line'") == 2


def test_counted_losses_per_session_and_uncounted_modes_named(world):
    world.add(world.gate, world.gate_line("R"), b"not json\n", b"[1, 2]\n")
    written = world.add(world.decision, *[{"ts": at(i), "mechanism": "m", "session": "R", "event": "e"} for i in range(3)])
    world.run()
    os.truncate(world.decision, written[1][0])
    report = world.run()
    assert report["losses_by_session"] == {"import:gate": {"malformed_source_line": 2},
                                           "import:decision": {"source_truncated": 1}}
    assert report["uncounted_modes"] == ["disk_full", "drop_marker_unwritable", "process_killed_before_emit",
                                         "rejected_line", "spool_append_failed"]


def test_deferred_batch_keeps_offset_sets_incomplete_and_exits_nonzero(world, monkeypatch, capsys):
    gate_lines(world)
    monkeypatch.setattr(importer, "BATCH_LINES", 2)
    monkeypatch.setattr(importer, "BUDGET_S", 0.0)
    monkeypatch.setattr(importer, "_sleep", lambda s: None)
    defer_batches(monkeypatch, 1)
    assert telemetry_cli.main(["import"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is False and report["errors"] == 0 and report["rows_added"] == 0
    assert world.offsets(world.gate) == []
    assert world.run()["rows_added"] == 4


def test_deferred_batch_retried_until_lock_released(world, monkeypatch):
    gate_lines(world)
    sleeps = []
    monkeypatch.setattr(importer, "_sleep", sleeps.append)
    state = defer_batches(monkeypatch, 2)
    report = world.run()
    assert sleeps == [1.0, 2.0] and state["calls"] == 2
    assert report["complete"] and report["rows_added"] == 4


def test_unreadable_source_and_failed_batch_each_count_one_error(world, monkeypatch):
    gate_lines(world, 2)
    world.decision.mkdir()
    defer_batches(monkeypatch, 1, failed=True)
    report = world.run()
    assert report["errors"] == 2 and report["complete"] is False


def test_import_run_emits_one_event_under_telemetry_importer(world):
    gate_lines(world, 2)
    report = world.run()
    rows = query("SELECT kind, source_key, provenance, fields FROM events WHERE session_id = 'telemetry:importer'")
    assert len(rows) == 1 and (rows[0]["kind"], rows[0]["provenance"]) == ("import.run", "live")
    fields = json.loads(rows[0]["fields"])
    assert fields["rows_added"] == report["rows_added"] == 2 and fields["complete"] is True


def test_stats_json_keys_and_exit_code(world, tmp_path, capsys):
    gate_lines(world, 2)
    target = tmp_path / "stats.json"
    assert telemetry_cli.main(["import", "--stats-json", str(target)]) == 0
    stats = json.loads(target.read_text())
    assert set(stats) == {"rows_added", "per_source", "errors", "malformed_lines", "complete", "peak_rss_mib",
                          "losses_by_session", "uncounted_modes", "rss_exceeded"}
    assert stats == json.loads(capsys.readouterr().out) and stats["rss_exceeded"] is False
    assert telemetry_cli.main(["import", "--max-rss-mib", "0.0001"]) == 1
    assert json.loads(capsys.readouterr().out)["rss_exceeded"] is True


# --- spools -----------------------------------------------------------------------------------------

def test_spool_import_reuses_the_spool_event_id(world):
    event_id = world.live_event("E", "command", "cli:status", STATUS)
    live.end_marker_path("E").write_text("")
    world.run()
    assert world.count("events", "event_id = ? AND provenance = 'live'", event_id) == 1


def test_undelivered_spool_of_ended_session_imported(world):
    world.live_event("E", "command", "cli:status", STATUS)
    live.end_marker_path("E").write_text("")
    world.live_event("F", "command", "cli:status", STATUS)
    world.run()
    assert world.count("events", "session_id = 'E'") == 1 and world.count("events", "session_id = 'F'") == 0
    world.run(replay_all_spools=True)
    assert world.count("events", "session_id = 'F'") == 1


def test_importer_retries_undrained_spool_replay(world, monkeypatch):
    world.live_event("E", "command", "cli:status", STATUS)
    live.end_marker_path("E").write_text("")
    real, sleeps = store.replay, []
    calls = {"n": 0}

    def fake(stem, deadline_s=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"inserted": 0, "deferred": False, "torn": 0, "superseded": 0, "drained": False, "failed": False}
        return real(stem, deadline_s=deadline_s)
    monkeypatch.setattr(store, "replay", fake)
    monkeypatch.setattr(importer, "_sleep", sleeps.append)
    world.run()
    assert sleeps == [1.0] and world.count("events", "session_id = 'E'") == 1


# --- bounded memory ---------------------------------------------------------------------------------

def _cli_import(tmp_path: Path, gate_mib: int, name: str, states: int = 0) -> float:
    """Peak RSS of `telemetry.py import` over a gate log of `gate_mib` MiB of 1 KiB lines and `states` engine
    state files of 512 KiB each. The log is written in chunks: a child's ru_maxrss inherits the high-water mark
    of the parent at fork, so a parent holding the whole log in memory would show it as the child's."""
    home = tmp_path / name
    (home / "log").mkdir(parents=True)
    chunk = line({"pad": "x" * 1000}) * 1024
    with open(home / "gate.jsonl", "wb") as handle:
        for _ in range(gate_mib):
            handle.write(chunk)
    del chunk
    state_dir = home / "cfg" / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    for number in range(states):
        session = f"S{number:04d}"
        steps = [{"ts": at(0), "event": "start", "pad": "y" * (256 * 1024)}, {"ts": at(10), "event": "plan"}]
        (state_dir / f"{session}.json").write_text(json.dumps(
            {"session_id": session, "task_id": session, "history": steps, "ballast": "z" * (256 * 1024)}))
    env = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "cfg"),
           "AGENTCTL_GATE_LOG": str(home / "gate.jsonl"), "AGENTCTL_DECISION_LOG": str(home / "decision.jsonl"),
           "AGENTCTL_JUDGE_LEDGER": str(home / "judge.jsonl"), "AGENTCTL_TELEMETRY_DB": str(home / "t.sqlite"),
           "AGENTCTL_TELEMETRY_SPOOL": str(home / "spool")}
    # Through a shell that forks the importer rather than exec-ing it: a process exec'd straight from pytest
    # starts its ru_maxrss at pytest's own high-water mark (about 120 MiB), which would hide any growth below it.
    done = subprocess.run(["/bin/sh", "-c", '"$0" "$1" import; exit $?', sys.executable, str(SCRIPTS / "telemetry.py")],
                          env=env, capture_output=True, text=True, timeout=600, cwd=str(SCRIPTS))
    assert done.returncode == 0, done.stderr[-500:]
    return json.loads(done.stdout.splitlines()[-1])["peak_rss_mib"]


def test_gate_log_read_in_bounded_memory(tmp_path):
    baseline = _cli_import(tmp_path, 0, "small")
    large = _cli_import(tmp_path, 256, "large")
    assert large - baseline <= 32


def test_engine_state_read_in_bounded_memory(tmp_path):
    """128 state files of 512 KiB (half in a history entry, half beside the history) are 64 MiB held whole;
    keeping only the fields the history import reads stays well under 32 MiB of growth."""
    baseline = _cli_import(tmp_path, 0, "small")
    large = _cli_import(tmp_path, 0, "large", states=128)
    assert large - baseline < 32
