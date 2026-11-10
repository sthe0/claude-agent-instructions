"""The per-Core-version report (lib/telemetry/report.py, `agent-report.py core-version`), its
record readers and the verification checks over the record (`telemetry.py check-events` and
`check-latency`). Every fixture is rows inserted straight into the isolated record."""
from __future__ import annotations

import builtins
import io
import json
import uuid

import pytest

from lib import config_root
from lib.telemetry import report, store, sync, tables
from telemetry_import_support import at, reply, world  # noqa: F401  (world is a fixture)
from telemetry_live_support import spool_events


def put(table, *rows):
    outcome = store.insert_rows(table, rows, deadline_s=60)
    assert outcome["failed"] == 0 and outcome["deferred"] == 0


def event(session, kind, seconds, fields=None, source_key=None, provenance="live") -> str:
    event_id = str(uuid.uuid4())
    put("events", {"event_id": event_id, "session_id": session, "kind": kind, "source_key": source_key or kind,
                   "provenance": provenance, "time": at(seconds), "fields": json.dumps(fields or {})})
    return event_id


def task(task_id, head="h1", weight="small", seconds=0, synthetic=0, session=None, status="resolved") -> tuple:
    session = session or f"s-{task_id}"
    put("sessions", {"session_id": session, "core_head": head, "started_at": at(seconds)})
    epoch = event(session, "task", seconds, {"action": "open", "task_id": task_id, "synthetic": bool(synthetic)},
                  "cli:start")
    origin = store.origin_id()
    put("tasks", {"origin": origin, "task_id": task_id, "epoch": epoch, "opened_at": at(seconds),
                  "weight_class": weight, "synthetic": synthetic, "status": status})
    conn = store.open_record()
    conn.execute("UPDATE sessions SET task_origin = ?, task_id = ?, task_epoch = ? WHERE session_id = ?",
                 (origin, task_id, epoch, session))
    conn.commit()
    conn.close()
    return origin, task_id, epoch


def command(key, seconds, name, outcome="ok", **fields):
    return event(f"s-{key[1]}", "command", seconds,
                 {"command": name, "outcome": outcome, "task_epoch": key[2], **fields}, f"cli:{name}")


def check(key, seconds, exit_code, digest="d1", ref="r1", name="tests"):
    return event(f"s-{key[1]}", "check", seconds, {"check": name, "exit": exit_code, "plan_digest": digest,
                                                   "code_ref": ref, "task_epoch": key[2]}, "cli:verify-final")


def tokens(key, message_id, model="claude-opus-x", inp=1000, out=0):
    put("token_usage", {"message_id": message_id, "session_id": f"s-{key[1]}", "role": "root", "phase": "other",
                        "input_tokens": inp, "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0,
                        "ts": at(1), "task_origin": key[0], "task_id": key[1], "task_epoch": key[2], "model": model})


def cause(cause_id, session, category="other", place="execution", index=0, **extra):
    row = {"cause_id": cause_id, "session": session, "difficulty_index": index, "cause_category": category,
           "cause_category_table_version": tables.load("categories").version, "machinery_induced": 0,
           "mechanism": "none", "mechanism_table_version": tables.load("registry").version,
           "functional_place": place, "functional_place_table_version": tables.load("places").version,
           "origin": store.origin_id(), **extra}
    put("causes", row)


def view(*argv) -> "tuple[int, dict]":
    out = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out):
        status = report.main(["--format", "json", *argv])
    return status, json.loads(out.getvalue())


def head(payload, name="h1") -> dict:
    return next(h for h in payload["heads"] if h["head"] == name)


# --- the view ----------------------------------------------------------------------------

def test_report_built_from_record_opens_no_transcript(world, monkeypatch):
    key = task("t1")
    command(key, 10, "replan")
    world.transcript("s-t1", [reply(1, "m1")])
    opened = []
    real_open, real_io_open = builtins.open, io.open

    def guard(real):
        def wrapper(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, "open", guard(real_open))
    monkeypatch.setattr(io, "open", guard(real_io_open))
    status, payload = view()
    assert status == 0 and head(payload)["rows"]["small"]["replans"] == 1
    assert not [p for p in opened if p.startswith(str(world.projects))]


def test_columns_per_head_normalized_by_weight_class():
    small = [task(f"s{i}", weight="small") for i in range(2)]
    large = [task(f"l{i}", weight="large") for i in range(4)]
    for key in small:
        command(key, 10, "replan")
    for key in large[:2]:
        command(key, 10, "plan-review")
    rows = head(view()[1])["rows"]
    assert rows["small"]["tasks"] == 2 and rows["small"]["per_task"]["replans"] == 1.0
    assert rows["large"]["tasks"] == 4 and rows["large"]["per_task"]["review_rounds"] == 0.5
    assert rows["small"]["share_of_head_tasks"] == pytest.approx(2 / 6)


def test_red_then_green_on_same_digest_and_ref_is_one_flake():
    key = task("t1")
    for offset, code in enumerate((1, 0, 1, 0)):
        check(key, 10 + offset, code)
    check(key, 20, 1, digest="d2")
    check(key, 21, 0, ref="r2")
    row = head(view()[1])["rows"]["small"]
    assert row["flakes"] == 1
    assert row["verify_red"] == 3 and row["verify_green"] == 3 and row["verify_red_rate"] == 0.5


def test_mechanism_use_and_cause_counts_per_head():
    one = task("t1", head="h1")
    two = task("t2", head="h2")
    command(one, 10, "replan", duration_s=30)
    command(one, 20, "replan", duration_s=30)
    command(two, 10, "classify")
    event("s-t1", "decision", 15, {"task_epoch": one[2]}, "hook:hook-branch-backup")
    cause("c1", "s-t1", "implementation_defect", "execution")
    cause("c2", "s-t2", "trunk_drift_landing", "landing")
    payload = view()[1]
    first, second = head(payload, "h1"), head(payload, "h2")
    assert first["mechanisms"]["cli:replan"] == {"events": 2, "decisions": 0, "minutes": 1.0}
    assert "cli:replan" not in second["mechanisms"]
    assert first["mechanisms"]["landing_path"]["decisions"] == 1
    assert first["causes_per_category"]["implementation_defect"] == 1
    assert first["causes_per_category"]["trunk_drift_landing"] == 0
    assert second["causes_per_place"]["landing"] == 1 and first["causes_per_place"]["landing"] == 0


def test_other_category_counted_per_head():
    task("t1", head="h1")
    task("t2", head="h2")
    cause("c1", "s-t1", "other")
    payload = view()[1]
    assert head(payload, "h1")["causes_per_category"]["other"] == 1
    assert head(payload, "h2")["causes_per_category"]["other"] == 0


def test_zero_count_categories_are_written():
    task("t1")
    counts = head(view()[1])["causes_per_category"]
    assert set(counts) == set(tables.load("categories").members) and set(counts.values()) == {0}
    assert set(head(view()[1])["causes_per_place"]) == set(tables.load("places").members)


def test_losses_and_unattributed_count_beside_shares():
    task("t1")
    put("losses", {"session_id": "s-t1", "loss_mode": "torn_event", "count": 3})
    put("losses", {"session_id": "s-x", "loss_mode": "torn_event", "count": 2})
    put("contours", {"contour_id": "c-orphan", "role": "judge"})
    payload = view()[1]
    assert payload["losses"]["counted"]["torn_event"] == 5
    assert "process_killed_before_emit" in payload["losses"]["uncounted_modes"]
    assert payload["unattributed_contours"] == 1
    assert payload["heads"][0]["rows"]["small"]["tasks"] == 1


def test_synthetic_tasks_excluded_by_default():
    task("real")
    task("fake", synthetic=1)
    payload = view()[1]
    assert head(payload)["rows"]["small"]["tasks"] == 1 and payload["synthetic_tasks_counted"] == 0
    with_synthetic = view("--include-synthetic")[1]
    assert head(with_synthetic)["rows"]["small"]["tasks"] == 2 and with_synthetic["synthetic_tasks_counted"] == 1


def test_report_emits_its_own_usage_event(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    task("t1")
    view()
    rows = spool_events(report.REPORT_SOURCE)
    assert rows, "no usage event in the report's spool"
    assert rows[-1]["kind"] == "report"
    assert rows[-1]["fields"]["source_key"] == report.REPORT_SOURCE and rows[-1]["fields"]["view"] == "core-version"


def test_every_event_resolves_to_one_registry_entry_or_is_counted_unresolved():
    key = task("t1")
    command(key, 10, "replan")
    event("s-t1", "command", 11, {"task_epoch": key[2]}, "cli:no-such-verb")
    event("s-t1", "command", 12, {"task_epoch": key[2]}, "cli:no-such-verb")
    status, payload = view("--check")
    assert payload["unresolved_source_keys"] == {"cli:no-such-verb": 2}
    assert status == 1
    assert head(payload)["mechanisms"]["cli:replan"]["events"] == 1
    assert sum(m["events"] for m in head(payload)["mechanisms"].values() if m) >= 2  # entry + its composites


def test_imported_unresolved_source_keys_counted_not_failed():
    task("t1")
    event("s-t1", "bash", 10, {}, "transcript:Nonexistent", provenance="transcript")
    event("s-t1", "command", 11, {}, "history:old-verb", provenance="imported")
    status, payload = view("--check")
    assert payload["imported_unresolved_source_keys"] == {"transcript:Nonexistent": 1, "history:old-verb": 1}
    assert payload["unresolved_source_keys"] == {} and status == 0


def test_per_head_tokens_count_each_message_once():
    key = task("t1")
    tokens(key, "m1", inp=100)
    tokens(key, "m1", inp=100)
    tokens(key, "m2", inp=50)
    assert head(view()[1])["rows"]["small"]["tokens"]["input"] == 150


def test_spend_usd_prices_each_row_by_its_model_on_a_mixed_model_fixture():
    from lib import transcript_cost
    key = task("t1")
    tokens(key, "m1", model="claude-opus-5-5", inp=1_000_000)
    tokens(key, "m2", model="claude-haiku-5-5", inp=1_000_000)
    expected = (transcript_cost.rates_for("claude-opus-5-5")["input"]
                + transcript_cost.rates_for("claude-haiku-5-5")["input"])
    conn = store.open_readonly()
    try:
        [row] = report.tasks(conn)
    finally:
        conn.close()
    assert expected > 2 * transcript_cost.rates_for("claude-haiku-5-5")["input"]
    assert row["spend_usd"] == pytest.approx(expected)


# --- readers ------------------------------------------------------------------------------

def test_readers_return_the_declared_fields():
    key = task("t1")
    command(key, 10, "plan-check", node_before="EXECUTING", node_after="DIAGNOSING")
    command(key, 20, "replan", node_before="DIAGNOSING", node_after="PLANNING")
    event("s-t1", "gate", 25, {"gate": "effort_fire", "outcome": "refuse", "task_epoch": key[2]}, "gate:effort_fire")
    command(key, 30, "status", node_before="PLAN_READY", node_after="DIAGNOSING")
    cause("c-live", "s-t1", "implementation_defect", index=0)
    cause("c-free", "fixture", "other", imported=1, alignment_session=None)
    conn = store.open_readonly()
    try:
        [row] = report.tasks(conn)
        found = report.difficulties(conn)
        sessions = report.sessions(conn, key)
        events = report.events(conn, key, kinds=("command",))
    finally:
        conn.close()
    assert set(row) == {"task", "weight_class", "status", "last_event_at", "minutes", "spend_usd", "replans",
                        "synthetic"}
    assert row["task"] == key and row["status"] == "RESOLVED" and row["replans"] == 1
    assert row["minutes"] == pytest.approx(0.5)
    first, second, free = found
    assert set(first) == {"difficulty", "task", "entry_at", "entry_route", "exit_at", "exit_kind", "cause"}
    assert (first["entry_route"], first["exit_kind"], first["exit_at"]) == ("failed_stage", "replan", at(20))
    assert first["cause"]["category"] == "implementation_defect"
    assert (second["entry_route"], second["exit_at"], second["cause"]) == ("review_cycle", None, None)
    assert free["difficulty"] == "c-free" and free["task"] is None and free["cause"]["category"] == "other"
    assert [s["session_id"] for s in sessions] == ["s-t1"] and sessions[0]["core_head"] == "h1"
    assert [e["fields"]["command"] for e in events] == ["plan-check", "replan", "status"]


def test_effort_fire_entry_route():
    key = task("t1")
    command(key, 10, "dispatch", node_before="EXECUTING", node_after="EXECUTING")
    event("s-t1", "gate", 15, {"gate": "effort_fire", "outcome": "refuse", "task_epoch": key[2]}, "gate:effort_fire")
    command(key, 20, "record-result", node_before="EXECUTING", node_after="DIAGNOSING")
    conn = store.open_readonly()
    try:
        [row] = report.difficulties(conn)
    finally:
        conn.close()
    assert row["entry_route"] == "effort_fire"


# --- one task's self cost ----------------------------------------------------------------

def landing(key, seconds, source_key, branch="b1"):
    return event(f"s-{key[1]}", "landing", seconds, {"branch": branch, "task_epoch": key[2]}, source_key)


def self_measured(key):
    """A task whose sessions carry every self-measurement entry, a recorder overhead and one landing."""
    command(key, 5, "status", recorder_s=0.004)
    event(f"s-{key[1]}", "recorder", 6, {}, "telemetry:recorder")
    event("telemetry:sync", "sync.commit", 7, {"session": f"s-{key[1]}"}, "telemetry:sync")
    event(f"s-{key[1]}", "report", 8, {"session": f"s-{key[1]}"}, "telemetry:report")
    landing(key, 100, "land:start")
    landing(key, 110, "land:attempt")
    landing(key, 160, "land:landed")


def test_self_cost_columns_overhead_landing_minutes_attempts():
    key = task("t1")
    self_measured(key)
    status, payload = view("--task", "t1")
    cost = payload["self_cost"]
    assert status == 0
    assert cost["recorder_overhead_ms"] == pytest.approx(4.0) and cost["recorder_bound_ms"] == 250.0
    assert cost["landing_minutes"] == pytest.approx(1.0) and cost["landing_intervals"] == 1
    assert cost["land_attempts"] == 1
    assert all(cost["entry_events"].values())


def test_check_self_cost_fails_without_overhead_landing_minutes_or_attempts():
    key = task("t1")
    self_measured(key)
    assert view("--task", "t1", "--check-self-cost")[0] == 0
    assert view("--task", "t1", "--check-self-cost", "--min-land-attempts", "2")[0] == 1
    bare = task("t2")
    command(bare, 5, "status")
    status, payload = view("--task", "t2", "--check-self-cost")
    assert status == 1
    assert {"no recorder overhead", "no landing minutes"} <= set(payload["self_cost_failures"])
    assert "land attempts 0 < 1" in payload["self_cost_failures"]


def test_land_attempts_counted_from_landing_events():
    key = task("t1")
    landing(key, 100, "land:start")
    for offset in range(3):
        landing(key, 110 + offset, "land:attempt")
    landing(key, 130, "land:landed")
    assert view("--task", "t1")[1]["self_cost"]["land_attempts"] == 3


def test_landing_minutes_sum_per_pr_intervals():
    key = task("t1")
    landing(key, 0, "land:start", "b1")
    landing(key, 60, "land:landed", "b1")
    landing(key, 3600, "land:start", "b2")
    landing(key, 3720, "land:landed", "b2")
    cost = view("--task", "t1")[1]["self_cost"]
    assert cost["landing_intervals"] == 2 and cost["landing_minutes"] == pytest.approx(3.0)


# --- check-events ----------------------------------------------------------------------------

@pytest.fixture
def acts(world, monkeypatch):
    """A task whose engine history has two commands and whose gate log has one evaluation."""
    gate_log = world.tmp / "gate-log.jsonl"
    monkeypatch.setattr(config_root, "agentctl_gate_log", lambda: gate_log)
    key = task("t1")
    world.state("s-t1", [{"event": "classify", "ts": at(10)}, {"event": "plan-check", "ts": at(20)}], task="t1")
    command(key, 11, "classify", duration_s=1.5)
    command(key, 21, "plan-check", duration_s=1.5)
    world.add(gate_log, world.gate_line("s-t1", "plan", seconds=20))
    event("s-t1", "gate", 20.5, {"gate": "plan", "outcome": "pass"}, "gate:plan")
    return world, gate_log


def check_events_cli(*argv) -> "tuple[int, dict]":
    import telemetry as telemetry_cli
    out = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out):
        status = telemetry_cli.main(["check-events", *argv])
    return status, json.loads(out.getvalue())


def test_check_events_fails_on_a_missing_event_row(acts):
    world, gate_log = acts
    status, result = check_events_cli("--task", "t1")
    assert status == 0 and result["matched"] == 3 and result["missing"] == []
    world.add(gate_log, world.gate_line("s-t1", "approve", seconds=30))
    status, result = check_events_cli("--task", "t1")
    assert status == 1 and [m["act"] for m in result["missing"]] == ["gate:approve"]
    world.state("s-t1", [{"event": "classify", "ts": at(10)}, {"event": "plan-check", "ts": at(20)},
                         {"event": "approve", "ts": at(40)}], task="t1")
    event("s-t1", "gate", 30, {"gate": "approve", "outcome": "pass"}, "gate:approve")
    status, result = check_events_cli("--task", "t1")
    assert status == 1 and [m["act"] for m in result["missing"]] == ["cli:approve"]


def test_check_events_excuses_acts_in_the_declared_unrecorded_window(acts):
    world, gate_log = acts
    world.add(gate_log, world.gate_line("s-t1", "approve", seconds=30))
    window = world.tmp / "window.json"
    window.write_text(json.dumps({"unrecorded_window": {"from": at(25), "to": at(35)}}), encoding="utf-8")
    status, result = check_events_cli("--task", "t1", "--unrecorded-window", str(window))
    assert status == 0 and [e["act"] for e in result["excused"]] == ["gate:approve"] and result["missing"] == []
    world.add(gate_log, world.gate_line("s-t1", "dispatch", seconds=50))
    assert check_events_cli("--task", "t1", "--unrecorded-window", str(window))[0] == 1


# --- check-latency ---------------------------------------------------------------------------

S = 600.0
TTL = sync.LIVE_TTL_S


def trigger(seconds, kind="sweep", session=None):
    event(sync.SYNC_SESSION, sync.TRIGGER_START, seconds, {"kind": kind, **({"session": session} if session else {})},
          sync.SOURCE_KEY)


def appeared(seconds, session):
    event(sync.SYNC_SESSION, sync.SYNC_COMMIT, seconds, {"session": session}, sync.SOURCE_KEY)


def ended(session, seconds=0):
    event(session, "command", seconds, {"command": "status", "outcome": "ok"}, "cli:status")


def latency(now_s, landed_s=-1.0) -> dict:
    conn = store.open_readonly()
    try:
        return report.check_latency(conn, S, at(landed_s), sync.parse_time(at(now_s)))
    finally:
        conn.close()


def status_of(result, session) -> str:
    return next(r["status"] for r in result["sessions"] if r["session"] == session)


def on_time(session="s-ok", seconds=0):
    ended(session, seconds)
    trigger(seconds + 10, "session_end", session)
    appeared(seconds + 20, session)


def test_check_latency_fails_past_the_bound():
    ended("s1")
    trigger(100, "session_end", "s1")
    appeared(100 + S + 100, "s1")
    result = latency(10_000)
    assert not result["ok"] and result["late"] == ["s1"]


def test_check_latency_measures_from_first_recorded_trigger_start_after_eligibility():
    ended("s1", 0)
    trigger(1000)
    trigger(TTL + 700)
    appeared(TTL + 710, "s1")
    result = latency(10_000)
    [row] = result["sessions"]
    assert row["latency_s"] == pytest.approx(10.0) and row["status"] == "on_time" and result["ok"]


def test_timely_first_commit_followed_by_commits_past_S_is_not_late():
    ended("s1")
    trigger(100, "session_end", "s1")
    appeared(110, "s1")
    appeared(110 + 3 * S, "s1")
    result = latency(10_000)
    assert result["sessions"][0]["latency_s"] == pytest.approx(10.0) and result["ok"]


def test_killed_session_E_unchanged_across_repeated_sweeps():
    ended("s1", 0)
    trigger(TTL + 100)
    appeared(TTL + 105, "s1")
    first = latency(10_000)["sessions"][0]
    trigger(TTL + 5000)
    appeared(TTL + 5005, "s1")
    second = latency(20_000)["sessions"][0]
    assert first["E"] == second["E"] == sync.parse_time(at(TTL))
    assert second["A"] == sync.parse_time(at(TTL + 105)) and second["status"] == "on_time"


def test_check_latency_ttl_boundary_race_commit_between_E_and_T1_is_not_late():
    ended("s1", 0)
    appeared(TTL + 50, "s1")
    trigger(TTL + 3000)
    result = latency(100_000)
    assert result["sessions"][0]["latency_s"] == 0.0 and result["ok"]


def test_check_latency_lists_a_session_without_trigger_as_waiting():
    on_time()
    ended("s-wait", 5000)
    result = latency(100_000)
    assert status_of(result, "s-wait") == "waiting" and result["ok"] and result["by_status"]["waiting"] == 1


def test_check_latency_fails_when_a_later_trigger_opportunity_passed():
    on_time()
    ended("s1", 5000)
    trigger(5000 + TTL + 10)
    result = latency(5000 + TTL + 10 + S + 1)
    assert status_of(result, "s1") == "late" and not result["ok"]


def test_check_latency_lists_an_in_flight_sync_younger_than_the_bound():
    on_time()
    ended("s1", 5000)
    trigger(5000 + TTL + 10)
    result = latency(5000 + TTL + 10 + S - 1)
    assert status_of(result, "s1") == "in_flight" and result["ok"]


def test_check_latency_lists_deferred_sync_rows_as_named_loss_not_late():
    on_time()
    for session in ("s-spooled", "s-deferred"):
        ended(session, 5000)
    trigger(5000 + TTL + 10)
    sync._keep_in_reserved_spool({"event_id": str(uuid.uuid4()), "kind": sync.SYNC_COMMIT, "time": at(5000 + TTL + 20),
                                  "fields": {"session": "s-spooled"}})
    event(sync.SYNC_SESSION, sync.SYNC_DEFERRED, 5000 + TTL + 20, {"session": "s-deferred"}, sync.SOURCE_KEY)
    result = latency(100_000)
    assert status_of(result, "s-spooled") == status_of(result, "s-deferred") == "sync_deferred"
    assert result["ok"] and result["late"] == []


def test_check_latency_ignores_sessions_ended_before_landing_and_synthetic():
    ended("s-before", 0)
    trigger(TTL + 10)
    task("fake", synthetic=1, seconds=10_000)
    trigger(10_000 + TTL + 10)
    on_time("s-ok", 20_000)
    result = latency(100_000, landed_s=TTL + 100)
    assert [r["session"] for r in result["sessions"]] == ["s-ok"] and result["ok"]


def test_check_latency_fails_on_an_empty_population(tmp_path):
    import telemetry as telemetry_cli
    on_time("s-ok", 0)
    landing_record = tmp_path / "landing.json"
    landing_record.write_text(json.dumps({"landed_at": at(50_000)}), encoding="utf-8")
    argv = ["check-latency", "--max-seconds", str(S), "--since-landing", str(landing_record)]
    assert telemetry_cli.main(argv) == 1
    landing_record.write_text(json.dumps({"landed_at": at(-10)}), encoding="utf-8")
    assert telemetry_cli.main(argv) == 0
