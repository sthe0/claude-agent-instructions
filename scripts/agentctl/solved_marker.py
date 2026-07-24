"""Resolution-gate marker: stamp `solved_by_007` on a resolved task's tracker artifact.

A task's tracker ticket, once the system resolves it, becomes a findable
precedent (GitHub `?labels=solved_by_007`, or the equivalent tag on whatever
tracker the machine's configured channel speaks) only if something stamps it.
Whether to stamp is fully decidable from observed state (resolved + a known
tracker key) — a rule, not a judgement — so it is mechanized here and invoked
unconditionally at `cmd_resolve`, never left to the coordinator to remember.

This module is pure dispatch: it reuses the difficulty_channel adapters'
auth/HTTP (`add_tag` / `add_label`) rather than re-implementing a client, and it
never raises — any failure (no key, no token, HTTP error, unclassifiable key,
a bare github number, a missing adapter plugin) resolves to a `stamped: False`
status dict so a marker failure never blocks resolution.

`key_shape` classifies by SHAPE, not by channel: Core knows what a
fully-qualified GitHub ref looks like, and it knows the `PROJ-123` shape every
issue tracker uses, but WHICH tracker owns such a key is this machine's
configured channel — resolved at stamp time via ``load_adapter``, so the plugin
is never needed just to import this module.
"""
from __future__ import annotations

import re
from typing import Callable

from difficulty_channel.adapters import BUILTIN_NAMES, github, load_adapter
from difficulty_channel.authority import read_configured_channel

SOLVED_MARKER = "solved_by_007"

# Identical to agentctl.classify.TRACKER_KEY_RE — a tracker's own key (e.g. PROJ-445).
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# A fully-qualified GitHub issue ref: owner/repo#N, owner/repo/issues/N, or a github.com URL.
GITHUB_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?[\w.-]+/[\w.-]+(?:#|/issues/)\d+/?$"
)


def key_shape(s: str | None) -> str | None:
    """Classify a tracker/issue reference: 'issue-key' | 'github' | None (unclassifiable)."""
    if not s:
        return None
    if ISSUE_KEY_RE.match(s):
        return "issue-key"
    if GITHUB_REF_RE.match(s):
        return "github"
    return None


def _configured_add_tag(channel: str) -> Callable[..., None]:
    """Resolve `add_tag` from the configured channel's plugin adapter.

    A built-in channel has no `add_tag`: github stamps labels on fully-qualified refs,
    which is the other branch of `stamp`. Raising here reports that as a skip reason.
    """
    if channel in BUILTIN_NAMES:
        raise LookupError(
            f"configured channel {channel!r} stamps fully-qualified refs only, "
            f"not bare issue keys"
        )
    return load_adapter(channel).add_tag


def stamp(
    tracker_key: str | None,
    *,
    repo: str | None = None,
    plugin_add: Callable[..., None] | None = None,
    github_add: Callable[..., None] = github.add_label,
) -> dict:
    """Stamp SOLVED_MARKER on the ticket/issue identified by `tracker_key`.

    Returns a status dict {channel, key, stamped, skipped_reason?} and never raises —
    every failure mode (no key, no token, HTTP error, unclassifiable key, a bare
    github number, a channel with no plugin on this machine) is caught and reported
    as a skip. ``plugin_add`` defaults to None and is resolved from the configured
    channel only when an issue-shaped key is actually being stamped (test seam: pass
    an explicit callable to skip the plugin loader entirely).
    """
    shape = key_shape(tracker_key)
    if shape is None:
        return {"channel": None, "key": tracker_key, "stamped": False,
                 "skipped_reason": "no key or unclassifiable key"}
    channel = "github" if shape == "github" else read_configured_channel()
    try:
        if shape == "issue-key":
            add = plugin_add or _configured_add_tag(channel)
            add(tracker_key, SOLVED_MARKER)
        else:
            github_add(tracker_key, SOLVED_MARKER, repo=repo)
        return {"channel": channel, "key": tracker_key, "stamped": True}
    except Exception as exc:  # noqa: BLE001 - fail-open by design, any error is a skip
        return {"channel": channel, "key": tracker_key, "stamped": False,
                 "skipped_reason": str(exc)}
