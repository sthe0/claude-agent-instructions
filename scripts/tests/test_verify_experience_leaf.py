"""Tests for verify-experience-leaf.py — unreplaced-TODO check and side-difficulty gate.

Verifies that:
  - A standalone difficulty/v1 leaf with a TODO in ## Cost is rejected.
  - A standalone leaf with a real Cost figure passes.
  - A ticket leaf (sections relaxed) passes even if Cost section is absent.
  - Legacy leaves (no schema) are exempt from the check.
  - side_difficulty_issue() detects inlined sub-difficulties without [[slug]] links.
  - side_difficulty_issue() is NOT part of check_content (per-mode severity).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_experience_leaf",
    Path(__file__).resolve().parents[1] / "verify-experience-leaf.py",
)
vel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vel)


_CONFIRMED = 'resolution_confirmed_by_user: "looks good, ship it"'

_STANDALONE_OK = f"""---
schema: difficulty/v1
{_CONFIRMED}
---

# Some difficulty

## Difficulty
A divergence.

## Order & criterion
The order.

## Contexts
### 2026-01-01 — first context
- Where: here
- Working plan: did this

## Cost
$1.23 on claude -p spawns, 5 min wall-clock, 0 user interventions (agentctl resolve surfaced the figure).
"""

_STANDALONE_TODO = f"""---
schema: difficulty/v1
{_CONFIRMED}
---

# Some difficulty

## Difficulty
A divergence.

## Order & criterion
The order.

## Contexts
### 2026-01-01 — first context
- Where: here
- Working plan: did this

## Cost
TODO — fill from the figure surfaced by `agentctl resolve` (see also scripts/cost-report.py)
"""

_STANDALONE_TODO_OLD = f"""---
schema: difficulty/v1
{_CONFIRMED}
---

# Some difficulty

## Difficulty
A divergence.

## Order & criterion
The order.

## Contexts
### 2026-01-01 — first context
- Where: here
- Working plan: did this

## Cost
TODO — fill via cost-report.py / tool-usage-report.py
"""

_TICKET_LEAF = f"""---
schema: difficulty/v1
{_CONFIRMED}
ticket: PROJ-42
---

# Some difficulty

Full structured record — in the ticket: PROJ-42.
"""

_LEGACY_LEAF = f"""---
{_CONFIRMED}
---

# Old leaf with no schema field

Some content.
"""


def test_standalone_real_cost_passes():
    assert vel.check_content(_STANDALONE_OK) is None


def test_standalone_todo_cost_new_message_rejected():
    err = vel.check_content(_STANDALONE_TODO)
    assert err is not None
    assert "TODO" in err
    assert "## Cost" in err
    assert "agentctl resolve" in err


def test_standalone_todo_cost_old_message_rejected():
    err = vel.check_content(_STANDALONE_TODO_OLD)
    assert err is not None
    assert "TODO" in err
    assert "## Cost" in err


def test_ticket_leaf_passes():
    assert vel.check_content(_TICKET_LEAF) is None


def test_legacy_leaf_no_schema_passes():
    assert vel.check_content(_LEGACY_LEAF) is None


def test_section_body_extracts_between_headings():
    body = "## Cost\nsome cost info\n\n## Self-critique\nfoo\n"
    import re
    rx = re.compile(r"^##\s+Cost\b", re.MULTILINE)
    result = vel._section_body(body, rx)
    assert result is not None
    assert "some cost info" in result
    assert "Self-critique" not in result


def test_section_body_at_end_of_file():
    body = "## Cost\nfinal section\n"
    import re
    rx = re.compile(r"^##\s+Cost\b", re.MULTILINE)
    result = vel._section_body(body, rx)
    assert result is not None
    assert "final section" in result


def test_section_body_returns_none_when_heading_absent():
    body = "## Difficulty\nsome text\n"
    import re
    rx = re.compile(r"^##\s+Cost\b", re.MULTILINE)
    assert vel._section_body(body, rx) is None


# ---------------------------------------------------------------------------
# side_difficulty_issue — tests for the sub-difficulty-inline gate
# ---------------------------------------------------------------------------

_SIDE_DIFFICULTY_NO_LINK = f"""---
schema: difficulty/v1
{_CONFIRMED}
---

# Difficulty with inlined side-difficulty

## Difficulty
Main divergence.

## Order & criterion
The order.

## Contexts
### 2026-01-01 — context
- Where: here
- Working plan: did this
Side-difficulties met: (1) something bad happened; (2) another issue arose.

## Cost
$1.00 on spawns. The overhead from side-difficulties: (1) bad thing; (2) other thing.
"""

_SIDE_DIFFICULTY_WITH_LINK = f"""---
schema: difficulty/v1
{_CONFIRMED}
---

# Difficulty with linked side-difficulty

## Difficulty
Main divergence.

## Order & criterion
The order.

## Contexts
### 2026-01-01 — context
- Where: here
- Working plan: did this
Side-difficulties met: see [[some-other-difficulty]].

## Cost
$1.00 on spawns.
"""


def test_side_difficulty_no_link_detected():
    issue = vel.side_difficulty_issue(_SIDE_DIFFICULTY_NO_LINK)
    assert issue is not None
    assert "[[slug]]" in issue


def test_side_difficulty_with_link_passes():
    assert vel.side_difficulty_issue(_SIDE_DIFFICULTY_WITH_LINK) is None


def test_side_difficulty_ticket_exempt():
    content = f"""---
schema: difficulty/v1
{_CONFIRMED}
ticket: PROJ-42
---

# Difficulty

Full record in ticket PROJ-42. Side-difficulties met: (1) foo happened.
"""
    assert vel.side_difficulty_issue(content) is None


def test_side_difficulty_legacy_exempt():
    content = f"""---
{_CONFIRMED}
---

Side-difficulties met: (1) foo happened.
"""
    assert vel.side_difficulty_issue(content) is None


def test_side_difficulty_no_marker_passes():
    assert vel.side_difficulty_issue(_STANDALONE_OK) is None


def test_side_difficulty_marker_variants_detected():
    # The marker must catch the natural English phrasings the docs use
    # ("a child difficulty", "side/child difficulties") and the Cyrillic forms,
    # not only "side-difficulty".
    for phrase in (
        "A child difficulty arose: foo broke.",
        "Sub-difficulty met: bar regressed.",
        "Встретилось подзатруднение: baz.",
        "Возникло дочернее затруднение здесь.",
    ):
        content = _SIDE_DIFFICULTY_NO_LINK.replace(
            "Side-difficulties met: (1) something bad happened; (2) another issue arose.",
            phrase,
        )
        assert vel.side_difficulty_issue(content) is not None, phrase


def test_side_difficulty_not_in_check_content():
    # side_difficulty_issue is separate from check_content — check_content must
    # still pass for a leaf that side_difficulty_issue would flag, so per-mode
    # severity is controlled at each call site.
    assert vel.check_content(_SIDE_DIFFICULTY_NO_LINK) is None


def test_cmd_file_side_difficulty_exits_1(tmp_path):
    exp_dir = tmp_path / "experience"
    exp_dir.mkdir()
    leaf = exp_dir / "2026-01-01-test.md"
    leaf.write_text(_SIDE_DIFFICULTY_NO_LINK)
    assert vel.cmd_file(str(leaf)) == 1


def test_cmd_file_linked_side_difficulty_exits_0(tmp_path):
    exp_dir = tmp_path / "experience"
    exp_dir.mkdir()
    leaf = exp_dir / "2026-01-01-test.md"
    leaf.write_text(_SIDE_DIFFICULTY_WITH_LINK)
    assert vel.cmd_file(str(leaf)) == 0
