"""Stage 4 of the landed-check plan: mechanically enumerate every in-tree TOML
fixture command and every README fenced code block, and assert zero unconverted
free-shell landed-ness checks remain (the shapes behind experience leaf
2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md: SHA
equality, a live-resolved `rev-parse`, a `merge-base --is-ancestor` shelled out
by the plan author instead of declared as `kind = "landed"`).

The one mechanical exemption is a fenced code block whose first line is the
literal marker `# BEFORE (do not copy)` — the migration recipe's own
counter-example, which must keep showing the old shape to document what NOT to
write anymore.

Scope, stated honestly: this enumerates scripts/tests/fixtures/*.toml (parsed
structurally via tomllib, not regex — every `[[final_check]].command` and every
`stage.verify_command`) and scripts/agentctl/README.md's fenced code blocks
only. It does NOT cover machine-local plans under ~/.claude-agent/plans/
(converted opportunistically, not tracked) or plans already registered in a
live session (frozen by design — a correction there is a replan, not an
in-place rewrite).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = SCRIPTS_DIR / "tests" / "fixtures"
README_PATH = SCRIPTS_DIR / "agentctl" / "README.md"

_BEFORE_MARKER = "# BEFORE (do not copy)"

# Deliberately high-recall: over-inclusive is fine, since every hit is either
# converted or must carry the explicit BEFORE marker.
_LANDED_IDIOMS = (
    re.compile(r"merge-base\s+--is-ancestor"),
    re.compile(r"rev-parse\b.*(==|=)\s*\S"),
    re.compile(r"rev-list\b[^\n]*\b[\w.-]+/[\w.-]+"),
)


def _looks_landed(text: str) -> bool:
    return any(p.search(text) for p in _LANDED_IDIOMS)


def _toml_commands():
    """Yield (label, command_text) for every final_check.command and every
    stage.verify_command across every *.toml fixture — structurally parsed,
    not grepped, so a command hiding in an unusual TOML layout is still found."""
    for path in sorted(FIXTURES_DIR.glob("*.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for i, fc in enumerate(data.get("final_check", []), 1):
            cmd = fc.get("command")
            if cmd:
                yield f"{path.name}:final_check[{i}]", cmd
        for s in data.get("stage", []):
            cmd = s.get("verify_command")
            if cmd:
                yield f"{path.name}:stage[{s.get('index')}]", cmd


def _readme_fenced_blocks():
    """Yield (label, block_text) for every fenced code block in the README,
    skipping only the block whose first line is the literal BEFORE marker."""
    text = README_PATH.read_text()
    for i, m in enumerate(re.finditer(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.DOTALL), 1):
        block = m.group(1)
        first_line = block.splitlines()[0].strip() if block.splitlines() else ""
        if first_line == _BEFORE_MARKER:
            continue
        yield f"README fenced block #{i}", block


def test_no_unconverted_free_shell_landed_checks_in_fixtures_and_readme():
    offenders = []
    for label, cmd in _toml_commands():
        if _looks_landed(cmd):
            offenders.append(label)
    for label, block in _readme_fenced_blocks():
        if _looks_landed(block):
            offenders.append(label)
    assert offenders == [], (
        f"unconverted free-shell landed-ness check(s) found: {offenders} "
        "(declare `kind = \"landed\"` instead of shelling out merge-base/rev-parse/rev-list, "
        "or mark a documented counter-example with the literal first line "
        f"{_BEFORE_MARKER!r})"
    )


def test_landed_example_fixture_declares_kind_landed(fixtures_dir):
    # Guards against the negative-end-state assertion above passing vacuously
    # (e.g. if every landed-idiom command were simply deleted rather than
    # migrated) by requiring at least one real `kind = "landed"` declaration
    # in the enumerated domain.
    with open(fixtures_dir / "plan_landed_example.toml", "rb") as f:
        data = tomllib.load(f)
    kinds = [s.get("verify_kind") for s in data.get("stage", [])]
    kinds += [fc.get("kind") for fc in data.get("final_check", [])]
    assert "landed" in kinds, "expected fixture to declare kind = \"landed\" at least once"
