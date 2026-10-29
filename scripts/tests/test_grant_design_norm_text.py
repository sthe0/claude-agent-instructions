"""The grant-design norm carries the read-the-code principle and the exact-push-rule design.

Each claim is asserted inside the section it belongs to, so a sentence moved elsewhere does
not pass, and each is also asserted ABSENT from the same section at the fork point of this
work (BASE), so a claim that held before the change cannot satisfy its own check."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASE = "b13652ad2433ab59732775087146de65d827299f"

REGEX_LEAF = "memory-global/leaves/regex-not-for-semantic-classification.md"
ACTING = "memory-global/leaves/acting-without-asking.md"
PLANNER = "skills/specializations/planner/policy.md"
README = "scripts/agentctl/README.md"

# (file, section start, section end pattern, claims)
CLAIMS = [
    (REGEX_LEAF, "### A third case: SCOPE/EFFECT", r"^#{1,3} ",
     ["a grant checker reads the code", "hook-resource-grant-check.py", "code_effects.effects_of"]),
    (ACTING, "- **(a′)", r"^- \*\*\(",
     ["regex-not-for-semantic-classification.md", "resource-grant hook"]),
    (PLANNER, "## Planning-time grant research", r"^#{1,2} ",
     ["hook-resource-grant-check.py", "runtime counterpart"]),
    (README, "### `[stage.grants]`", r"^#{1,3} ",
     ["push_allow_rules", "_push_config_blocker", "Bash(git push <remote> <branch>)"]),
    (README, "### Resource-based permissions", r"^#{1,3} ",
     ["hook-resource-grant-check.py", "fail-closed judge"]),
    (README, "### Durable evidence and checkpoint commits", r"^#{1,3} ",
     ["push_checkpoint_text", "says commit only"]),
]


def _section(text: str, start: str, end: str) -> "str | None":
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(start):
            body = [line]
            for later in lines[i + 1:]:
                if re.match(end, later):
                    break
                body.append(later)
            return "\n".join(body)
    return None


def _base_text(relpath: str) -> "str | None":
    proc = subprocess.run(["git", "-C", str(REPO), "show", f"{BASE}:{relpath}"],
                          capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def _base_available() -> bool:
    return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{BASE}^{{commit}}"],
                          capture_output=True, check=False).returncode == 0


CASES = [(path, start, end, claim) for path, start, end, claims in CLAIMS for claim in claims]
IDS = [f"{Path(path).stem}:{claim}" for path, _s, _e, claim in CASES]


@pytest.mark.parametrize("path, start, end, claim", CASES, ids=IDS)
def test_the_claim_is_in_its_section(path, start, end, claim):
    section = _section((REPO / path).read_text(), start, end)
    assert section is not None, f"section {start!r} not found in {path}"
    assert claim in section, f"{claim!r} missing from section {start!r} of {path}"


@pytest.mark.parametrize("path, start, end, claim", CASES, ids=IDS)
def test_the_claim_was_not_there_at_the_fork_point(path, start, end, claim):
    if not _base_available():
        pytest.skip(f"fork-point commit {BASE[:8]} is not in this clone")
    text = _base_text(path)
    assert text is not None, f"{path} missing at {BASE[:8]}"
    section = _section(text, start, end)
    assert section is not None, f"section {start!r} not found in {path} at {BASE[:8]}"
    assert claim not in section, f"{claim!r} already held at {BASE[:8]}: the check proves nothing"
