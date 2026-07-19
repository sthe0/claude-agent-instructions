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

# The activity-ontology elements a stage may supply to a dependent stage (plan.py)
# or a raised question may target (premise.py). Lives here rather than in plan.py
# so premise.py can reuse the vocabulary without importing plan's TOML parsing /
# state machinery — the same reason PLACEHOLDER_SET lives here instead of gates.py.
ELEMENT_NAMES = frozenset(
    {
        "material", "result", "invariants",   # subject cluster
        "means", "method",                    # means cluster
        "executor", "capability",             # actor cluster
        "criterion", "done_criterion",        # criterion cluster
        "principle", "conditions",
    }
)
