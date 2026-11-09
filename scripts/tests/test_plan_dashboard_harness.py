"""The timing harness (``plan_dashboard_harness.py``): statistics, screen reading, event checks and the
exit codes of ``verify-floors`` and ``verify-acceptance`` on fake reports, event streams and runs.

Standard library plus pytest; the one test that drives a fake dashboard over a pseudo-terminal needs ``pyte``
and is skipped without it. Nothing here touches the real agent home or the real dashboard.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("plan_dashboard_harness", TESTS / "plan_dashboard_harness.py")
h = importlib.util.module_from_spec(_spec)
sys.modules["plan_dashboard_harness"] = h
_spec.loader.exec_module(h)

CONTRACT = json.loads((TESTS / "fixtures" / "plan_dashboard_contract.json").read_text(encoding="utf-8"))
T0 = 1_000_000_000_000


@pytest.fixture(autouse=True)
def keep_events_dir(monkeypatch):
    monkeypatch.setattr(h, "EVENTS_DIR", h.EVENTS_DIR)


def ns(seconds: float) -> int:
    return T0 + int(seconds * 1e9)


# --- statistics ---------------------------------------------------------------------------------------

def test_bootstrap_interval_is_deterministic_and_brackets_the_median():
    values = [1.0, 1.1, 0.9, 1.2, 1.0, 0.95, 1.05]
    assert h.bootstrap_ci(values) == h.bootstrap_ci(values)
    low, high = h.bootstrap_ci(values)
    assert low <= h.median(values) <= high


def test_a_tight_sample_settles_and_a_wide_one_does_not():
    assert h.settled([1.0, 1.01, 0.99, 1.0, 1.0])
    assert not h.settled([1.0, 2.0, 0.5, 3.0, 1.0])
    assert not h.settled([1.0, 1.0, 1.0, 1.0])


def test_the_cap_rule_compares_the_upper_bound_of_the_new_median_at_the_cap():
    old = [1.0, 1.01, 0.99, 1.0, 1.0] * 6
    new = [0.95, 1.4, 0.8, 1.2, 1.1, 0.7] * 5
    assert len(new) == h.MAX_RUNS and h.cap_applies(new, old)
    bound = h.bootstrap_ci(new)[1]
    assert h.within_factor(new, old, 1.1) == (bound <= 1.1 * h.median(old))
    few = new[:10]
    assert not h.cap_applies(few, old)
    assert h.within_factor(few, old, 1.1) == (h.median(few) <= 1.1 * h.median(old))


def test_runs_alternate_old_first_within_a_cell():
    runs = []
    arms = []
    for _ in range(6):
        arm = h.next_arm(runs)
        arms.append(arm)
        runs.append({"arm": arm})
    assert arms == ["old", "new", "old", "new", "old", "new"]


def test_a_cell_needs_five_settled_runs_of_each_arm_or_the_cap():
    steady = [{"arm": "old", "first_rows_s": 1.0}] * 5
    steady_new = [{"arm": "new", "first_rows_s": 0.3}] * 5
    assert h.cell_complete(steady, steady_new, ("first_rows_s",))
    assert not h.cell_complete(steady[:4], steady_new, ("first_rows_s",))
    noisy = [{"arm": "new", "first_rows_s": v} for v in (0.1, 0.9, 0.2, 1.5, 0.3)]
    assert not h.cell_complete(steady, noisy, ("first_rows_s",))
    capped = [{"arm": "new", "first_rows_s": 0.1 + (i % 7) * 0.3} for i in range(h.MAX_RUNS)]
    old_capped = [{"arm": "old", "first_rows_s": 1.0 + (i % 5) * 0.4} for i in range(h.MAX_RUNS)]
    assert h.cell_complete(old_capped, capped, ("first_rows_s",))


@pytest.mark.parametrize("count,band", [(0, "near-zero"), (10, "near-zero"), (11, None), (79, None), (80, "80-100"),
                                        (100, "80-100"), (101, "101-130"), (130, "101-130"), (131, "131-170"),
                                        (170, "131-170"), (171, None), (219, None), (220, "220-280"),
                                        (280, "220-280"), (281, None), (349, None), (350, "350+"), (4000, "350+"),
                                        (None, None)])
def test_a_changed_count_falls_in_exactly_its_band(count, band):
    assert h.band_of(count) == band
    for cell in h.BANDS:
        assert h.in_band(cell, count) == (cell == band)
    assert h.in_band("no-snapshot", count) and h.in_band("damaged", None)


# --- identities, the changed-source count and which task a source belongs to --------------------------

def item(path, kind="state", **over):
    base = {"path": path, "kind": kind, "dev": 1, "ino": 10, "size": 5, "mtime_ns": 1, "ctime_ns": 1}
    return {**base, **over}


def test_changed_since_counts_changed_created_and_deleted_sources():
    recorded = [item("/a"), item("/b"), item("/c")]
    live = [item("/a"), item("/b", mtime_ns=2), item("/d")]
    changed = h.changed_since(recorded, live)
    assert {c["path"] for c in changed if not c.get("deleted")} == {"/b", "/d"}
    assert [c["path"] for c in changed if c.get("deleted")] == ["/c"]
    assert h.changed_since(recorded, recorded) == []


def test_an_identity_is_stat_fields_named_as_the_contract_names_them(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}")
    got = h.identity(f, "state")
    contract_keys = set(CONTRACT["snapshot"]["schema"]["sources"][0])
    assert set(got) == contract_keys
    assert got["ino"] == os.stat(f).st_ino and h.identity(tmp_path / "gone", "state") is None


def test_sources_map_to_tasks_by_the_contract_rule():
    bindings = {"harness-1": ["task-a", "task-b"]}
    assert h.tasks_of_source("/x/task-a.json", "state", bindings) == {"task-a"}
    assert h.tasks_of_source("/x/harness-1.json", "binding", bindings) == {"task-a", "task-b"}
    assert h.tasks_of_source("/x/harness-1.jsonl", "transcript", bindings) == {"task-a", "task-b", "harness-1"}
    assert h.tasks_of_source("/ledger", "ledger", bindings) is None
    assert "driven list" in CONTRACT["source_to_task"]["binding"]


def test_bindings_are_read_from_the_driven_list(tmp_path):
    (tmp_path / "h1.json").write_text(json.dumps({"driven": [{"session": "t1", "ts": 1}, {"session": "t2"}]}))
    (tmp_path / "broken.json").write_text("{")
    assert h.bindings_map(tmp_path) == {"h1": ["t1", "t2"]}


# --- the screen ----------------------------------------------------------------------------------------

def screen(rows, header="task  wait  session  live  node"):
    lines = [" plan dashboard", " Tasks  Detail  Chat", f" {header}"]
    wait_at, session_at, live_at = (header.index(w) + 1 for w in ("wait", "session", "live"))
    for task, wait, session, live in rows:
        line = f" {task}".ljust(wait_at) + wait.ljust(session_at - wait_at) + session.ljust(live_at - session_at) + live
        lines.append(line.ljust(60))
    return lines


def test_rows_are_read_by_header_offsets_with_the_wait_cell_between_headers():
    parsed = h.parse_rows(screen([("alpha", "~", "sess-0001", "active"), ("beta", "", "sess-0002", "idle"),
                                  ("gamma", "Q", "sess-0003", "stale")]))
    assert parsed == {"rows": {"sess-0001": "~", "sess-0002": "", "sess-0003": "Q"}}


def test_there_are_no_rows_before_the_header_is_on_the_screen():
    assert h.parse_rows([" plan dashboard", " Tasks  Detail  Chat"]) is None
    assert h.parse_rows(screen([])) == {"rows": {}}


def test_screen_summary_stamps_first_rows_and_the_two_confirmations():
    frames = [(0.5, None), (1.0, {"rows": {"s1": "~", "s2": "~"}}), (2.0, {"rows": {"s1": "", "s2": "~"}}),
              (3.0, {"rows": {"s1": "", "s2": "?"}}), (4.0, {"rows": {"s1": "", "s2": "Q"}})]
    out = h.screen_summary(frames)
    assert (out["first_rows_s"], out["first_screen_rows"], out["old_marking_at_first_rows"]) == (1.0, 2, True)
    assert out["rows_confirmed_s"] == 3.0
    assert out["marks_confirmed_s"] == 4.0


def test_a_first_screen_without_the_old_marking_is_reported_so():
    out = h.screen_summary([(1.0, {"rows": {"s1": "", "s2": "~"}})])
    assert out["old_marking_at_first_rows"] is False


def test_the_old_marking_text_is_the_contracts():
    assert h.OLD_MARK == CONTRACT["screen"]["old_marking"]["text"]
    assert h.UNREAD_MARK == CONTRACT["screen"]["unread_marking"]["text"]
    assert list(h.HEADER_WORDS) == CONTRACT["screen"]["columns_header"]


# --- event streams: payload shapes ---------------------------------------------------------------------

def ev(name, at, **payload):
    return {"ev": name, "t_ns": ns(at), **payload}


def new_stream(applied_end, applied_mark, task="task-a"):
    return [ev("cycle_start", 0.0, cycle=1), ev("rows_painted", 0.2, from_snapshot=True, rows=1),
            ev("mark_painted", 0.25, task=task), ev("mark_confirmed", 0.5, task=task, applied=applied_mark),
            ev("first_screen_confirmed", 0.6), ev("last_old_cleared", 0.7),
            ev("live_check_end", 0.8, applied=applied_end), ev("cycle_end", 0.9, cycle=1, applied=applied_end)]


def test_the_stream_summary_reads_each_moment_and_the_snapshot_flag():
    changes = {"/s/task-a.json": {"task-a"}}
    out = h.event_summary(new_stream(["/s/task-a.json"], ["/s/task-a.json"]), T0, arm="new",
                          first_screen_ids={"task-a"}, changes=changes)
    assert out["rows_painted_s"] == pytest.approx(0.2) and out["from_snapshot"] is True
    assert out["first_screen_confirmed_s"] == pytest.approx(0.6)
    assert out["last_old_cleared_s"] == pytest.approx(0.7) and out["live_check_end_s"] == pytest.approx(0.8)
    assert out["mark_confirmed_max_s"] == pytest.approx(0.5) and out["marks_missing"] == 0
    assert out["applied_ok"] is True


def test_an_applied_list_that_misses_a_changed_source_of_the_task_fails_the_run():
    changes = {"/s/task-a.json": {"task-a"}, "/s/task-a-transcript.jsonl": {"task-a"}}
    both = ["/s/task-a.json", "/s/task-a-transcript.jsonl"]
    bad_mark = h.event_summary(new_stream(both, ["/s/task-a.json"]), T0, arm="new", first_screen_ids={"task-a"},
                               changes=changes)
    bad_end = h.event_summary(new_stream(["/s/task-a.json"], both), T0, arm="new", first_screen_ids={"task-a"},
                              changes=changes)
    good = h.event_summary(new_stream(both, both), T0, arm="new", first_screen_ids={"task-a"}, changes=changes)
    assert (bad_mark["applied_ok"], bad_end["applied_ok"], good["applied_ok"]) == (False, False, True)


def test_a_source_of_another_task_need_not_be_in_this_tasks_applied_list():
    changes = {"/s/task-a.json": {"task-a"}, "/s/task-b.json": {"task-b"}, "/ledger": None}
    stream = new_stream(["/s/task-a.json", "/s/task-b.json", "/ledger"], ["/s/task-a.json", "/ledger"])
    out = h.event_summary(stream, T0, arm="new", first_screen_ids={"task-a"}, changes=changes)
    assert out["applied_ok"] is True


def test_an_old_code_stream_is_not_checked_for_applied_lists():
    stream = new_stream("all", "all")
    out = h.event_summary(stream, T0, arm="old", first_screen_ids={"task-a"}, changes={"/x": {"task-a"}})
    assert out["applied_ok"] is None


def test_a_shown_session_id_is_matched_to_its_confirmed_task_by_prefix():
    stream = new_stream("all", "all", task="6c549bb8-long-session-id")
    out = h.event_summary(stream, T0, arm="old", first_screen_ids={"6c549bb8", "other"}, changes={})
    assert out["marks_missing"] == 1


def test_r2_is_the_latest_of_screen_stream_and_mark_moments_or_nothing():
    run = {"first_screen_confirmed_s": 0.6, "marks_confirmed_s": 0.8, "mark_confirmed_max_s": 0.75, "marks_missing": 0}
    assert h.r2_value(run) == 0.8
    assert h.r2_value({**run, "marks_confirmed_s": None}) is None
    assert h.r2_value({**run, "marks_missing": 1}) is None


# --- cycles (R3) ---------------------------------------------------------------------------------------

def test_cycle_records_pair_starts_with_ends_and_keep_applied():
    events = [ev("cycle_start", 0.0, cycle=1), ev("cycle_end", 0.3, cycle=1, applied=["/a"]),
              ev("cycle_start", 2.0, cycle=2), ev("cycle_end", 2.1, cycle=2, applied=["/a", "/b"]),
              ev("cycle_start", 4.0, cycle=3)]
    cycles = h.cycle_records(events, T0)
    assert [c["cycle"] for c in cycles] == [1, 2]
    assert cycles[1]["duration_s"] == pytest.approx(0.1) and cycles[1]["applied"] == ["/a", "/b"]


def test_cycle_coverage_requires_every_due_source_in_the_applied_set():
    cycles = [{"cycle": 2, "applied": ["/a"], "duration_s": 0.1, "start_s": 2.0},
              {"cycle": 3, "applied": ["/a", "/b"], "duration_s": 0.1, "start_s": 4.0},
              {"cycle": 4, "applied": "all", "duration_s": 0.1, "start_s": 6.0}]
    covered = h.check_cycle_coverage(cycles, {2: ["/a", "/b"], 3: ["/a", "/b"], 4: ["/z"]})
    assert [c["covered"] for c in covered] == [False, True, True]


def test_a_source_modified_after_the_cycle_start_stamp_is_not_yet_due(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("1")
    watcher = h.StatWatcher([str(f)])
    f.write_text("22")
    cycle_start_wall = os.stat(f).st_mtime_ns - 1_000_000
    assert watcher.poll(cycle_start_wall) == []
    assert watcher.poll(os.stat(f).st_mtime_ns + 1_000_000) == [str(f)]
    assert watcher.poll(os.stat(f).st_mtime_ns + 1_000_000) == []


def test_an_r3_run_takes_twenty_cycles_after_the_first_and_checks_coverage():
    cycles = [{"cycle": n, "duration_s": 0.1 if n % 2 else 0.2, "covered": True, "start_s": n, "applied": []}
              for n in range(1, 30)]
    run = h.r3_run_record("new", cycles)
    assert len(run["cycles"]) == h.R3_CYCLES and run["cycles"][0]["cycle"] == 2 and run["covered_all"]
    cycles[10]["covered"] = False
    assert not h.r3_run_record("new", cycles)["covered_all"]
    assert not h.r3_run_record("new", cycles[:10])["covered_all"]


# --- the changed count and the R3 due set follow the contract's rule, not the 2026-11-06 trace -------------

DAY_NS = 24 * 3600 * 10**9


def fake_home(tmp_path, monkeypatch):
    """An agent home with state, binding and transcript folders, and a ``HOME`` holding only the cost ledger."""
    home = tmp_path / "agent-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    for folder in (home / "agentctl" / "state", home / "agentctl" / "engine-session-bindings",
                   home / "projects" / "-proj", h.cost_ledger().parent):
        folder.mkdir(parents=True)
    h.cost_ledger().write_text("{}\n")
    return home


def put(path: Path, text: str = "x\n", age_ns: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if age_ns:
        at = time.time_ns() - age_ns
        os.utime(path, ns=(at, at))
    return path


class FakeTrace:
    def __init__(self, frames=()):
        self.frames, self.events, self.t0_ns, self.wall_minus_mono_ns = list(frames), [], 0, 0


def test_the_changed_count_follows_the_contract_not_the_trace(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    state, binding = home / "agentctl" / "state", home / "agentctl" / "engine-session-bindings"
    proj = home / "projects" / "-proj"
    put(state / "task-a.json", age_ns=3 * DAY_NS)          # a row of the copy, outside the window
    put(state / "task-b.json", age_ns=3 * DAY_NS)          # no row, outside the window: not shown
    put(state / "task-c.json")                             # no row, inside the window: shown, no session yet
    put(binding / "h1.json", json.dumps({"driven": [{"session": "task-a"}]}), age_ns=3 * DAY_NS)
    shown_a = put(proj / "task-a.jsonl")
    nested_h1 = put(proj / "nested" / "h1.jsonl")          # recorded, but not in the */*.jsonl listing
    others = [put(proj / f"other-{n}.jsonl") for n in range(20)]
    trace_population = [str(p) for p in proj.glob("*.jsonl")]
    recorded = [h.identity(p, kind) for p, kind in (
        (state / "task-a.json", "state"), (state / "task-b.json", "state"), (state / "task-c.json", "state"),
        (binding / "h1.json", "binding"), (h.cost_ledger(), "ledger"),
        (shown_a, "transcript"), (nested_h1, "transcript"))]
    snapshot = tmp_path / "copy-0001.json"
    snapshot.write_text(json.dumps({"version": 1, "sources": recorded, "sections": {
        "live:24:": {"snapshot": {"tasks": [{"session_id": "task-a"}]}}}}))
    copy_ = {"file": str(snapshot), "taken_at": h.iso_now(), "sources": recorded}

    runner = h.RealRunner(tmp_path / "state", tmp_path / "old", tmp_path / "new", CONTRACT, trace_population)
    camp = h.Campaign(tmp_path / "state", tmp_path / "old", tmp_path / "new", save_every=3600, until=1e12,
                      watch=[], runner=runner, clock=lambda: 0.0, log=lambda m: None)
    camp.copies = [copy_]
    seen = {}
    monkeypatch.setattr(h, "run_pty", lambda *a, **k: FakeTrace())
    monkeypatch.setattr(h, "summarize_run", lambda trace, **k: seen.update(k) or {})

    def counted():
        found = camp.pick_copy("near-zero", runner("live", None))
        runner("run", {"cell": "near-zero", "arm": "new", "copy": copy_, "live": None})
        assert found is not None and found[1] == seen["changed_count"]
        return seen["changed_count"], set(seen["changes"])

    for p in others[:10]:
        p.write_text("written after the copy\n")
    put(proj / "other-new.jsonl")
    put(proj / "task-b.jsonl")
    assert counted() == (0, set()), "transcripts of no shown task are no source of any row"

    shown_a.write_text("x\nmore\n")
    assert counted() == (1, {str(shown_a)})

    new_session = put(proj / "task-c.jsonl")
    assert counted() == (2, {str(shown_a), str(new_session)})

    (state / "task-b.json").write_text("changed\n")
    four = {str(shown_a), str(new_session), str(state / "task-b.json"), str(proj / "task-b.jsonl")}
    assert counted() == (4, four)

    nested_h1.unlink()                                     # a recorded session gone counts, but is no row's source
    assert counted() == (5, four)
    h.cost_ledger().write_text("{}\n{}\n")
    assert counted()[0] == 6


def test_r3_due_sources_include_sessions_created_after_the_trace(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    state, proj = home / "agentctl" / "state", home / "projects" / "-proj"
    put(state / "aaaa1111-full.json", age_ns=3 * DAY_NS)   # on the screen, outside the window
    put(state / "bbbb2222.json")                           # inside the window, no row on the screen
    put(state / "cccc3333.json", age_ns=3 * DAY_NS)        # neither: not shown
    late_state = put(state / "dddd4444.json", age_ns=3 * DAY_NS)   # touched only after the cycle starts
    put(home / "agentctl" / "engine-session-bindings" / "hsess.json",
        json.dumps({"driven": [{"session": "bbbb2222"}]}), age_ns=3 * DAY_NS)
    driven = put(proj / "hsess.jsonl", age_ns=DAY_NS)
    unshown = put(proj / "cccc3333.jsonl", age_ns=DAY_NS)
    late_task = put(proj / "dddd4444.jsonl", age_ns=DAY_NS)
    trace_population = [str(driven), str(unshown)]
    new_session = proj / "aaaa1111-full.jsonl"
    due = {}

    def fake_run_pty(argv, *, on_event=None, **kw):
        put(new_session)
        driven.write_text("x\nmore\n")
        unshown.write_text("x\nmore\n")
        late_task.write_text("x\nmore\n")
        cycle_ns = time.time_ns() + 5 * 10**9
        late_state.write_text("changed\n")
        os.utime(late_state, ns=(cycle_ns + 10**9, cycle_ns + 10**9))
        trace = FakeTrace(frames=[(0.1, {"rows": {"aaaa1111": "~"}})])
        on_event(trace, {"ev": "cycle_start", "cycle": 2, "t_ns": cycle_ns})
        return trace

    monkeypatch.setattr(h, "run_pty", fake_run_pty)
    monkeypatch.setattr(h, "check_cycle_coverage", lambda cycles, by_cycle: due.update(by_cycle) or [])
    runner = h.RealRunner(tmp_path / "state", tmp_path / "old", tmp_path / "new", CONTRACT, trace_population)
    runner("r3", {"arm": "new"})
    assert set(due[2]) == {str(new_session), str(driven)}


# --- floors and acceptance: exit codes on fake reports -------------------------------------------------

def good_floors():
    return {"floors": {"R1": {"median_s": 0.3}, "R2": {"median_s": 0.5}, "R3": {"median_s": 0.1}},
            "changed_count": 150, "instrument_sha256": h.instrument_sha256(), "base_commit": "abc",
            "base_tree_sha256": "def", "mtime_resolution": {"value_ns": 1000}, "stream_screen_max_ms": 12.0,
            "read_set": {"state": 10}}


SOURCES = {"kinds": {"state": {"files": True}}}


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def verify_floors(tmp_path, report, *extra):
    sources = write(tmp_path, "sources.json", SOURCES)
    rep = write(tmp_path, "floors.json", report)
    return h.main(["verify-floors", "--report", str(rep), "--sources", str(sources), *extra])


def test_verify_floors_passes_a_good_report(tmp_path):
    assert verify_floors(tmp_path, good_floors()) == 0


@pytest.mark.parametrize("limit", ["R1=0.001", "R2=0.001", "R3=0.001"])
def test_a_limit_below_a_floor_is_a_miss_with_exit_1(tmp_path, limit):
    assert verify_floors(tmp_path, good_floors(), "--limit", limit) == 1


def test_a_limit_can_only_be_tightened_never_loosened(tmp_path):
    report = good_floors()
    report["floors"]["R1"]["median_s"] = 0.8
    assert verify_floors(tmp_path, report, "--limit", "R1=100") == 1


def test_a_floor_at_the_limit_is_a_miss(tmp_path):
    report = good_floors()
    report["floors"]["R3"]["median_s"] = h.DEFAULT_LIMITS["R3"]
    assert verify_floors(tmp_path, report) == 1


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("mtime_resolution"),
    lambda r: r.update(changed_count=99),
    lambda r: r.update(instrument_sha256="stale"),
    lambda r: r["floors"].pop("R2"),
    lambda r: r.pop("base_commit"),
    lambda r: r.update(mtime_resolution={"value_ns": 0}),
])
def test_a_report_that_cannot_be_judged_is_exit_2(tmp_path, mutate):
    report = good_floors()
    mutate(report)
    assert verify_floors(tmp_path, report) == 2


def test_an_unreadable_or_malformed_report_is_exit_2(tmp_path):
    sources = write(tmp_path, "sources.json", SOURCES)
    assert h.main(["verify-floors", "--report", str(tmp_path / "none.json"), "--sources", str(sources)]) == 2
    assert verify_floors(tmp_path, ["not", "a", "report"]) == 2


def test_a_stream_to_screen_gap_over_20_ms_is_a_miss(tmp_path):
    report = good_floors()
    report["stream_screen_max_ms"] = 25.0
    assert verify_floors(tmp_path, report) == 1
    report["stream_screen_max_ms"] = None
    assert verify_floors(tmp_path, report) == 1


def test_a_read_set_kind_missing_from_the_sources_fixture_is_a_miss(tmp_path):
    report = good_floors()
    report["read_set"] = {"state": 3, "mystery": 1}
    assert verify_floors(tmp_path, report) == 1


def test_an_empty_sources_fixture_is_exit_2(tmp_path):
    rep = write(tmp_path, "floors.json", good_floors())
    sources = write(tmp_path, "sources.json", {"kinds": {}})
    assert h.main(["verify-floors", "--report", str(rep), "--sources", str(sources)]) == 2


def test_a_bad_limit_flag_is_refused():
    with pytest.raises(SystemExit):
        h.parse_limits(["R9=1"])


def test_the_default_limits_are_the_requirements():
    assert h.DEFAULT_LIMITS == {"R1": 0.70, "R2": 0.95, "R3": 0.25, "R6": 1.1}
    assert h.limits_with({"R1": 100.0})["R1"] == 0.70


def git_run(tree, *args):
    return subprocess.run(["git", "-C", str(tree), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                           *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def base_tree(tmp_path):
    tree = tmp_path / "base"
    (tree / "scripts" / "plan_dashboard").mkdir(parents=True)
    (tree / "scripts" / "plan_dashboard" / "m.py").write_text("X = 1\n")
    (tree / "scripts" / "plan-dashboard.py").write_text("print(1)\n")
    git_run(tree, "init", "-q")
    git_run(tree, "add", "-A")
    git_run(tree, "commit", "-q", "-m", "base")
    return tree


def floors_exit(tmp_path, base, census):
    return h.main(["--state-dir", str(tmp_path / "state"), "floors", "--census", str(census), "--base", str(base),
                   "--out", str(tmp_path / "floors.json"), "--sources", str(write(tmp_path, "s.json", SOURCES))])


def test_floors_refuses_a_base_that_is_not_a_clean_commit(tmp_path, base_tree):
    census = write(tmp_path, "census.json", {"sources": []})
    (base_tree / "scripts" / "plan_dashboard" / "m.py").write_text("X = 2\n")
    assert floors_exit(tmp_path, base_tree, census) == 2
    assert floors_exit(tmp_path, tmp_path / "missing", census) == 2


def test_floors_exits_2_while_fewer_than_100_sources_changed_since_the_census(tmp_path, base_tree, monkeypatch):
    home = tmp_path / "home"
    (home / "agentctl" / "state").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    for n in range(5):
        (home / "agentctl" / "state" / f"s{n}.json").write_text("{}")
    census = write(tmp_path, "census.json", {"sources": h.live_sources()})
    assert floors_exit(tmp_path, base_tree, census) == 2
    for n in range(5):
        (home / "agentctl" / "state" / f"s{n}.json").write_text('{"changed": true}')
    assert floors_exit(tmp_path, base_tree, census) == 2
    assert not (tmp_path / "floors.json").exists()


def test_the_tree_hash_changes_with_any_dashboard_file(base_tree):
    before = h.tree_sha256(base_tree)
    assert before == h.committed_tree_sha256(base_tree, "HEAD")
    (base_tree / "scripts" / "plan_dashboard" / "m.py").write_text("X = 3\n")
    assert h.tree_sha256(base_tree) != before


def test_mtime_resolution_is_measured_in_the_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    out = h.mtime_resolution(tmp_path / "state")
    assert isinstance(out["value_ns"], int) and out["value_ns"] > 0
    assert list((tmp_path / "state").iterdir()) == []


# --- acceptance ----------------------------------------------------------------------------------------

def run_record(arm, changed=90, first_rows=0.3, **over):
    base = {"arm": arm, "changed_count": changed, "first_rows_s": first_rows, "old_marking_at_first_rows": True,
            "rows_confirmed_s": 0.8, "marks_confirmed_s": 0.9, "first_screen_confirmed_s": 0.85,
            "mark_confirmed_max_s": 0.88, "marks_missing": 0, "applied_ok": True, "last_old_cleared_s": 1.0,
            "live_check_end_s": 1.1}
    if arm == "old":
        base.update(first_rows_s=2.0, rows_confirmed_s=2.0, marks_confirmed_s=9.0, applied_ok=None,
                    old_marking_at_first_rows=False, last_old_cleared_s=None, live_check_end_s=None,
                    first_screen_confirmed_s=None, mark_confirmed_max_s=None)
    return {**base, **over}


def good_cell(name):
    changed = {"near-zero": 3, "80-100": 90, "101-130": 110, "131-170": 150, "220-280": 250, "350+": 400}.get(name)
    runs = []
    for _ in range(h.MIN_RUNS):
        runs += [run_record("old", changed), run_record("new", changed)]
    return {"runs": runs}


def r6_cell(name):
    cell = good_cell(name)
    for run in cell["runs"]:
        if run["arm"] == "old":
            run.update(first_rows_s=1.0, rows_confirmed_s=1.0, marks_confirmed_s=1.0)
        else:
            run.update(rows_confirmed_s=1.0, marks_confirmed_s=1.0, mark_confirmed_max_s=1.0, last_old_cleared_s=1.0,
                       first_screen_confirmed_s=0.9, live_check_end_s=1.05)
    return cell


def good_report(tmp_path, tree, old):
    cells = {}
    for name in h.REQUIRED_CELLS:
        cells[name] = r6_cell(name) if name in h.R6_CELLS else good_cell(name)
    runs = [{"arm": a, "median_s": 0.1, "covered_all": True, "cycles": []} for a in ("old", "new") * h.R3_MIN_RUNS]
    k1 = {"old": [{"arm": "old", "exec_kinds": ["tmux", "git"], "children_between_cycles": [2], "created_files": []}
                  for _ in range(3)],
          "new": [{"arm": "new", "exec_kinds": ["tmux"], "children_between_cycles": [1], "created_files": []}
                  for _ in range(3)],
          "added_files": ["scripts/plan_dashboard/snapshot.py"]}
    return {"instrument_sha256": h.instrument_sha256(), "new_tree_sha256": h.tree_sha256(tree),
            "old_tree_sha256": h.tree_sha256(old), "cells": cells, "r3": {"runs": runs}, "k1": k1}


@pytest.fixture
def trees(tmp_path):
    new, old = tmp_path / "new", tmp_path / "old"
    for tree, text in ((new, "X = 2\n"), (old, "X = 1\n")):
        (tree / "scripts" / "plan_dashboard").mkdir(parents=True)
        (tree / "scripts" / "plan_dashboard" / "m.py").write_text(text)
        (tree / "scripts" / "plan-dashboard.py").write_text("print(1)\n")
    return new, old


def acceptance(tmp_path, report, trees, *extra):
    new, old = trees
    rep = write(tmp_path, "acceptance.json", report)
    flo = write(tmp_path, "floors.json", {"base_tree_sha256": h.tree_sha256(old)})
    return h.main(["verify-acceptance", "--report", str(rep), "--floors", str(flo), "--tree", str(new),
                   "--old-tree", str(old), *extra])


def test_a_good_acceptance_report_passes(tmp_path, trees):
    assert acceptance(tmp_path, good_report(tmp_path, *trees), trees) == 0


def test_a_tightened_r1_limit_turns_the_same_report_into_a_miss(tmp_path, trees):
    assert acceptance(tmp_path, good_report(tmp_path, *trees), trees, "--limit", "R1=0.001") == 1


def test_a_tightened_r3_limit_is_a_miss(tmp_path, trees):
    assert acceptance(tmp_path, good_report(tmp_path, *trees), trees, "--limit", "R3=0.001") == 1


def test_a_report_for_another_tree_is_exit_2(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["new_tree_sha256"] = "0" * 64
    assert acceptance(tmp_path, report, trees) == 2


def test_a_run_outside_its_band_makes_the_report_unjudgeable(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["cells"]["80-100"]["runs"][0]["changed_count"] = 500
    assert acceptance(tmp_path, report, trees) == 2


def test_a_missing_required_cell_is_a_miss_but_350_plus_is_optional(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    del report["cells"]["131-170"]
    assert acceptance(tmp_path, report, trees) == 1
    report = good_report(tmp_path, *trees)
    assert "350+" not in report["cells"]
    assert acceptance(tmp_path, report, trees) == 0


def test_a_first_screen_without_the_old_marking_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["cells"]["near-zero"]["runs"][1]["old_marking_at_first_rows"] = False
    assert acceptance(tmp_path, report, trees) == 1


def test_an_applied_set_that_missed_a_source_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["cells"]["80-100"]["runs"][1]["applied_ok"] = False
    assert acceptance(tmp_path, report, trees) == 1


def test_an_r3_run_over_the_limit_or_missing_a_source_is_a_miss(tmp_path, trees):
    slow = good_report(tmp_path, *trees)
    slow["r3"]["runs"][1]["median_s"] = 0.30
    uncovered = good_report(tmp_path, *trees)
    uncovered["r3"]["runs"][1]["covered_all"] = False
    assert acceptance(tmp_path, slow, trees) == 1
    assert acceptance(tmp_path, uncovered, trees) == 1


def test_a_new_start_of_a_command_the_old_code_does_not_start_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["k1"]["new"][0]["exec_kinds"].append("curl")
    assert acceptance(tmp_path, report, trees) == 1


def test_more_child_processes_between_cycles_than_the_old_code_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["k1"]["new"][0]["children_between_cycles"] = [9]
    assert acceptance(tmp_path, report, trees) == 1


def test_files_created_by_the_new_code_but_not_the_old_are_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["k1"]["new"][0]["created_files"] = ["<home>/.cache/stray"]
    assert acceptance(tmp_path, report, trees) == 1


def test_a_service_or_timer_in_the_diff_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["k1"]["added_files"] = ["scripts/plan_dashboard/x.timer"]
    assert acceptance(tmp_path, report, trees) == 1


def test_an_r6_metric_over_the_factor_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    for run in report["cells"]["101-130"]["runs"]:
        if run["arm"] == "new":
            run["rows_confirmed_s"] = 1.5
    assert acceptance(tmp_path, report, trees) == 1


# --- scheduling of the campaign -----------------------------------------------------------------------

class Script:
    """A runner that answers the campaign's requests from a canned world."""

    def __init__(self, live_changed=0, run_changed=None):
        self.calls = []
        self.copy_count = 0
        self.live_changed = live_changed
        self.run_changed = run_changed

    def __call__(self, what, arg):
        self.calls.append((what, arg))
        if what == "save":
            self.copy_count += 1
            return {"file": f"/c{self.copy_count}", "sources": [item(f"/p{n}") for n in range(300)], "taken_at": "t"}
        if what == "live":
            return [item(f"/p{n}", mtime_ns=2 if n < self.live_changed else 1) for n in range(300)]
        if what == "run":
            return run_record(arg["arm"], self.run_changed)
        return {"arm": arg["arm"], "median_s": 0.1, "covered_all": True, "cycles": []}


def campaign(tmp_path, runner, old=None, new=None, until=1e12):
    return h.Campaign(tmp_path, old or tmp_path / "old", new or tmp_path / "new", save_every=3600, until=until,
                      watch=[], runner=runner, clock=lambda: 0.0, log=lambda m: None)


def finished_cell(camp, name, changed):
    camp.runs[name] = [run_record(arm, changed) for _ in range(h.MIN_RUNS) for arm in ("old", "new")]


def test_the_campaign_alternates_old_and_new_within_a_cell(tmp_path):
    runner = Script()
    camp = campaign(tmp_path, runner)
    assert [camp.step() for _ in range(4)] == [True] * 4
    assert [r["arm"] for r in camp.runs["no-snapshot"]] == ["old", "new", "old", "new"]
    assert [c[0] for c in runner.calls].count("save") == 1


def test_a_band_cell_runs_on_the_saved_copy_whose_count_is_in_its_band(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    runner = Script(live_changed=95, run_changed=95)
    monkeypatch.setattr(h, "live_sources", lambda: runner("live", None))
    camp = campaign(tmp_path, runner)
    for name, changed in (("no-snapshot", None), ("damaged", None), ("near-zero", 3)):
        finished_cell(camp, name, changed)
    assert camp.step() is True
    (job,) = [arg for what, arg in runner.calls if what == "run"]
    assert (job["cell"], job["arm"], job["copy"]["file"]) == ("80-100", "old", "/c1")
    assert len(camp.runs["80-100"]) == 1 and camp.largest == 95


def test_a_run_whose_count_misses_its_band_is_dropped_and_counted(tmp_path):
    camp = campaign(tmp_path, Script(run_changed=999))
    for name in ("no-snapshot", "damaged"):
        finished_cell(camp, name, None)
    assert camp.step() is True
    assert camp.dropped["near-zero"] == 1 and camp.runs["near-zero"] == []


def test_the_campaign_report_names_instrument_and_both_tree_hashes(tmp_path, trees):
    new, old = trees
    camp = campaign(tmp_path, Script(), old=old, new=new, until=0)
    report = camp.run()
    assert report["instrument_sha256"] == h.instrument_sha256()
    assert (report["new_tree_sha256"], report["old_tree_sha256"]) == (h.tree_sha256(new), h.tree_sha256(old))
    assert set(h.REQUIRED_CELLS) <= set(report["cells"])


# --- the harness stays outside the dashboard -----------------------------------------------------------

def test_the_harness_does_not_import_the_dashboard_or_textual():
    names = set()
    for node in ast.walk(ast.parse((TESTS / "plan_dashboard_harness.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    assert not names & {"plan_dashboard", "textual", "agentctl", "lib"}


def test_the_harness_state_directory_is_not_under_the_agent_home():
    home = h.agent_home()
    assert home not in h.STATE_DIR_DEFAULT.parents and h.STATE_DIR_DEFAULT != home


def test_a_tmux_listing_is_taken_from_the_recorded_command_and_from_nothing_else():
    recorded = {"kinds": {"tmux_listing:tmux list-windows": {"files": False, "command": ["tmux", "list-windows"]},
                          "state": {"files": True}}}
    assert h.tmux_command(recorded) == ["tmux", "list-windows"]
    assert h.tmux_command({"kinds": {"state": {"files": True}}}) is None
    assert h.tmux_command({"kinds": {"tmux_listing:x": {"files": False}}}) is None


def test_the_sources_fixture_either_records_the_tmux_command_or_says_none_ran():
    fixture = json.loads((TESTS / "fixtures" / "plan_dashboard_sources.json").read_text(encoding="utf-8"))
    commands = h.tmux_command(fixture)
    if commands:
        assert commands[0] == "tmux"
        return
    none_ran = fixture["non_file_sources"]["tmux_listing"]
    assert none_ran["executed"] is False
    assert not any(kind.split()[0] == "tmux" for kind in fixture["exec_kinds"])
    assert "locator_listing" in fixture["kinds"]


# --- the pseudo-terminal runner, against a fake dashboard ---------------------------------------------

FAKE = r"""
import json, os, sys, time
fifo = sys.argv[sys.argv.index("--events") + 1]
fd = os.open(fifo, os.O_WRONLY)
def emit(ev, **p):
    os.write(fd, (json.dumps({"ev": ev, "t_ns": time.monotonic_ns(), **p}) + "\n").encode())
emit("cycle_start", cycle=1)
sys.stdout.write("\x1b[2J\x1b[H plan dashboard\n task  wait  session   live\n")
sys.stdout.write(" alpha ~     sess-0001 active\n")
sys.stdout.flush()
emit("rows_painted", from_snapshot=False, rows=1)
time.sleep(0.4)
sys.stdout.write("\x1b[3;1H alpha       sess-0001 active\n")
sys.stdout.flush()
emit("mark_confirmed", task="sess-0001", applied="all")
time.sleep(0.3)
"""


def test_a_fake_dashboard_over_the_terminal_gives_rows_marks_and_events(tmp_path, monkeypatch):
    pytest.importorskip("pyte")
    monkeypatch.setattr(h, "EVENTS_DIR", tmp_path / "events")
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    argv = [sys.executable, str(script), "--events", h.EVENTS_SLOT]
    trace = h.run_pty(argv, cwd=tmp_path, env=dict(os.environ), events=True, max_s=20)
    summary = h.screen_summary(trace.frames)
    assert summary["first_rows_s"] is not None and summary["old_marking_at_first_rows"] is True
    assert summary["marks_confirmed_s"] > summary["first_rows_s"]
    assert [e["ev"] for e in trace.events] == ["cycle_start", "rows_painted", "mark_confirmed"]
    assert trace.exit_code == 0 and trace.bad_lines == []
    assert list((tmp_path / "events").iterdir()) == []
    stamped = [trace.seconds(e["t_ns"]) for e in trace.events]
    assert stamped == sorted(stamped) and stamped[0] >= 0


def test_the_deep_copy_of_a_report_is_not_what_the_judge_mutates(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    before = copy.deepcopy(report)
    h.judge_report(report, h.limits_with({}))
    assert report == before
