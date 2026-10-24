"""The textual layer of the console view, driven headless through textual's pilot.

The only test file besides ``tui_app.py`` that imports textual. Run with
``PLAN_DASHBOARD_REQUIRE_TEXTUAL=1`` a missing textual fails the run; without it the file is skipped.
Layout and key decisions are pinned in ``test_plan_dashboard_tui.py``; here the widgets must show them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os

import pytest

if os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1":
    import textual  # noqa: F401
else:
    pytest.importorskip("textual", exc_type=ImportError)

from textual.widgets import DataTable, TabbedContent, Tree  # noqa: E402

from plan_dashboard import model, tui  # noqa: E402
from plan_dashboard.tui_app import DashboardApp  # noqa: E402
from plan_dashboard_fixtures import build_fixture_root, read_only_guard  # noqa: E402
from plan_dashboard_support import (  # noqa: E402,F401
    ALPHA, BETA, NOW, STALE, at, console_root, event, isolated_home, make_state,
)

SIZE = (150, 40)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


def make_app(root, clock=None, **options):
    return DashboardApp(state_root=root.state_root, cost_log=root.cost_log, transcripts_root=root.transcripts_root,
                        clock=clock or Clock(), **{"interval": 3600.0, **options})


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def run(scenario, root, **options):
    app = make_app(root, **options)

    async def main():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await scenario(app, pilot)

    asyncio.run(main())
    return app


def table_ids(app):
    return [key.value for key in app.query_one(DataTable).rows]


def table_cells(app, index):
    cells = [cell.plain for cell in app.query_one(DataTable).get_row_at(index)]
    return dict(zip(tui.OVERVIEW_COLUMNS, cells))


def labels(node):
    return [node.label.plain] + [text for child in node.children for text in labels(child)]


def tree_text(app):
    return "\n".join(labels(app.query_one(Tree).root))


def count_collects(monkeypatch):
    calls = []
    real = model.collect

    def counting(*args, **kwargs):
        calls.append(kwargs.get("now"))
        return real(*args, **kwargs)

    monkeypatch.setattr(model, "collect", counting)
    return calls


def test_tasks_tab_lists_the_live_tasks(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        assert set(table_ids(app)) == {ALPHA, BETA, "broken"}
        by_id = {sid: index for index, sid in enumerate(table_ids(app))}
        alpha = table_cells(app, by_id[ALPHA])
        assert [alpha[name] for name in ("session", "task", "live", "node", "stage", "diff", "replans")] == [
            "alpha-00", f"task-{ALPHA}", "active", "EXECUTING", "stage 1/3 [###-----]", "0", "0"]
        assert alpha["cost"] == "≥$1.50"
        assert alpha["goal"] == "Build the thing [with brackets]"
        assert table_cells(app, by_id["broken"])["stage"] == "stage ?/?"
        assert "3 live tasks" in app.sub_title
        head = str(app.query_one("#overview-head").render())
        assert head.endswith("3 active  0 idle  3 total") and "2026-10-22 12:00:00 UTC" in head

    run(scenario, root)


def test_enter_opens_the_detail_of_the_selected_task(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        index = table_ids(app).index(ALPHA)
        for _ in range(index):
            await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        text = tree_text(app)
        assert "[done] 1. Stage 1 (in_thread, measurable)" in text
        assert "[active] 2. Stage 2 (in_thread, measurable)" in text
        assert "stage 2: wall open (10m00s so far)" in text
        assert "alpha-00 EXECUTING  Build the thing [with brackets]" in text

    run(scenario, root)


def test_clicking_a_row_opens_its_detail(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        index = table_ids(app).index(ALPHA)
        await pilot.click(DataTable, offset=(4, 1 + index))
        await pilot.click(DataTable, offset=(4, 1 + index))
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        assert "[active] 2. Stage 2" in tree_text(app)

    run(scenario, root)


def test_escape_goes_back_and_tab_returns_to_the_detail(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("escape")
        assert app.query_one(TabbedContent).active == "tasks"
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        await pilot.press("tab")
        assert app.query_one(TabbedContent).active == "tasks"

    run(scenario, root)


def test_a_toggles_between_live_and_all_tasks(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        assert STALE not in table_ids(app)
        await pilot.press("a")
        await settle(app, pilot)
        assert STALE in table_ids(app) and "all tasks" in app.sub_title
        await pilot.press("a")
        await settle(app, pilot)
        assert STALE not in table_ids(app) and "live tasks" in app.sub_title

    run(scenario, root)


def test_q_quits(tmp_path):
    root = console_root(tmp_path)
    exited = []

    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.pause()
        exited.append(app._exit)

    run(scenario, root)
    assert exited == [True]


def test_r_reads_again_at_once(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    calls = count_collects(monkeypatch)

    async def scenario(app, pilot):
        before = len(calls)
        await pilot.press("r")
        await settle(app, pilot)
        assert len(calls) == before + 1

    run(scenario, root)


def test_the_view_reads_again_once_the_interval_has_passed(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    calls = count_collects(monkeypatch)
    clock = Clock()

    async def scenario(app, pilot):
        before = len(calls)
        await pilot.pause(0.7)
        assert len(calls) == before
        clock.advance(10)
        await pilot.pause(0.7)
        await settle(app, pilot)
        assert len(calls) > before
        assert calls[-1] == NOW + dt.timedelta(seconds=10)
        assert "read 12:00:10" in app.sub_title

    run(scenario, root, clock=clock, interval=5.0)


def test_the_cursor_follows_its_task_when_a_read_reorders_the_rows(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    reads = []
    real = model.collect

    def reversing(*args, **kwargs):
        snapshot = real(*args, **kwargs)
        reads.append(1)
        if len(reads) > 1:
            tasks = sorted(snapshot["tasks"], key=lambda t: t["last_activity"])
            stamps = [task["last_activity"] for task in tasks]
            for task, stamp in zip(tasks, reversed(stamps)):
                task["last_activity"] = stamp
        return snapshot

    monkeypatch.setattr(model, "collect", reversing)

    async def scenario(app, pilot):
        before = table_ids(app)
        followed = before[0]
        await pilot.press("r")
        await settle(app, pilot)
        assert table_ids(app) == before[::-1]
        assert table_ids(app)[app.query_one(DataTable).cursor_row] == followed
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.view.selected == followed

    run(scenario, root)


def test_the_detail_tab_says_so_when_the_selected_task_vanishes(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        for _ in range(table_ids(app).index(ALPHA)):
            await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "Stage 2" in tree_text(app)
        (root.state_root / f"{app.view.selected}.json").unlink()
        await pilot.press("r")
        await settle(app, pilot)
        assert tree_text(app) == "no such session"

    run(scenario, root)


def test_the_app_builds_the_child_index_once_per_read(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    builds = []
    real = model.child_index
    monkeypatch.setattr(model, "child_index", lambda *a, **k: builds.append(1) or real(*a, **k))

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        before = len(builds)
        await pilot.press("r")
        await settle(app, pilot)
        assert len(builds) == before + 1

    run(scenario, root)


def test_the_app_writes_nothing(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("a")
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)

    with read_only_guard(root.home):
        run(scenario, root)


def test_every_key_of_the_pure_layer_is_bound():
    bound = {binding.key for binding in DashboardApp.BINDINGS}
    assert {"up", "down", "j", "k", "enter", "escape", "backspace", "tab", "a", "r", "q"} <= bound
    assert {"j", "k", "space", "left", "right"} <= {b.key for b in DashboardApp.BINDINGS if "tree_key" in b.action}
    assert tui.TABS == ("tasks", "detail")


# --- the Detail tree: collapsed start, keyboard navigation, state kept across a refresh -------------

REVIEWED_A = "review-aaaa-0001"
REVIEWED_B = "review-bbbb-0002"
PHASE = ("phases", "phase:plan v1")
ROUND = PHASE + ("phase:review round 1",)


def reviewed_root(tmp_path):
    """Two tasks in plan review: an open ``plan v1`` phase holding two review-round children."""
    def reviewing(sid, minutes):
        return make_state(sid, node="PLAN_READY", stages=("PENDING",), history=[
            event("classify", at(minutes=minutes)), event("submit_plan", at(minutes=minutes - 10)),
            event("plan_review", at(minutes=minutes - 15)), event("plan_review", at(minutes=minutes - 18))])
    return build_fixture_root(tmp_path, [reviewing(REVIEWED_A, 40), reviewing(REVIEWED_B, 39)], now=NOW)


def tree_of(app):
    return app.query_one(Tree)


def find(tree, path):
    node = tree.root
    for depth in range(1, len(path) + 1):
        node = next(child for child in node.children if child.data == path[:depth])
    return node


def top_level(app):
    return {child.data[0]: child for child in tree_of(app).root.children}


async def open_task(app, pilot, sid):
    target = table_ids(app).index(sid)
    while app.view.cursor != target:
        await pilot.press("down" if app.view.cursor < target else "up")
    await pilot.press("enter")
    await settle(app, pilot)


def cursor_path(app):
    return tree_of(app).cursor_node.data


def test_the_detail_tree_opens_collapsed_with_focus_on_the_tree_by_enter(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        assert app.query_one(TabbedContent).active == "detail"
        assert app.focused is tree_of(app)
        top = top_level(app)
        assert {"totals", "stages", "phases"} <= set(top)
        assert top["stages"].children and not top["stages"].is_expanded
        assert not any(node.is_expanded for node in top.values())

    run(scenario, root)


def test_tab_and_a_click_on_the_tab_focus_the_tree_and_escape_the_table(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.focused is app.query_one(DataTable)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.focused is tree_of(app)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.focused is app.query_one(DataTable)
        await pilot.click("#--content-tab-detail")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail" and app.focused is tree_of(app)

    run(scenario, root)


def test_arrow_keys_move_the_tree_cursor_and_enter_and_space_toggle_a_node(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("down")
        assert cursor_path(app) == ("totals",)
        await pilot.press("down")
        assert cursor_path(app) == ("stages",)
        await pilot.press("enter")
        assert top_level(app)["stages"].is_expanded
        await pilot.press("down")
        assert cursor_path(app) == ("stages", "stage:1")
        await pilot.press("up")
        await pilot.press("space")
        assert not top_level(app)["stages"].is_expanded
        await pilot.press("space")
        assert top_level(app)["stages"].is_expanded

    run(scenario, root)


def test_j_k_right_and_left_drive_the_tree(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("j", "j")
        assert cursor_path(app) == ("stages",)
        await pilot.press("right")
        assert top_level(app)["stages"].is_expanded
        await pilot.press("j")
        assert cursor_path(app) == ("stages", "stage:1")
        await pilot.press("left")
        assert cursor_path(app) == ("stages",) and top_level(app)["stages"].is_expanded
        await pilot.press("left")
        assert not top_level(app)["stages"].is_expanded
        await pilot.press("k")
        assert cursor_path(app) == ("totals",)

    run(scenario, root)


def test_escape_hands_the_arrow_keys_back_to_the_table(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("escape")
        await settle(app, pilot)
        before = app.query_one(DataTable).cursor_row
        await pilot.press("down" if before == 0 else "up")
        assert app.query_one(DataTable).cursor_row != before

    run(scenario, root)


def test_a_click_expands_a_phase_node(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.click(Tree, offset=(6, 3))
        assert top_level(app)["phases"].is_expanded
        await pilot.click(Tree, offset=(6, 5))
        assert find(tree_of(app), PHASE).is_expanded

    run(scenario, root)


def test_a_refresh_keeps_what_the_user_opened_and_the_cursor_though_the_label_changes(tmp_path):
    root = reviewed_root(tmp_path)
    clock = Clock()

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "j", "j", "right", "j")
        assert cursor_path(app) == ROUND
        before = find(tree_of(app), PHASE).label.plain
        clock.advance(120)
        await pilot.press("r")
        await settle(app, pilot)
        after = find(tree_of(app), PHASE).label.plain
        assert before != after
        assert top_level(app)["phases"].is_expanded and find(tree_of(app), PHASE).is_expanded
        assert not top_level(app)["stages"].is_expanded
        assert cursor_path(app) == ROUND

    run(scenario, root, clock=clock)


def test_a_vanished_cursor_node_falls_back_to_its_nearest_surviving_ancestor(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "j", "j", "right", "j")
        assert cursor_path(app) == ROUND
        state_file = root.state_root / f"{REVIEWED_A}.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data["history"] = [e for e in data["history"] if e.get("event") != "plan_review"]
        state_file.write_text(json.dumps(data), encoding="utf-8")
        await pilot.press("r")
        await settle(app, pilot)
        assert cursor_path(app) == PHASE

    run(scenario, root)


def test_another_task_opens_collapsed_though_the_same_key_path_was_open_in_the_first(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right")
        assert top_level(app)["phases"].is_expanded
        await pilot.press("escape")
        await settle(app, pilot)
        await open_task(app, pilot, REVIEWED_B)
        assert app.view.selected == REVIEWED_B
        assert ("phases",) in tui.key_paths(tui.detail_tree(model.detail(
            root.state_root, REVIEWED_B, cost_log=root.cost_log), NOW))
        assert not top_level(app)["phases"].is_expanded

    run(scenario, root)


def test_the_tree_pilot_runs_inside_the_read_only_guard(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "r")
        await settle(app, pilot)

    with read_only_guard(root.home):
        run(scenario, root)
