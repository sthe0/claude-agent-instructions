"""Engine-gate path classification — the single source of truth for *which* file
edits the agentctl gate governs, shared by hook-state-gate.py and
hook-prewrite-plan-check.py so the two can never drift.

Difficulty removed: the agent's own configuration and instructions (CLAUDE.md,
skills/**, settings*.json, the instructions repo) used to bypass the coordination
spine via a broad ``claude-agent-instructions`` / ``/.claude/`` skip set, so edits
to the agent system itself never passed classify -> approve -> resolve. The policy
is now uniform: EVERY state-changing edit flows through the spine; the only
unconditionally exempt writes are the three memory roots and scratch (``/tmp``).

Plan artifacts (``~/.claude/plans/``) are NOT unconditionally exempt: a plan is
the result-image of *active planning*, so a plan-file write is legitimate only at
a planning-position node (``CLASSIFIED``/``ROUTED``/``PLANNING``/``PLAN_READY``).
Changing a plan during execution is a *difficulty* to be overcome reflexively
(``overcome-difficulty`` -> ``replan_substantive`` re-arms at ``PLAN_READY``),
not an in-place edit. That node-aware rule lives in hook-state-gate.py;
``is_plan_file`` below just identifies a plan path so the gate can apply it.

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

# The ONLY edits unconditionally exempt from the engine gate. Memory (all three
# scopes) is exempt by design; ``/tmp`` is scratch. Plan artifacts are NOT here —
# they are node-aware (see is_plan_file + hook-state-gate.py).
_EXEMPT_SUBSTRINGS = (
    "/tmp/",
    "/memory/",
    "/memory-global/",
    "/agent-memory/",
)

# A plan file: gated, but the gate applies a node-aware rule (writable only at a
# planning-position node) rather than the standard EXECUTING-only rule.
_PLAN_SUBSTRING = "/.claude/plans/"


def is_production_file(path: str) -> bool:
    """True if the path's extension makes it a gate candidate."""
    return bool(_PRODUCTION_FILE_RE.search(path or ""))


def is_engine_exempt(path: str) -> bool:
    """True if the path is unconditionally exempt from the engine gate
    (memory / scratch). Plan files are NOT exempt here — see is_plan_file."""
    p = path or ""
    return any(seg in p for seg in _EXEMPT_SUBSTRINGS)


def is_plan_file(path: str) -> bool:
    """True if the path is a coordination plan artifact under ~/.claude/plans/.
    Such files are gated, but the gate applies a node-aware rule (writable only at
    a planning-position node) instead of the standard EXECUTING-only rule."""
    return _PLAN_SUBSTRING in (path or "")


def is_gated_path(path: str) -> bool:
    """True if an edit to this path is governed by the engine gate: a production
    file that is not on the exempt list."""
    return is_production_file(path) and not is_engine_exempt(path)
