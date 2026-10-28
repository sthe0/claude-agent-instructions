"""Stage-1 tests of scripts/lib/time_profile.py: the per-session exclusive time partition.

Expected totals below are computed by hand from the fixture transcripts in
fixtures/time_profile/projects/proj/, entry by entry; none is produced by running the
extractor. The mutants in time_profile_mutants.py must each make this file fail.
"""
from __future__ import annotations

import ast
import builtins
import dataclasses
import datetime as dt
import io
import json
import os
import shutil
import tracemalloc
from pathlib import Path

import pytest

from lib import time_profile
from time_profile_mutants import time_profile_mutant  # noqa: F401  (autouse fixture)

FIX = Path(__file__).resolve().parent / "fixtures" / "time_profile"
MODULE = Path(time_profile.__file__)

COMMIT_INDEX = [(1772366500, "newer"), (1772366400, "onsha"), (5, "old")]

EXPECTED_PARTITION = {
    "sess-a": {
        "model:gap": 5400,
        "model:compact": 200,
        "unattributed": 1700,
        "user:wait": 63900,
        "wait:background": 10000,
        "tool:Bash:agentctl-cli.py:status": 1800,
        "hook:hook-guard.py": 200,
        "tool:Read": 351,
        "tool:Grep": 649,
        "skill:tdd": 2000,
        "tool:Agent:developer": 10000,
        "mcp:intrasearch": 800,
    },
    "sess-b": {
        "user:wait": 5000,
        "model:gap": 6500,
        "model:compact": 200,
        "wait:background": 3000,
        "unattributed": 500,
        "tool:Read": 334,
        "tool:Bash:git:status": 834,
        "mcp:srv": 1332,
        "tool:Bash:pytest": 2700,
        "hook:hook-log.sh": 300,
        "tool:Write": 500,
        "tool:Agent:general-purpose": 2700,
        "hook:hook-a.py": 300,
    },
    "sess-c": {
        "model:gap": 2500,
        "model:compact": 500,
        "user:wait": 30500,
        "wait:background": 3500,
        "tool:Bash:lint.sh": 1251,
        "tool:Edit": 2249,
        "hook:h.sh": 500,
    },
}
EXPECTED_WALL_MS = {"sess-a": 97000, "sess-b": 24200, "sess-c": 41000}
EXPECTED_COVERAGE = {
    "sess-a": {"timed_events": 26, "untimed_events": 1, "exposed_hook_ms": 200},
    "sess-b": {"timed_events": 25, "untimed_events": 0, "exposed_hook_ms": 600},
    "sess-c": {"timed_events": 13, "untimed_events": 0, "exposed_hook_ms": 500},
}
EXPECTED_OVERLAYS = {
    "sess-a": {
        "node:ROUTED": 5000,
        "node:PLANNING": 15000,
        "node:APPROVED": 10000,
        "spawn:developer": 12500,
        "spawn:thinker": 4000,
        "judge:plan-review": 3750,
        "judge:acceptance": 500,
    },
    "sess-b": {"judge:acceptance": 1000},
    "sess-c": {},
}
EXPECTED_SHA = {"sess-a": "aaaa111", "sess-b": "bbbb222", "sess-c": "onsha"}
SESSIONS = sorted(EXPECTED_PARTITION)

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def iso_ms(value: str) -> int:
    return (dt.datetime.fromisoformat(value.replace("Z", "+00:00")) - EPOCH) // dt.timedelta(milliseconds=1)


def own_first_last_ms(path: Path) -> tuple:
    stamps = []
    for line in path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("timestamp") and not obj.get("isSidechain"):
            stamps.append(iso_ms(obj["timestamp"]))
    return stamps[0], stamps[-1]


@pytest.fixture
def fx(tmp_path):
    root = tmp_path / "fx"
    shutil.copytree(FIX, root)
    return root


def run_all(fx: Path, state: Path, **over):
    kwargs = dict(
        state_dir=state,
        projects_root=fx / "projects",
        agentctl_state_dir=fx / "agentctl_state",
        spawn_ledger=fx / "ledgers" / "spawn.jsonl",
        judge_ledger_path=fx / "ledgers" / "judge.jsonl",
        policy_ledger=fx / "ledgers" / "policy.jsonl",
        commit_index=COMMIT_INDEX,
    )
    kwargs.update(over)
    return time_profile.profile_all(**kwargs)


def by_session(run) -> dict:
    return {p.session_id: p for p in run.profiles}


@pytest.fixture
def profiles(fx, tmp_path):
    run = run_all(fx, tmp_path / "state")
    assert run.skipped == []
    return by_session(run)


def lone_ctx(**over):
    return time_profile.Ctx(commit_index=[], **over)


def write_lines(path: Path, *entries) -> Path:
    path.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries))
    return path


# ------------------------------------------------------------------ the partition


@pytest.mark.parametrize("sid", SESSIONS)
def test_partition_totals_are_exact_per_key(profiles, sid):
    assert profiles[sid].partition == EXPECTED_PARTITION[sid]


@pytest.mark.parametrize("sid", SESSIONS)
def test_partition_conserves_wall_clock_and_bounds_are_the_files_own(profiles, fx, sid):
    profile = profiles[sid]
    first, last = own_first_last_ms(fx / "projects" / "proj" / f"{sid}.jsonl")
    assert (profile.first_ts_ms, profile.last_ts_ms) == (first, last)
    assert profile.last_ts_ms - profile.first_ts_ms == EXPECTED_WALL_MS[sid]
    assert time_profile.conservation_error_ms(profile) == 0


@pytest.mark.parametrize("sid", SESSIONS)
def test_coverage_counts(profiles, sid):
    coverage = profiles[sid].coverage
    for key, value in EXPECTED_COVERAGE[sid].items():
        assert coverage[key] == value


def test_every_partition_key_family_is_exercised(profiles):
    fixed = {"user:wait", "wait:background", "model:gap", "model:compact", "unattributed"}
    families = {k if k in fixed else k.split(":")[0] for p in profiles.values() for k in p.partition}
    assert families == fixed | {"tool", "skill", "mcp", "hook"}


def test_conservation_error_reports_a_broken_partition(profiles):
    good = profiles["sess-c"]
    broken = dataclasses.replace(good, partition={**good.partition, "model:gap": 2501})
    assert time_profile.conservation_error_ms(broken) == 1


def test_a_single_timed_entry_session_is_profiled_with_zero_wall_clock(tmp_path):
    path = write_lines(tmp_path / "one.jsonl", {"type": "user", "timestamp": "2026-03-01T10:00:00Z",
                                                "message": {"content": [{"type": "text", "text": "x"}]}})
    profile = time_profile.profile_session(path, lone_ctx())
    assert isinstance(profile, time_profile.SessionProfile)
    assert profile.partition == {}
    assert profile.first_ts_ms == profile.last_ts_ms
    assert time_profile.conservation_error_ms(profile) == 0


def test_a_compacted_transcript_is_profiled_not_skipped(profiles):
    assert isinstance(profiles["sess-a"], time_profile.SessionProfile)
    assert profiles["sess-a"].partition["model:compact"] == 200


# ------------------------------------------------------------------------ skipping


def test_each_skip_reason_is_reported_as_skipped(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    garbage = write_lines(tmp_path / "garbage.jsonl", "not json at all", "{broken")
    untimed = write_lines(tmp_path / "untimed.jsonl", {"type": "summary", "summary": "s"},
                          {"type": "user", "message": {"content": "x"}})
    missing = tmp_path / "missing.jsonl"
    expected = {empty: "empty", garbage: "unparseable", untimed: "no_timestamps", missing: "empty"}
    for path, reason in expected.items():
        result = time_profile.profile_session(path, lone_ctx())
        assert isinstance(result, time_profile.Skipped), path
        assert (result.reason, result.session_id, result.transcript) == (reason, path.stem, str(path))
    assert set(expected.values()) == set(time_profile.SKIP_REASONS)


def test_profile_all_lists_skipped_beside_profiles(fx, tmp_path):
    (fx / "projects" / "proj" / "sess-empty.jsonl").write_text("")
    run = run_all(fx, tmp_path / "state")
    assert [(s.session_id, s.reason) for s in run.skipped] == [("sess-empty", "empty")]
    assert sorted(by_session(run)) == SESSIONS


# ----------------------------------------------------------------------- overlays


@pytest.mark.parametrize("sid", SESSIONS)
def test_overlays_are_joined_by_session(profiles, sid):
    assert profiles[sid].overlays == EXPECTED_OVERLAYS[sid]


@pytest.mark.parametrize("sid", SESSIONS)
def test_overlays_never_enter_the_partition(profiles, sid):
    profile = profiles[sid]
    assert not set(profile.partition) & set(profile.overlays)
    assert not any(k.startswith(("node:", "spawn:", "judge:")) for k in profile.partition)


def test_node_dwell_drops_an_interval_with_an_untimed_event_inside(profiles):
    coverage = profiles["sess-a"].coverage
    assert coverage["node_timed_events"] == 5
    assert coverage["node_untimed_events"] == 1
    assert "node:PLAN_READY" not in profiles["sess-a"].overlays
    assert "node_timed_events" not in profiles["sess-b"].coverage


def test_spawn_child_is_linked_to_its_spawner(profiles):
    assert (profiles["sess-c"].child_of, profiles["sess-c"].kind) == ("sess-a", "developer")
    assert (profiles["sess-a"].child_of, profiles["sess-a"].kind) == (None, "main")


@pytest.mark.parametrize("sid", SESSIONS)
def test_core_sha_comes_from_the_policy_ledger_else_the_commit_index(profiles, sid):
    assert profiles[sid].core_sha == EXPECTED_SHA[sid]


# ------------------------------------------------------------- shell and tool keys


@pytest.mark.parametrize("command, label", [
    ("ls -la", "ls"),
    ("cd /x && git status", "git:status"),
    ("git -C /y log --oneline", "git:log"),
    ("git", "git"),
    ("python3 -m pytest scripts -q", "pytest"),
    ("python3 -c 'print(1)'", "python3"),
    ("bash -lc 'echo hi'", "bash"),
    ("python3 scripts/agentctl-cli.py --session s status", "agentctl-cli.py:status"),
    ("FOO=1 env BAR=2 ls", "ls"),
    ("timeout 30 bash scripts/lint.sh --all", "lint.sh"),
    ("cat <<'EOF'\nbody\nEOF", "cat"),
    ("cd /x", None),
    ("echo 'unterminated", None),
    ("", None),
    (None, None),
])
def test_command_label(command, label):
    assert time_profile.command_label(command) == label


@pytest.mark.parametrize("name, tool_input, key", [
    ("AskUserQuestion", {}, "user:wait"),
    ("Skill", {"skill": "tdd"}, "skill:tdd"),
    ("Agent", {"subagent_type": "developer"}, "tool:Agent:developer"),
    ("Task", {}, "tool:Agent:general-purpose"),
    ("mcp__intrasearch__search", {}, "mcp:intrasearch"),
    ("Read", None, "tool:Read"),
    ("Bash", {"command": 7}, "tool:Bash"),
    (None, {}, "tool:unknown"),
])
def test_tool_key(name, tool_input, key):
    assert time_profile.tool_key(name, tool_input) == key


# ---------------------------------------------------------------------- the cache


def test_a_second_profile_all_with_unchanged_mtimes_reparses_nothing(fx, tmp_path):
    state = tmp_path / "state"
    first = run_all(fx, state)
    assert sorted(Path(p).stem for p in first.reparsed) == SESSIONS
    second = run_all(fx, state)
    assert second.reparsed == []
    assert [p.partition for p in second.profiles] == [p.partition for p in first.profiles]


def test_a_changed_transcript_alone_is_reparsed_and_stays_exact(fx, tmp_path):
    state = tmp_path / "state"
    run_all(fx, state)
    target = fx / "projects" / "proj" / "sess-b.jsonl"
    with target.open("a") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-03-01T11:00:25.000Z",
                             "message": {"id": "b10", "content": [{"type": "text", "text": "late"}]}}) + "\n")
    again = run_all(fx, state)
    assert [Path(p).stem for p in again.reparsed] == ["sess-b"]
    profile = by_session(again)["sess-b"]
    assert profile.last_ts_ms - profile.first_ts_ms == 25000
    assert profile.partition["model:gap"] == 6500 + 800
    assert time_profile.conservation_error_ms(profile) == 0


def test_a_touched_transcript_of_unchanged_size_is_reparsed(fx, tmp_path):
    state = tmp_path / "state"
    run_all(fx, state)
    target = fx / "projects" / "proj" / "sess-c.jsonl"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert [Path(p).stem for p in run_all(fx, state).reparsed] == ["sess-c"]


# ---------------------------------------------------------------- the ledger cursor


@pytest.fixture
def ledger_reads(monkeypatch):
    reads = []
    real = time_profile.iter_ledger_rows

    def spy(path, offset=0):
        record = {"path": Path(path), "offset": offset, "rows": 0}
        reads.append(record)
        for item in real(path, offset):
            record["rows"] += 1
            yield item

    monkeypatch.setattr(time_profile, "iter_ledger_rows", spy)
    return reads


def read_of(reads, path: Path) -> list:
    return [r for r in reads if r["path"] == path]


JUDGE_ROW = {"ts": "2026-03-01T10:00:50Z", "kind": "call", "judge": "plan-review", "duration": 1.0,
             "source": "sess-a"}


def test_appending_to_the_judge_ledger_reads_only_the_new_bytes(fx, tmp_path, ledger_reads):
    state = tmp_path / "state"
    judge = fx / "ledgers" / "judge.jsonl"
    run_all(fx, state)
    assert [(r["offset"], r["rows"]) for r in read_of(ledger_reads, judge)] == [(0, 5)]
    size = judge.stat().st_size
    ledger_reads.clear()
    with judge.open("a") as fh:
        fh.write(json.dumps(JUDGE_ROW) + "\n")
    run = run_all(fx, state)
    assert [(r["offset"], r["rows"]) for r in read_of(ledger_reads, judge)] == [(size, 1)]
    assert read_of(ledger_reads, fx / "ledgers" / "spawn.jsonl") == []
    assert by_session(run)["sess-a"].overlays["judge:plan-review"] == 3750 + 1000


def test_an_unfinished_last_line_is_left_for_the_next_read(fx, tmp_path, ledger_reads):
    state = tmp_path / "state"
    judge = fx / "ledgers" / "judge.jsonl"
    run_all(fx, state)
    whole = json.dumps(JUDGE_ROW) + "\n"
    with judge.open("a") as fh:
        fh.write(whole[:30])
    partial = by_session(run_all(fx, state))["sess-a"]
    assert partial.overlays["judge:plan-review"] == 3750
    with judge.open("a") as fh:
        fh.write(whole[30:])
    done = by_session(run_all(fx, state))["sess-a"]
    assert done.overlays["judge:plan-review"] == 4750


def test_truncating_the_ledger_triggers_a_rescan(fx, tmp_path, ledger_reads):
    state = tmp_path / "state"
    judge = fx / "ledgers" / "judge.jsonl"
    run_all(fx, state)
    head = judge.read_text().splitlines(keepends=True)[:2]
    ledger_reads.clear()
    with judge.open("w") as fh:
        fh.write("".join(head))
    run = run_all(fx, state)
    assert [(r["offset"], r["rows"]) for r in read_of(ledger_reads, judge)] == [(0, 2)]
    assert by_session(run)["sess-a"].overlays["judge:plan-review"] == 3750
    assert "judge:acceptance" not in by_session(run)["sess-a"].overlays
    assert "judge:acceptance" not in by_session(run)["sess-b"].overlays


def test_replacing_the_ledger_with_a_new_inode_of_at_least_the_size_triggers_a_rescan(fx, tmp_path, ledger_reads):
    state = tmp_path / "state"
    judge = fx / "ledgers" / "judge.jsonl"
    run_all(fx, state)
    old = judge.stat()
    row = {"ts": "2026-03-01T10:00:50Z", "kind": "call", "judge": "rotated", "duration": 7.0,
           "source": "sess-a", "pad": "x" * 200}
    body = (json.dumps(row) + "\n") * (old.st_size // len(json.dumps(row)) + 2)
    assert len(body) >= old.st_size
    replacement = judge.with_name("judge.new")
    replacement.write_text(body)
    os.replace(replacement, judge)
    assert judge.stat().st_ino != old.st_ino
    ledger_reads.clear()
    run = run_all(fx, state)
    assert [r["offset"] for r in read_of(ledger_reads, judge)] == [0]
    overlays = by_session(run)["sess-a"].overlays
    assert overlays["judge:rotated"] == body.count("\n") * 7000
    assert not any(k.startswith("judge:") and k != "judge:rotated" for k in overlays)


def test_a_missing_ledger_contributes_nothing(fx, tmp_path):
    run = run_all(fx, tmp_path / "state", judge_ledger_path=fx / "ledgers" / "absent.jsonl")
    assert not any(k.startswith("judge:") for p in run.profiles for k in p.overlays)


# ----------------------------------------------------------- laziness and the budget


def synthetic_ledger(path: Path, min_bytes: int) -> int:
    row = json.dumps({"ts": "2026-03-01T10:00:00Z", "kind": "call", "judge": "j", "duration": 1.0,
                      "source": "sess-x", "pad": "p" * 200}) + "\n"
    with path.open("w") as fh:
        for _ in range(min_bytes // len(row) + 1):
            fh.write(row)
    return path.stat().st_size


def test_readers_are_lazy_iterators(fx):
    entries = time_profile.iter_entries(fx / "projects" / "proj" / "sess-a.jsonl")
    assert iter(entries) is entries
    rows = time_profile.iter_ledger_rows(fx / "ledgers" / "judge.jsonl")
    assert iter(rows) is rows


@pytest.mark.parametrize("reader", ["iter_entries", "iter_ledger_rows"])
def test_reading_a_multi_megabyte_file_holds_a_fraction_of_it(tmp_path, reader):
    path = tmp_path / "big.jsonl"
    size = synthetic_ledger(path, 8 * 1024 * 1024)
    assert size >= 8 * 1024 * 1024
    consumed = 0
    tracemalloc.start()
    try:
        for _ in getattr(time_profile, reader)(path):
            consumed += 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert consumed > 1000
    assert peak < size // 4


def test_profiling_a_multi_megabyte_transcript_streams_it(tmp_path):
    path = tmp_path / "big.jsonl"
    row = json.dumps({"type": "assistant", "timestamp": "2026-03-01T10:00:00.000Z",
                      "message": {"id": "m", "content": [{"type": "text", "text": "t" * 200}]}}) + "\n"
    with path.open("w") as fh:
        for _ in range(8 * 1024 * 1024 // len(row) + 1):
            fh.write(row)
    size = path.stat().st_size
    tracemalloc.start()
    try:
        result = time_profile.profile_session(path, lone_ctx())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert isinstance(result, time_profile.SessionProfile)
    assert peak < size // 4


# ------------------------------------------------------------------ module hygiene


def test_the_module_imports_neither_re_nor_regex():
    tree = ast.parse(MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"re", "regex"}


def test_the_gate_log_is_never_opened(fx, tmp_path, monkeypatch):
    for decoy in (fx / "agentctl_state", fx / "ledgers", tmp_path / "state"):
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "gate-log.jsonl").write_text('{"decoy": true}\n')
    monkeypatch.setenv("AGENTCTL_GATE_LOG", str(fx / "agentctl_state" / "gate-log.jsonl"))
    opened = []
    real = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    run_all(fx, tmp_path / "state")
    assert any(name.endswith("sess-a.jsonl") for name in opened)
    assert not [name for name in opened if "gate-log" in name]


# ------------------------------------------- id namespaces, failures, versions, odd inputs


def clock(second: int) -> str:
    return f"2026-03-01T10:{second // 60:02d}:{second % 60:02d}Z"


def prompt_entry(second: int, text: str = "go") -> dict:
    return {"type": "user", "timestamp": clock(second),
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def reply_entry(second: int, message_id: str = "m") -> dict:
    return {"type": "assistant", "timestamp": clock(second),
            "message": {"id": message_id, "content": [{"type": "text", "text": "ok"}]}}


def hook_entry(second: int, command: str, duration_ms: int) -> dict:
    return {"type": "attachment", "timestamp": clock(second),
            "attachment": {"type": "hook_success", "command": command, "durationMs": duration_ms}}


def queued_entry(second: int, **attachment) -> dict:
    return {"type": "attachment", "timestamp": clock(second),
            "attachment": {"type": "queued_command", **attachment}}


def profile_of(tmp_path, *entries):
    result = time_profile.profile_session(write_lines(tmp_path / "s.jsonl", *entries), lone_ctx())
    assert isinstance(result, time_profile.SessionProfile)
    assert time_profile.conservation_error_ms(result) == 0
    return result


def test_overlays_join_when_the_engine_session_id_differs_from_the_harness_id(fx, tmp_path):
    proj = fx / "projects" / "proj"
    write_lines(proj / "sess-h.jsonl", prompt_entry(0), reply_entry(10))
    write_lines(proj / "sess-hc.jsonl", prompt_entry(0), reply_entry(4))
    (fx / "agentctl_state" / "engine-e.json").write_text(json.dumps({"history": [
        {"event": "classify", "ts": clock(0)}, {"event": "plan", "ts": clock(5)},
        {"event": "submit_plan", "ts": clock(20)}]}))
    bindings = fx / "engine-session-bindings"
    bindings.mkdir()
    (bindings / "sess-h.json").write_text(json.dumps({"driven": [{"session": "engine-e", "ts": "x"}]}))
    with (fx / "ledgers" / "spawn.jsonl").open("a") as fh:
        fh.write(json.dumps({"event": "spawn", "kind": "reviewer", "duration_ms": 3000,
                             "spawner_session": "engine-e", "session_id": "sess-h",
                             "child_session_id": "sess-hc"}) + "\n")
    run = by_session(run_all(fx, tmp_path / "state"))
    assert run["sess-h"].overlays == {"node:ROUTED": 5000, "node:PLANNING": 5000, "spawn:reviewer": 3000}
    assert (run["sess-hc"].child_of, run["sess-hc"].kind) == ("sess-h", "reviewer")


def test_one_unreadable_session_is_skipped_and_the_rest_are_still_profiled(fx, tmp_path, monkeypatch):
    real = time_profile._walk_transcript

    def failing(path):
        if path.stem == "sess-b":
            raise RuntimeError("boom")
        return real(path)

    monkeypatch.setattr(time_profile, "_walk_transcript", failing)
    run = run_all(fx, tmp_path / "state")
    assert [(s.session_id, s.reason, s.detail) for s in run.skipped] == [
        ("sess-b", "unparseable", "RuntimeError: boom")]
    assert sorted(by_session(run)) == ["sess-a", "sess-c"]


def test_a_new_walker_version_reparses_transcripts_and_rescans_ledgers(fx, tmp_path, monkeypatch, ledger_reads):
    state = tmp_path / "state"
    judge = fx / "ledgers" / "judge.jsonl"
    run_all(fx, state)
    ledger_reads.clear()
    monkeypatch.setattr(time_profile, "WALKER_VERSION", "after-a-rule-change")
    again = run_all(fx, state)
    assert sorted(Path(p).stem for p in again.reparsed) == SESSIONS
    assert [r["offset"] for r in read_of(ledger_reads, judge)] == [0]
    assert by_session(again)["sess-a"].overlays["judge:plan-review"] == 3750


def test_node_dwell_reads_epoch_second_timestamps_beside_iso_ones(tmp_path):
    state = tmp_path / "engine.json"
    state.write_text(json.dumps({"history": [
        {"event": "classify", "ts": 1772366400.0}, {"event": "plan", "ts": 1772366405.5},
        {"event": "approve", "ts": "2026-03-01T12:00:15Z"}, {"event": "partition", "ts": 1772366420},
        {"event": "note", "ts": True}]}))
    dwell = time_profile.node_dwell(state)
    assert dwell == {"dwell": {"ROUTED": 5500, "PLANNING": 9500, "APPROVED": 5000}, "timed": 4, "untimed": 1}


def test_a_message_typed_mid_turn_is_model_time_but_a_task_notification_is_background(tmp_path):
    profile = profile_of(
        tmp_path, prompt_entry(0), reply_entry(2), queued_entry(5, prompt="also check X"),
        queued_entry(9, prompt="<task-notification>done</task-notification>"),
        queued_entry(12, commandMode="task-notification", prompt="timer"))
    assert profile.partition == {"model:gap": 5000, "wait:background": 7000}


def test_an_inter_agent_message_wakes_an_idle_agent_and_is_background(tmp_path):
    # Shapes of real queued_command attachments: an isMeta message with an origin, commandMode "prompt".
    agent_message = queued_entry(
        5, commandMode="prompt", isMeta=True, origin={"kind": "agent"},
        prompt='<agent-message from="worker-1">review is done</agent-message>')
    cross_session = queued_entry(
        9, commandMode="prompt", isMeta=True, origin={"kind": "peer"},
        prompt="<cross-session-message from=\"b\">ping</cross-session-message>")
    only_tagged = queued_entry(12, commandMode="prompt", prompt='<agent-message from="w">x</agent-message>')
    only_origin = queued_entry(15, commandMode="prompt", origin={"kind": "peer"}, prompt="plain text")
    typed_by_human = queued_entry(24, commandMode="prompt", origin={"kind": "human"}, prompt="and Y")
    typed = queued_entry(20, commandMode="prompt", prompt="also check X")
    profile = profile_of(tmp_path, prompt_entry(0), reply_entry(2),
                         agent_message, cross_session, only_tagged, only_origin, typed,
                         typed_by_human)
    assert profile.partition == {"model:gap": 11000, "wait:background": 13000}


def test_more_open_calls_than_interval_milliseconds_then_a_same_stamp_hook_does_not_crash(tmp_path):
    def stamped(entry, millis):
        entry["timestamp"] = f"2026-03-01T10:00:01.{millis:03d}Z"
        return entry

    calls = {"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}
        for i, name in enumerate(["Glob", "Grep", "Read"])]}}
    results = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"} for i in range(3)]}}
    profile = profile_of(
        tmp_path, prompt_entry(0), stamped(calls, 0), stamped(results, 2),
        stamped(hook_entry(0, "/x/h.sh", 100), 2))
    assert profile.partition == {"model:gap": 1000, "hook:h.sh": 2}


def test_a_hook_after_the_interval_was_already_taken_by_earlier_same_stamp_hooks_is_dropped(tmp_path):
    profile = profile_of(
        tmp_path, prompt_entry(0), reply_entry(1), hook_entry(2, "/x/h1.sh", 5000),
        hook_entry(2, "/x/h2.sh", 700), hook_entry(2, "/x/h3.sh", 700))
    assert profile.partition == {"model:gap": 1000, "hook:h1.sh": 1000}


def test_node_dwell_is_clipped_to_the_transcript_window_of_each_driving_session(fx, tmp_path):
    proj = fx / "projects" / "proj"
    write_lines(proj / "sess-early.jsonl", prompt_entry(0), reply_entry(10))
    write_lines(proj / "sess-late.jsonl", prompt_entry(30), reply_entry(50))
    (fx / "agentctl_state" / "engine-r.json").write_text(json.dumps({"history": [
        {"event": "classify", "ts": clock(0)}, {"event": "plan", "ts": clock(5)},
        {"event": "submit_plan", "ts": clock(40)}, {"event": "approve", "ts": clock(60)}]}))
    bindings = fx / "engine-session-bindings"
    bindings.mkdir()
    for harness in ("sess-early", "sess-late"):
        (bindings / f"{harness}.json").write_text(json.dumps({"driven": [{"session": "engine-r"}]}))
    run = by_session(run_all(fx, tmp_path / "state"))
    assert run["sess-early"].overlays == {"node:ROUTED": 5000, "node:PLANNING": 5000}
    assert run["sess-late"].overlays == {"node:PLANNING": 10000, "node:PLAN_READY": 10000}


def test_parallel_hooks_stamped_alike_each_take_their_duration_out_of_the_interval(tmp_path):
    profile = profile_of(
        tmp_path, prompt_entry(0), reply_entry(1), hook_entry(3, "/x/h1.sh", 500),
        hook_entry(3, "/x/h2.sh", 700), hook_entry(3, "/x/h3.sh", 5000))
    assert profile.partition == {"model:gap": 1000, "hook:h1.sh": 500, "hook:h2.sh": 700, "hook:h3.sh": 800}
    assert profile.coverage["exposed_hook_ms"] == 2000


def test_non_finite_ledger_numbers_are_skipped_and_the_cursor_still_advances(fx, tmp_path):
    state = tmp_path / "state"
    judge, spawn = fx / "ledgers" / "judge.jsonl", fx / "ledgers" / "spawn.jsonl"
    with judge.open("a") as fh:
        fh.write('{"kind":"call","judge":"j","duration":NaN,"source":"sess-a"}\n')
        fh.write('{"kind":"call","judge":"j","duration":Infinity,"source":"sess-a"}\n')
    with spawn.open("a") as fh:
        fh.write('{"event":"spawn","kind":"developer","duration_ms":Infinity,"session_id":"sess-a"}\n')
    run = by_session(run_all(fx, state))
    assert run["sess-a"].overlays == EXPECTED_OVERLAYS["sess-a"]
    saved = json.loads((state / "ledgers.json").read_text())
    assert saved["judge"]["offset"] == judge.stat().st_size
    assert saved["spawn"]["offset"] == spawn.stat().st_size


def test_a_finite_number_too_large_to_scale_is_skipped_not_an_overflow(fx, tmp_path):
    with (fx / "ledgers" / "judge.jsonl").open("a") as fh:
        fh.write('{"kind":"call","judge":"j","duration":1e306,"source":"sess-a"}\n')
    run = by_session(run_all(fx, tmp_path / "state"))
    assert run["sess-a"].overlays == EXPECTED_OVERLAYS["sess-a"]


def test_the_commit_index_follows_trunk_not_the_checked_out_branch(tmp_path):
    import subprocess

    def git(*args, when):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": f"{when} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}
        subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false", *args],
                       check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main", when=1_000_000_000)
    git("commit", "-q", "--allow-empty", "-m", "trunk", when=1_000_000_000)
    git("checkout", "-q", "-b", "topic", when=1_000_000_000)
    git("commit", "-q", "--allow-empty", "-m", "branch", when=1_000_000_100)
    assert [stamp for stamp, _ in time_profile.load_commit_index(tmp_path)] == [1_000_000_000]
    assert time_profile.load_commit_index(tmp_path / "no-such-dir") == []
