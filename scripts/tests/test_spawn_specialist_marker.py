"""The return-marker contract, at its historical home.

These eight names were migrated verbatim from the pre-refactor file of the same
name — the marker word is the message's LABEL, detected on ANY line, never a word
mid-sentence. The bodies now call the SHARED validator
(scripts/lib/planner_plan_check.py::validate_marker) by ordinary import, so the one
implementation both spawn wrappers bind is the one under test here. Keeping the file
and the eight names is deliberate: a rename would force a visible edit to this
stage's pinned control rather than a quiet loss of coverage.

The two identity tests pin BOTH wrappers to the shared check_planner_return /
validate_marker objects, so the contract cannot drift back into per-wrapper copies.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from lib import planner_plan_check as MOD

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


# --- return-marker contract (8 migrated tests, names preserved) --------------

def test_marker_on_first_line_ok():
    text, ok = MOD.validate_marker("COMPLETED: done\n\nsummary here")
    assert ok is True
    assert text == "COMPLETED: done\n\nsummary here"  # unchanged on success


def test_marker_after_prose_preamble_ok():
    body = "Here is what I did:\n- implemented X\n- ran tests\n\nCOMPLETED: X implemented, tests green"
    text, ok = MOD.validate_marker(body)
    assert ok is True
    assert text == body


def test_marker_on_last_line_ok():
    text, ok = MOD.validate_marker("preamble line\nPLAN-READY: ready\nPlan: /tmp/p.toml")
    assert ok is True


@pytest.mark.parametrize("marker", list(MOD.RETURN_MARKERS))
def test_every_known_marker_detected_after_preamble(marker):
    _, ok = MOD.validate_marker(f"some preamble\n{marker}: detail")
    assert ok is True


def test_review_pass_on_last_line_ok():
    text, ok = MOD.validate_marker("checked the plan against the stages\nREVIEW: pass")
    assert ok is True
    assert text == "checked the plan against the stages\nREVIEW: pass"


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
    text, ok = MOD.validate_marker("I considered whether to ESCALATE this but did not")
    assert ok is False
    assert text.startswith("MALFORMED:")


# --- identity: both wrappers bind the SHARED functions -----------------------

def _load_wrapper(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "").replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("wrapper", ["spawn-specialist.py", "spawn-cursor-specialist.py"])
def test_wrapper_binds_the_shared_check_planner_return(wrapper):
    mod = _load_wrapper(wrapper)
    assert mod.check_planner_return is MOD.check_planner_return


@pytest.mark.parametrize("wrapper", ["spawn-specialist.py", "spawn-cursor-specialist.py"])
def test_wrapper_binds_the_shared_validate_marker(wrapper):
    mod = _load_wrapper(wrapper)
    assert mod.validate_marker is MOD.validate_marker
