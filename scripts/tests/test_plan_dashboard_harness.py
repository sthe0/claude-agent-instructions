"""The timing harness (``plan_dashboard_harness.py``): statistics, screen reading, event checks and the
exit codes of ``verify-floors`` and ``verify-acceptance`` on fake reports, event streams and runs.

Standard library plus pytest; the one test that drives a fake dashboard over a pseudo-terminal needs ``pyte``
and is skipped without it. Nothing here touches the real agent home or the real dashboard.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
h = sys.modules.get("plan_dashboard_harness")   # the one a mutation is installed on, when one is
if h is None:
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
                                        (170, "131-170"), (171, None), (224, None), (225, "225-275"),
                                        (275, "225-275"), (276, None), (359, None), (360, "360-440"), (440, "360-440"),
                                        (441, None), (4000, None), (None, None)])
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
            ev("mark_painted", 0.25, task=task), ev("startup_applied", 0.45, applied=applied_end),
            ev("mark_confirmed", 0.5, task=task, applied=applied_mark),
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


def cyc(number, start, duration, applied=(), gap=None, found_by=None):
    return {"cycle": number, "start_ns": ns(start), "end_ns": ns(start + duration), "start_s": start,
            "duration_s": duration, "gap_s": gap, "applied": applied if applied == "all" else list(applied),
            "found_by": found_by}


def due_at(path, at):
    return {"path": path, "change_ns": ns(at)}


def test_cycle_coverage_requires_every_due_source_in_the_applied_set():
    cycles = [cyc(2, 2.0, 0.1, ["/a"]), cyc(3, 4.0, 0.1, ["/a", "/b"]), cyc(4, 6.0, 0.1, "all")]
    due = {2: [due_at("/a", 1.0), due_at("/b", 1.0)], 3: [due_at("/a", 3.0), due_at("/b", 3.0)], 4: [due_at("/z", 5.0)]}
    covered = h.check_cycle_coverage(cycles, due, 0)
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


def stat_cycles(count, duration=0.1, gap=2.0):
    step = duration + gap
    return [cyc(n, (n - 1) * step, duration, gap=None if n == 1 else gap) for n in range(1, count + 1)]


def stat_run(cycles, arm="old", path="stat", events=(), sweeps=(), due=None, stamps=None, **kw):
    covered = h.check_cycle_coverage(cycles, due or {}, 0)
    return h.r3_run_record(arm, path, covered, list(events), list(sweeps), stamps=stamps or {}, **kw)


def test_an_r3_run_takes_twenty_cycles_after_the_first_and_checks_coverage():
    cycles = stat_cycles(29)
    run = stat_run(cycles)
    assert len(run["cycles"]) == h.R3_CYCLES and run["cycles"][0]["cycle"] == 2 and run["covered_all"]
    assert run["counted"] is True
    due = {12: [due_at("/a", 0.0)]}
    assert not stat_run(cycles, due=due)["covered_all"]
    short = stat_run(cycles[:10])
    assert short["counted"] is False and short["void_reason"] == "short"


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
    late_state = put(state / "dddd4444.json", age_ns=3 * DAY_NS)   # touched only after the watcher started
    put(home / "agentctl" / "engine-session-bindings" / "hsess.json",
        json.dumps({"driven": [{"session": "bbbb2222"}]}), age_ns=3 * DAY_NS)
    driven = put(proj / "hsess.jsonl", age_ns=DAY_NS)
    unshown = put(proj / "cccc3333.jsonl", age_ns=DAY_NS)
    late_task = put(proj / "dddd4444.jsonl", age_ns=DAY_NS)
    new_session = proj / "aaaa1111-full.jsonl"
    watcher = h.ContractWatcher()
    put(new_session)
    driven.write_text("x\nmore\n")
    unshown.write_text("x\nmore\n")
    late_task.write_text("x\nmore\n")
    late_state.write_text("changed\n")
    changes = watcher({"aaaa1111"})
    assert {c["path"] for c in changes} == {str(new_session), str(driven), str(late_state), str(late_task)}
    assert all(c["change_ns"] > 0 for c in changes) and watcher({"aaaa1111"}) == []


class SavedTrace(FakeTrace):
    def event_times(self, name):
        return [0.1] if name in ("snapshot_written", "live_check_end") else []


def test_a_save_records_the_harness_census_as_the_copy_sources(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    state, proj = home / "agentctl" / "state", home / "projects" / "-proj"
    shown = put(state / "task-a.json")
    put(state / "task-b.json", age_ns=3 * DAY_NS)          # outside the window: not in the trimmed snapshot
    put(proj / "task-a.jsonl")                             # a transcript the trimmed snapshot omits too
    runner = h.RealRunner(tmp_path / "state", tmp_path / "old", tmp_path / "new", CONTRACT, [])
    trimmed = [h.identity(shown, "state")]

    def save_run(*_args, **_kwargs):
        runner.snapshot_path("new").parent.mkdir(parents=True, exist_ok=True)
        runner.snapshot_path("new").write_text(json.dumps({"version": 1, "sources": trimmed, "sections": {}}))
        return SavedTrace()

    monkeypatch.setattr(h, "run_pty", save_run)
    copy_ = runner("save", None)
    census = h.live_sources()
    assert len(census) > len(trimmed)
    assert copy_["sources"] == census


def test_a_save_waits_for_the_app_exit_as_long_as_the_hang_guard(tmp_path, monkeypatch):
    """After SIGTERM a save run reads until the app exits, bounded by its own hang guard, not a shorter wait:
    the app writes its snapshot on exit, and a short fixed wait kills a write slowed by machine load."""
    fake_home(tmp_path, monkeypatch)
    runner = h.RealRunner(tmp_path / "state", tmp_path / "old", tmp_path / "new", CONTRACT, [])
    calls = []

    def save_run(*_args, **kwargs):
        calls.append(kwargs)
        runner.snapshot_path("new").parent.mkdir(parents=True, exist_ok=True)
        runner.snapshot_path("new").write_text(json.dumps({"version": 1, "sources": [], "sections": {}}))
        return SavedTrace()

    monkeypatch.setattr(h, "run_pty", save_run)
    assert runner("save", None) is not None
    (call,) = calls
    assert call["terminate"] is True
    assert call.get("term_wait_s", 6.0) >= call["max_s"]


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
    report["floors"]["R1"]["median_s"] = 1.6
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
    assert h.DEFAULT_LIMITS == {"R1": 1.5, "R2": 0.95, "R3": 0.25, "R6": 1.1, "R3_latency": 2.75,
                                "R3_latency_watch": 0.75}
    assert h.limits_with({"R1": 100.0})["R1"] == 1.5


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
            "live_check_end_s": 1.1, "rows_unconfirmed_unmarked": 0, "last_row_shown_s": 1.0, "last_mark_shown_s": 1.0}
    if arm == "old":
        base.update(first_rows_s=2.0, rows_confirmed_s=2.0, marks_confirmed_s=9.0, applied_ok=None,
                    old_marking_at_first_rows=False, last_old_cleared_s=None, live_check_end_s=None,
                    first_screen_confirmed_s=None, mark_confirmed_max_s=None)
    return {**base, **over}


def good_cell(name):
    changed = {"near-zero": 3, "80-100": 90, "101-130": 110, "131-170": 150, "225-275": 250, "360-440": 400}.get(name)
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


def r3_rec(arm, path, **over):
    base = {"arm": arm, "path": path, "counted": True, "void_reason": None, "median_s": 0.1, "covered_all": True,
            "latency_max_s": 0.5 if path == "event" else 2.2, "redraw_gap_min_s": 0.6 if path == "event" else None,
            "cycles": []}
    return {**base, **over}


def good_report(tmp_path, tree, old):
    cells = {}
    for name in h.REQUIRED_CELLS:
        cells[name] = r6_cell(name) if name in h.R6_CELLS else good_cell(name)
    runs = [r3_rec(arm, path) for arm, path in h.R3_CELLS for _ in range(h.R3_MIN_RUNS)]
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


def test_a_missing_required_cell_is_a_miss_but_360_440_is_optional(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    del report["cells"]["131-170"]
    assert acceptance(tmp_path, report, trees) == 1
    report = good_report(tmp_path, *trees)
    assert "360-440" not in report["cells"]
    assert acceptance(tmp_path, report, trees) == 0


def test_a_first_screen_row_without_any_old_marking_proof_is_a_miss(tmp_path, trees):
    report = good_report(tmp_path, *trees)
    report["cells"]["near-zero"]["runs"][1]["rows_unconfirmed_unmarked"] = 1
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
        if what == "r3":
            return r3_rec(arg["arm"], arg["path"])
        return {"arm": arg["arm"], "exec_kinds": [], "children_between_cycles": [], "created_files": []}


def campaign(tmp_path, runner, old=None, new=None, until=1e12):
    return h.Campaign(tmp_path, old or tmp_path / "old", new or tmp_path / "new", save_every=3600, until=until,
                      watch=[], runner=runner, clock=lambda: 0.0, log=lambda m: None)


def finished_cell(camp, name, changed):
    camp.runs[name] = [run_record(arm, changed) for _ in range(h.MIN_RUNS) for arm in ("old", "new")]


def test_the_campaign_alternates_old_and_new_within_a_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "INTERLEAVE_EVERY", 99)
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


# --- the interleaved campaign, R1 and R2 judged per row and per cell ----------------------------------

class BandScript(Script):
    """Answers every band run with a settled record whose count sits in the cell's band."""

    def __call__(self, what, arg):
        if what != "run":
            return super().__call__(what, arg)
        self.calls.append((what, arg))
        return run_record(arg["arm"], sum(h.BANDS[arg["cell"]]) // 2 if arg["cell"] in h.BANDS else None)


@pytest.mark.parametrize("every", [2, 3])
def test_r3_and_k1_reach_their_minimum_under_a_cap_with_bands_always_runnable(tmp_path, monkeypatch, every):
    monkeypatch.setattr(h, "INTERLEAVE_EVERY", every)
    monkeypatch.setattr(h.Campaign, "pick_copy", lambda self, cell, live: ({"file": "/c"}, sum(h.BANDS[cell]) // 2))
    runner = BandScript()
    camp = campaign(tmp_path, runner)
    owed = len(h.R3_CELLS) * h.R3_MIN_RUNS + 2 * h.K1_MIN_RUNS
    jobs = lambda: len([c for c in runner.calls if c[0] in ("run", "r3", "k1")])  # noqa: E731
    reached = None
    for _ in range(300):
        if camp.finished():
            break
        assert camp.step()
        if reached is None and h.next_measurement(camp.r3, camp.k1) is None:
            reached = jobs()
    assert camp.finished() and len(camp.r3) + len(camp.k1) == owed
    assert reached <= (every + 1) * owed


def test_r1_old_marking_is_required_of_each_row_painted_before_its_confirmation():
    marks = {"aaaa": False, "bbbb": False}
    painted = ev("rows_painted", 0.2, from_snapshot=True, rows=2)
    one = [ev("row_confirmed", 0.1, rows=["aaaa-full-id"]), painted]
    both = [ev("row_confirmed", 0.1, rows=["aaaa-full-id", "bbbb-full-id"]), painted]
    assert h.unconfirmed_unmarked_rows(one, marks) == 1
    assert h.unconfirmed_unmarked_rows(both, marks) == 0
    assert h.unconfirmed_unmarked_rows(one, {"aaaa": False, "bbbb": True}) == 0
    assert h.unconfirmed_unmarked_rows([painted, ev("row_confirmed", 0.3, rows=["aaaa-full-id"])], marks) == 2
    assert h.unconfirmed_unmarked_rows([ev("row_confirmed", 0.1, rows=["aaaa-full-id"])], marks) is None
    cell = {"runs": [run_record("old"), *[run_record("new", rows_unconfirmed_unmarked=1)] * h.MIN_RUNS]}
    cell["runs"] += [run_record("old")] * (h.MIN_RUNS - 1)
    assert any("old marking" in m for m in h.judge_cell_r1("80-100", cell, h.limits_with({})))


def cell_with(new_runs):
    return {"runs": [*new_runs, *[run_record("old") for _ in range(h.MIN_RUNS)]]}


def test_r1_and_r2_time_limits_are_judged_on_the_cell_median():
    limits = h.limits_with({})
    r1 = lambda *vals: [m for m in h.judge_cell_r1("c", cell_with([run_record("new", first_rows=v) for v in vals]),  # noqa: E731
                                                   limits) if "R1 median" in m]
    assert r1(0.3, 0.4, 0.5, 0.6, 1.6) == []
    assert len(r1(1.6, 1.7, 1.8, 0.4, 0.3)) == 1
    r2 = lambda *vals: [m for m in h.judge_cell_r2("c", cell_with([run_record("new", marks_confirmed_s=v)  # noqa: E731
                                                                  for v in vals]), limits) if "R2 median" in m]
    assert r2(0.5, 0.6, 0.7, 0.8, 1.2) == []
    assert len(r2(1.2, 1.3, 1.4, 0.5, 0.6)) == 1


def test_r2_fails_a_run_whose_startup_applied_set_misses_a_pre_launch_change():
    changes = {"/s/task-a.json": {"task-a"}}
    covering = ["/s/task-a.json"]

    def summary(stream):
        return h.event_summary(stream, T0, arm="new", first_screen_ids={"task-a"}, changes=changes)

    stream = new_stream(covering, covering)
    assert summary(stream)["applied_ok"] is True
    missing = [ev("startup_applied", 0.45, applied=["/s/other.json"]) if e["ev"] == "startup_applied" else e
               for e in stream]
    assert summary(missing)["applied_ok"] is False
    assert summary([e for e in stream if e["ev"] != "startup_applied"])["applied_ok"] is False
    cell = cell_with([run_record("new", applied_ok=summary(missing)["applied_ok"]) for _ in range(h.MIN_RUNS)])
    assert any("applied set" in m for m in h.judge_cell_r2("c", cell, h.limits_with({})))


def test_r2_completeness_is_judged_at_the_last_confirmation_event_not_the_end_of_the_check():
    changes = {"/s/task-a.json": {"task-a"}}
    covering = ["/s/task-a.json"]

    def summary(startup_at):
        stream = [ev("cycle_start", 0.0, cycle=1), ev("startup_applied", startup_at, applied=covering),
                  ev("startup_applied", 0.3, applied=[]),
                  ev("mark_confirmed", 0.5, task="task-a", applied=covering), ev("first_screen_confirmed", 0.6),
                  ev("live_check_end", 0.8, applied=covering)]
        stream.sort(key=lambda e: e["t_ns"])
        return h.event_summary(stream, T0, arm="new", first_screen_ids={"task-a"}, changes=changes)

    assert summary(0.7)["applied_ok"] is False
    assert summary(0.55)["applied_ok"] is True


def test_the_r2_pre_launch_census_finishes_before_the_dashboard_starts(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    order = []
    copy = tmp_path / "copy.json"
    copy.write_text("{}")
    monkeypatch.setattr(h, "contract_changes", lambda saved, live: order.append("census") or [])
    monkeypatch.setattr(h, "run_pty", lambda *a, **k: order.append("launch") or FakeTrace())
    monkeypatch.setattr(h, "summarize_run", lambda *a, **k: {})
    monkeypatch.setattr(h, "last_moments", lambda *a, **k: {})
    monkeypatch.setattr(h, "run_env", lambda *a, **k: {})
    runner = h.RealRunner.__new__(h.RealRunner)
    runner.state_dir, runner.old, runner.new, runner.uv_cache = tmp_path / "state", tmp_path / "old", tmp_path / "new", None
    runner.do_run({"cell": "80-100", "arm": "new", "copy": {"file": str(copy)}, "live": []})
    assert order == ["census", "launch"]


# --- R3: sweeps between cycles, due sets, coverage, untimed cycles ------------------------------------

def test_r3_due_sets_come_from_between_cycle_sweeps_and_change_times(tmp_path):
    ticks = iter(range(100, 1000, 10))
    jobs = []
    found = [{"path": "/a", "kind": "state", "change_ns": 1}]
    taker = h.SweepTaker(lambda ids: found, spawn=jobs.append, now=lambda: next(ticks))
    taker.on_event(FakeTrace(), ev("cycle_start", 1.0, cycle=2))
    assert jobs == [] and not taker.busy
    taker.on_event(FakeTrace(), ev("cycle_end", 1.1, cycle=2))
    taker.on_event(FakeTrace(), ev("cycle_end", 1.2, cycle=3))
    assert len(jobs) == 1 and taker.busy
    jobs[0]()
    assert taker.sweeps == [{"start_ns": 100, "end_ns": 110, "changes": found}] and not taker.busy
    cycles = [cyc(2, 2.0, 0.1), cyc(3, 4.0, 0.1)]
    start3, res = ns(4.0), 1000
    late = [{"path": "/a", "change_ns": ns(1.5)}, {"path": "/b", "change_ns": start3 - res},
            {"path": "/c", "change_ns": start3 - res + 1}]
    seen_after_cycle_2 = {"start_ns": ns(2.2), "end_ns": ns(2.3), "changes": late}
    due = h.due_sets(cycles, [seen_after_cycle_2], 0, res)
    assert due[2] == [] and [c["path"] for c in due[3]] == ["/a", "/b"]
    assert h.change_time_ns({"mtime_ns": 5, "ctime_ns": 9}, "/x") == 9
    stat = os.stat(tmp_path)
    assert h.change_time_ns(None, str(tmp_path / "gone")) == max(stat.st_mtime_ns, stat.st_ctime_ns)


def test_one_uncovered_cycle_fails_r3():
    cycles = stat_cycles(26)
    due = {12: [due_at("/a", 0.0)]}
    run = stat_run(cycles, arm="new", events=[ev("detection", 0.0, path="stat", reason="off")], due=due)
    assert run["covered_all"] is False and run["uncovered"] == 1
    pad = [r3_rec("new", p) for p in ("event", "stat") for _ in range(h.R3_MIN_RUNS)]
    misses = h.judge_r3({"r3": {"runs": [run, *pad]}}, h.limits_with({}))
    assert any("applied set misses" in m for m in misses)


def test_a_cycle_during_a_sweep_is_untimed_but_judged_for_coverage():
    sweep = {"start_ns": ns(10.0), "end_ns": ns(10.5), "changes": []}
    assert h.cycle_untimed(cyc(1, 10.2, 0.1), [sweep]) and h.cycle_untimed(cyc(1, 9.9, 0.2), [sweep])
    assert not h.cycle_untimed(cyc(1, 9.0, 0.9), [sweep]) and not h.cycle_untimed(cyc(1, 10.6, 0.1), [sweep])
    cycles = [*stat_cycles(26)[:4], cyc(5, 8.4, 3.0, gap=2.0), *stat_cycles(26)[5:]]
    sweeps = [{"start_ns": ns(8.45), "end_ns": ns(8.46), "changes": []}]
    run = stat_run(cycles, sweeps=sweeps)
    assert run["untimed_cycles"] == 1 and run["timed_cycles"] == h.R3_CYCLES and run["counted"] is True
    assert run["latency_max_untimed_s"] == pytest.approx(5.0) and run["latency_max_s"] == pytest.approx(2.1)
    assert stat_run(cycles, sweeps=sweeps, due={5: [due_at("/z", 8.0)]})["covered_all"] is False
    assert stat_run(stat_cycles(21))["counted"] is True
    short = stat_run(stat_cycles(20))
    assert short["counted"] is False and short["void_reason"] == "short"
    assert h.R3_RUN_CAP_S == 660


def test_r3_runs_are_taken_on_both_detection_paths(tmp_path):
    env = {"A": "1", h.WATCH_ENV: "on"}
    assert h.r3_env(env, "stat")[h.WATCH_ENV] == "off" and h.WATCH_ENV not in h.r3_env(env, "event")
    camp = campaign(tmp_path, Script())
    while camp.measure():
        pass
    cells = [(r["arm"], r["path"]) for r in camp.r3]
    assert {c: cells.count(c) for c in set(cells)} == {c: h.R3_MIN_RUNS for c in (("new", "event"), ("new", "stat"),
                                                                                  ("old", "stat"))}
    detect = lambda path: [ev("detection", 0.0, path=path, reason="x")] if path else []  # noqa: E731
    run = lambda arm, path, **kw: stat_run(stat_cycles(21), arm=arm, events=detect(path), **kw)  # noqa: E731
    assert run("new", "event")["void_reason"] == "fallback" and run("new", None)["void_reason"] == "no detection"
    assert run("new", "stat")["counted"] is True and run("old", None)["counted"] is True


# --- R3 latency on both detection paths ---------------------------------------------------------------

def redraw(at, *cycles):
    return ev("redraw", at, cycles=list(cycles))


def event_run(*redraws, change=7.8, stamps=None, applied=(5,), due=True):
    cycles = [cyc(n, (n - 1) * 2.0, 0.1, ["/s/a"] if n in applied else []) for n in range(1, 22)]
    events = [ev("detection", 0.0, path="event", reason="start"), *redraws]
    covered = h.check_cycle_coverage(cycles, {5: [due_at("/s/a", change)]} if due else {}, 0)
    return h.r3_run_record("new", "event", covered, events, [], stamps=stamps or {})


def r3_misses(*runs):
    pad = [r3_rec("new", p) for p in ("event", "stat") for _ in range(h.R3_MIN_RUNS)]
    return h.judge_r3({"r3": {"runs": [*runs, *pad]}}, h.limits_with({}))


def latency_missed(*runs):
    return any("latency" in m for m in r3_misses(*runs))


def test_event_path_latency_is_judged_per_changed_source_to_the_redraw():
    passing, failing = event_run(redraw(8.5, 5)), event_run(redraw(8.6, 5))
    assert passing["latency_max_s"] == pytest.approx(0.7) and failing["latency_max_s"] == pytest.approx(0.8)
    assert not latency_missed(passing) and latency_missed(failing)
    unnamed = event_run(redraw(10.3, 9))
    assert unnamed["latency_max_s"] == pytest.approx(0.3) and not latency_missed(unnamed)
    nothing_due = event_run(redraw(8.5, 5), due=False)
    assert nothing_due["counted"] is False and nothing_due["void_reason"] == "no due source"
    assert event_run(redraw(8.5, 5), change=8.0)["counted"] is True
    offset = 5 * 10**9
    cycles = [cyc(5, 8.0, 0.1, ["/s/a"])]
    (shifted,) = h.check_cycle_coverage(cycles, {5: [{"path": "/s/a", "change_ns": ns(7.8) + offset}]}, offset)
    assert shifted["sources"][0]["change_ns"] == ns(7.8) and shifted["covered"] is True


def test_event_path_latency_is_timed_from_the_first_change_not_yet_shown():
    late = redraw(8.5, 5)
    unshown = event_run(late, stamps={"/s/a": [ns(7.0), ns(7.8)]})
    assert unshown["latency_max_s"] == pytest.approx(1.5) and latency_missed(unshown)
    shown_by_cycle_3 = event_run(late, stamps={"/s/a": [ns(4.0), ns(7.8)]}, applied=(3, 5))
    assert shown_by_cycle_3["latency_max_s"] == pytest.approx(0.7) and not latency_missed(shown_by_cycle_3)
    assert shown_by_cycle_3["latency_stat_timed"] == 0 and event_run(late)["latency_stat_timed"] == 1


def sweep_at(start, end):
    return {"start_ns": ns(start), "end_ns": ns(end)}


def event_run_with_applier(applier, due_in, change, redraw_at, *, stamps=None, sweeps=(), also_applied=()):
    cycles = [cyc(n, (n - 1) * 2.0, 0.1, ["/s/a"] if n in (applier, *also_applied) else []) for n in range(1, 24)]
    if applier == 1:
        cycles[0] = cyc(1, 0.0, 1.0, ["/s/a"])
    events = [ev("detection", 0.0, path="event", reason="start"), redraw(redraw_at, applier)]
    covered = h.check_cycle_coverage(cycles, {due_in: [due_at("/s/a", change)]}, 0)
    return h.r3_run_record("new", "event", covered, events, list(sweeps), stamps=stamps or {})


def test_event_latency_is_timed_when_the_applying_cycle_is_the_first_or_an_untimed_one():
    first = event_run_with_applier(1, 2, 0.5, 1.3)
    assert first["counted"] is True and first["latency_max_s"] == pytest.approx(0.8)
    untimed = event_run_with_applier(4, 6, 6.05, 6.4, sweeps=[sweep_at(5.9, 6.05)])
    assert untimed["untimed_cycles"] == 1 and untimed["latency_max_s"] == pytest.approx(0.35)
    covered_earlier = event_run_with_applier(6, 6, 8.5, 10.4, stamps={"/s/a": [ns(6.05), ns(8.5)]},
                                             sweeps=[sweep_at(5.9, 6.05)], also_applied=(4,))
    assert covered_earlier["latency_max_s"] == pytest.approx(1.9)


def test_the_run_observer_stamps_writes_renames_and_unlinks_but_not_reads(tmp_path):
    existing = tmp_path / "read.json"
    existing.write_text("1")
    observer = h.RunObserver([str(tmp_path)])
    observer.start()
    try:
        assert observer.ok is True
        existing.read_text()
        os.stat(existing)
        (tmp_path / "w.json").write_text("2")
        (tmp_path / "tmp.json").write_text("3")
        os.rename(tmp_path / "tmp.json", tmp_path / "moved.json")
        os.unlink(tmp_path / "w.json")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and str(tmp_path / "moved.json") not in observer.stamps:
            time.sleep(0.02)
    finally:
        observer.stop()
    assert str(existing) not in observer.stamps
    assert str(tmp_path / "w.json") in observer.stamps and str(tmp_path / "moved.json") in observer.stamps
    assert observer.ok is True


def test_a_run_observer_with_a_directory_it_cannot_watch_is_not_ok(tmp_path):
    observer = h.RunObserver([str(tmp_path / "missing")])
    observer.start()
    observer.stop()
    assert observer.ok is False
    assert h.r3_void_reason("event", "new", [ev("detection", 0.0, path="event")], [], observer.ok) == "observer"


def test_r3_latency_is_judged_on_gap_plus_cycle():
    new_stat = lambda cycles: stat_run(cycles, arm="new", events=[ev("detection", 0.0, path="stat", reason="off")])  # noqa: E731
    ok, slow = new_stat(stat_cycles(21, 0.2, 2.0)), new_stat(stat_cycles(21, 0.2, 2.6))
    assert ok["latency_max_s"] == pytest.approx(2.2) and slow["latency_max_s"] == pytest.approx(2.8)
    assert not latency_missed(ok) and latency_missed(slow)
    assert h.cycle_latency_s(cyc(2, 0.0, 0.25, gap=2.5)) == 2.75
    assert not latency_missed(new_stat(stat_cycles(21, 0.25, 2.5)))


def event_changes_run(slow_ms):
    cycles, due = [], {}
    for n in range(1, 22):
        start, path = ns(n * 2.0), f"/s/{n}"
        cycles.append({"cycle": n, "start_ns": start, "end_ns": start + 100_000_000, "start_s": n * 2.0,
                       "duration_s": 0.1, "gap_s": None if n == 1 else 1.9, "applied": [path], "found_by": None})
        due[n] = [{"path": path, "change_ns": start - 200_000_000}]
    slow = {"ev": "redraw", "t_ns": due[11][0]["change_ns"] + slow_ms * 1_000_000, "cycles": [11]}
    covered = h.check_cycle_coverage(cycles, due, 0)
    return h.r3_run_record("new", "event", covered, [ev("detection", 0.0, path="event", reason="x"), slow], [], stamps={})


def test_a_run_fails_on_its_slowest_change_not_its_median():
    slow = event_changes_run(900)
    assert slow["latency_median_s"] == pytest.approx(0.3) and slow["latency_max_s"] == pytest.approx(0.9)
    assert latency_missed(slow) and not latency_missed(event_changes_run(750))
    cycles = stat_cycles(21)
    cycles[10] = cyc(11, 21.0, 0.1, gap=2.9)
    stat = stat_run(cycles, arm="new", events=[ev("detection", 0.0, path="stat", reason="off")])
    assert stat["latency_median_s"] == pytest.approx(2.1) and stat["latency_max_s"] == pytest.approx(3.0)
    assert latency_missed(stat)


def product_constant(module: str, name: str):
    tree = ast.parse((TESTS.parent / "plan_dashboard" / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [name]:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{module}.{name} is not a literal assignment")


def test_r3_latency_limit_is_the_code_s_interval_plus_tick_plus_cycle():
    interval = product_constant("tui", "DEFAULT_INTERVAL_S")
    tick, spacing = product_constant("tui_app", "TICK_S"), product_constant("tui_app", "REDRAW_SPACING_S")
    assert product_constant("watch", "TICK_S") == tick and product_constant("watch", "RECONCILE_S") == 30.0
    assert product_constant("tui_app", "LIGHT_TICK_S") == 1.0
    assert h.DEFAULT_LIMITS["R3_latency"] == interval + tick + h.DEFAULT_LIMITS["R3"]
    assert h.DEFAULT_LIMITS["R3_latency_watch"] == tick + h.DEFAULT_LIMITS["R3"]
    assert spacing <= tick and h.REDRAW_SPACING_MIN_S == spacing
    assert "--interval" not in inspect.getsource(h.RealRunner.do_r3)


def test_limits_override_only_tightens_and_every_default_limit_is_a_flag(tmp_path, trees):
    assert h.limits_with({"R1": 0.70})["R1"] == 0.70
    assert h.limits_with({"R1": 2.0})["R1"] == 1.5 and h.limits_with({"R1": 1.5})["R1"] == 1.5
    assert set(h.parse_limits([f"{k}=1" for k in h.DEFAULT_LIMITS])) == set(h.DEFAULT_LIMITS)
    report = good_report(tmp_path, *trees)
    assert not any("latency" in m for m in h.judge_report(report, h.limits_with({})))
    for key in ("R3_latency", "R3_latency_watch"):
        tight = h.limits_with(h.parse_limits([f"{key}=0.001"]))
        assert any("latency" in m for m in h.judge_report(report, tight))


def test_r3_limits_judge_new_code_runs_only():
    bad_old = [r3_rec("old", "stat", median_s=5.0, covered_all=False, latency_max_s=5.0) for _ in range(h.R3_MIN_RUNS)]
    assert r3_misses(*bad_old) == []
    new = [r3_rec(arm, path) for arm, path in h.R3_CELLS[:2] for _ in range(h.R3_MIN_RUNS)]
    assert h.next_measurement([*new, *bad_old], [])["what"] == "k1"


def test_r3_fails_a_new_code_cell_short_of_its_counted_runs():
    stat_only = [r3_rec("new", "stat") for _ in range(2 * h.R3_MIN_RUNS)]
    event_only = [r3_rec("new", "event") for _ in range(2 * h.R3_MIN_RUNS)]
    miss = lambda runs, path: any(f"on the {path} path" in m for m in h.judge_r3({"r3": {"runs": runs}},  # noqa: E731
                                                                               h.limits_with({})))
    assert miss(stat_only, "event") and not miss(stat_only, "stat")
    assert miss(event_only, "stat") and not miss(event_only, "event")
    assert miss([r3_rec("new", "stat", counted=False) for _ in range(9)] + stat_only[:4], "stat")


def test_a_write_landing_mid_cycle_and_applied_by_that_cycle_is_covered():
    cycles = [cyc(5, 8.4, 0.1, ["/a"]), cyc(6, 10.5, 0.1, [])]
    mid = h.check_cycle_coverage(cycles, {6: [due_at("/a", 8.45)]}, 0)
    assert mid[1]["covered"] is True and mid[1]["sources"][0]["applied_by"] == 5
    after = h.check_cycle_coverage(cycles, {6: [due_at("/a", 8.55)]}, 0)
    assert after[1]["covered"] is False and after[1]["sources"][0]["applied_by"] is None


def test_redraws_closer_than_half_a_second_fail_the_run():
    def spaced(gap_ns):
        first = ns(9.0)
        return event_run({"ev": "redraw", "t_ns": first, "cycles": [5]},
                         {"ev": "redraw", "t_ns": first + gap_ns, "cycles": [6]})

    exact, close = spaced(500_000_000), spaced(490_000_000)
    assert exact["redraw_gap_min_s"] == 0.5 and close["redraw_gap_min_s"] == 0.49
    spacing_missed = lambda run: any("redraws come closer" in m for m in r3_misses(run))  # noqa: E731
    assert not spacing_missed(exact) and spacing_missed(close)
    repaints = event_run(redraw(9.0, 5), ev("header_repaint", 9.1), ev("light_tick", 9.2, tmux_changed=False),
                         redraw(9.6, 6))
    assert repaints["redraws"] == 2 and repaints["redraw_gap_min_s"] == pytest.approx(0.6)
    before_first_detection = [redraw(0.0, 1), ev("detection", 0.2, path="event", reason="start"), redraw(0.3, 2)]
    assert h.redraw_summary(before_first_detection)["redraw_gap_min_s"] is None
    assert not spacing_missed(r3_rec("new", "stat", redraw_gap_min_s=0.1))


def test_found_by_marks_are_counted_and_judge_nothing():
    cycles = [cyc(2, 2.0, 0.1, found_by={"/a": "event", "/b": "stat"}), cyc(3, 4.0, 0.1, found_by={"/a": "event"})]
    assert h.found_by_counts(cycles) == {"event": 2, "stat": 1}
    assert h.found_by_counts(stat_cycles(3)) is None
    assert r3_misses(r3_rec("new", "stat", found_by_counts={"x": 999})) == []


def test_untimed_share_and_load_are_recorded_per_run():
    sweeps = [{"start_ns": ns(4.25), "end_ns": ns(4.26), "changes": []}, {"start_ns": ns(8.45), "end_ns": ns(8.46), "changes": []}]
    run = stat_run(stat_cycles(26), sweeps=sweeps, loads=(0.5, 0.7))
    assert run["untimed_cycles"] == 2 and run["untimed_share"] == pytest.approx(2 / 22)
    assert (run["load_start"], run["load_end"]) == (0.5, 0.7)
    assert "untimed_share=" in h.r3_digest([run])[0] and "load=0.5->0.7" in h.r3_digest([run])[0]
    assert stat_run(stat_cycles(26))["untimed_share"] is None


def test_r2_runs_record_the_screen_time_to_marks_and_its_factor(tmp_path):
    assert h.marks_screen_s(run_record("new", rows_confirmed_s=0.4, marks_confirmed_s=0.9)) == 0.9
    assert h.marks_screen_s(run_record("new", marks_confirmed_s=None)) is None
    camp = campaign(tmp_path, Script())
    camp.runs["near-zero"] = [run_record("old"), run_record("new", marks_screen_s=0.9)]
    summary = camp.report()["cells"]["near-zero"]["marks_screen"]
    assert summary["new_median_s"] == 0.9 and summary["old_median_s"] == 9.0 and summary["factor"] == pytest.approx(0.1)
    camp.runs["131-170"] = [run_record("old"), run_record("new", marks_screen_s=0.9)]
    assert "marks_screen" not in camp.report()["cells"]["131-170"]


def test_r2_and_r6_rows_and_marks_are_compared_as_redraw_moments():
    cleared = [ev("last_old_cleared", 1.0), redraw(4.45, 1), ev("mark_confirmed", 1.1, task="t"), redraw(4.7, 1)]
    moments = h.last_moments(cleared, T0, "101-130")
    assert moments == {"last_row_shown_s": pytest.approx(4.45), "last_mark_shown_s": pytest.approx(4.7)}
    no_marked = [redraw(0.7, 1), ev("live_check_end", 1.0), redraw(1.5, 2)]
    assert h.last_moments(no_marked, T0, "80-100")["last_row_shown_s"] == pytest.approx(0.7)
    assert h.last_moments(no_marked, T0, "damaged")["last_row_shown_s"] == pytest.approx(1.5)
    assert h.last_moments([ev("last_old_cleared", 1.0)], T0, "80-100")["last_row_shown_s"] == pytest.approx(1.0)
    assert h.last_moments([], T0, "80-100") == {"last_row_shown_s": None, "last_mark_shown_s": None}

    def verdict(shown):
        cell = r6_cell("101-130")
        for run in cell["runs"]:
            if run["arm"] == "old":
                run["first_rows_s"] = 4.0
            else:
                run["last_row_shown_s"] = shown
        return [m for m in h.judge_cell_r6("101-130", cell, h.limits_with({})) if "last_row_shown_s" in m]

    assert verdict(4.35) == [] and len(verdict(4.45)) == 1


def test_k1_does_not_count_the_light_tick_s_tmux_list_child(monkeypatch):
    table = {100: (1, ["root"]), 101: (100, ["/usr/bin/tmux", "list-windows"]), 102: (100, ["git", "status"]),
             103: (102, ["sh"]), 104: (101, ["cat"])}
    monkeypatch.setattr(h, "proc_table", lambda: table)
    monkeypatch.setattr(h, "TMUX_LIST_ARGV", ("tmux", "list-windows"))
    assert h.descendants(100) == 2
    monkeypatch.setattr(h, "TMUX_LIST_ARGV", ())
    assert h.descendants(100) == 4


def test_each_r3_and_k1_measurement_logs_one_line(tmp_path):
    lines = []
    camp = h.Campaign(tmp_path, tmp_path / "old", tmp_path / "new", save_every=3600, until=1e12, watch=[],
                      runner=Script(), clock=lambda: 0.0, log=lines.append)
    while camp.measure():
        pass
    owed = len(h.R3_CELLS) * h.R3_MIN_RUNS + 2 * h.K1_MIN_RUNS
    assert len(lines) == owed and lines[0] == "measured r3 new-event counted -" and lines[-1] == "measured k1 new counted -"
    voided = []
    void_camp = h.Campaign(tmp_path, tmp_path / "old", tmp_path / "new", save_every=3600, until=1e12, watch=[],
                           runner=lambda what, arg: r3_rec(arg["arm"], arg["path"], counted=False, void_reason="short"),
                           clock=lambda: 0.0, log=voided.append)
    void_camp.measure()
    assert voided == ["measured r3 new-event void short"]


def test_bands_are_the_order_s_numeric_bands():
    assert h.BANDS == {"near-zero": (0, 10), "80-100": (80, 100), "101-130": (101, 130), "131-170": (131, 170),
                       "225-275": (225, 275), "360-440": (360, 440)}
    ranges = sorted(h.BANDS.values())
    assert all(a[1] < b[0] for a, b in zip(ranges, ranges[1:]))
    assert h.OPTIONAL_CELLS == ("360-440",) and "360-440" not in h.REQUIRED_CELLS and "225-275" in h.REQUIRED_CELLS
    assert (h.band_of(250), h.band_of(400), h.band_of(300)) == ("225-275", "360-440", None)
