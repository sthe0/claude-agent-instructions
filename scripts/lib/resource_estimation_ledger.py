"""The request-vs-fact ledger: one row per (task, stage) comparing an executor's resource request with its recorded uses.

Difficulty removed: the engine (agentctl/resource_estimation.py) writes the rows and
scripts/self-diagnose.py reads them, so the path and the replace-by-key rule live in one
place both import. The path honours `AGENTCTL_RESOURCE_ESTIMATION_LEDGER` so a test run never
touches the real file under the home directory.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

LEDGER_ENV = "AGENTCTL_RESOURCE_ESTIMATION_LEDGER"


def ledger_path() -> Path:
    override = os.environ.get(LEDGER_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "log" / "claude-resource-estimation.jsonl"


def read_rows(path: "Path | None" = None) -> "list[dict]":
    """Every readable row; an absent file, a torn line or a non-object line is skipped."""
    target = path if path is not None else ledger_path()
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def replace_row(row: dict, path: "Path | None" = None) -> None:
    """Write `row`, dropping an earlier row of the same (task_id, stage) so a replan never duplicates it."""
    target = path if path is not None else ledger_path()
    key = (row.get("task_id"), row.get("stage"))
    kept = [r for r in read_rows(target) if (r.get("task_id"), r.get("stage")) != key]
    kept.append(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".estimation-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
