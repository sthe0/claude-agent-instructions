"""The textual layer of the console view, driven headless through textual's pilot.

The only test file besides ``tui_app.py`` that imports textual. Run with
``PLAN_DASHBOARD_REQUIRE_TEXTUAL=1`` a missing textual fails the run; without it the file is skipped.
Layout and key decisions are pinned in ``test_plan_dashboard_tui.py``; here the widgets must show them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

if os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1":
    import textual  # noqa: F401
else:
    pytest.importorskip("textual", exc_type=ImportError)

from textual.widgets import DataTable, TabbedContent, Tree  # noqa: E402

from plan_dashboard import model, tui  # noqa: E402
from plan_dashboard.tui_app import DashboardApp  # noqa: E402
from plan_dashboard_fixtures import read_only_guard  # noqa: E402
from plan_dashboard_support import ALPHA, BETA, NOW, STALE, console_root, isolated_home  # noqa: E402,F401

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
    assert tui.TABS == ("tasks", "detail")
