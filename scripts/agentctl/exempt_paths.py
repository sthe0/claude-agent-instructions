"""Engine-gate path classification — the single source of truth for *which* file
edits the agentctl gate governs, shared by hook-state-gate.py and
hook-prewrite-plan-check.py so the two can never drift.

Difficulty removed: the agent's own configuration and instructions (CLAUDE.md,
skills/**, settings*.json, the instructions repo) used to bypass the coordination
spine via a broad ``claude-agent-instructions`` / ``/.claude/`` skip set, so edits
to the agent system itself never passed classify -> approve -> resolve. The policy
is now uniform: EVERY state-changing edit flows through the spine; the only
unconditionally exempt writes are the three memory roots and scratch (``/tmp``).

Plan artifacts (``~/.claude/plans/`` or, on an isolated-root install,
``~/.claude-agent/plans/``) are NOT unconditionally exempt: a plan is the
result-image of *active planning*, so a plan-file write is legitimate only at a
planning-position node (``CLASSIFIED``/``ROUTED``/``PLANNING``/``PLAN_READY``).
Changing a plan during execution is a *difficulty* to be overcome reflexively
(``overcome-difficulty`` -> ``replan_substantive`` re-arms at ``PLAN_READY``),
not an in-place edit. That node-aware rule lives in hook-state-gate.py;
``is_plan_file`` below just identifies a plan path so the gate can apply it —
it recognizes both the legacy and isolated-root plan directories so a plan is
gated correctly on either layout, including mid-migration.

Memory-substring precision (the subtle part): the three memory scopes live at
distinct path fragments, and ``/memory/`` does NOT match the other two — so all
three are listed explicitly:
  - ``/memory/``         native auto-memory (project, via the symlink)
  - ``/memory-global/``  global engineering memory
  - ``/agent-memory/``   project memory (direct repo path)
"""
from __future__ import annotations

import os
import re
import tempfile

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
# planning-position node) rather than the standard EXECUTING-only rule. Both the
# legacy in-place layout and the isolated-root layout are recognized, since a
# half-migrated machine (or a not-yet-migrated one) may have live plans under
# either directory.
_PLAN_SUBSTRINGS = ("/.claude/plans/", "/.claude-agent/plans/")


def is_production_file(path: str) -> bool:
    """True if the path's extension makes it a gate candidate."""
    return bool(_PRODUCTION_FILE_RE.search(path or ""))


def _gate_scratch_roots() -> "tuple[str, ...]":
    """scratch_roots() minus any root so broad it would exempt the home directory:
    '/', $HOME, or an ancestor of $HOME — whatever its source (TMPDIR is
    user-settable). $HOME is read at call time. Gate side only; the ledger keeps
    scratch_roots() unfiltered."""
    home = os.path.realpath(os.environ.get("HOME") or os.path.expanduser("~"))
    keep = []
    for root in scratch_roots():
        r = os.path.realpath(root)
        if r == os.sep or home == r or home.startswith(r + os.sep):
            continue
        keep.append(root)
    return tuple(keep)


def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root + os.sep)


def _under_local_state(p: str) -> bool:
    """True if the realpath `p` is under <projects-root>/<one component>/local-state/."""
    try:
        from lib import config_root
        roots = config_root.projects_roots()
    except Exception:
        return False
    for root in roots:
        r = os.path.realpath(str(root))
        if not p.startswith(r + os.sep):
            continue
        parts = p[len(r) + 1:].split(os.sep)
        if len(parts) >= 3 and parts[0] and parts[1] == "local-state":
            return True
    return False


def is_engine_exempt(path: str) -> bool:
    """True if the path is unconditionally exempt from the engine gate: memory,
    ``/tmp/``, anything that resolves (realpath containment, never substring) under
    a scratch root that is not as broad as $HOME, and the personal per-project
    ``<projects-root>/<hash>/local-state/`` directory. Plan files are NOT exempt
    here — see is_plan_file."""
    p = path or ""
    if any(seg in p for seg in _EXEMPT_SUBSTRINGS):
        return True
    if not p:
        return False
    rp = os.path.realpath(os.path.normpath(p))
    if any(_under(rp, os.path.realpath(root)) for root in _gate_scratch_roots()):
        return True
    return _under_local_state(rp)


def is_plan_file(path: str) -> bool:
    """True if the path is a coordination plan artifact under ~/.claude/plans/ or
    the isolated-root ~/.claude-agent/plans/. Such files are gated, but the gate
    applies a node-aware rule (writable only at a planning-position node) instead
    of the standard EXECUTING-only rule."""
    p = path or ""
    return any(seg in p for seg in _PLAN_SUBSTRINGS)


def is_gated_path(path: str) -> bool:
    """True if an edit to this path is governed by the engine gate: a production
    file that is not on the exempt list."""
    return is_production_file(path) and not is_engine_exempt(path)


# Override for scratch_roots(): os.pathsep-separated, REPLACES the defaults when
# set and non-empty. Mirrors edit_ledger.py's $AGENTCTL_EDIT_LEDGER override idiom
# so both in-process and subprocess controls can state which roots count.
SCRATCH_ROOTS_ENV = "AGENTCTL_SCRATCH_ROOTS"


def scratch_roots() -> "tuple[str, ...]":
    """Resolve, at call time, the set of roots that count as ephemeral OS-temp
    scratch for is_ledger_noise(). Never cached at import: an env change inside a
    test or a subprocess must be honoured on the next call.

    Default roots: "/tmp" (unconditional — scratch by convention even when
    $TMPDIR points elsewhere), $TMPDIR if set, and tempfile.gettempdir(). Each
    root is kept in both its normpath and realpath form (on a machine where
    $TMPDIR is itself a symlink target, e.g. /var/tmp -> /place/vartmp, a caller
    may hand either form), empty entries dropped, order-stable dedupe.
    """
    override = os.environ.get(SCRATCH_ROOTS_ENV)
    if override:
        candidates = [c for c in override.split(os.pathsep) if c]
    else:
        candidates = ["/tmp"]
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir:
            candidates.append(tmpdir)
        candidates.append(tempfile.gettempdir())

    roots: "list[str]" = []
    seen = set()
    for c in candidates:
        for form in (os.path.normpath(c), os.path.realpath(c)):
            if form and form not in seen:
                seen.add(form)
                roots.append(form)
    return tuple(roots)


def under_scratch_root(path: str) -> bool:
    """True if `path` resolves under one of scratch_roots(), by root containment
    (never a substring test): "<root>-evil/x.py" must not match root "<root>", and a
    path need not literally contain a trailing slash to be recognized as being at a
    root — mirrors hook-orphan-worktree-sweep.py's is_temp_root(). The one place that
    decides "is this path OS-temp scratch"; is_ledger_noise and the engine's
    ephemeral-output-artifact and evidence-directory checks all answer their own
    questions by calling this rather than re-deriving the containment test.
    """
    p = os.path.realpath(os.path.normpath(path or ""))
    for root in scratch_roots():
        if p == root or p.startswith(root + os.sep):
            return True
    return False


def is_ledger_noise(path: str) -> bool:
    """True if `path` is not worth recording in the durable edit ledger: ONLY
    ephemeral OS-temp scratch (under_scratch_root()). This answers a different
    question from is_engine_exempt: that predicate decides what the engine GATE
    permits (memory is exempt from the gate by design); this one decides what the
    durable attribution LEDGER observes. Memory is exactly the file class the ledger
    most needs to attribute, so it is NOT ledger noise even though it is gate-exempt
    — the two predicates must stay independent, or the ledger silently inherits the
    gate's permission answer for a question the gate was never asked.

    The scratch roots are /tmp, $TMPDIR and tempfile.gettempdir(); a session
    scratchpad living under one of them (e.g. TMPDIR=/var/tmp) is therefore
    scratch here, while one outside them stays ledgered. The gate's extra
    exemptions (local-state, the breadth guard on roots) do not apply here.
    """
    return under_scratch_root(path)
