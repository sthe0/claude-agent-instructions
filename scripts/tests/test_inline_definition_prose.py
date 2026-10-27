"""The prose surfaces that restate the inline class agree with scripts/agentctl/inline_class.py."""
import re
from pathlib import Path

import pytest

from agentctl import inline_class as ic

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEAF = REPO_ROOT / "memory-global/leaves/inline-class.md"

CRITERION_SURFACES = (
    "CLAUDE.md",
    "cursor/rules/claude-code-sync.mdc",
    "memory-global/leaves/acting-without-asking.md",
    "skills/specializations/curator/SKILL.md",
    "memory-global/leaves/principles/coordinator-executes-through-specialists.md",
    "docs/concepts/task.md",
)
ROW_SURFACES = ("CLAUDE.md", "cursor/rules/claude-code-sync.mdc")
NOT_COVERED_NAMES = (
    "cohesion",
    "design decision",
    r"public contract",
    r"dependenc(?:y|ies)",
    "rework round",
    "failure analysis",
    "coordinator's estimate",
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _small_change_row(rel: str) -> str:
    rows = [line for line in _read(rel).splitlines() if line.startswith("| **Small change**")]
    assert len(rows) == 1, rel
    return rows[0]


def _fenced_blocks(text: str) -> "list[str]":
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, flags=re.S | re.M)


def test_leaf_quotes_the_definition_in_a_fenced_block():
    assert any(ic.INLINE_DEFINITION in block for block in _fenced_blocks(LEAF.read_text(encoding="utf-8")))


def test_leaf_not_covered_section_names_every_limit():
    text = LEAF.read_text(encoding="utf-8")
    section = re.search(r"^#+ Not covered\n(.*?)(?=^#+ |\Z)", text, flags=re.S | re.M)
    assert section, "no 'Not covered' section"
    body = section.group(1).lower()
    for name in NOT_COVERED_NAMES:
        assert re.search(name, body), name


@pytest.mark.parametrize("rel", ROW_SURFACES)
def test_small_change_row_has_every_criterion_and_links_the_leaf(rel):
    row = _small_change_row(rel)
    assert ic.missing_criteria(row) == ()
    assert "inline-class.md" in row


@pytest.mark.parametrize("rel", ROW_SURFACES)
def test_small_change_row_leaf_link_resolves(rel):
    targets = [t for t in re.findall(r"\]\(([^)]+)\)", _small_change_row(rel)) if t.endswith("inline-class.md")]
    assert targets, rel
    for target in targets:
        assert ((REPO_ROOT / rel).parent / target).resolve() == LEAF.resolve(), target


@pytest.mark.parametrize("rel", CRITERION_SURFACES)
def test_no_surface_states_single_file_as_the_criterion(rel):
    assert "single file" not in _read(rel)
