"""Detect a permission-surface JSON document by shape, and diff two such
documents for widening.

Difficulty removed: an agent that hits a permissions-layer denial must never
clear it by editing its own `permissions.allow`/`deny` — but nothing enforced
that beyond prose. A hook that wants to catch a self-grant in the act needs
two primitives neither of which existed: (1) recognize that an arbitrary
JSON document IS a permissions surface at all, without maintaining a path
whitelist that a new settings file (a new profile, a new mount) would silently
fall outside of; (2) given a before/after pair of such documents, decide
whether the change WIDENS what the agent may do, as opposed to narrowing,
reordering, reformatting, deduping, or touching an unrelated key. This module
holds only that recognition + diff; a caller supplies its own policy over the
result (deny the edit, ask the user, log it).
"""
from __future__ import annotations

from typing import Any


def is_permission_surface(doc: Any) -> bool:
    """True iff `doc` is a JSON object carrying a `permissions` object with an
    `allow` or `deny` list.

    Recognition is by SHAPE ONLY -- there is no path/filename check anywhere
    in this module. This deliberately also means a document is recognized
    regardless of which file it came from, so a caller must not reintroduce a
    whitelist on top of this.

    The `permissions/*.json` format managed by `scripts/permissions-cli.py`
    (`{"permissions": [...]}`, a LIST) is a different, unrelated schema and is
    correctly NOT recognized: its `permissions` value is a list, not an
    object with `allow`/`deny`.
    """
    if not isinstance(doc, dict):
        return False
    perms = doc.get("permissions")
    if not isinstance(perms, dict):
        return False
    return isinstance(perms.get("allow"), list) or isinstance(perms.get("deny"), list)


def _string_list(value: Any) -> list[str]:
    """`value` as a list of its string elements if it is a list, else empty.

    A non-string element is dropped rather than raising -- this module makes
    no claim about a malformed entry, only about entries it can compare."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def widens(old_doc: Any, new_doc: Any) -> list[str] | None:
    """Entries ADDED to `permissions.allow` plus entries REMOVED from
    `permissions.deny`, going from `old_doc` to `new_doc` -- either of which
    widens what the agent may do. Order and duplicates in either document
    never affect the result; only set membership does.

    Three outcomes, not two:
      - `None`            -- UNKNOWN: `old_doc` is absent or unparseable (not
                              a JSON object), so no baseline exists to diff
                              against. Never silently coerced to "no widening".
      - `[]`               -- no widening: the change only narrows, reorders,
                              reformats, dedupes, or touches an unrelated key.
      - non-empty list     -- the widening entries themselves.

    A `new_doc` that is absent or unparseable is treated as introducing no
    surface at all, and is never UNKNOWN: only `old_doc`'s absence makes the
    comparison itself impossible. It is NOT, however, always `[]` -- an empty
    new surface removes every `permissions.deny` entry `old_doc` carried, so
    those entries are reported as widening. That over-reports rather than
    under-reports, which is the direction this module is required to fail in.

    A `old_doc` that IS a JSON object but simply has no `permissions` key (or
    a `permissions` object with no `allow`/`deny`) is a known, empty baseline
    -- not UNKNOWN -- so introducing permissions where none existed before is
    correctly reported as widening by the entries it adds.
    """
    if not isinstance(old_doc, dict):
        return None

    def field_of(doc: Any, key: str) -> list[str]:
        perms = doc.get("permissions") if isinstance(doc, dict) else None
        return _string_list(perms.get(key)) if isinstance(perms, dict) else []

    old_allow = set(field_of(old_doc, "allow"))
    old_deny = set(field_of(old_doc, "deny"))
    new_allow = field_of(new_doc, "allow")
    new_deny = field_of(new_doc, "deny")

    allow_added = list(dict.fromkeys(e for e in new_allow if e not in old_allow))
    deny_removed = list(dict.fromkeys(e for e in old_deny if e not in set(new_deny)))

    return allow_added + deny_removed
