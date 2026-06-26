"""CODEOWNERS fail-closed coverage of the protected Core (ADR-0001 § Governance).

Every protected-Core glob enumerated by the ADR must have a CODEOWNERS entry with an
owner. The list below is the single source the test reuses — a Core path added to the
ADR governance set without a CODEOWNERS entry fails here (fail-closed), which is the
whole point: Core must not silently become un-owned.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The protected-Core globs from ADR-0001 § "Governance over the core slice".
# One list, reused — not re-spelled ad hoc per assertion.
REQUIRED_CORE_GLOBS = [
    "/CLAUDE.md",
    "/config.md",
    "/skills/",
    "/agents/",
    "/cursor/",
    "*.mdc",
    "/scripts/agentctl/",
]


def _codeowners_path() -> Path:
    for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        p = REPO_ROOT / rel
        if p.is_file():
            return p
    raise AssertionError("no CODEOWNERS file in root, .github/, or docs/")


def _owned_entries() -> dict[str, list[str]]:
    """Map each CODEOWNERS glob -> its list of owners (entries with >=1 owner)."""
    entries: dict[str, list[str]] = {}
    for raw in _codeowners_path().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        glob, owners = tokens[0], [t for t in tokens[1:] if t.startswith("@")]
        if owners:
            entries[glob] = owners
    return entries


@pytest.mark.parametrize("glob", REQUIRED_CORE_GLOBS)
def test_protected_core_glob_is_owned(glob):
    entries = _owned_entries()
    assert glob in entries, f"protected-Core glob {glob!r} has no owned CODEOWNERS entry"
    assert entries[glob], f"protected-Core glob {glob!r} has an entry but no @owner"


def test_codeowners_file_exists():
    assert _codeowners_path().is_file()
