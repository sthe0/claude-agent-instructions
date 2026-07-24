"""Resolution-gate marker: stamp `solved_by_007` on a resolved task's tracker artifact.

A task's tracker ticket, once the system resolves it, becomes a findable
precedent (Startrek `Tags: "solved_by_007"` / GitHub `?labels=solved_by_007`)
only if something stamps it. Whether to stamp is fully decidable from observed
state (resolved + a known tracker key) — a rule, not a judgement — so it is
mechanized here and invoked unconditionally at `cmd_resolve`, never left to the
coordinator to remember.

This module is pure dispatch: it reuses the difficulty_channel adapters'
auth/HTTP (`add_tag` / `add_label`) rather than re-implementing a client, and it
never raises — any failure (no key, no token, HTTP error, unclassifiable key,
a bare github number, a missing adapter plugin) resolves to a `stamped: False`
status dict so a marker failure never blocks resolution.

The startrek channel is not a Core built-in (ADR-0001 B1): its ``add_tag`` is
resolved lazily via ``load_adapter`` only when a startrek-shaped key is actually
being stamped, so this module never requires the plugin to be installed just to
import cleanly on a machine that doesn't use that channel.
"""
from __future__ import annotations

import re
from typing import Callable

from difficulty_channel.adapters import github, load_adapter

SOLVED_MARKER = "solved_by_007"

# Identical to agentctl.classify.TRACKER_KEY_RE — a Startrek-shaped key (e.g. DEEPAGENT-445).
STARTREK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# A fully-qualified GitHub issue ref: owner/repo#N, owner/repo/issues/N, or a github.com URL.
GITHUB_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?[\w.-]+/[\w.-]+(?:#|/issues/)\d+/?$"
)


def looks_like_key(s: str | None) -> str | None:
    """Classify a tracker/issue key string: 'startrek' | 'github' | None (unclassifiable)."""
    if not s:
        return None
    if STARTREK_KEY_RE.match(s):
        return "startrek"
    if GITHUB_REF_RE.match(s):
        return "github"
    return None


def stamp(
    tracker_key: str | None,
    *,
    repo: str | None = None,
    startrek_add: Callable[..., None] | None = None,
    github_add: Callable[..., None] = github.add_label,
) -> dict:
    """Stamp SOLVED_MARKER on the ticket/issue identified by `tracker_key`.

    Returns a status dict {channel, key, stamped, skipped_reason?} and never raises —
    every failure mode (no key, no token, HTTP error, unclassifiable key, a bare
    github number, a missing startrek plugin on this machine) is caught and reported
    as a skip. ``startrek_add`` defaults to None and is resolved via ``load_adapter``
    only when a startrek-shaped key is actually being stamped (test seam: pass an
    explicit callable to skip the plugin loader entirely).
    """
    channel = looks_like_key(tracker_key)
    if channel is None:
        return {"channel": None, "key": tracker_key, "stamped": False,
                 "skipped_reason": "no key or unclassifiable key"}
    try:
        if channel == "startrek":
            add = startrek_add or load_adapter("startrek").add_tag
            add(tracker_key, SOLVED_MARKER)
        else:
            github_add(tracker_key, SOLVED_MARKER, repo=repo)
        return {"channel": channel, "key": tracker_key, "stamped": True}
    except Exception as exc:  # noqa: BLE001 - fail-open by design, any error is a skip
        return {"channel": channel, "key": tracker_key, "stamped": False,
                 "skipped_reason": str(exc)}
