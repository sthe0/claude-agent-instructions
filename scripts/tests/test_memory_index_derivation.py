"""Verify that the memory-index-max-bytes derivation has been moved to a leaf.

These tests are RED on the original config.md (row is ~3570 chars) and GREEN
after the row is shortened and the derivation leaf is created.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent


def test_memory_index_row_is_short():
    """The memory-index-max-bytes row must be short: derivation belongs in the leaf."""
    config = _REPO / "config.md"
    text = config.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "| `memory-index-max-bytes`" in line:
            assert len(line) < 800, (
                f"memory-index-max-bytes row is {len(line)} chars; "
                "the derivation should be in the leaf, not in config.md"
            )
            return
    raise AssertionError("memory-index-max-bytes row not found in config.md")


def test_derivation_leaf_exists():
    """The derivation leaf must exist after the row is shrunk."""
    leaf = _REPO / "memory-global" / "leaves" / "memory-index-max-bytes-derivation.md"
    assert leaf.exists(), f"Derivation leaf not found: {leaf}"


def test_memory_md_points_to_derivation_leaf():
    """memory-global/MEMORY.md must have a pointer to the derivation leaf."""
    memory_md = _REPO / "memory-global" / "MEMORY.md"
    text = memory_md.read_text(encoding="utf-8")
    assert "memory-index-max-bytes-derivation.md" in text, (
        "memory-global/MEMORY.md has no pointer to the derivation leaf"
    )


def test_row_still_names_ceiling_unit_and_harness_constant():
    """The shortened row must still name the ceiling, the unit, and the harness constant."""
    config = _REPO / "config.md"
    text = config.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "| `memory-index-max-bytes`" in line:
            assert "24000" in line, "ceiling value 24000 missing from row"
            assert "bytes" in line, "unit 'bytes' missing from row"
            assert "25000" in line, "harness constant 25000 missing from row"
            assert "memory-index-max-bytes-derivation.md" in line, (
                "pointer to derivation leaf missing from row"
            )
            return
    raise AssertionError("memory-index-max-bytes row not found in config.md")
