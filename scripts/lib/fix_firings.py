"""Append-only firing log of flagged fixes (`fix-firing/v1`), one JSONL row per action a fix takes.

Core writes raw observables only: `kind` and `let_slip` stay null here and are judged later from
the engine's own records, outside Core, so a fix never grades its own firings. Fail-open: a
logging failure never changes the behaviour of the fix that fired.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "fix-firing/v1"
LOG_ENV = "AGENTCTL_FIX_FIRINGS_LOG"
DEFAULT_LOG = Path.home() / ".local" / "log" / "claude-fix-firings.jsonl"


def log_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get(LOG_ENV)
    return Path(override) if override else DEFAULT_LOG


def append(
    fix: str,
    row: str,
    event: str,
    observables: dict,
    *,
    session: str | None = None,
    task: str | None = None,
    stage: int | None = None,
    minutes: float = 0.0,
    cost_usd: float = 0.0,
) -> None:
    entry = {
        "schema": SCHEMA,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fix": fix,
        "row": row,
        "session": session,
        "task": task,
        "stage": stage,
        "event": event,
        "observables": observables,
        "minutes": round(float(minutes), 3),
        "cost_usd": round(float(cost_usd), 4),
        "kind": None,
        "let_slip": None,
    }
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
