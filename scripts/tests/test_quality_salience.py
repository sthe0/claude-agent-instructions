"""Fix #3 (quality-rating salience): the agent-proposed 1-5 quality rating that
must ride the resolution AskUserQuestion has to be named on BOTH read-at-
resolution surfaces, or the norm (which otherwise lives only in the
quality-regression-investigation leaf + the engine gate) is silently skipped.

Guards the two surfaces against silent regression:
  1. CLAUDE.md's § On task resolution region names the 1-5 quality rating.
  2. hook-resolution-reminder.py's parked-gate print names the quality rating
     and points at the quality-regression-investigation leaf.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
RESOLUTION_HOOK = REPO_ROOT / "scripts" / "hook-resolution-reminder.py"


def test_claude_md_resolution_region_names_quality_rating():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    # Anchor on the resolution section so the clause is guarded *in context*,
    # not merely present somewhere in the file.
    anchor = text.find("A substantive task is **resolved**")
    assert anchor != -1, "resolution section anchor missing from CLAUDE.md"
    region = text[anchor:]
    assert "1-5 quality rating" in region


def test_resolution_hook_print_names_quality_rating_and_leaf():
    src = RESOLUTION_HOOK.read_text(encoding="utf-8")
    assert "quality rating" in src
    assert "quality-regression-investigation" in src
