"""Console view of the plan dashboard: the pure layout in ``plan_dashboard.tui``, the ``tui``
subcommand and the pieces around them (textual confinement, the doc section).

Plain pytest, standard library only. The textual widget layer is driven by
``test_plan_dashboard_tui_app.py``. Each test names the property it pins; the mutation plugin's
TUI family (``plan_dashboard_mutations.TUI_MUTATIONS``) breaks them one by one.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import plan_dashboard_support as support
from agentctl.state import Declaration, Difficulty
from plan_dashboard import model, tui
from plan_dashboard_fixtures import build_fixture_root, read_only_guard
from plan_dashboard_support import (  # noqa: F401  (isolated_home is an autouse fixture)
    ALPHA, BETA, NOW, RATE, STALE, at, console_root, event, isolated_home, make_state,
)

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
TESTS = SCRIPTS / "tests"
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def snapshot(root, **kwargs):
    return model.collect(root.state_root, now=NOW, cost_log=root.cost_log, **kwargs)


def detail(root, sid):
    return model.detail(root.state_root, sid, cost_log=root.cost_log, transcripts_root=root.transcripts_root)


def once(root, **kwargs):
    options = dict(window_hours=24, include_all=False, sessions=[], cost_log=root.cost_log,
                   transcripts_root=root.transcripts_root, now=NOW, width=120)
    return tui.once(root.state_root, **{**options, **kwargs})


def row_for(rows, sid):
    return next(row for row in rows if row["session_id"] == sid)


def cells_of(rows, sid):
    return dict(zip(tui.OVERVIEW_COLUMNS, row_for(rows, sid)["cells"]))


def summary(sid, **fields):
    return {
        "session_id": sid, "task_id": f"task-{sid}", "goal": "g", "node": "EXECUTING", "weight_class": "SUBSTANTIVE",
        "liveness": "active", "current_stage": 1, "stages_done": 1, "stages_total": 2, "open_difficulty": None,
        "difficulties": {"open": 0, "stacked": 0, "closed": 0}, "replans": 0, "last_activity": "2026-10-22T11:50:00+00:00",
        "readable": True, "reason": None,
        "totals": model.totals(wall_clock_s=60, spawn_cost_usd=1.0, root_cost_usd=None, root_counted=False),
        **fields,
    }


def listing(*tasks):
    return {"schema": model.SCHEMA, "generated_at": "2026-10-22T12:00:00+00:00", "hostname": "host-1", "tasks": list(tasks)}


def tree_text(nodes):
    return "\n".join(tui.render_lines(nodes))


# --- the Tasks tab ---------------------------------------------------------------------------

def test_overview_row_cells(tmp_path):
    rows = tui.overview_rows(snapshot(console_root(tmp_path)), NOW)
    assert row_for(rows, ALPHA)["cells"] == (
        "alpha-00", f"task-{ALPHA}", "active", "EXECUTING", "stage 1/3 [###-----]", "0", "0", "10m00s ago", "1h50m",
        "≥$1.50", "Build the thing [with brackets]")
    beta = cells_of(rows, BETA)
    assert (beta["session"], beta["task"], beta["node"], beta["stage"], beta["age"]) == (
        "beta-111", f"task-{BETA}", "PLAN_READY", "stage 0/1 [--------]", "5m00s ago")


def test_overview_header_names_host_time_and_the_counts(tmp_path):
    root = console_root(tmp_path)
    snap = snapshot(root)
    assert tui.overview_header(snap, NOW) == f"{snap['hostname']}  2026-10-22 12:00:00 UTC  3 active  0 idle  3 total"
    assert tui.overview_header(snapshot(root, include_all=True), NOW).endswith("3 active  0 idle  4 total")
    assert tui.overview_header(listing(), NOW) == "host-1  2026-10-22 12:00:00 UTC  0 active  0 idle  0 total"


def test_overview_header_counts_each_liveness_band():
    snap = listing(summary("a"), summary("b", liveness="idle"), summary("c", liveness="idle"),
                   summary("d", liveness="stale"))
    assert tui.overview_header(snap, NOW).endswith("1 active  2 idle  4 total")


def test_the_header_opens_the_plain_view_and_the_table_follows(tmp_path):
    lines = once(console_root(tmp_path)).splitlines()
    assert lines[0].endswith("3 active  0 idle  3 total")
    assert lines[1].split() == list(tui.OVERVIEW_COLUMNS)


@pytest.mark.parametrize("done, total, expected", [
    (0, 0, "no plan"), (None, 0, "no plan"), (0, 4, "stage 0/4 [--------]"), (1, 4, "stage 1/4 [##------]"),
    (4, 4, "stage 4/4 [########]"), (5, 4, "stage 5/4 [########]"), (None, 4, "stage 0/4 [--------]"),
])
def test_stage_progress_has_a_bar_and_says_no_plan_without_stages(done, total, expected):
    cells = cells_of(tui.overview_rows(listing(summary("a", stages_done=done, stages_total=total)), NOW), "a")
    assert cells["stage"] == expected


@pytest.mark.parametrize("counts, title, expected", [
    ({"open": 0, "stacked": 0, "closed": 0}, None, "0"),
    ({"open": 0, "stacked": 0, "closed": 2}, None, "2"),
    ({"open": 1, "stacked": 0, "closed": 0}, "boom", "!1"),
    ({"open": 1, "stacked": 2, "closed": 3}, "boom", "!6"),
    ({"open": 0, "stacked": 1, "closed": 0}, None, "1"),
])
def test_difficulty_cell_marks_an_open_one_and_counts_all(counts, title, expected):
    row = summary("a", difficulties=counts, open_difficulty=title)
    assert cells_of(tui.overview_rows(listing(row), NOW), "a")["diff"] == expected


def test_replans_cell_shows_the_replan_count():
    assert cells_of(tui.overview_rows(listing(summary("a", replans=3)), NOW), "a")["replans"] == "3"


def test_diff_and_replans_cells_come_from_a_collected_task(tmp_path):
    busy = make_state(
        "busy", stages=("ACTIVE",), closed_difficulties=[{"declaration": {"mismatch": "old"}}],
        difficulty_stack=[Difficulty(declaration=Declaration(expected="e", actual="a", mismatch="parked"))],
        difficulty=Difficulty(declaration=Declaration(expected="e", actual="a", mismatch="live")),
        history=[event("approve", at(hours=2)), event("replan", at(hours=1)), event("replan", at(minutes=50))])
    calm = make_state("calm", stages=("ACTIVE",), closed_difficulties=[{"declaration": {"mismatch": "old"}}],
                      history=[event("approve", at(hours=2))])
    rows = tui.overview_rows(snapshot(build_fixture_root(tmp_path, [busy, calm], now=NOW)), NOW)
    assert (cells_of(rows, "busy")["diff"], cells_of(rows, "busy")["replans"]) == ("!3", "2")
    assert (cells_of(rows, "calm")["diff"], cells_of(rows, "calm")["replans"]) == ("1", "0")


def test_long_task_ids_are_cut():
    row = summary("a", task_id="agent-recursion-depth-1-continue-the-prior-stage")
    task = cells_of(tui.overview_rows(listing(row), NOW), "a")["task"]
    assert len(task) == tui.TASK_ID_WIDTH and task.endswith("…")


def test_active_tasks_come_first_then_the_most_recent():
    snap = listing(
        summary("idle-new", liveness="idle", last_activity="2026-10-22T11:59:00+00:00"),
        summary("active-old", last_activity="2026-10-22T11:30:00+00:00"),
        summary("active-new", last_activity="2026-10-22T11:50:00+00:00"),
        summary("idle-old", liveness="idle", last_activity="2026-10-22T10:00:00+00:00"))
    assert [r["session_id"] for r in tui.overview_rows(snap, NOW)] == ["active-new", "active-old", "idle-new", "idle-old"]


def test_overview_hides_stale_unless_asked(tmp_path):
    root = console_root(tmp_path)
    assert STALE not in [r["session_id"] for r in tui.overview_rows(snapshot(root), NOW)]
    assert STALE in [r["session_id"] for r in tui.overview_rows(snapshot(root, include_all=True), NOW)]


def test_unreadable_task_is_a_row_with_unknown_stage(tmp_path):
    cells = cells_of(tui.overview_rows(snapshot(console_root(tmp_path)), NOW), "broken")
    assert cells["stage"] == "stage ?/?"
    assert cells["goal"].startswith("unreadable")
    assert (cells["task"], cells["diff"], cells["replans"]) == ("-", "-", "-")


def test_a_task_without_a_task_id_shows_a_dash():
    row = summary("a", task_id=None)
    assert cells_of(tui.overview_rows(listing(row), NOW), "a")["task"] == "-"


def test_overview_age_is_measured_from_the_given_clock(tmp_path):
    snap = snapshot(console_root(tmp_path))
    later = NOW + dt.timedelta(hours=1)
    assert cells_of(tui.overview_rows(snap, later), ALPHA)["age"] == "1h10m ago"


def test_overview_cost_is_a_lower_bound_until_the_root_is_counted(tmp_path):
    assert cells_of(tui.overview_rows(snapshot(console_root(tmp_path)), NOW), ALPHA)["cost"].startswith("≥$")


# --- value formatting ------------------------------------------------------------------------

@pytest.mark.parametrize("args, expected", [
    ((1.5, False), "$1.50"),
    ((1.5, True), "~$1.50"),
    ((1.5, False, True), "≥$1.50"),
    ((1.5, True, True), "≥$1.50"),
    ((0.0, False), "$0.00"),
    ((None, False), "not counted"),
    ((None, True), "not counted"),
    ((None, False, True), "not counted"),
])
def test_fmt_value(args, expected):
    assert tui.fmt_value(*args) == expected


@pytest.mark.parametrize("seconds, expected", [
    (None, "?"), (0, "0m00s"), (59.9, "0m59s"), (3600, "1h00m"), (7140, "1h59m"), (-1, "0m00s"), (-7140, "0m00s"),
])
def test_fmt_duration_never_goes_negative(seconds, expected):
    assert tui.fmt_duration(seconds) == expected


def test_a_start_after_now_reads_as_no_time_elapsed(tmp_path):
    earlier = NOW - dt.timedelta(hours=1)
    rows = tui.overview_rows(snapshot(console_root(tmp_path)), earlier)
    assert cells_of(rows, ALPHA)["age"] == "0m00s ago"


# --- the Detail tab --------------------------------------------------------------------------

def test_detail_tree_stages_carry_status_marks(tmp_path):
    text = tree_text(tui.detail_tree(detail(console_root(tmp_path), ALPHA), NOW))
    assert "[done] 1. Stage 1 (in_thread, measurable)" in text
    assert "[active] 2. Stage 2 (in_thread, measurable)" in text
    assert "[pending] 3. Stage 3 (in_thread, measurable)" in text


@pytest.mark.parametrize("status, mark", [
    ("PASSED", "done"), ("ACTIVE", "active"), ("FAILED", "failed"), ("SKIPPED", "skipped"),
    ("PENDING", "pending"), ("MYSTERY", "?"),
])
def test_status_marks(status, mark):
    tree = tui.detail_tree({
        "readable": True, "session_id": "s" * 8, "node": "EXECUTING", "goal": "g",
        "totals": model.totals(wall_clock_s=60, spawn_cost_usd=1.0, root_cost_usd=None, root_counted=False),
        "stages": [{"index": 1, "title": "t", "actor": "in_thread", "criterion_type": "measurable",
                    "status": status}],
        "phases": [], "open_difficulties": [], "closed_difficulties": [], "children": [], "history": [],
    }, NOW)
    assert tree_text(tree).count(f"[{mark}] 1. t") == 1


def test_open_phase_is_open_and_measured_from_the_given_clock(tmp_path):
    root = console_root(tmp_path)
    assert "stage 2: wall open (10m00s so far)" in tree_text(tui.detail_tree(detail(root, ALPHA), NOW))
    later = NOW + dt.timedelta(minutes=5)
    assert "stage 2: wall open (15m00s so far)" in tree_text(tui.detail_tree(detail(root, ALPHA), later))


def test_estimates_are_marked_and_missing_root_cost_is_not_counted(tmp_path):
    root = console_root(tmp_path)
    counted = tree_text(tui.detail_tree(detail(root, ALPHA), NOW))
    assert f"stage 1: wall 10m00s  spawn $1.50  root ~${RATE:.2f}" in counted
    assert "planning: wall ~" in counted
    uncounted = tree_text(tui.detail_tree(detail(root, BETA), NOW))
    assert "root not counted" in uncounted
    assert "root $0.00" not in uncounted and "root ~$0.00" not in uncounted


def test_nested_spawn_cost_is_not_attributed():
    item = {"label": "review round 1", "start": "2026-10-22T10:00:00+00:00", "end": "2026-10-22T10:02:00+00:00",
            "wall_clock_s": 120.0, "spawn_cost_usd": None, "root_cost_usd": None, "children": [],
            "estimate": {"wall_clock_s": True, "spawn_cost_usd": False, "root_cost_usd": True}}
    assert tui._phase_node(item, NOW)["label"] == (
        "review round 1: wall ~2m00s  spawn not attributed  root not counted")


def test_open_phase_with_an_unreadable_start_reads_unknown():
    item = {"label": "stage 2", "start": "not a time", "end": None, "wall_clock_s": None, "spawn_cost_usd": None,
            "root_cost_usd": None, "children": [],
            "estimate": {"wall_clock_s": False, "spawn_cost_usd": False, "root_cost_usd": False}}
    assert tui._phase_node(item, NOW)["label"].startswith("stage 2: wall open (? so far)")


def test_a_vanished_selection_is_one_no_such_session_node():
    assert tree_text(tui.missing_detail_tree()) == "no such session"


def test_unreadable_detail_is_one_line(tmp_path):
    tree = tui.detail_tree(detail(console_root(tmp_path), "broken"), NOW)
    assert len(tree) == 1 and tree[0]["label"].startswith("unreadable")


# --- the Detail tree: keys, node keys and kept state -----------------------------------------

def test_tree_keys_belong_to_the_tree_only_on_the_detail_tab():
    for key in ("up", "down", "j", "k", "enter", "space", "left", "right"):
        assert tui.key_owner("detail", key) == "tree", key
        assert tui.key_owner("tasks", key) == "global", key
    for key in ("escape", "backspace", "tab", "a", "r", "q"):
        assert tui.key_owner("detail", key) == "global", key


@pytest.mark.parametrize("key, node, action", [
    ("j", {}, "down"), ("down", {}, "down"), ("k", {}, "up"), ("up", {}, "up"),
    ("enter", {"expandable": True}, "toggle"), ("space", {"expandable": True}, "toggle"),
    ("enter", {"expandable": False}, None),
    ("right", {"expandable": True, "expanded": False}, "expand"),
    ("right", {"expandable": True, "expanded": True}, None),
    ("right", {"expandable": False}, None),
    ("left", {"expandable": True, "expanded": True}, "collapse"),
    ("left", {"expandable": True, "expanded": False, "has_parent": True}, "parent"),
    ("left", {"expandable": False, "has_parent": True}, "parent"),
    ("left", {"expandable": False, "has_parent": False}, None),
    ("x", {}, None),
])
def test_tree_action_decides_what_a_tree_key_does(key, node, action):
    node = {"expandable": False, "expanded": False, "has_parent": False, **node}
    assert tui.tree_action(key, **node) == action


def phase_detail(root, sid):
    return model.detail(root.state_root, sid, cost_log=root.cost_log)


def test_node_keys_do_not_change_when_only_the_live_part_of_a_label_changes(tmp_path):
    root = console_root(tmp_path)
    before = tui.detail_tree(phase_detail(root, ALPHA), NOW)
    after = tui.detail_tree(phase_detail(root, ALPHA), NOW + dt.timedelta(seconds=90))
    assert tui.key_paths(before) == tui.key_paths(after)
    assert tui.render_lines(before) != tui.render_lines(after)
    assert ("stages", "stage:2") in tui.key_paths(before)
    assert ("phases", "phase:stage 2") in tui.key_paths(before)


def test_node_keys_are_unique_among_siblings(tmp_path):
    tree = tui.detail_tree(phase_detail(console_root(tmp_path), ALPHA), NOW)

    def check(nodes):
        keys = [node["key"] for node in nodes]
        assert len(keys) == len(set(keys)), keys
        for node in nodes:
            check(node["children"])

    check(tree)


def test_state_is_kept_for_the_same_task_and_reset_for_another():
    state = tui.TreeState(task="a", expanded=frozenset({("stages",)}), cursor=("stages", "stage:2"))
    assert tui.state_for_task(state, "a") is state
    other = tui.state_for_task(state, "b")
    assert other == tui.TreeState(task="b") and not other.expanded and other.cursor == ()


def test_surviving_path_falls_back_to_the_nearest_ancestor():
    nodes = [{"label": "stages", "key": "stages", "children": [{"label": "1", "key": "stage:1", "children": []}]}]
    assert tui.surviving_path(nodes, ("stages", "stage:1")) == ("stages", "stage:1")
    assert tui.surviving_path(nodes, ("stages", "stage:9")) == ("stages",)
    assert tui.surviving_path(nodes, ("gone", "x")) == ()
    assert tui.surviving_path(nodes, ()) == ()


# --- keys and refresh ------------------------------------------------------------------------

def view(**fields):
    return tui.View(session_ids=("a", "b", "c"), **fields)


def press(start, *keys):
    for key in keys:
        start = tui.handle_key(start, key)
    return start


@pytest.mark.parametrize("keys, cursor", [
    (("down",), 1), (("j",), 1), (("down", "down"), 2), (("down",) * 5, 2),
    (("up",), 0), (("k",), 0), (("down", "down", "up"), 1),
])
def test_cursor_keys_move_and_clamp(keys, cursor):
    assert press(view(), *keys).cursor == cursor


def test_enter_opens_the_row_under_the_cursor():
    after = press(view(), "down", "enter")
    assert (after.tab, after.selected, after.refresh) == ("detail", "b", True)


def test_enter_does_nothing_without_rows_or_on_the_detail_tab():
    assert press(tui.View(), "enter") == tui.View()
    opened = press(view(), "enter")
    assert press(opened, "enter") == dataclasses.replace(opened, refresh=False)


@pytest.mark.parametrize("key", ["escape", "backspace"])
def test_back_keys_return_to_tasks_and_keep_the_selection(key):
    after = press(view(), "enter", key)
    assert (after.tab, after.selected) == ("tasks", "a")


def test_tab_switches_tabs_and_asks_for_detail_only_when_something_is_selected():
    assert not press(view(), "tab").refresh
    after = press(view(), "enter", "escape", "tab")
    assert (after.tab, after.refresh) == ("detail", True)
    assert press(after, "tab").tab == "tasks"


def test_a_toggles_all_and_asks_for_a_refresh():
    after = press(view(), "a")
    assert after.include_all and after.refresh
    assert not press(after, "a").include_all


def test_r_requests_a_refresh_once():
    after = press(view(), "r")
    assert after.refresh
    assert not press(after, "down").refresh


def test_q_requests_quit_and_other_keys_do_nothing():
    assert press(view(), "q").quit
    assert press(view(), "x") == view()


@pytest.mark.parametrize("cursor, ids, expected", [
    (1, ("c", "b", "a"), 1),
    (0, ("c", "b", "a"), 2),
    (2, ("b", "c"), 1),
    (1, ("c", "a"), 1),
    (2, ("x",), 0),
    (1, (), 0),
])
def test_reconcile_keeps_the_cursor_on_its_task_and_clamps_when_it_is_gone(cursor, ids, expected):
    after = tui.reconcile(view(cursor=cursor), ids)
    assert (after.cursor, after.session_ids) == (expected, tuple(ids))


def test_reconcile_starts_at_the_top_and_keeps_the_rest_of_the_view():
    assert tui.reconcile(tui.View(), ("a", "b")).cursor == 0
    opened = press(view(), "down", "enter", "a")
    after = tui.reconcile(opened, ("c", "b", "a"))
    assert (after.selected, after.tab, after.include_all, after.cursor) == ("b", "detail", True, 1)


@pytest.mark.parametrize("last, elapsed, expected", [
    (None, 0, True), (NOW, 0, False), (NOW, 1.99, False), (NOW, 2, True), (NOW, 60, True),
])
def test_due(last, elapsed, expected):
    assert tui.due(NOW + dt.timedelta(seconds=elapsed), last, 2) is expected


# --- the plain-text fallback -----------------------------------------------------------------

def test_once_lists_tasks_and_details_only_the_named_session(tmp_path):
    root = console_root(tmp_path)
    plain = once(root)
    assert "alpha-00" in plain and "stage 1/3" in plain and "beta-111" in plain
    assert "\nstages\n" not in plain
    pinned = once(root, sessions=[ALPHA])
    assert "[active] 2. Stage 2" in pinned
    assert "[active] 2. Stage 2" not in once(root, sessions=[BETA])


def test_once_without_tasks(tmp_path):
    lines = once(build_fixture_root(tmp_path, [], now=NOW)).splitlines()
    assert lines[1:] == ["no live tasks"] and lines[0].endswith("0 active  0 idle  0 total")


def test_once_cuts_the_goal_to_the_width(tmp_path):
    root = console_root(tmp_path)
    assert "Build the thing [with brackets]" in once(root, width=200)
    narrow = once(root, width=140)
    assert "Build the thing [with brackets]" not in narrow
    alpha = next(line for line in narrow.splitlines() if line.startswith("alpha-00"))
    assert alpha.endswith("  ≥$1.50  Build the thing [with b…")


def test_the_console_view_writes_nothing(tmp_path):
    root = console_root(tmp_path)
    with read_only_guard(root.home):
        once(root, sessions=[ALPHA])
        tui.overview_rows(snapshot(root), NOW)
        tui.detail_tree(detail(root, ALPHA), NOW)


# --- the tui subcommand ----------------------------------------------------------------------

def cli_args(root, *extra):
    return ["tui", "--state-root", str(root.state_root), "--cost-log", str(root.cost_log),
            "--transcripts-root", str(root.transcripts_root), *extra]


def test_tui_options():
    parsed = cli.build_parser().parse_args(["tui"])
    assert (parsed.once, parsed.interval) == (False, tui.DEFAULT_INTERVAL_S)
    assert tui.DEFAULT_INTERVAL_S == 2
    parsed = cli.build_parser().parse_args(["tui", "--once", "--interval", "5", "--all", "--session", "x"])
    assert (parsed.once, parsed.interval, parsed.include_all, parsed.sessions) == (True, 5, True, ["x"])


def test_tui_once_prints_the_plain_view(tmp_path, capsys):
    root = console_root(tmp_path)
    assert cli.main(cli_args(root, "--once", "--all", "--session", ALPHA)) == 0
    out = capsys.readouterr().out
    assert "alpha-00" in out and "stage 1/3" in out and "[active] 2. Stage 2" in out


@pytest.mark.parametrize("sessions, missing", [
    ([ALPHA, "nope"], "nope"),
    (["nope", "also-nope"], "nope, also-nope"),
])
def test_tui_once_names_a_session_that_does_not_exist_and_exits_3(tmp_path, capsys, sessions, missing):
    extra = [arg for s in sessions for arg in ("--session", s)]
    assert cli.main(cli_args(console_root(tmp_path), "--once", *extra)) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"plan-dashboard: no such session: {missing}\n"


def test_snapshot_and_tui_once_agree_on_a_missing_session(tmp_path, capsys):
    root = console_root(tmp_path)
    snapshot_args = ["snapshot", "--state-root", str(root.state_root), "--cost-log", str(root.cost_log),
                     "--session", "nope"]
    assert cli.main(snapshot_args) == cli.main(cli_args(root, "--once", "--session", "nope")) == 3
    first, second = capsys.readouterr().err.splitlines()
    assert first == second


def test_once_raises_for_a_missing_session(tmp_path):
    with pytest.raises(tui.NoSuchSession) as raised:
        once(console_root(tmp_path), sessions=[BETA, "nope"])
    assert raised.value.names == ["nope"]


def test_tui_once_needs_no_uv(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(cli_args(console_root(tmp_path), "--once", "--all")) == 0
    assert "alpha-00" in capsys.readouterr().out


def test_interactive_tui_without_uv_exits_4(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    called = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 0)
    assert cli.main(cli_args(console_root(tmp_path))) == 4
    captured = capsys.readouterr()
    assert captured.err == "interactive console needs uv; use 'plan-dashboard.py tui --once'\n"
    assert captured.out == "" and called == []


def test_interactive_tui_runs_the_app_with_uv(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv")
    called = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 7)
    extra = ("--interval", "3", "--all", "--window-hours", "6", "--session", ALPHA)
    assert cli.main(cli_args(root, *extra)) == 7
    argv = called[0]
    assert argv == tui.launch_argv(cli.build_parser().parse_args(cli_args(root, *extra)))
    assert argv[:2] == ["uv", "run"] and Path(argv[2]).name == "tui_app.py"
    pairs = dict(zip(argv, argv[1:]))
    assert pairs["--interval"] == "3.0" and pairs["--window-hours"] == "6.0" and pairs["--session"] == ALPHA
    assert pairs["--state-root"] == str(root.state_root) and "--all" in argv


# --- textual stays in the app layer ----------------------------------------------------------

def test_textual_importers_sees_every_import_form_and_nothing_else(tmp_path):
    sources = {
        "plain.py": "import textual\n",
        "from_import.py": "from textual.app import App\n",
        "dotted.py": "import textual.widgets as w\n",
        "inside.py": "def f():\n    import textual\n",
        "mention.py": "# import textual\nNAME = 'textual'\nimport textual_like\n",
        "broken.py": "def (:\n",
    }
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    found = {p.name for p in support.textual_importers(sorted(tmp_path.iterdir()) + [tmp_path / "missing.py"])}
    assert found == {"plain.py", "from_import.py", "dotted.py", "inside.py"}


def test_textual_is_imported_only_by_the_app_and_its_test():
    checked = [*sorted((SCRIPTS / "plan_dashboard").glob("*.py")), SCRIPTS / "plan-dashboard.py",
               *sorted(TESTS.glob("*plan_dashboard*.py"))]
    assert {p.name for p in support.textual_importers(checked)} == {"tui_app.py", "test_plan_dashboard_tui_app.py"}


def run_app_tests(tmp_path, require: bool):
    (tmp_path / "textual.py").write_text("raise ImportError('textual is poisoned for this test')\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(TESTS), str(SCRIPTS)])}
    env.pop("PLAN_DASHBOARD_REQUIRE_TEXTUAL", None)
    if require:
        env["PLAN_DASHBOARD_REQUIRE_TEXTUAL"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(TESTS / "test_plan_dashboard_tui_app.py")],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def test_pilot_tests_fail_rather_than_skip_when_textual_is_required(tmp_path):
    done = run_app_tests(tmp_path, require=True)
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    assert "poisoned" in done.stdout + done.stderr


def test_pilot_tests_skip_without_textual_when_it_is_not_required(tmp_path):
    done = run_app_tests(tmp_path, require=False)
    assert done.returncode in (0, 5), done.stdout + done.stderr
    assert "1 skipped" in done.stdout


# --- the documentation section ---------------------------------------------------------------

def console_section() -> str:
    lines, inside = [], False
    for line in (REPO / "docs" / "components" / "plan-dashboard.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line == "## Console view"
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines)


def test_console_view_section_is_written():
    section = console_section()
    assert "Not written yet." not in section
    for term in ("tui --once", "--interval", "textual==8.2.8", "uv run", "collapsed", "refresh", "Left", "Right", "Space"):
        assert term in section, term
