"""Timing bench of the console's reads on the state it is pointed at: ``plan-dashboard.py bench``.

Each read the console waits on is timed once against a budget, and the run fails when any read is
over. The pilots pin how many times a read touches the store; this measures what the user waits for at
the volume of the real state.
"""
from __future__ import annotations

import datetime as dt
import sys
import time

from lib import engine_session_binding
from plan_dashboard import model, tui

BUDGETS = {"live table": 4.0, "live overlay": 3.0, "all table": 15.0, "all overlay": 30.0, "detail": 5.0}
EXIT_OVER = 1
EXIT_NO_SUCH_SESSION = 2

clock = time.monotonic


def budget_flag(name: str) -> str:
    """The option overriding the budget of measure ``name``, e.g. ``--budget-live-table``."""
    return "--budget-" + name.replace(" ", "-")


def budget_dest(name: str) -> str:
    return "budget_" + name.replace(" ", "_")


def verdict(seconds: float, budget: float) -> str:
    return "ok" if seconds <= budget else "over"


def line(name: str, seconds: float, budget: float) -> str:
    return f"{name}: {seconds:.1f}s (budget {budget:g}s) {verdict(seconds, budget)}"


def measure(read):
    start = clock()
    value = read()
    return value, clock() - start


def run(state_root, *, session=None, window_hours=24.0, cost_log=None, transcripts_root=None, locator=None,
        budgets=None, now=None, out=print) -> int:
    """Time the live and all table reads, their overlays and the Detail read of ``session`` (by default
    the first live task, else the first of all); print one line per measure and the volume."""
    budgets = {**BUDGETS, **(budgets or {})}
    now = now or dt.datetime.now(dt.timezone.utc)
    seconds, snapshots = {}, {}

    def read_detail(target):
        return model.detail(state_root, target, cost_log=cost_log, transcripts_root=transcripts_root,
                            children=model.child_index(state_root))

    if session is not None:
        try:
            _, seconds["detail"] = measure(lambda: read_detail(session))
        except model.UnknownSession:
            print(f"bench: session {session} not found", file=sys.stderr)
            return EXIT_NO_SUCH_SESSION
    for mode in ("live", "all"):
        snapshots[mode], seconds[f"{mode} table"] = measure(lambda: model.collect(
            state_root, now=now, window_hours=window_hours, include_all=mode == "all", sessions=(),
            cost_log=cost_log))
        _, seconds[f"{mode} overlay"] = measure(
            lambda: tui.read_attention(snapshots[mode], locator, transcripts_root))
    if session is None:
        tasks = snapshots["live"]["tasks"] or snapshots["all"]["tasks"]
        _, seconds["detail"] = measure(lambda: read_detail(tasks[0]["session_id"])) if tasks else (None, 0.0)
    for name in BUDGETS:
        out(line(name, seconds[name], budgets[name]))
    out(f"volume: {len(snapshots['all']['tasks'])} tasks, {len(engine_session_binding.binding_paths())} binding files")
    return EXIT_OVER if any(verdict(seconds[name], budgets[name]) == "over" for name in BUDGETS) else 0
