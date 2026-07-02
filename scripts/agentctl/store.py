"""State persistence — the ONLY filesystem seam in the engine.

machine.py and classify.py are pure; cli.py is the only caller that loads/saves.
Isolating durable IO behind the StateStore Protocol is what lets a later
Variant-3/MCP server swap FileStateStore for a network-backed store without
touching the state machine or classification logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from lib import config_root

from .state import SessionState

DEFAULT_ROOT = config_root.agentctl_state_dir()


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


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
        return SessionState.from_json(p.read_text(encoding="utf-8"))

    def save(self, state: SessionState) -> None:
        state.check_invariants()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(state.session_id).write_text(state.to_json(), encoding="utf-8")
