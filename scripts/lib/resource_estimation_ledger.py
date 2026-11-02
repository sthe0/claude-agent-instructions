"""The request-vs-fact ledger: one row per (task, stage) comparing an executor's resource request with its recorded uses.

Difficulty removed: the engine (agentctl/resource_estimation.py) writes the rows and
scripts/self-diagnose.py reads them, so the path and the latest-row-wins rule live in one
place both import. A write only appends; a re-dispatch or replan of the same (task, stage)
appends a newer row and the reader keeps the last one, so no write rewrites the file. The
path honours `AGENTCTL_RESOURCE_ESTIMATION_LEDGER` so a test run never touches the real file
under the home directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from lib.transcript_cost import iter_jsonl

LEDGER_ENV = "AGENTCTL_RESOURCE_ESTIMATION_LEDGER"


def ledger_path() -> Path:
    override = os.environ.get(LEDGER_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "log" / "claude-resource-estimation.jsonl"


def read_rows(path: "Path | None" = None) -> "list[dict]":
    """The last row of every (task_id, stage), in order of that last row; an absent file or a torn line is skipped."""
    latest: "dict[tuple, dict]" = {}
    for row in iter_jsonl(path if path is not None else ledger_path()):
        key = (row.get("task_id"), row.get("stage"))
        latest.pop(key, None)
        latest[key] = row
    return list(latest.values())


def append_row(row: dict, path: "Path | None" = None) -> None:
    target = path if path is not None else ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
