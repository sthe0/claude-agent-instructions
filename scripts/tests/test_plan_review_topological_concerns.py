"""Regression coverage for KNOWN DEFECT 2 in scripts/plan-review-topological.py:
`_classify()` over-eagerly claimed a REVIEW:-prefixed prose line inside the concerns
region as a second terminal marker, and `_parse_concerns()` could not tell an indented
markdown bullet elaborating an open concern from a column-0 bullet masquerading as a
new, unprefixed concern. Confirmed against three real pair transcripts (15-14, 24-23,
15-9) in the de572 r26 live run.

Minimal synthetic strings only -- no spawn/rig harness, no full transcripts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SHA = hashlib.sha256(b"concerns-test plan bytes\n").hexdigest()


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def drv():
    return _load("plan_review_topological_for_concerns", "plan-review-topological.py")


def test_review_prefixed_elaboration_folds_instead_of_reopening_the_marker(drv):
    """Mirrors pair 15-14: a REVIEW:-prefixed prose sentence inside the concerns
    region whose tail is neither pass/revise/empty must classify as "other" and
    fold onto the open concern, not re-open the terminal REVIEW:/Verdict: anchor."""
    elaboration = "REVIEW: revise this stage once the missing grant is added back"
    kind, value = drv._classify(elaboration)
    assert (kind, value) == ("other", "")

    stdout = (
        f"REVIEW:\nVerdict: revise\nPlan digest: {SHA}\n"
        f"C4: the stage omits a grant\n{elaboration}\nC1: other concern"
    )
    parsed = drv.parse_review_output(stdout)
    assert parsed.verdict == "revise"
    assert parsed.concerns == [
        f"C4: the stage omits a grant {elaboration}",
        "C1: other concern",
    ]


def test_indented_bullet_elaborating_an_open_concern_folds(drv):
    """Mirrors pairs 24-23 and 15-9: an indented markdown bullet under an already-open
    condition-prefixed concern is a continuation, not a new unprefixed concern."""
    stdout = (
        f"REVIEW:\nVerdict: revise\nPlan digest: {SHA}\n"
        "C2: the open concern\n"
        "  - elaboration bullet indented"
    )
    parsed = drv.parse_review_output(stdout)
    assert parsed.verdict == "revise"
    assert parsed.concerns == ["C2: the open concern - elaboration bullet indented"]


def test_column_zero_bullet_before_any_concern_still_refused(drv):
    """The abuse case the narrowed guard must keep catching: a column-0 bullet
    appearing before any condition-prefixed line, with concerns still empty, is
    refused as an unprefixed concern -- even though a condition-prefixed line
    follows later in the reply (so the earlier "no condition-prefixed concerns"
    check does not fire first and mask this one)."""
    stdout = (
        f"REVIEW:\nVerdict: revise\nPlan digest: {SHA}\n"
        "- a bullet before any concern\n"
        "C1: later concern"
    )
    with pytest.raises(drv.TopoRefused, match="unprefixed concern"):
        drv.parse_review_output(stdout)
