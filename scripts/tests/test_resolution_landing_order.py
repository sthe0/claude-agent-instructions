"""Order contract on the instruction surfaces: land into trunk, then verify-final,
then the marked resolution AskUserQuestion carrying the 1-5 rating.

These tests decide presence/absence of phrases only; whether each surface MEANS
the order is an acceptance-review item, not something a phrase check can prove.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEAVES = "memory-global/leaves/"

CLAUDE = "CLAUDE.md"
LANDING = LEAVES + "landing-discipline.md"
QUALITY = LEAVES + "quality-regression-investigation.md"
CURSOR = "cursor/rules/claude-code-sync.mdc"
PITFALLS = LEAVES + "coordinator-pitfalls.md"
SCAN = LEAVES + "systemic-pattern-scan.md"
COORD_INDEX = LEAVES + "coordination/MEMORY.md"
REVIEW = LEAVES + "review-accompanies-code.md"

ANCHORS = [
    (CLAUDE, "before `verify-final`"),
    (LANDING, "before `verify-final`"),
    (QUALITY, "before `verify-final`"),
    (CURSOR, "before `verify-final`"),
    (CLAUDE, "[resolution-ask]"),
    (LANDING, "[resolution-ask]"),
    (QUALITY, "[resolution-ask]"),
    (CURSOR, "[resolution-ask]"),
    (LANDING, "landing_waiver"),
    (PITFALLS, "land before the resolution ask"),
    (SCAN, "landing has already happened by then"),
    (REVIEW, "before the resolution AskUserQuestion"),
    (COORD_INDEX, "before the resolution AskUserQuestion"),
]

FORBIDDEN = [
    (CLAUDE, "landing is **first and `(Recommended)`**"),
    (CLAUDE, "(push, scope)"),
    (CLAUDE, "Push, then land into trunk/main"),
    (QUALITY, "inside the SAME resolution"),
    (LANDING, "Bundle the delivering step into the resolution"),
    (CURSOR, "bundle the delivering option into that same ask"),
    (SCAN, 'and "push?"'),
    (COORD_INDEX, "the AskUserQuestion bundling rule"),
    (REVIEW, "trunk/main at the resolution gate"),
]


def _texts(rel: str) -> tuple[str, str]:
    raw = (REPO_ROOT / rel).read_text(encoding="utf-8")
    return raw, " ".join(raw.split())


@pytest.mark.parametrize("rel,phrase", ANCHORS)
def test_anchor_phrase_present(rel, phrase):
    raw, normalized = _texts(rel)
    assert phrase in raw or phrase in normalized, f"{rel} lacks {phrase!r}"


@pytest.mark.parametrize("rel,phrase", FORBIDDEN)
def test_forbidden_phrase_absent(rel, phrase):
    raw, normalized = _texts(rel)
    assert phrase not in raw and phrase not in normalized, f"{rel} still has {phrase!r}"


def test_claude_md_resolution_region_keeps_rating_phrase():
    raw, _ = _texts(CLAUDE)
    region = raw[raw.index("A substantive task is **resolved**"):]
    assert "1-5 quality rating" in region


def test_pitfalls_corrective_action_says_land_before_resolution_ask():
    _, normalized = _texts(PITFALLS)
    assert "land before the resolution ask" in normalized
