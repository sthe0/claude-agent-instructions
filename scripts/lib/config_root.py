"""Python config-root resolver — the read-time analog of ``scripts/lib/config-root.sh``.

Difficulty removed: the isolation refactor moved every *install target* to
``$CLAUDE_AGENT_HOME`` (``~/.claude-agent`` by default) via ``config-root.sh``,
but the runtime *readers* (hooks, spawn resolvers, verifiers, identity readers)
kept hardcoding ``Path.home() / ".claude"``. On a migrated machine that root no
longer holds the system's skills / identity, so those readers silently miss
them. This module is the single structural home every Python reader calls
instead of hardcoding the path.

Semantics differ from the shell resolver on purpose:

- ``config-root.sh`` is sourced at **install** time and must always target the
  isolated root so setup can *create* it — hence it defaults unconditionally to
  ``~/.claude-agent``.
- This module runs at **read** time and must find where artifacts *actually
  live*, so after the env overrides it probes for an existing ``~/.claude-agent``
  and falls back to ``~/.claude`` for a not-yet-migrated (legacy in-place)
  machine.

Resolution order (first hit wins):
  1. ``$CLAUDE_CONFIG_DIR``  — the CLI relocates its entire config root here
     (the ``claude-agent`` / ``claude-task`` launchers export it).
  2. ``$CLAUDE_AGENT_HOME``  — explicit install-root override / overlay.
  3. ``~/.claude-agent``     — the isolated default, when it exists.
  4. ``~/.claude``           — legacy non-isolated fallback.
"""
from __future__ import annotations

import os
from pathlib import Path


def agent_home() -> Path:
    """Resolve the system config root for runtime reads (see module docstring)."""
    for env_var in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME"):
        val = os.environ.get(env_var)
        if val:
            return Path(val).expanduser()
    isolated = Path.home() / ".claude-agent"
    if isolated.exists():
        return isolated
    return Path.home() / ".claude"


def skills_dir() -> Path:
    """Directory holding the system's specialization skills (``<root>/skills``)."""
    return agent_home() / "skills"


def identity_file() -> Path:
    """Per-machine ``agent-identity.local``.

    Honors an explicit ``$CLAUDE_AGENT_IDENTITY`` override (kept for parity with
    the shell readers) before falling back to ``<root>/agent-identity.local``.
    """
    override = os.environ.get("CLAUDE_AGENT_IDENTITY")
    if override:
        return Path(override).expanduser()
    return agent_home() / "agent-identity.local"


def agentctl_dir() -> Path:
    """Root of agentctl's own persisted state (``<root>/agentctl``)."""
    return agent_home() / "agentctl"


def agentctl_state_dir() -> Path:
    """Session-state JSON store (``<root>/agentctl/state`` — see agentctl/store.py)."""
    return agentctl_dir() / "state"


def agentctl_gate_log() -> Path:
    """Gate-transition telemetry log (``<root>/agentctl/gate-log.jsonl``)."""
    return agentctl_dir() / "gate-log.jsonl"


def agentctl_scopes_dir() -> Path:
    """Session-scope registry directory (``<root>/agentctl/scopes`` — see
    session_scope/registry.py)."""
    return agentctl_dir() / "scopes"


def agentctl_edit_log() -> Path:
    """Durable session->edit ledger (``<root>/agentctl/edit-log.jsonl`` — see
    agentctl/edit_ledger.py). Honors an ``$AGENTCTL_EDIT_LEDGER`` override at
    the call site (edit_ledger.py), mirroring agentctl_gate_log()'s role for
    gate-log.jsonl."""
    return agentctl_dir() / "edit-log.jsonl"


def plans_dir() -> Path:
    """Coordination plan artifacts directory (``<root>/plans``)."""
    return agent_home() / "plans"


def legacy_home() -> Path:
    """The pre-isolation root (``~/.claude``), for read-time fallback only —
    never a write target. Distinct from ``agent_home()``'s own legacy fallback:
    that one applies when the isolated root has never been created; this one
    lets a *reader* also find state left behind under ``~/.claude`` on a
    machine where ``~/.claude-agent`` now exists but a given artifact predates
    the migration (mid-migration or not-yet-migrated-for-that-artifact)."""
    return Path.home() / ".claude"


def agentctl_legacy_state_dir() -> Path:
    """Legacy (pre-isolation) session-state dir — read-time fallback only."""
    return legacy_home() / "agentctl" / "state"


def sanitize_session_id(session_id: str | None) -> str:
    """Filesystem-safe session id (alnum/-/_ only), matching agentctl/store.py's
    FileStateStore sanitization — kept in sync so the same session resolves to
    the same filename everywhere it is looked up."""
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def resolve_agentctl_state_file(session_id: str | None) -> Path | None:
    """Find a session's agentctl state JSON file: current root first, then the
    legacy pre-isolation root — so a session started before migrate-to-
    isolated.sh ran is still found on a half-migrated machine (fail CLOSED for
    gates: 'new root has nothing for this session' must never mean 'allow').
    None when the file exists on neither root; callers must not create one —
    that is the exclusive job of agentctl/store.py's FileStateStore."""
    fname = f"{sanitize_session_id(session_id)}.json"
    current = agentctl_state_dir() / fname
    if current.exists():
        return current
    legacy = agentctl_legacy_state_dir() / fname
    if legacy != current and legacy.exists():
        return legacy
    return None
