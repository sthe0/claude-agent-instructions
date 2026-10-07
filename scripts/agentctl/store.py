"""State persistence — the ONLY filesystem seam in the engine.

machine.py and classify.py are pure; cli.py is the only caller that loads/saves.
Isolating durable IO behind the StateStore Protocol is what lets a later
Variant-3/MCP server swap FileStateStore for a network-backed store without
touching the state machine or classification logic.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol

from lib import config_root

from .state import SessionState

DEFAULT_ROOT = config_root.agentctl_state_dir()


def safe_session_id(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


_safe = safe_session_id


def stamp_new_history(state: SessionState, now: dt.datetime | None = None) -> None:
    """Give every history event appended since this state was loaded an ISO-8601 UTC
    `ts`, once. The clock lives here, at the store seam, because SessionState.log and
    the gate modules are pure. Events that were already in the file at load time stay
    exactly as they were: stamping a pre-change event with the save time would make a
    session that began before this field existed look as if it began after it. A state
    never loaded from a file (a new session) counts all its events as appended."""
    start = getattr(state, "_loaded_history_len", 0)
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    for event in state.history[start:]:
        event.setdefault("ts", stamp)
    state._loaded_history_len = len(state.history)


class StateStore(Protocol):
    def exists(self, session_id: str) -> bool: ...
    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, state: SessionState) -> None: ...


class FileStateStore:
    """JSON-file-backed store at <root>/<session_id>.json."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    def path(self, session_id: str) -> Path:
        return self.root / f"{_safe(session_id)}.json"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).exists()

    def load(self, session_id: str) -> SessionState | None:
        p = self.path(session_id)
        if not p.exists():
            return None
        state = SessionState.from_json(p.read_text(encoding="utf-8"))
        state._loaded_history_len = len(state.history)
        return state

    def save(self, state: SessionState) -> None:
        state.check_invariants()
        stamp_new_history(state)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(state.session_id).write_text(state.to_json(), encoding="utf-8")
