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

Two roots, not one
------------------

``agent_home()`` answers *"where do the system's artifacts live"*. That is a
different question from *"which config root is the running harness loading
hooks, settings and skills from"*, and on a migrated machine the two answers
differ. ``harness_config_root()`` answers the second one, using the CLI's own
rule and nothing else: ``$CLAUDE_CONFIG_DIR`` else ``~/.claude``.

- **They coincide** under the ``claude-agent`` / ``claude-task`` launchers,
  which export ``CLAUDE_CONFIG_DIR=$CLAUDE_AGENT_HOME`` — the harness and the
  system read the same directory.
- **They diverge** under a bare ``claude`` on a migrated machine: the harness
  loads ``~/.claude`` (the personal root, where the system installs nothing)
  while ``agent_home()`` resolves ``~/.claude-agent``.

Conflating them makes a gate report on a root that is not the one enforcing it:
a check run against ``agent_home()`` finds every hook correctly registered and
reports green, while the harness that would actually have to fire those hooks
has none of them. Ask ``harness_config_root()`` whenever the subject is the
harness's own wiring — registered hooks, ``settings.json``, permissions.
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


def harness_config_root() -> Path:
    """Resolve the config root the *running harness* loads from — the CLI's own
    rule, ``$CLAUDE_CONFIG_DIR`` else ``~/.claude`` (see module docstring § Two
    roots).

    Deliberately does NOT probe for ``~/.claude-agent``: the CLI does not, so
    probing would report a root the harness never reads and re-introduce the
    very conflation this accessor exists to remove.
    """
    val = os.environ.get("CLAUDE_CONFIG_DIR")
    if val:
        return Path(val).expanduser()
    return Path.home() / ".claude"


def projects_roots() -> list[Path]:
    """Every existing ``<root>/projects`` directory, deduplicated, agent root first.

    Answers *"where have sessions written"*. That is a THIRD question, distinct
    from both accessors above: ``agent_home()`` says where the system is
    installed and ``harness_config_root()`` says where the running harness loads
    settings from, and neither answers where transcripts and per-project memory
    actually landed. They coincided on an unmigrated machine, which is how a
    dozen call sites came to ask ``agent_home()`` and stay plausibly green.

    Both roots are read because both get written to: `claude-agent` /
    `claude-task` sessions write under the isolated root while a bare `claude`
    writes under ``~/.claude``, and on a machine using both, a report that picks
    either single root is wrong for the sessions that used the other one — and
    wrong SILENTLY, printing a smaller number rather than an error.

    Deliberately NEUTRAL about what lives inside: transcripts and per-project
    ``memory/`` directories are different domains sharing one location, so this
    returns the locations and lets each caller keep its own selection. See
    ``iter_transcripts()`` for the transcript view layered on top; a caller that
    wants ``*/memory`` globs these roots itself.

    Dedup is by ``Path.resolve()`` — the launchers export
    ``CLAUDE_CONFIG_DIR=$CLAUDE_AGENT_HOME``, so on most machines the two roots
    are the same directory and must yield ONE entry, and a half-migrated machine
    can reach one through a symlink to the other. Dedup is by root, never by
    project: the same cwd-hash under two roots is two distinct session sets.

    A root with no ``projects/`` yet is skipped, not an error — a fresh machine
    or a root that has simply never hosted a session is a normal state.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for root in (agent_home(), harness_config_root()):
        candidate = root / "projects"
        if not candidate.is_dir():
            continue
        try:
            key = candidate.resolve()
        except OSError:  # pragma: no cover - unresolvable path, keep the raw one
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def iter_transcripts(pattern: str = "*/*.jsonl") -> list[Path]:
    """Session transcripts across every root from ``projects_roots()``, sorted.

    ``pattern`` is glob-relative to each root and defaults to the main-session
    layout ``<project>/<session>.jsonl``. Pass ``"**/*.jsonl"`` to include the
    per-session ``<session>/subagents/*.jsonl`` transcripts as well.

    The two selections are NOT interchangeable — the default counts sessions, the
    recursive one counts every transcript file — so each caller keeps the one it
    already had. Changing a caller's pattern changes WHAT it reports, not merely
    where it looks.
    """
    out: list[Path] = []
    for root in projects_roots():
        out.extend(root.glob(pattern))
    return sorted(out)


def harness_settings_file() -> Path:
    """The harness's live ``settings.json`` (``<harness root>/settings.json``) —
    the file whose ``hooks`` block decides which hooks actually fire."""
    return harness_config_root() / "settings.json"


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


def hook_state_dir(name: str) -> Path:
    """Durable marker/log directory for one hook (``<root>/state/<name>``).

    Hooks that must remember something across turns (turn-gate's per-turn
    markers, the review guardian's per-review nudge markers, the review
    auto-arm hook's claim markers and poller logs) each derived this path
    themselves, so a relocated config root had to be honored in as many places
    as there were hooks. The single derivation lives here; each caller keeps
    only its own fail-open guard around the import, which is the part a shared
    function cannot cover.
    """
    return agent_home() / "state" / str(name)


def agentctl_dir() -> Path:
    """Root of agentctl's own persisted state (``<root>/agentctl``)."""
    return agent_home() / "agentctl"


def agentctl_state_dir() -> Path:
    """Session-state JSON store (``<root>/agentctl/state`` — see agentctl/store.py)."""
    return agentctl_dir() / "state"


def agentctl_gate_log() -> Path:
    """Gate-transition telemetry log (``<root>/agentctl/gate-log.jsonl``)."""
    return agentctl_dir() / "gate-log.jsonl"


def canon_roots_file() -> Path:
    """Machine-local list of extra canon-read-only path roots, one per line
    (``<root>/canon-roots.local`` — see hook-guard-canon-readonly.py). Org-
    neutral by construction: Core code only ever sees an opaque path list, never
    an org-specific token. Honors an ``$CLAUDE_CANON_ROOTS_FILE`` override,
    mirroring ``agentctl_edit_log()``'s ``$AGENTCTL_EDIT_LEDGER`` pattern."""
    override = os.environ.get("CLAUDE_CANON_ROOTS_FILE")
    if override:
        return Path(override).expanduser()
    return agent_home() / "canon-roots.local"


def skill_first_classes_file() -> Path:
    """Machine-local extra operation classes for hook-skill-first.py
    (``<root>/skill-first-classes.local``, JSON). Org-neutral by construction,
    the same way ``canon_roots_file()`` is: Core code only ever sees an opaque
    name/regex/skill-family triple, never an org-specific command verb. Honors a
    ``$CLAUDE_SKILL_FIRST_CLASSES_FILE`` override."""
    override = os.environ.get("CLAUDE_SKILL_FIRST_CLASSES_FILE")
    if override:
        return Path(override).expanduser()
    return agent_home() / "skill-first-classes.local"


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
