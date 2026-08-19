"""Shared text-shape primitives: normalization + the placeholder anti-template set.

Extracted out of gates.py so a second consumer (plan.py's substantive-stage
validation) can apply the same anti-template check without gates.py and plan.py
importing each other. Pure string helpers only — no SessionState/PlanDoc types,
so this module has no opinion about which document shape uses it.
"""
from __future__ import annotations

import unicodedata


def normalize_string(s: str) -> str:
    """Normalize a string for comparison: casefold, strip, and collapse internal whitespace."""
    return " ".join((s or "").casefold().split())


def normalize_for_match(s: str) -> str:
    """`normalize_string` plus: drop every Unicode format character (category Cf).

    For comparing bytes we REGISTERED against bytes a client RENDERED. A soft
    hyphen (U+00AD), a zero-width space/joiner (U+200B/U+200D), a BOM (U+FEFF)
    or a bidi mark can be introduced or dropped anywhere along that path; the
    difference is invisible to whoever authored the text, so no amount of care
    avoids it, and it carries no content of its own. Dropping such characters
    therefore cannot mask a genuinely missing word or line — a real omission
    still fails the comparison — while their presence on one side alone would
    otherwise fail a delivery that did happen.

    Separate from `normalize_string` rather than folded into it because that one
    also guards the plan-field placeholder checks (PLACEHOLDER_SET, the
    substantive-stage validation), where a field whose text is padded with
    invisible characters is something a validator should NOTICE, not silently
    look through.
    """
    return normalize_string(
        "".join(c for c in (s or "") if unicodedata.category(c) != "Cf")
    )


# Placeholder values a required free-text field must not use.
PLACEHOLDER_SET = frozenset({
    "todo", "tbd", "n/a", "na", "...", "expected", "actual", "mismatch", "-"
})

# The activity-ontology elements a stage may supply to a dependent stage (plan.py)
# or a raised question may target (premise.py). Lives here rather than in plan.py
# so premise.py can reuse the vocabulary without importing plan's TOML parsing /
# state machinery — the same reason PLACEHOLDER_SET lives here instead of gates.py.
#
# Every name here must be a place one stage can HAND ANOTHER, because that is what a
# supply edge is. Several are not stage FIELDS — `order` and `requirements` live on the
# plan's [meta.order], `control` is recorded on the session rather than authored in the
# plan, `procedure` is a means-side field — and that asymmetry is deliberate: a stage
# whose result IS the order, the control instrument or the procedure the next stage
# works under has a name for what it provides, while `stage_question_key` has no field
# of its own to cover for those names (see its docstring).
#
# The vocabulary stops here on purpose. It is the menu an author picks from when the
# submission seam makes them name an edge, and a validator can tell that a name is legal
# but never that it is the RIGHT one — so every name added without an edge that really
# hands that place over makes a plausible-but-wrong pick likelier at no gain. Add a name
# when a stage can be shown to supply it, not to complete a taxonomy.
#
# Weighed and declined, so a later reader knows they were considered: `customer` and
# `functional_place` — nothing hands either over as a place, and the parts of the order a
# stage CAN supply are already `order` / `requirements`; `coverage` — a property of a plan,
# not a place of a stage's activity. `functional_place` is the arguable one: a stage that
# problematizes and hands a design stage the place it must fill is a real shape in this
# tradition. It stays out until such an edge exists to name, per the rule above.
ELEMENT_NAMES = frozenset(
    {
        "material", "result", "invariants",         # subject cluster
        "knowledge",                                # knowledge cluster
        "means", "method", "procedure",             # means cluster
        "executor", "capability",                   # actor cluster
        "criterion", "done_criterion", "control",   # criterion cluster
        "order", "requirements",                    # order cluster
        "principle", "conditions", "preconditions",
    }
)

# The reserved WHOLE-STAGE entry of a per-stage `element name -> change-decision key` map
# (plan.stage_element_keys, read by premise.validate_questions). Empty rather than a word
# because a question target must name at least one character, so nothing an author can
# write ever collides with it.
WHOLE_STAGE_ELEMENT = ""
