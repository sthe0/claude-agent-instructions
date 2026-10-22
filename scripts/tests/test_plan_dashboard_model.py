"""Tests of the plan-dashboard data layer (scripts/plan_dashboard/model.py) and its command line.

Every test builds a throwaway agent home with plan_dashboard_fixtures and points
CLAUDE_CONFIG_DIR at it. The model is always called through the module (``model.collect``)
so the mutation plugin's wrappers reach it.
"""
from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentctl import effort
from agentctl.state import (
    Actor, Criterion, CriterionType, Declaration, Difficulty, GateRecord, Means, Outcome, SessionState,
    Stage, Subject,
)
from lib import transcript_cost
from plan_dashboard import model
from plan_dashboard_fixtures import (
    WriteAttempt, build_fixture_root, read_only_guard, set_mtime, write_transcript,
)

SCRIPTS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
MILLION = 1_000_000
RATE = transcript_cost.token_cost({"input_tokens": MILLION}, "claude-sonnet-5-5")


def at(**delta) -> dt.datetime:
    return NOW - dt.timedelta(**delta)


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def event(name: str, moment: dt.datetime, **fields) -> dict:
    return {"event": name, "ts": iso(moment), **fields}


def make_stage(index: int, status: str) -> Stage:
    return Stage(
        index=index, title=f"Stage {index}", subject=Subject(material="m", result="r"),
        means=Means(means="m", method="x"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type=CriterionType.MEASURABLE.value, done_criterion="d"),
        outcome=Outcome(status=status),
    )


def make_state(sid: str, *, history=(), stages=(), node="EXECUTING", **fields) -> SessionState:
    fields.setdefault("weight_class", "SUBSTANTIVE")
    fields.setdefault("approval", GateRecord("plan_approval", passed=node not in ("CLASSIFIED", "ROUTED", "PLAN_READY")))
    fields.setdefault("resolution", GateRecord("resolution", passed=node == "RESOLVED"))
    return SessionState(
        session_id=sid, task_id=f"task-{sid}", goal=fields.pop("goal", f"goal of {sid}"), node=node,
        history=list(history),
        stages=[make_stage(i, status) for i, status in enumerate(stages, 1)], **fields,
    )


def usage_row(message_id: str, moment, million_input_tokens: int) -> dict:
    return {"message_id": message_id, "ts": moment, "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": million_input_tokens * MILLION, "output_tokens": 0}}


def rows_by_session(snapshot: dict) -> dict:
    return {task["session_id"]: task for task in snapshot["tasks"]}


@contextlib.contextmanager
def recorded_opens():
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def spy(real):
        def wrapper(file, *args, **kwargs):
            if not isinstance(file, int):
                opened.append(os.fspath(file))
            return real(file, *args, **kwargs)
        return wrapper

    builtins.open, io.open, os.open = spy(real_open), spy(real_io_open), spy(real_os_open)
    try:
        yield opened
    finally:
        builtins.open, io.open, os.open = real_open, real_io_open, real_os_open


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    model.clear_cache()
    yield
    model.clear_cache()


def cli_args(root, *extra) -> list[str]:
    return ["snapshot", "--state-root", str(root.state_root), "--cost-log", str(root.cost_log),
            "--transcripts-root", str(root.transcripts_root), *extra]


def collect(root, **kwargs) -> dict:
    return model.collect(root.state_root, now=kwargs.pop("now", NOW), cost_log=root.cost_log, **kwargs)


def detail(root, sid, **kwargs) -> dict:
    return model.detail(root.state_root, sid, cost_log=root.cost_log,
                        transcripts_root=root.transcripts_root, **kwargs)


# --- liveness and selection ----------------------------------------------------------------

def liveness_sessions():
    return [
        make_state("s-resolved", node="RESOLVED", history=[event("resolve", at(hours=3))]),
        make_state("s-stub", node="CLASSIFIED", weight_class=None),
        make_state("s-active-recent", history=[event("approve", at(minutes=10))]),
        make_state("s-active-pid", history=[event("approve", at(hours=3))], effort_child_runs=[
            {"child_session": "c", "stage_index": 1, "started": at(hours=3).timestamp(),
             "ended": None, "dispatcher_pid": os.getpid()}]),
        make_state("s-idle", history=[event("approve", at(hours=3))]),
        make_state("s-stale", history=[event("approve", at(hours=30))]),
        make_state("s-stale-fresh-mtime", history=[event("approve", at(hours=30))]),
        make_state("s-dead-run", history=[event("approve", at(hours=3))], effort_child_runs=[
            {"child_session": "c", "stage_index": 1, "started": at(hours=3).timestamp(),
             "ended": None, "dispatcher_pid": 0}]),
    ]


def liveness_root(tmp_path):
    root = build_fixture_root(tmp_path, liveness_sessions(), now=NOW)
    set_mtime(root.state_root / "s-stale-fresh-mtime.json", NOW)
    return root


def test_each_liveness_value_follows_its_rule(tmp_path):
    rows = rows_by_session(collect(liveness_root(tmp_path), include_all=True))
    assert {sid: row["liveness"] for sid, row in rows.items()} == {
        "s-resolved": "resolved", "s-stub": "stub", "s-active-recent": "active", "s-active-pid": "active",
        "s-idle": "idle", "s-stale": "stale", "s-stale-fresh-mtime": "stale", "s-dead-run": "idle",
    }


def test_default_selection_shows_only_active_and_idle(tmp_path):
    snapshot = collect(liveness_root(tmp_path))
    assert set(rows_by_session(snapshot)) == {"s-active-recent", "s-active-pid", "s-idle", "s-dead-run"}
    assert snapshot["schema"] == "plan-dashboard/v1"


def test_all_widens_to_stub_and_session_pins_a_resolved_task(tmp_path):
    root = liveness_root(tmp_path)
    assert "s-stub" in rows_by_session(collect(root, include_all=True))
    pinned = rows_by_session(collect(root, sessions=["s-resolved"]))
    assert pinned["s-resolved"]["liveness"] == "resolved"
    assert "s-stub" not in pinned and "s-stale" not in pinned


def test_window_hours_moves_the_idle_stale_boundary(tmp_path):
    rows = rows_by_session(collect(liveness_root(tmp_path), include_all=True, window_hours=2))
    assert rows["s-idle"]["liveness"] == "stale"
    assert rows["s-active-recent"]["liveness"] == "active"


def test_out_of_window_parent_of_an_in_window_child_is_read_and_others_are_not(tmp_path):
    sessions = [
        make_state("parent", history=[event("approve", at(hours=40))]),
        make_state("child", history=[event("approve", at(hours=1))],
                   parent_link={"parent_session": "parent", "stage_index": 1}),
        make_state("far-away", history=[event("approve", at(hours=50))]),
    ]
    root = build_fixture_root(tmp_path, sessions, now=NOW)
    with recorded_opens() as opened:
        snapshot = collect(root)
    names = {Path(p).name for p in opened}
    assert "parent.json" in names and "child.json" in names
    assert "far-away.json" not in names
    assert set(rows_by_session(snapshot)) == {"child"}
    assert rows_by_session(snapshot)["child"]["parent_session"] == "parent"


def test_sidecar_files_are_not_tasks(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("real", history=[event("approve", at(hours=1))])], now=NOW)
    (root.state_root / "real.delivery.json").write_text("{}", encoding="utf-8")
    (root.state_root / "alias.json").write_text(json.dumps({"session_id": "real"}), encoding="utf-8")
    assert set(rows_by_session(collect(root, include_all=True))) == {"real"}


# --- summary fields -------------------------------------------------------------------------

def test_counts_stage_progress_and_difficulties(tmp_path):
    state = make_state(
        "counted", stages=["PASSED", "SKIPPED", "ACTIVE", "PENDING"], current_stage=3,
        review_rounds=4, plan_review_rounds=2,
        history=[event("approve", at(hours=2)), event("replan", at(hours=1, minutes=30)),
                 event("replan", at(hours=1)), event("replan", at(minutes=50), change_class="no_change")],
        closed_difficulties=[{"declaration": {"mismatch": "old problem"}}],
        difficulty=Difficulty(declaration=Declaration(expected="e", actual="a", mismatch="the live problem")),
    )
    row = rows_by_session(collect(build_fixture_root(tmp_path, [state], now=NOW)))["counted"]
    assert (row["replans"], row["review_rounds"], row["plan_review_rounds"]) == (2, 4, 2)
    assert (row["current_stage"], row["stages_done"], row["stages_total"]) == (3, 2, 4)
    assert row["difficulties"] == {"open": 1, "stacked": 0, "closed": 1}
    assert row["open_difficulty"] == "the live problem"
    assert row["goal"] == "goal of counted" and row["node"] == "EXECUTING"
    assert row["last_activity"] == iso(at(minutes=50))


def test_last_activity_falls_back_to_file_mtime_without_history(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("quiet")], now=NOW)
    set_mtime(root.state_root / "quiet.json", at(minutes=5))
    row = rows_by_session(collect(root))["quiet"]
    assert row["last_activity"] == iso(at(minutes=5)) and row["liveness"] == "active"


def test_every_time_field_is_a_recorded_timestamp_not_an_age(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("s", history=[event("approve", at(hours=2))])], now=NOW)
    later = rows_by_session(collect(root, now=NOW + dt.timedelta(minutes=40)))["s"]
    assert later == rows_by_session(collect(root))["s"]


# --- unreadable and legacy files -----------------------------------------------------------

def broken_root(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("fine", history=[event("approve", at(hours=1))])], now=NOW)
    (root.state_root / "torn.json").write_text('{"session_id": "torn", "node": ', encoding="utf-8")
    return root


def test_half_written_file_is_reported_unreadable_not_raised(tmp_path):
    root = broken_root(tmp_path)
    row = rows_by_session(collect(root))["torn"]
    assert row["readable"] is False and row["reason"] and row["totals"] is None
    assert rows_by_session(collect(root))["fine"]["readable"] is True


def test_unreadable_detail_has_the_full_key_set_with_null_values(tmp_path):
    root = broken_root(tmp_path)
    result = detail(root, "torn")
    assert set(result) == set(model._DETAIL_KEYS)
    assert result["readable"] is False and result["reason"]
    assert result["plan_path"] is None and result["totals"] is None and result["goal"] is None
    for key in ("stages", "open_difficulties", "closed_difficulties", "children", "history", "phases",
                "harness_sessions"):
        assert result[key] == []


def test_unreadable_task_prints_as_unreadable_in_text(tmp_path, capsys):
    assert cli.main(cli_args(broken_root(tmp_path), "--format", "text", "--all")) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("torn"))
    assert "stage ?/?" in line and "unreadable" in line


def test_a_file_unreadable_once_is_read_again(tmp_path, monkeypatch):
    root = build_fixture_root(tmp_path, [make_state("flaky", history=[event("approve", at(hours=1))])], now=NOW)
    real = Path.read_text
    calls = []

    def torn_first(self, *args, **kwargs):
        if self.name == "flaky.json" and not calls:
            calls.append(self)
            return '{"session_id": '
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", torn_first)
    assert rows_by_session(collect(root))["flaky"]["readable"] is True


def test_the_second_read_of_a_torn_file_waits_for_the_writer(tmp_path, monkeypatch):
    root = build_fixture_root(tmp_path, [make_state("flaky", history=[event("approve", at(hours=1))])], now=NOW)
    real = Path.read_text
    torn = []

    def torn_first(self, *args, **kwargs):
        if self.name == "flaky.json" and not torn:
            torn.append(self)
            return '{"session_id": '
        return real(self, *args, **kwargs)

    pauses = []
    monkeypatch.setattr(Path, "read_text", torn_first)
    monkeypatch.setattr(model.time, "sleep", pauses.append)
    assert rows_by_session(collect(root))["flaky"]["readable"] is True
    assert len(pauses) == 1 and 0 < pauses[0] <= 1


def test_a_file_read_cleanly_the_first_time_is_not_waited_for(tmp_path, monkeypatch):
    root = build_fixture_root(tmp_path, [make_state("calm", history=[event("approve", at(hours=1))])], now=NOW)
    pauses = []
    monkeypatch.setattr(model.time, "sleep", pauses.append)
    collect(root)
    assert pauses == []


def test_replan_count_accepts_the_history_only_stand_in_the_summary_passes():
    history = [event("replan", at(hours=3)), event("replan", at(hours=2), change_class="no_change"),
               event("approve", at(hours=1)), event("replan", at(minutes=30))]
    real = make_state("counted", history=history)
    stand_in = types.SimpleNamespace(history=history)
    assert effort.replan_count(stand_in) == effort.replan_count(real) == 2


def test_legacy_dict_missing_keys_is_still_summarized(tmp_path):
    root = build_fixture_root(tmp_path, [], now=NOW)
    (root.state_root / "legacy.json").write_text(json.dumps({"session_id": "legacy", "node": "EXECUTING"}),
                                                  encoding="utf-8")
    row = rows_by_session(collect(root))["legacy"]
    assert row["readable"] is True and row["stages_total"] == 0 and row["replans"] == 0
    assert detail(root, "legacy")["stages"] == []


# --- detail ---------------------------------------------------------------------------------

def test_detail_carries_the_task_stages_children_history_and_bindings(tmp_path):
    parent = make_state(
        "parent", stages=["PASSED", "ACTIVE"], current_stage=2, plan_path="/plans/p.toml",
        tracker_key="ABC-1", repo_root="/repo", history=[event("note", at(hours=3, minutes=i)) for i in range(25)],
        closed_difficulties=[{"declaration": {"actual": "was broken"}}],
        difficulty=Difficulty(declaration=Declaration(expected="e", actual="a", mismatch="open now")),
    )
    child = make_state("child", history=[event("approve", at(hours=1))],
                       parent_link={"parent_session": "parent", "stage_index": 2})
    root = build_fixture_root(tmp_path, [parent, child], bindings={"harness-1": ["parent"]}, now=NOW)
    result = detail(root, "parent")
    assert (result["goal"], result["node"], result["current_stage"]) == ("goal of parent", "EXECUTING", 2)
    assert [(s["index"], s["status"], s["actor"], s["criterion_type"]) for s in result["stages"]] == [
        (1, "PASSED", "in_thread", "measurable"), (2, "ACTIVE", "in_thread", "measurable")]
    assert result["children"] == [{"session_id": "child", "stage_index": 2, "node": "EXECUTING",
                                   "goal": "goal of child"}]
    assert len(result["history"]) == 20 and result["history"][-1]["ts"] == iso(at(hours=3, minutes=24))
    assert result["harness_sessions"] == ["harness-1"]
    assert [d["title"] for d in result["open_difficulties"]] == ["open now"]
    assert [d["title"] for d in result["closed_difficulties"]] == ["was broken"]
    assert (result["plan_path"], result["tracker_key"], result["repo_root"]) == ("/plans/p.toml", "ABC-1", "/repo")
    assert set(result) == set(model._DETAIL_KEYS)


def test_detail_of_an_unknown_id_raises_unknown_session(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("known")], now=NOW)
    with pytest.raises(model.UnknownSession):
        detail(root, "00000000-0000-4000-8000-000000000000")
    assert issubclass(model.UnknownSession, KeyError)


def test_unknown_session_option_exits_3(tmp_path, capsys):
    root = build_fixture_root(tmp_path, [make_state("known")], now=NOW)
    assert cli.main(cli_args(root, "--session", "00000000-0000-4000-8000-000000000000")) == 3
    assert "no such session" in capsys.readouterr().err


def test_cli_looks_collect_and_detail_up_on_the_module(tmp_path, monkeypatch, capsys):
    root = build_fixture_root(tmp_path, [make_state("only", history=[event("approve", at(hours=1))])], now=NOW)
    reached = []
    real_collect, real_detail = model.collect, model.detail
    monkeypatch.setattr(model, "collect", lambda *a, **k: reached.append("collect") or real_collect(*a, **k))
    monkeypatch.setattr(model, "detail", lambda *a, **k: reached.append("detail") or real_detail(*a, **k))
    assert cli.main(cli_args(root, "--all", "--detail")) == 0
    assert reached == ["collect", "detail"]
    assert '"detail"' in capsys.readouterr().out


# --- serializer and digest -----------------------------------------------------------------

def idle_root(tmp_path):
    return build_fixture_root(tmp_path, [make_state("idle-one", history=[event("approve", at(hours=3))])], now=NOW)


def test_digest_is_stable_across_now_inside_a_liveness_band(tmp_path):
    root = idle_root(tmp_path)
    first = collect(root)
    second = collect(root, now=NOW + dt.timedelta(hours=1))
    assert first["generated_at"] != second["generated_at"]
    assert model.content_digest(first) == model.content_digest(second)


def test_digest_changes_after_a_file_change(tmp_path):
    root = idle_root(tmp_path)
    before = model.content_digest(collect(root))
    path = root.state_root / "idle-one.json"
    path.write_text(path.read_text(encoding="utf-8").replace("goal of idle-one", "another goal"), encoding="utf-8")
    assert model.content_digest(collect(root)) != before


def test_cli_json_is_exactly_the_serializer_bytes(tmp_path, monkeypatch, capsys):
    root = idle_root(tmp_path)
    captured = []
    real = model.collect

    def pinned(*args, **kwargs):
        snapshot = real(*args, **{**kwargs, "now": NOW})
        captured.append(snapshot)
        return snapshot

    monkeypatch.setattr(model, "collect", pinned)
    assert cli.main(cli_args(root, "--format", "json")) == 0
    assert capsys.readouterr().out == model.to_json(captured[0]) + "\n"
    assert '"session_id": "idle-one"' in model.to_json(captured[0])


def test_text_shows_a_long_goal_on_one_cut_line(tmp_path, capsys):
    goal = "first line\nsecond " + "word " * 60
    root = build_fixture_root(tmp_path, [make_state("wordy", goal=goal, history=[event("approve", at(hours=1))])],
                              now=NOW)
    assert cli.main(cli_args(root, "--format", "text", "--all")) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert out.rstrip().endswith("…") and "first line second word" in out
    assert len(out.split("  ")[-1].rstrip()) <= cli.GOAL_WIDTH


def test_text_prefixes_a_partial_total_with_a_lower_bound_sign(tmp_path, capsys):
    root = build_fixture_root(tmp_path, [make_state("costed", history=[event("approve", at(hours=1))])],
                              ledger_rows=[{"spawner_session": "costed", "ts": iso(at(minutes=30)), "cost_usd": 1.5}],
                              now=NOW)
    assert cli.main(cli_args(root, "--format", "text", "--all")) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("costed"))
    assert "≥$1.50" in line and "root not counted" in line


# --- phases ---------------------------------------------------------------------------------

T0 = NOW - dt.timedelta(hours=6)
PLAN = "/plans/p.toml"


def m(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def full_history() -> list[dict]:
    return [
        event("classify", m(0)), event("submit_plan", m(10)), event("plan_review", m(20)),
        event("plan_review", m(30)), event("submit_plan", m(40)), event("approve", m(50)),
        event("next_stage", m(60), stage=1), event("record_result", m(80), stage=1, status="PASSED"),
        event("next_stage", m(85), stage=2), event("difficulty_open", m(90), uid="d1"),
        event("record_result_attempt", m(95), stage=2), event("difficulty_close", m(100), uid="d1"),
        event("record_result", m(110), stage=2, status="PASSED"), event("difficulty_open", m(120), uid="d2"),
        event("next_stage", m(125), stage=3), event("record_result", m(140), stage=3, status="PASSED"),
        event("verify_final", m(150)), event("resolve", m(160)),
    ]


def phased_state() -> SessionState:
    return make_state(
        "phased", node="RESOLVED", plan_path=PLAN, history=full_history(), stages=["PASSED"] * 3,
        effort_child_runs=[{"child_session": "kid", "stage_index": 2, "started": m(86).timestamp(),
                            "ended": m(100).timestamp(), "dispatcher_pid": 0}],
    )


OWN_LEDGER = [
    {"spawner_session": "phased", "stage_index": 2, "ts": iso(m(30)), "cost_usd": 1.0},
    {"spawner_session": "phased", "review_pair": "1-2", "ts": iso(m(15)), "cost_usd": 0.5},
    {"spawner_session": "phased", "ts": iso(m(62)), "cost_usd": 0.25},
    {"spawner_session": "phased", "ts": iso(m(-5)), "cost_usd": 0.125},
    {"plan_path": PLAN, "stage_index": 1, "ts": iso(m(63)), "cost_usd": 2.0},
]
FOREIGN_LEDGER = [
    {"spawner_session": "other", "ts": iso(m(62)), "cost_usd": 100},
    {"plan_path": "/plans/other.toml", "ts": iso(m(62)), "cost_usd": 50},
    {"spawner_session": "other", "plan_path": PLAN, "ts": iso(m(62)), "cost_usd": 70},
]
LEDGER = OWN_LEDGER + FOREIGN_LEDGER
OWN_SPAWN = sum(row["cost_usd"] for row in OWN_LEDGER)

# usage rows in millions of input tokens: outside the task (before, after), inside it, and undated
TRANSCRIPT = [
    usage_row("m-before", m(-60), 5), usage_row("m-plan", m(15), 1), usage_row("m-stage1", m(62), 2),
    usage_row("m-after", m(170), 6), usage_row("m-no-ts", None, 3), usage_row("m-child", m(88), 4),
]
TASK_ROOT = RATE * (1 + 2 + 3 + 4)


def phased_root(tmp_path, *, transcript=True):
    return build_fixture_root(tmp_path, [phased_state()], ledger_rows=LEDGER, now=NOW,
                              transcripts={"phased": TRANSCRIPT} if transcript else None)


def by_label(items: list[dict]) -> dict:
    return {item["label"]: item for item in items}


def test_phase_windows_come_from_the_history(tmp_path):
    items = detail(phased_root(tmp_path), "phased")["phases"]
    assert [(i["kind"], i["label"]) for i in items] == [
        ("planning", "planning"), ("plan", "plan v1"), ("plan", "plan v2"), ("stage", "stage 1"),
        ("stage", "stage 2"), ("difficulty", "difficulty d1"), ("difficulty", "difficulty d2"),
        ("stage", "stage 3"), ("resolution", "resolution"), ("unattributed", "unattributed")]
    phases = by_label(items)
    spans = {label: (p["start"], p["end"], p["wall_clock_s"]) for label, p in phases.items()}
    assert spans["planning"] == (iso(m(0)), iso(m(10)), 600)
    assert spans["plan v1"] == (iso(m(10)), iso(m(40)), 1800)
    assert spans["plan v2"] == (iso(m(40)), iso(m(50)), 600)
    assert spans["stage 1"] == (iso(m(60)), iso(m(80)), 1200)
    assert spans["stage 2"] == (iso(m(85)), iso(m(110)), 1500)
    assert spans["difficulty d1"] == (iso(m(90)), iso(m(100)), 600)
    assert spans["difficulty d2"] == (iso(m(120)), None, None)
    assert spans["resolution"] == (iso(m(150)), iso(m(160)), 600)
    assert spans["unattributed"] == (None, None, None)
    assert phases["stage 2"]["attempts"] == 2 and phases["stage 1"]["attempts"] == 1
    rounds = by_label(phases["plan v1"]["children"])
    assert [(r["start"], r["end"]) for r in rounds.values()] == [(iso(m(10)), iso(m(20))), (iso(m(20)), iso(m(30)))]
    assert list(rounds) == ["review round 1", "review round 2"]
    child = phases["stage 2"]["children"][0]
    assert (child["kind"], child["label"], child["wall_clock_s"]) == ("child run", "kid", 840)


def test_estimate_flags_mark_inferred_values_only(tmp_path):
    phases = by_label(detail(phased_root(tmp_path), "phased")["phases"])
    assert phases["planning"]["estimate"]["wall_clock_s"] is True
    assert phases["plan v1"]["children"][0]["estimate"]["wall_clock_s"] is True
    for label in ("plan v1", "stage 1", "stage 2", "difficulty d1", "resolution"):
        assert phases[label]["estimate"]["wall_clock_s"] is False
        assert phases[label]["estimate"]["spawn_cost_usd"] is False
        assert phases[label]["estimate"]["root_cost_usd"] is True


def test_spawn_rows_are_attributed_by_stage_then_plan_then_time_and_foreign_rows_are_excluded(tmp_path):
    phases = by_label(detail(phased_root(tmp_path), "phased")["phases"])
    spent = {label: (p["spawn_cost_usd"], p["spawn_count"]) for label, p in phases.items() if p["spawn_count"]}
    assert spent == {"stage 2": (1.0, 1), "plan v1": (0.5, 1), "stage 1": (2.25, 2), "unattributed": (0.125, 1)}


def test_nested_windows_report_spawn_as_not_attributed_not_as_zero(tmp_path):
    phases = by_label(detail(phased_root(tmp_path), "phased")["phases"])
    nested = [*phases["plan v1"]["children"], *phases["stage 2"]["children"]]
    assert [c["kind"] for c in nested] == ["review round", "review round", "child run"]
    for child in nested:
        assert child["spawn_cost_usd"] is None and child["spawn_count"] is None
    assert phases["plan v1"]["spawn_cost_usd"] == pytest.approx(0.5)
    assert phases["stage 3"]["spawn_cost_usd"] == 0 and phases["stage 3"]["spawn_count"] == 0


def test_text_detail_prints_nested_spawn_as_not_attributed(tmp_path, capsys):
    assert cli.main(cli_args(phased_root(tmp_path), "--format", "text", "--session", "phased", "--detail")) == 0
    lines = {l.split(":")[0].strip(): l for l in capsys.readouterr().out.splitlines() if ": wall" in l}
    assert "spawn not attributed" in lines["review round 1"] and "spawn not attributed" in lines["kid"]
    assert "spawn $0.50" in lines["plan v1"] and "spawn $0.00" in lines["stage 3"]


def test_json_detail_carries_the_totals_once_per_task_and_counts_the_root(tmp_path, capsys):
    assert cli.main(cli_args(phased_root(tmp_path), "--session", "phased", "--detail")) == 0
    task = json.loads(capsys.readouterr().out)["tasks"][0]
    assert "totals" not in task["detail"]
    assert task["totals"]["root_counted"] is True and task["totals"]["root_cost_usd"] == pytest.approx(TASK_ROOT)


def test_attribute_spawn_prefers_the_stage_index_over_the_timestamp():
    windows = [
        model._window("plan", "plan v1", m(0), m(60)),
        model._window("stage", "stage 1", m(10), m(20), _stage=1),
        model._window("unattributed", "unattributed", None, None),
    ]
    assert model.attribute_spawn({"stage_index": 1, "ts": iso(m(30))}, windows)["label"] == "stage 1"
    assert model.attribute_spawn({"review_pair": "x", "ts": iso(m(15))}, windows)["label"] == "plan v1"
    assert model.attribute_spawn({"ts": iso(m(15))}, windows)["label"] == "stage 1"
    assert model.attribute_spawn({"ts": iso(m(70))}, windows)["label"] == "unattributed"
    assert model.attribute_spawn({"stage_index": 9}, windows)["label"] == "unattributed"


def test_root_cost_is_time_sliced_dropping_rows_outside_the_task_and_flagged_estimate(tmp_path):
    result = detail(phased_root(tmp_path), "phased")
    phases = by_label(result["phases"])
    assert {label: p["root_cost_usd"] for label, p in phases.items()} == pytest.approx({
        "planning": 0, "plan v1": RATE * 1, "plan v2": 0, "stage 1": RATE * 2, "stage 2": RATE * 4,
        "difficulty d1": 0, "difficulty d2": 0, "stage 3": 0, "resolution": 0, "unattributed": RATE * 3})
    assert phases["plan v1"]["children"][0]["root_cost_usd"] == pytest.approx(RATE * 1)
    assert phases["stage 2"]["children"][0]["root_cost_usd"] == pytest.approx(RATE * 4)
    totals = result["totals"]
    assert totals["root_cost_usd"] == pytest.approx(TASK_ROOT)
    assert totals["spawn_cost_usd"] == pytest.approx(OWN_SPAWN)
    assert totals["cost_usd"] == pytest.approx(OWN_SPAWN + TASK_ROOT)
    assert totals["wall_clock_s"] == 9600
    assert totals["root_counted"] is True and totals["partial"] is False
    assert totals["estimate"] == {"wall_clock_s": False, "spawn_cost_usd": False, "root_cost_usd": True,
                                  "cost_usd": True}


def test_no_transcript_gives_a_null_root_cost_never_zero(tmp_path):
    result = detail(phased_root(tmp_path, transcript=False), "phased")

    def walk(items):
        for item in items:
            assert item["root_cost_usd"] is None
            walk(item["children"])

    walk(result["phases"])
    totals = result["totals"]
    assert totals["root_cost_usd"] is None and totals["root_counted"] is False and totals["partial"] is True
    assert totals["cost_usd"] == pytest.approx(totals["spawn_cost_usd"])
    assert '"phases"' in model.to_json(result) and '"totals"' in model.to_json(result)


def test_empty_history_gives_only_unattributed_and_no_wall_clock(tmp_path):
    root = build_fixture_root(tmp_path, [make_state("empty")], now=NOW)
    result = detail(root, "empty")
    assert [p["kind"] for p in result["phases"]] == ["unattributed"]
    assert result["totals"]["wall_clock_s"] is None and result["totals"]["estimate"]["wall_clock_s"] is False


def in_flight_sessions():
    start = at(hours=3, minutes=30)
    return [
        make_state("planning-only", node="CLASSIFIED", history=[event("classify", start)]),
        make_state("awaiting-approval", node="PLAN_READY",
                   history=[event("classify", start), event("submit_plan", at(hours=3))]),
        make_state("running-stage", history=[
            event("classify", start), event("submit_plan", at(hours=3, minutes=20)),
            event("approve", at(hours=3, minutes=10)), event("next_stage", at(hours=3), stage=1)],
            stages=["ACTIVE"], effort_child_runs=[
                {"child_session": "kid", "stage_index": 1, "started": at(hours=3).timestamp(), "ended": None,
                 "dispatcher_pid": 0}]),
        make_state("closing", node="VERIFYING", history=[event("classify", start), event("verify_final", at(hours=3))]),
    ]


def test_open_windows_have_a_start_and_no_end_and_do_not_move_with_now(tmp_path):
    root = build_fixture_root(tmp_path, in_flight_sessions(), now=NOW)
    later = NOW + dt.timedelta(minutes=40)
    first_snapshot, later_snapshot = collect(root), collect(root, now=later)
    assert model.content_digest(first_snapshot) == model.content_digest(later_snapshot)
    assert {row["liveness"] for row in first_snapshot["tasks"]} == {"idle"}
    for sid, kind in (("planning-only", "planning"), ("awaiting-approval", "plan"),
                      ("running-stage", "stage"), ("closing", "resolution")):
        phases = [p for p in detail(root, sid)["phases"] if p["kind"] == kind]
        assert len(phases) == 1
        assert phases[0]["start"] is not None and phases[0]["end"] is None and phases[0]["wall_clock_s"] is None
    stage = next(p for p in detail(root, "running-stage")["phases"] if p["kind"] == "stage")
    assert (stage["children"][0]["end"], stage["children"][0]["wall_clock_s"]) == (None, None)


def test_detail_phases_and_totals_read_no_clock(tmp_path, monkeypatch):
    root = build_fixture_root(tmp_path, in_flight_sessions(), now=NOW)
    first = {sid: detail(root, sid) for sid in ("planning-only", "awaiting-approval", "running-stage", "closing")}

    def no_clock(*args, **kwargs):
        raise AssertionError("detail read the clock")

    class NoNow(dt.datetime):
        now = utcnow = today = classmethod(no_clock)

    # the names the model looks up in its own namespace, so the stdlib modules stay untouched
    monkeypatch.setattr(model, "dt", types.SimpleNamespace(
        datetime=NoNow, timezone=dt.timezone, timedelta=dt.timedelta))
    monkeypatch.setattr(model, "time", types.SimpleNamespace(
        time=no_clock, monotonic=no_clock, sleep=lambda _: None))
    second = {sid: detail(root, sid) for sid in first}
    assert first == second


# --- totals ---------------------------------------------------------------------------------

def test_summary_totals_never_read_a_transcript_and_stay_partial(tmp_path):
    root = phased_root(tmp_path)
    row = rows_by_session(collect(root, include_all=True))["phased"]
    totals = row["totals"]
    assert totals["root_cost_usd"] is None and totals["root_counted"] is False and totals["partial"] is True
    assert totals["cost_usd"] == totals["spawn_cost_usd"] == pytest.approx(OWN_SPAWN)
    assert totals["estimate"]["cost_usd"] is False


def test_collect_opens_no_transcript_and_a_new_usage_row_leaves_the_digest_alone(tmp_path):
    root = phased_root(tmp_path)
    with recorded_opens() as opened:
        before = collect(root, include_all=True)
    assert not [p for p in opened if p.endswith(".jsonl") and "projects" in p]
    write_transcript(root.transcripts_root, "phased", [*TRANSCRIPT, usage_row("m-new", m(70), 7)])
    assert model.content_digest(collect(root, include_all=True)) == model.content_digest(before)


# --- cache bound ---------------------------------------------------------------------------

def twenty_task_root(tmp_path):
    sessions = [make_state(f"in-{i:02d}", history=[event("approve", at(hours=1, minutes=i))]) for i in range(10)]
    sessions += [make_state(f"out-{i:02d}", history=[event("approve", at(hours=30 + i))]) for i in range(10)]
    return build_fixture_root(tmp_path, sessions, ledger_rows=LEDGER, now=NOW)


def test_second_collect_opens_neither_the_ledger_nor_out_of_window_state(tmp_path):
    root = twenty_task_root(tmp_path)
    with recorded_opens() as first:
        collect(root)
    with recorded_opens() as second:
        collect(root)
    assert str(root.cost_log) in first and str(root.cost_log) not in second
    for opened in (first, second):
        assert not [p for p in opened if Path(p).name.startswith("out-")]
    assert len([p for p in second if Path(p).name.startswith("in-")]) == 10


def test_ledger_is_read_again_when_it_changes(tmp_path):
    root = twenty_task_root(tmp_path)
    before = rows_by_session(collect(root, include_all=True))
    row = {"spawner_session": "in-00", "ts": iso(at(minutes=5)), "cost_usd": 4.0}
    with open(root.cost_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    after = rows_by_session(collect(root, include_all=True))
    assert after["in-00"]["totals"]["spawn_cost_usd"] == before["in-00"]["totals"]["spawn_cost_usd"] + 4.0


def reads_spy(monkeypatch) -> list[tuple[int, int]]:
    reads: list[tuple[int, int]] = []
    real = model._read_from

    def spy(path, offset):
        data = real(path, offset)
        reads.append((offset, len(data)))
        return data

    monkeypatch.setattr(model, "_read_from", spy)
    return reads


def append(path: Path, data: bytes) -> None:
    with open(path, "ab") as fh:
        fh.write(data)


def test_second_detail_opens_no_transcript_and_an_append_is_read_incrementally(tmp_path, monkeypatch):
    root = phased_root(tmp_path)
    transcript = root.transcripts_root / "-fixture-project" / "phased.jsonl"
    detail(root, "phased")
    with recorded_opens() as opened:
        detail(root, "phased")
    assert str(transcript) not in opened
    reads = reads_spy(monkeypatch)
    line = (json.dumps({"type": "assistant", "timestamp": iso(m(70)), "message": {
        "id": "m-appended", "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 8 * MILLION, "output_tokens": 0}}}) + "\n").encode()
    partial = b'{"type": "assistant", "timest'
    append(transcript, line + partial)
    grown = detail(root, "phased")
    assert grown["totals"]["root_cost_usd"] == pytest.approx(TASK_ROOT + RATE * 8)
    assert len(reads) == 1 and reads[0][1] == len(line) + len(partial)
    reads.clear()
    detail(root, "phased")
    assert reads == []


def test_a_replaced_transcript_is_read_whole(tmp_path, monkeypatch):
    root = phased_root(tmp_path)
    transcript = root.transcripts_root / "-fixture-project" / "phased.jsonl"
    detail(root, "phased")
    reads = reads_spy(monkeypatch)
    replacement = transcript.with_name("replacement.tmp")
    replacement.write_text(
        json.dumps({"type": "assistant", "timestamp": iso(m(62)), "message": {
            "id": "m-replaced", "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": MILLION, "output_tokens": 0}}}) + "\n", encoding="utf-8")
    os.replace(replacement, transcript)
    assert detail(root, "phased")["totals"]["root_cost_usd"] == pytest.approx(RATE * 1)
    assert reads == [(0, transcript.stat().st_size)]


def test_a_same_length_in_place_rewrite_of_a_state_file_is_seen(tmp_path):
    state = make_state("flip", stages=["ACTIVE"], history=[event("approve", at(hours=1))])
    root = build_fixture_root(tmp_path, [state], now=NOW)
    assert rows_by_session(collect(root))["flip"]["stages_done"] == 0
    path = root.state_root / "flip.json"
    text = path.read_text(encoding="utf-8")
    assert text.count('"status": "ACTIVE"') == 1
    path.write_text(text.replace('"status": "ACTIVE"', '"status": "PASSED"'), encoding="utf-8")
    set_mtime(path, at(minutes=1))
    assert rows_by_session(collect(root))["flip"]["stages_done"] == 1


def test_cache_advance_runs_under_the_cache_lock_and_threads_agree(tmp_path, monkeypatch):
    root = phased_root(tmp_path)
    transcript = root.transcripts_root / "-fixture-project" / "phased.jsonl"
    real = model._read_transcript

    def locked_advance(path):
        assert model.CACHE_LOCK.locked(), "transcript cache advanced without CACHE_LOCK"
        result = real(path)
        assert model.CACHE_LOCK.locked(), "transcript cache advanced without CACHE_LOCK"
        return result

    monkeypatch.setattr(model, "_read_transcript", locked_advance)
    cold = detail(root, "phased")["totals"]["root_cost_usd"]
    model.clear_cache()

    def threaded() -> list[float]:
        barrier = threading.Barrier(8)

        def one(_):
            barrier.wait()
            return detail(root, "phased")["totals"]["root_cost_usd"]

        with ThreadPoolExecutor(8) as pool:
            return list(pool.map(one, range(8)))

    assert threaded() == [pytest.approx(cold)] * 8
    line = json.dumps({"type": "assistant", "timestamp": iso(m(70)), "message": {
        "id": "m-late", "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 8 * MILLION, "output_tokens": 0}}}) + "\n"
    append(transcript, line.encode())
    assert threaded() == [pytest.approx(cold + RATE * 8)] * 8


# --- whose transcript ----------------------------------------------------------------------

def conversation_root(tmp_path):
    """One conversation (harness-9) drove a parent, its linked child and an unrelated task."""
    parent = make_state("parent", plan_path=PLAN, history=full_history(), node="RESOLVED", stages=["PASSED"] * 3)
    child = make_state("child", history=[event("classify", m(61)), event("approve", m(62))],
                       parent_link={"parent_session": "parent", "stage_index": 1})
    sibling = make_state("sibling", history=[event("classify", m(200))])
    return build_fixture_root(
        tmp_path, [parent, child, sibling], bindings={"harness-9": ["parent", "child", "sibling"]},
        transcripts={"parent": TRANSCRIPT, "harness-9": [usage_row("h-conversation", m(65), 1)],
                     "child": [usage_row("c-own", m(70), 2)]},
        now=NOW)


def test_a_linked_child_is_priced_from_its_own_transcript_only(tmp_path):
    root = conversation_root(tmp_path)
    child = detail(root, "child")
    assert child["totals"]["root_cost_usd"] == pytest.approx(RATE * 2)
    assert child["harness_sessions"] == ["harness-9"]
    assert detail(root, "parent")["totals"]["root_cost_usd"] == pytest.approx(TASK_ROOT + RATE * 1)


def test_a_task_in_a_shared_conversation_names_the_other_tasks_but_not_its_linked_child(tmp_path):
    root = conversation_root(tmp_path)
    assert detail(root, "parent")["totals"]["root_shared_with"] == ["sibling"]
    assert detail(root, "child")["totals"]["root_shared_with"] == []
    assert detail(phased_root(tmp_path / "alone"), "phased")["totals"]["root_shared_with"] == []
    assert detail(phased_root(tmp_path / "bare", transcript=False), "phased")["totals"]["root_shared_with"] is None


def test_the_children_index_is_built_once_and_matches_the_per_call_lookup(tmp_path, monkeypatch):
    root = conversation_root(tmp_path)
    index = model.child_index(root.state_root)
    assert index == {"parent": [{"session_id": "child", "stage_index": 1, "node": "EXECUTING",
                                 "goal": "goal of child"}]}
    scans = []
    real = model.child_index
    monkeypatch.setattr(model, "child_index", lambda *a, **k: scans.append(a) or real(*a, **k))
    assert cli.main(cli_args(root, "--all", "--detail")) == 0
    assert len(scans) == 1
    assert detail(root, "parent", children=index)["children"] == detail(root, "parent")["children"] == index["parent"]


# --- read-only guarantee -------------------------------------------------------------------

def linked_root(tmp_path):
    parent = make_state("parent", plan_path=PLAN, history=full_history(), node="RESOLVED", stages=["PASSED"] * 3)
    child = make_state("child", history=[event("approve", at(hours=1))],
                       parent_link={"parent_session": "parent", "stage_index": 1})
    return build_fixture_root(tmp_path, [parent, child], ledger_rows=LEDGER, bindings={"harness-9": ["parent"]},
                              transcripts={"parent": TRANSCRIPT, "harness-9": [usage_row("m-bound", m(20), 1)]},
                              now=NOW)


def test_collect_detail_and_cli_write_nothing_under_the_agent_home(tmp_path, capsys):
    root = linked_root(tmp_path)
    with read_only_guard(root.home):
        snapshot = collect(root, include_all=True)
        result = detail(root, "parent")
        assert cli.main(cli_args(root, "--all", "--detail")) == 0
    assert result["totals"]["root_counted"] is True
    assert result["harness_sessions"] == ["harness-9"] and len(result["children"]) == 1
    assert {"parent", "child"} <= set(rows_by_session(snapshot))
    assert json.loads(capsys.readouterr().out)["tasks"]


def test_the_guard_refuses_a_write_under_the_root(tmp_path):
    root = linked_root(tmp_path)
    with pytest.raises(WriteAttempt):
        with read_only_guard(root.home):
            (root.state_root / "new.json").write_text("{}", encoding="utf-8")
