"""The effort-divergence trigger's documentation contract.

*Difficulty removed:* an automatic transition the coordinator did not request is read as
an engine bug and worked around, rather than diagnosed, when the read-first surface does
not describe it. `verify-doc-concepts.py` checks that a registered concept's doc section
exists and that its code anchors resolve — it cannot check that the section says the
things a reader needs. These tests pin the content claims: the four scales, the window,
both fire sites, the honest limits, and — the one that rots silently — that every
config.md key the section cites is a key config.md actually defines.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_README = _REPO / "scripts" / "agentctl" / "README.md"
_CONFIG = _REPO / "config.md"
_BINDINGS = _REPO / "scripts" / "doc-bindings.json"

_SECTION_TITLE = "Effort divergence"


def _section() -> str:
    """The § Effort divergence body, from its own heading to the next `## ` heading."""
    text = _README.read_text(encoding="utf-8")
    start = text.index(f"\n## {_SECTION_TITLE}\n")
    rest = text[start + 1 :]
    end = rest.index("\n## ", 1)
    return rest[: end + 1]


def test_the_section_exists_and_is_registered_as_a_concept():
    """An unregistered section makes the `verify-doc-concepts` final_check vacuous for it."""
    assert f"\n## {_SECTION_TITLE}\n" in _README.read_text(encoding="utf-8")
    concepts = json.loads(_BINDINGS.read_text(encoding="utf-8"))["concepts"]
    bound = [c for c in concepts if c["doc"].get("section") == _SECTION_TITLE]
    assert len(bound) == 1, "exactly one concept must bind this section"
    assert bound[0]["doc"]["file"] == "scripts/agentctl/README.md"


@pytest.mark.parametrize("scale", ["spend", "wall_clock", "replans", "interactions"])
def test_the_section_names_every_scale(scale):
    assert f"`{scale}`" in _section()


def test_the_section_names_the_window_both_fire_sites_and_the_no_question_terminal():
    body = _section()
    assert "arming-relative window" in body.lower()
    assert "record-result" in body and "verify-final" in body
    # the load-bearing claim of the whole feature, per the order
    assert "never a question" in body.lower() or "asks the user nothing" in body.lower()


def test_the_section_carries_seven_honest_limits():
    """The count is pinned so a limit cannot be quietly dropped when the mechanism it
    documents is extended. Raising it is a deliberate act: the sixth arrived with the
    turn-driven watch, whose closure is scoped to a session that started the engine, and
    the seventh with the resolved-reentry count, whose `task_id` key is chosen by the
    actor it counts."""
    body = _section()
    limits = body[body.index("**Honest limits.**") :]
    numbered = re.findall(r"^\d+\. ", limits, flags=re.MULTILINE)
    assert len(numbered) == 7, f"expected 7 honest limits, found {len(numbered)}"


def test_every_config_key_the_section_cites_is_defined_in_config_md():
    """Values live in config.md; the README references them BY KEY. A cited key that
    config.md does not define is a dangling single-source-of-truth pointer."""
    cited = set(re.findall(r"`((?:effort|budget)-[a-z0-9-]+)`", _section()))
    assert cited, "the section must reference its constants by key"
    config = _CONFIG.read_text(encoding="utf-8")
    for key in sorted(cited):
        assert f"| `{key}` |" in config, f"{key} is cited by the README but not defined in config.md"


def test_the_section_quotes_no_threshold_value():
    """config.md is the single source of truth for the numbers. A value copied into the
    README is a second source that drifts silently the first time the row is retuned."""
    body = _section()
    for row, value in (("effort-divergence-multiple", "5"),
                       ("effort-replan-absolute", "3")):
        # the key may appear; `<key>` immediately followed by its value may not
        assert not re.search(rf"`{row}`[^.\n]*?(?:\bis\b|=)\s*`?{value}\b", body), row
