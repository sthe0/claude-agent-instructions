"""spawn-specialist.py validate_marker: a return marker is the label of the
specialist's message and must be detected on ANY line, not only the first
non-empty one — specialists routinely write a short summary before the marker,
and the old first-line-only rule false-BLOCKed otherwise-passing stages. The
^MARKER: anchor keeps ordinary prose from matching by accident."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_marker_on_first_line_ok():
    text, ok = MOD.validate_marker("COMPLETED: done\n\nsummary here")
    assert ok is True
    assert text == "COMPLETED: done\n\nsummary here"  # unchanged on success


def test_marker_after_prose_preamble_ok():
    # the recurring real-world shape: summary first, marker after — must pass
    body = "Here is what I did:\n- implemented X\n- ran tests\n\nCOMPLETED: X implemented, tests green"
    text, ok = MOD.validate_marker(body)
    assert ok is True
    assert text == body


def test_marker_on_last_line_ok():
    text, ok = MOD.validate_marker("preamble line\nPLAN-READY: ready\nPlan: /tmp/p.md")
    assert ok is True


@pytest.mark.parametrize("marker", list(MOD.RETURN_MARKERS))
def test_every_known_marker_detected_after_preamble(marker):
    _, ok = MOD.validate_marker(f"some preamble\n{marker}: detail")
    assert ok is True


def test_review_pass_on_last_line_ok():
    text, ok = MOD.validate_marker("checked the plan against the stages\nREVIEW: pass")
    assert ok is True
    assert text == "checked the plan against the stages\nREVIEW: pass"  # unchanged on success


def test_review_revise_after_long_preamble_ok():
    preamble = "\n".join(f"finding {i}: some detail" for i in range(20))
    body = f"{preamble}\nREVIEW: revise"
    text, ok = MOD.validate_marker(body)
    assert ok is True
    assert text == body


def test_no_marker_is_malformed():
    text, ok = MOD.validate_marker("just a summary, no marker at all")
    assert ok is False
    assert text.startswith("MALFORMED:")
    assert "no known return marker" in text
    assert text.endswith("just a summary, no marker at all")  # original text still forwarded


def test_marker_word_mid_sentence_not_matched():
    # ^MARKER: anchor: a marker word inside prose (not at line start) must NOT match
    text, ok = MOD.validate_marker("I considered whether to ESCALATE this but did not")
    assert ok is False
    assert text.startswith("MALFORMED:")
