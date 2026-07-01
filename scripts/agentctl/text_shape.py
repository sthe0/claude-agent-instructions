"""Shared text-shape primitives: normalization + the placeholder anti-template set.

Extracted out of gates.py so a second consumer (plan.py's substantive-stage
validation) can apply the same anti-template check without gates.py and plan.py
importing each other. Pure string helpers only — no SessionState/PlanDoc types,
so this module has no opinion about which document shape uses it.
"""
from __future__ import annotations


def normalize_string(s: str) -> str:
    """Normalize a string for comparison: casefold, strip, and collapse internal whitespace."""
    return " ".join((s or "").casefold().split())


# Placeholder values a required free-text field must not use.
PLACEHOLDER_SET = frozenset({
    "todo", "tbd", "n/a", "na", "...", "expected", "actual", "mismatch", "-"
})
