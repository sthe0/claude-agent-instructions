"""telemetry-compare.py, telemetry-synthetic-task.py and telemetry-bench.py: the record side
against the transcript side over one task set, the synthetic top-up, the trace classifier and the
recording-cost bounds."""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path
from unittest import mock

import pytest

from lib.telemetry import store
from telemetry_import_support import _gz_tar, _sha, at, bash_call, bash_result, world  # noqa: F401
from telemetry_live_support import start, tasks

SCRIPTS = Path(__file__).resolve().parents[1]
STEPS = ((10, "classify", "ROUTED"), (100, "plan", "PLANNING"), (400, "approve", "APPROVED"),
         (1000, "record-result", "VERIFYING"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _load("telemetry-compare")
driver = _load("telemetry-synthetic-task")
bench = _load("telemetry-bench")


# --- the record ------------------------------------------------------------------------------

def put(table, *rows):
    assert store.insert_rows(table, rows)["failed"] == 0


def event(session, kind, seconds, fields, provenance) -> str:
    event_id = str(uuid.uuid4())
    put("events", {"event_id": event_id, "session_id": session, "kind": kind, "source_key": f"test:{kind}",
                   "provenance": provenance, "time": at(seconds), "fields": json.dumps(fields)})
    return event_id


def make_task(task_id, provenance="live", seconds=0, session=None, bind=True, synthetic=0, status="resolved"):
    """A task opened by an event in `session`; the epoch is that event's id, carried as task_epoch
    so an unbound shared session still attributes its rows."""
    session = session or f"s-{task_id}"
    put("sessions", {"session_id": session, "core_head": "h1", "started_at": at(seconds)})
    epoch = str(uuid.uuid4())
    put("events", {"event_id": epoch, "session_id": session, "kind": "task", "source_key": "test:task",
                   "provenance": provenance, "time": at(seconds),
                   "fields": json.dumps({"action": "open", "task_id": task_id, "task_epoch": epoch})})
    key = (store.origin_id(), task_id, epoch)
    put("tasks", {"origin": key[0], "task_id": task_id, "epoch": epoch, "opened_at": at(seconds),
                  "weight_class": "small", "synthetic": synthetic, "status": status})
    if bind:
        _bind(session, key)
    return key


def _bind(session, key, role=None):
    conn = store.open_record()
    conn.execute("INSERT OR IGNORE INTO sessions (session_id, core_head, started_at) VALUES (?, 'h1', ?)",
                 (session, at(0)))
    conn.execute("UPDATE sessions SET task_origin = ?, task_id = ?, task_epoch = ?, role = ? WHERE session_id = ?",
                 (*key, role, session))
    conn.commit()
    conn.close()


def steps(key, session, provenance="live", offset=0):
    for seconds, _, node in STEPS:
        event(session, "command", offset + seconds, {"node_after": node, "task_epoch": key[2]}, provenance)


def transcript_lines(offset=0, delay=1):
    lines = []
    for seconds, verb, _ in STEPS:
        call = f"c{offset + seconds}"
        lines += [bash_call(offset + seconds, call, f"python3 ~/claude-agent-instructions/scripts/agentctl-cli.py {verb}"),
                  bash_result(offset + seconds + delay, call)]
    return lines


def loss(session):
    put("losses", {"session_id": session, "loss_mode": compare.PARTIAL_LOSS, "count": 1})


def live_task(world, task_id, transcript=True, **kwargs):
    key = make_task(task_id, **kwargs)
    steps(key, f"s-{task_id}")
    if transcript:
        world.transcript(f"s-{task_id}", transcript_lines())
    return key


def chosen_live(conn=None):
    conn = conn or store.open_readonly()
    return compare.live_set(conn, compare.local_transcripts())


# --- the historical side ---------------------------------------------------------------------

def historical_task(task_id, session=None, bind=True, offset=0):
    session = session or f"s-{task_id}"
    key = make_task(task_id, "imported", offset, session, bind)
    steps(key, session, "imported", offset)
    return key


def snapshot(world, members: dict, wrong_sha=False):
    """(premises, snapshot path): the snapshot holds `members` (session -> transcript lines)."""
    path = world.tmp / "snapshot.tar.gz"
    _gz_tar(path, [(f"p/{s}.jsonl", b"".join(json.dumps(o).encode() + b"\n" for o in lines))
                   for s, lines in members.items()])
    premises = {"transcript_snapshot_sha256": "0" * 64 if wrong_sha else _sha(path), "analysis_cutoff": at(10_000)}
    return premises, path


def run_historical(world, members, min_tasks=1, wrong_sha=False):
    premises, path = snapshot(world, members, wrong_sha)
    conn = store.open_readonly()
    try:
        return compare.historical(conn, premises, path, min_tasks)
    finally:
        conn.close()


def shares_agree(payload):
    return all(abs(p["record"] - p["transcript"]) < 1e-9 for p in payload["phases"].values())


# --- tolerance and agreement -----------------------------------------------------------------

def test_tolerance_is_max_of_two_points_and_reconstruction_half_width():
    assert compare.tolerance({"lo": 0.10, "hi": 0.20, "se": 0.5}) == pytest.approx(0.05)
    assert compare.tolerance({"lo": 0.10, "hi": 0.11, "se": 0.5}) == pytest.approx(0.02)


def test_compare_fails_above_tolerance():
    same = compare.agreement([{"order": 100}], [{"order": 100}], [{"order": 100}])
    assert same["ok"]
    apart = compare.agreement([{"order": 100}], [{"order": 50, "planning": 50}], [{"order": 50, "planning": 50}])
    assert not apart["ok"]
    assert apart["phases"]["order"]["difference"] == pytest.approx(0.5)
    near = compare.agreement([{"order": 100}], [{"order": 99, "planning": 1}], [{"order": 99, "planning": 1}])
    assert near["ok"]


# --- the historical side ---------------------------------------------------------------------

def test_historical_transcript_side_computed_over_the_record_side_task_set(world):
    historical_task("t1")
    historical_task("t2", offset=2000)
    historical_task("t3", offset=4000)
    status, payload = run_historical(world, {"s-t1": transcript_lines(), "s-t2": transcript_lines(2000)})
    assert status == 0, payload
    assert payload["tasks"] == 2
    assert payload["historical_tasks_without_transcript"] == 1
    assert set(payload["phases"]) == {"order", "planning", "execution"}
    assert shares_agree(payload)


def test_compare_refuses_a_snapshot_with_another_sha256(world):
    historical_task("t1")
    status, payload = run_historical(world, {"s-t1": transcript_lines()}, wrong_sha=True)
    assert status == compare.EXIT_PREMISE
    assert "sha256" in payload["error"]


def test_historical_task_missing_from_the_snapshot_left_out_of_both_sides(world):
    historical_task("t1")
    historical_task("t2", offset=2000)
    status, payload = run_historical(world, {"s-t1": transcript_lines()})
    assert status == 0, payload
    assert payload["tasks"] == 1
    assert payload["historical_tasks_without_transcript"] == 1


def test_historical_side_fails_when_no_task_joins_the_snapshot(world):
    historical_task("t1")
    status, payload = run_historical(world, {"s-other": transcript_lines()})
    assert status == compare.EXIT_DIFFER
    assert payload["tasks"] == 0
    assert "fewer than 1" in payload["error"]


def test_historical_transcript_side_splits_a_session_at_task_boundaries(world):
    historical_task("t1", session="shared", bind=False)
    historical_task("t2", session="shared", bind=False, offset=2000)
    status, payload = run_historical(world, {"shared": transcript_lines() + transcript_lines(2000)})
    assert status == 0, payload
    assert payload["tasks"] == 2
    assert shares_agree(payload)
    # t2's window opens 10 s inside the verification t1 left open: 600 + 600 of 990 + 1000.
    assert payload["phases"]["execution"]["record"] == pytest.approx(1200 / 1990)


def test_historical_task_with_partially_recorded_session_left_out_of_both_sides(world):
    historical_task("t1")
    historical_task("t2", offset=2000)
    loss("s-t2")
    status, payload = run_historical(world, {"s-t1": transcript_lines(), "s-t2": transcript_lines(2000)})
    assert status == 0, payload
    assert payload["tasks"] == 1
    assert payload["historical_tasks_partially_recorded"] == 1


def test_historical_task_with_unimported_transcript_left_out_of_both_sides(world):
    historical_task("t1")
    key = historical_task("t2", offset=2000)
    _bind("s-t2-child", key)
    members = {"s-t1": transcript_lines(), "s-t2": transcript_lines(2000), "s-t2-child": transcript_lines(2000)}
    status, payload = run_historical(world, members)
    assert status == 0, payload
    assert payload["tasks"] == 1
    assert payload["historical_tasks_transcript_not_imported"] == 1


def test_historical_task_with_transcriptless_judge_contour_stays_in_both_sides(world):
    key = historical_task("t1")
    _bind("s-t1-judge", key, role=compare.JUDGE_ROLE)
    event("s-t1-judge", "judge", 50, {"task_epoch": key[2]}, "imported")
    status, payload = run_historical(world, {"s-t1": transcript_lines()})
    assert status == 0, payload
    assert payload["tasks"] == 1
    assert payload["historical_tasks_without_transcript"] == 0
    assert shares_agree(payload)


# --- the live side ---------------------------------------------------------------------------

def test_live_side_excludes_partially_recorded_and_open_tasks(world):
    kept = live_task(world, "t1")
    live_task(world, "t2", seconds=0)
    loss("s-t2")
    live_task(world, "t3", status="open")
    chosen, counts = chosen_live()
    assert [k for k, _ in chosen] == [kept]
    assert counts["live_tasks_partially_recorded"] == 1
    assert counts["live_tasks_open"] == 1


def test_live_side_excludes_imported_only_tasks(world):
    kept = live_task(world, "t1")
    historical_task("t2")
    world.transcript("s-t2", transcript_lines())
    chosen, counts = chosen_live()
    assert [k for k, _ in chosen] == [kept]
    assert counts["live_tasks_imported_only"] == 1


def test_live_side_excludes_tasks_with_an_imported_session(world):
    kept = live_task(world, "t1")
    key = live_task(world, "t2")
    _bind("s-t2-child", key)
    event("s-t2-child", "command", 200, {"node_after": "PLANNING", "task_epoch": key[2]}, "imported")
    world.transcript("s-t2-child", transcript_lines())
    chosen, counts = chosen_live()
    assert [k for k, _ in chosen] == [kept]
    assert counts["live_tasks_mixed_provenance"] == 1


def test_live_task_without_transcript_left_out_of_both_sides(world):
    kept = live_task(world, "t1")
    live_task(world, "t2", transcript=False)
    conn = store.open_readonly()
    chosen, counts = chosen_live(conn)
    assert [k for k, _ in chosen] == [kept]
    assert counts["live_tasks_without_transcript"] == 1
    record, early, late = compare.sides(conn, chosen, lambda s: compare._file_lines(compare.local_transcripts()[s]))
    assert len(record) == len(early) == len(late) == 1
    assert compare.agreement(record, early, late)["ok"]


def test_task_with_transcriptless_judge_contour_stays_in_the_live_set(world):
    key = live_task(world, "t1")
    event("s-t1", "spawn_cost", 20, {"task_epoch": key[2]}, "imported")
    _bind("s-t1-judge", key, role=compare.JUDGE_ROLE)
    event("s-t1-judge", "judge", 50, {"task_epoch": key[2]}, "imported")
    chosen, counts = chosen_live()
    assert [k for k, _ in chosen] == [key]
    assert counts["live_tasks_without_transcript"] == counts["live_tasks_mixed_provenance"] == 0


def test_synthesize_tops_up_to_min_tasks(world, monkeypatch):
    live_task(world, "t1")
    made = []

    class FakeDriver:
        @staticmethod
        def run(count, trace=False):
            for _ in range(count):
                made.append(live_task(world, f"syn{len(made)}", synthetic=1, seconds=10 * len(made)))
            return []

    monkeypatch.setattr(compare, "load_driver", lambda: FakeDriver)
    status, payload = compare.live(3, True, 10)
    assert status == 0, payload
    assert (payload["tasks"], payload["synthesized"]) == (3, 2)
    status, payload = compare.live(6, True, 1)
    assert status == compare.EXIT_DIFFER
    assert payload["synthesized"] == 1
    assert payload["live_tasks_synthetic"] == 2
    assert len(made) == 3


def test_driver_run_task_reads_synthetic_true(world, monkeypatch):
    state_root = world.tmp / "driver-state"

    def fake_run_task(prompt, env, trace_log=None):
        with mock.patch.dict(os.environ, env):
            start(state_root, "s-driver", "synthetic-task")
        return 0

    monkeypatch.setattr(driver, "run_task", fake_run_task)
    results = driver.run(1)
    assert results == [{"returncode": 0, "trace": None}]
    rows = [t for t in tasks() if t["task_id"] == "synthetic-task"]
    assert [t["synthetic"] for t in rows] == [1]


# --- trace ----------------------------------------------------------------------------------

def test_trace_classifies_hook_and_command_path_processes(tmp_path):
    log = tmp_path / "trace.log"
    log.write_text(
        '100 execve("/usr/bin/python3", ["python3", "/x/scripts/hook-guard-canon-readonly.py"], 0x7ffd /* 3 vars */) = 0\n'
        '100 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3\n'
        '101 execve("/usr/bin/python3", ["python3", "/x/scripts/agentctl-cli.py", "status"], 0x7ffd /* 3 vars */) = 0\n'
        "102 execve '/usr/bin/git' ['git', 'status']\n", encoding="utf-8")
    assert driver.classify_process(["python3", "/x/scripts/hook-prompt-reminders.py"]) == driver.HOOK_PATH
    assert driver.classify_process(["python3", "/x/scripts/agentctl-cli.py"]) == driver.COMMAND_PATH
    assert driver.trace_summary(log) == {driver.HOOK_PATH: 1, driver.COMMAND_PATH: 2}


# --- bench ----------------------------------------------------------------------------------

def _fake_runs(monkeypatch, hook_ms, excess):
    """hook_run returns hook_ms; each recording-on command run costs the next value of `excess`
    over its recording-off pair."""
    values = iter(excess)
    monkeypatch.setattr(bench, "hook_run", lambda env: hook_ms)
    monkeypatch.setattr(bench, "command_run",
                        lambda verb, env, root: 0.0 if env.get(bench.OFF_ENV) == "off" else next(values))


def test_bench_fails_when_a_bound_is_exceeded(monkeypatch):
    _fake_runs(monkeypatch, 5.0, [300.0] * 40)
    result = bench.bench(3, 20, 10.0, 250.0)
    assert not result["ok"]
    assert len(result["failures"]) == 2
    _fake_runs(monkeypatch, 50.0, [1.0] * 40)
    result = bench.bench(3, 20, 10.0, 250.0)
    assert not result["ok"]
    assert result["failures"] == ["hook emission p95 50.0 ms > 10.0 ms"]
    _fake_runs(monkeypatch, 5.0, [1.0] * 40)
    assert bench.bench(3, 20, 10.0, 250.0)["ok"]


def test_bench_command_excess_is_p95_over_at_least_20_pairs(monkeypatch):
    with pytest.raises(ValueError):
        bench.bench(1, 19, 10.0, 250.0)
    assert bench.main(["--hook-runs", "1", "--pairs", "19"]) == 2
    _fake_runs(monkeypatch, 1.0, [float(v) for v in range(1, 21)] * 2)
    result = bench.bench(1, 20, 10.0, 250.0)
    assert result["command_excess_p95_ms"] == {"status": 19.0, "classify --title bench": 19.0}
    assert result["pairs"] == 20
