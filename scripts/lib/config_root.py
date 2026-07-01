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
