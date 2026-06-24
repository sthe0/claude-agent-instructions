"""Engine-gate path classification — the single source of truth for *which* file
edits the agentctl gate governs, shared by hook-state-gate.py and
hook-prewrite-plan-check.py so the two can never drift.

Difficulty removed: the agent's own configuration and instructions (CLAUDE.md,
skills/**, settings*.json, the instructions repo) used to bypass the coordination
spine via a broad ``claude-agent-instructions`` / ``/.claude/`` skip set, so edits
to the agent system itself never passed classify -> approve -> resolve. The policy
is now uniform: EVERY state-changing edit flows through the spine; the only exempt
writes are the three memory roots, scratch (``/tmp``), and coordination plan
artifacts (which are authored *before* the EXECUTING node and would otherwise be
impossible to write once the gate covers ``.md``).

Memory-substring precision (the subtle part): the three memory scopes live at
distinct path fragments, and ``/memory/`` does NOT match the other two — so all
three are listed explicitly:
  - ``/memory/``         native auto-memory (project, via the symlink)
  - ``/memory-global/``  global engineering memory
  - ``/agent-memory/``   project memory (direct repo path)
"""
from __future__ import annotations

import re

# Edits to files matching this pattern are *candidates* for the gate; anything
# else (prose .txt, images, data) is never gated. ``.md`` / ``.mdc`` are included
# so the agent's own instructions (CLAUDE.md, skills/**/*.md, the Cursor mirror)
# are governed — memory ``.md`` is rescued by is_engine_exempt below.
_PRODUCTION_FILE_RE = re.compile(
    r"\.(py|sh|yaml|yml|json|ts|tsx|js|jsx|go|rs|cpp|c|h|java|kt|rb|tf|toml|cfg|conf|ini|md|mdc)$",
    re.IGNORECASE,
)

# The ONLY edits exempt from the engine gate. Memory (all three scopes) is exempt
# by design; ``/tmp`` is scratch; plan artifacts must be writable during PLANNING
# (a plan is authored before the gate-passing EXECUTING node exists).
_EXEMPT_SUBSTRINGS = (
    "/tmp/",
    "/memory/",
    "/memory-global/",
    "/agent-memory/",
    "/.claude/plans/",
)


def is_production_file(path: str) -> bool:
    """True if the path's extension makes it a gate candidate."""
    return bool(_PRODUCTION_FILE_RE.search(path or ""))


def is_engine_exempt(path: str) -> bool:
    """True if the path is exempt from the engine gate (memory / scratch / plans)."""
    p = path or ""
    return any(seg in p for seg in _EXEMPT_SUBSTRINGS)


def is_gated_path(path: str) -> bool:
    """True if an edit to this path is governed by the engine gate: a production
    file that is not on the exempt list."""
    return is_production_file(path) and not is_engine_exempt(path)
