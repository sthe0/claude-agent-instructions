#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Smallest textual start the dashboard's launcher can make: the floor of R1 and R2.

Launched like the dashboard (``uv run <file>``), it imports textual, reads the files named by ``--read``
(each parsed as JSON when it ends in ``.json``, standing for a snapshot), paints a table whose header and
rows are laid out the way the dashboard's overview is (columns ``task wait session live``) and waits for
``q``. ``plan_dashboard_harness.py floors`` times it from outside, on a pseudo-terminal, as the first
moment a row is on the screen; nothing the dashboard does can start faster than this. It writes nothing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import DataTable

COLUMNS = ("task", "wait", "session", "live")
ROWS = (("probe-task", "Q", "probe-session-0001", "active"),
        ("probe-task-2", "", "probe-session-0002", "idle"))


class StartProbe(App):
    BINDINGS = [("q", "quit", "quit")]

    def compose(self) -> ComposeResult:
        yield DataTable()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*COLUMNS)
        for row in ROWS:
            table.add_row(*row)


def read_snapshots(paths) -> int:
    total = 0
    for path in paths:
        data = Path(path).read_bytes()
        total += len(data)
        if str(path).endswith(".json"):
            json.loads(data)
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read", action="append", default=[], type=Path)
    args = parser.parse_args()
    read_snapshots(args.read)
    StartProbe().run()


if __name__ == "__main__":
    main()
