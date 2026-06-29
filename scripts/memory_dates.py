#!/usr/bin/env python3
"""Shared validation for the memory-leaf temporal frontmatter fields.

`created` / `last_verified` / `last_accessed` — see
memory-global/leaves/memory-temporal-frontmatter.md for the contract (field
semantics, the ISO `YYYY-MM-DD` format, who writes each). One helper so the three
verifiers (verify-memory-index, verify-experience-leaf, verify-leaf-structure)
and the PreToolUse reminder hook validate the dates identically instead of
drifting their own copies.

Importable as ``memory_dates`` from any scripts/ context: every invocation path
(verify-all run from scripts/, a standalone verifier, a hook, pytest with the
conftest path insert) puts scripts/ on sys.path, and the module name carries no
hyphen so a plain ``import`` resolves it.
"""
from __future__ import annotations

import datetime as _dt
import re

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def top_level_value(fm_body: str, key: str) -> str | None:
    """Return a top-level (column-0) frontmatter value, or None if absent.

    The temporal fields are always top-level, never nested under `metadata:` —
    this mirrors verify-memory-index's top-level discipline so a nested
    `created:` is never mistaken for the real one.
    """
    for line in fm_body.splitlines():
        if line[:1] in (" ", "\t"):
            continue
        m = re.match(rf"{re.escape(key)}\s*:\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip().strip("\"'")
    return None


def parse_iso(value: str) -> _dt.date | None:
    """Parse a strict ISO `YYYY-MM-DD` date, or None if malformed."""
    if not value or not ISO_DATE_RE.match(value):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def validate_temporal(fm_body: str, *, require: bool) -> list[str]:
    """Return human-readable issues with the temporal fields in `fm_body`.

    require=True  — `created` and `last_verified` must be present (the universal
                    index/enforcement check).
    require=False — only validate the fields that ARE present (the mirror check
                    used by the schema-specific verifiers, which must not reject
                    a leaf merely for not yet carrying the dates).

    `last_accessed` is always optional and only format-checked when present.
    Whenever both `created` and `last_verified` parse, `last_verified >= created`
    is enforced.
    """
    issues: list[str] = []
    created_raw = top_level_value(fm_body, "created")
    verified_raw = top_level_value(fm_body, "last_verified")
    accessed_raw = top_level_value(fm_body, "last_accessed")

    created = verified = None

    if created_raw is None:
        if require:
            issues.append("missing top-level `created:` date (ISO YYYY-MM-DD)")
    else:
        created = parse_iso(created_raw)
        if created is None:
            issues.append(f"`created: {created_raw}` is not a valid ISO YYYY-MM-DD date")

    if verified_raw is None:
        if require:
            issues.append("missing top-level `last_verified:` date (ISO YYYY-MM-DD)")
    else:
        verified = parse_iso(verified_raw)
        if verified is None:
            issues.append(f"`last_verified: {verified_raw}` is not a valid ISO YYYY-MM-DD date")

    if accessed_raw is not None and parse_iso(accessed_raw) is None:
        issues.append(f"`last_accessed: {accessed_raw}` is not a valid ISO YYYY-MM-DD date")

    if created is not None and verified is not None and verified < created:
        issues.append(
            f"`last_verified` ({verified_raw}) is before `created` ({created_raw})"
        )

    return issues
