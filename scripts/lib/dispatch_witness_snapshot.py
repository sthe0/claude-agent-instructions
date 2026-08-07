"""The old-wiring snapshot: one writer, one reader, one schema.

Difficulty removed: the snapshot is a contract between two steps that never
run together — a wiring-capture step writes it BEFORE the hooks are
re-registered, and check-dispatch-witness.py reads it after. Nothing but prose
held the two ends together, so each could drift into a shape the other did not
expect and the mismatch would surface as a witness that fails for the wrong
reason (or, worse, passes on a field it misread). Both ends now import this
module: the shape is written by ``write_snapshot`` and validated by
``load_snapshot``, and a round-trip test pins them to each other.

The file records, per judge-calling hook, what its registration looked like
BEFORE the change:

    {
      "schema": "dispatch-witness-old-wiring/v2",
      "hooks": {
        "hook-turn-end-gate.py": {
          "status": "wired", "timeout": 5,
          "scope_qualified": true,
          "members_read": ["/home/u/.claude-agent/settings.json"]
        },
        "hook-...-gate.py": {
          "status": "absent", "timeout": null,
          "scope_qualified": true, "members_read": [...]
        }
      }
    }

``status`` carries one of lib/hook_wiring.py's three probe outcomes verbatim
and ``timeout`` is a number or an explicit null. Both are needed: a bare null
cannot tell "was never registered" (so any execution is new evidence) apart
from "could not be determined" (so nothing may be concluded).

``scope_qualified`` is the field v1 lacked, and the reason for the version
bump. hook_wiring.probe() reads the project-level ``.claude/settings.json``
only when the harness named a project root in ``$CLAUDE_PROJECT_DIR``; without
it, ABSENT means "not registered in any USER-LEVEL member of this root", not
"not registered", and WIRED's timeout is the largest among the members it could
see rather than the largest that exists. A reader that spends either as the
unqualified fact reasons from a probe that never looked everywhere. Carrying
the qualification lets the reader fail closed on BOTH statuses;
``members_read`` records which files the claim actually rests on, so the
failure can name them.

The field is DERIVED, not declared. ``entry_for`` reads
``Wiring.project_scope_covered`` — the probe's own record of what it reached —
so a document cannot carry ``scope_qualified: false`` beside a ``members_read``
list holding nothing but user-level files. The earlier shape took the answer
from a caller keyword, which no reader could cross-check and no writer had
grounds to set.

There is no v1 compatibility path on purpose. A v1 file carries no scope
information at all, so it cannot be upgraded — only guessed at — and the
writer ships in the same change as the reader, with no v1 file written
anywhere. load_snapshot() therefore rejects it with the ordinary schema
mismatch message rather than pretending to interpret it.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import hook_wiring

SNAPSHOT_SCHEMA = "dispatch-witness-old-wiring/v2"

# Every field a reader is allowed to rely on, and its admissible type. An
# entry missing any of them is malformed rather than partially usable.
# ``members_read`` is in the list because check-dispatch-witness.py names its
# contents in the refusal it prints for a qualified entry: a field a reader
# relies on and the validator does not check is a field that can arrive as
# anything.
_REQUIRED_FIELDS = ("status", "timeout", "scope_qualified", "members_read")

_STATUSES = frozenset({hook_wiring.WIRED, hook_wiring.ABSENT, hook_wiring.UNKNOWN})


def old_timeout(wiring: hook_wiring.Wiring) -> "int | None":
    """The limit a recorded call must outlive to prove the NEW wiring ran.

    The maximum across the hook's registrations, because beating the largest
    old limit beats all of them; a registration with no readable timeout
    poisons the answer to None, since an unknown limit cannot be beaten by
    argument.
    """
    if not wiring.registrations:
        return None
    timeouts = [registration.timeout for registration in wiring.registrations]
    if any(timeout is None for timeout in timeouts):
        return None
    return max(timeouts)


def entry_for(wiring: hook_wiring.Wiring) -> dict:
    """One hook's snapshot entry, from its probe result."""
    return {
        "status": wiring.status,
        "timeout": old_timeout(wiring) if wiring.wired else None,
        # Read off the probe's own record of how far it reached, so the field
        # cannot disagree with the members_read beside it.
        "scope_qualified": not wiring.project_scope_covered,
        "members_read": [str(member) for member in wiring.members_read],
    }


def write_snapshot(path: Path, wirings: "list[hook_wiring.Wiring]") -> dict:
    """Write the snapshot for ``wirings`` and return the document written.

    There is no seam for declaring the scope: a caller convinced it saw every
    settings member has no way to prove it to this module, and the one actor
    that does know is the probe. Widen ``hook_wiring.settings_chain`` if a
    member is being missed; do not assert past it here.
    """
    document = {
        "schema": SNAPSHOT_SCHEMA,
        "hooks": {wiring.basename: entry_for(wiring) for wiring in wirings},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return document


def _entry_error(basename: str, entry) -> str:
    if not isinstance(entry, dict):
        return f"snapshot entry for {basename!r} is not an object"
    missing = [field for field in _REQUIRED_FIELDS if field not in entry]
    if missing:
        return f"snapshot entry for {basename!r} is missing {', '.join(missing)}"
    if entry["status"] not in _STATUSES:
        return f"snapshot entry for {basename!r} has status {entry['status']!r}"
    timeout = entry["timeout"]
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
    ):
        return f"snapshot entry for {basename!r} has a non-numeric timeout"
    if not isinstance(entry["scope_qualified"], bool):
        return f"snapshot entry for {basename!r} has a non-boolean scope_qualified"
    members = entry["members_read"]
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        return (
            f"snapshot entry for {basename!r} has a members_read that is not a "
            f"list of paths"
        )
    return ""


def load_snapshot(path: Path) -> "tuple[dict | None, str]":
    """Return (hooks map, error). A missing, unreadable, malformed or
    wrong-schema snapshot yields (None, reason) — never an empty map, which a
    caller could mistake for "no hooks to check"."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"old-wiring snapshot unreadable: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"old-wiring snapshot is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "old-wiring snapshot is not a JSON object"
    if data.get("schema") != SNAPSHOT_SCHEMA:
        return None, (
            f"old-wiring snapshot schema is {data.get('schema')!r}, "
            f"expected {SNAPSHOT_SCHEMA!r}"
        )
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return None, "old-wiring snapshot has no 'hooks' object"
    for basename, entry in sorted(hooks.items()):
        error = _entry_error(basename, entry)
        if error:
            return None, error
    return hooks, ""
