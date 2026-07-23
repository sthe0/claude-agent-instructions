"""A correct return marker wrapped in markdown emphasis / a code span must parse
identically to the bare form — at the wrapper site (lib.extract_marker), the
engine-routing site (agentctl.dispatch.parse_marker), the planner ``Plan:`` line,
and the overcome-difficulty escape site (spawn-cursor-escape.validate_marker).

Difficulty covered: a specialist whose work SUCCEEDED but who rendered its marker as
``**COMPLETED:**`` / `` `COMPLETED:` `` / ``__COMPLETED:__`` was rejected as MALFORMED
on formatting alone, parking the stage and forcing a manual recovery. There was zero
test coverage for wrapped markers at any site before this file.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentctl.dispatch import parse_marker
from agentctl import dispatch
from lib.planner_plan_check import extract_marker, validate_planner_plan

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _wrapped_forms(marker: str) -> list[str]:
    """The three emphasis forms actually observed in the incident (bold, code span,
    underscore), each carrying the same body ``done``."""
    return [f"**{marker}:** done", f"`{marker}: done`", f"__{marker}:__ done"]


# --- wrapper site + engine-routing site agree, for every marker & form -------

@pytest.mark.parametrize("marker", list(dispatch.RETURN_MARKERS))
def test_bare_and_wrapped_yield_same_marker_at_both_sites(marker):
    bare = f"{marker}: done"
    assert extract_marker(bare) == marker
    assert parse_marker(bare) == (marker, "done")
    for wrapped in _wrapped_forms(marker):
        assert extract_marker(wrapped) == marker, wrapped
        # body is cleaned of the trailing emphasis run when the line was wrapped:
        # "done", not "** done" / "done`" / "__ done".
        assert parse_marker(wrapped) == (marker, "done"), wrapped


@pytest.mark.parametrize("marker", list(dispatch.RETURN_MARKERS))
def test_wrapped_marker_found_after_a_preamble(marker):
    # the any-line contract still holds when the marker is wrapped
    text = f"here is my summary\nmore notes\n**{marker}:** done"
    assert extract_marker(text) == marker
    assert parse_marker(text) == (marker, "done")


# --- unwrapped body is byte-identical (no behaviour change on the good path) --

def test_unwrapped_body_ending_in_emphasis_is_left_intact():
    # an UNWRAPPED marker whose body legitimately ends in '*' keeps that char —
    # the trailing-trim only fires when the marker line was wrapped.
    assert parse_marker("COMPLETED: see foo*") == ("COMPLETED", "see foo*")
    assert parse_marker("REPLAN: widen _scope_") == ("REPLAN", "widen _scope_")


# --- planner Plan: label tolerates the same emphasis --------------------------

def _valid_plan_toml(tmp_path) -> str:
    body = '''
[meta]
task_id = "check-plan"
goal = "g"
done_criterion = "d"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "n/a"

[[stage]]
index = 1
title = "Only stage"
executor = "in_thread"
expected_result_image = "r"
criterion_type = "measurable"
done_criterion = "d"
verify_command = "true"
material = "m"
means = "e"
method = "meth"
conditions = "c"
invariants = "inv"
capability_required = "cap"
[stage.principle]
statement = "s"
source = "src"
derivation = "der follows from src"
confidence = "high"
refutation = "ref"
'''
    p = tmp_path / "plan.toml"
    p.write_text(body)
    return str(p)


def test_bolded_plan_ready_and_bolded_plan_label_resolve_like_bare(tmp_path):
    plan = _valid_plan_toml(tmp_path)
    bare_text, bare_ok = validate_planner_plan(f"PLAN-READY: ready\nPlan: {plan}")
    assert bare_ok is True
    # both the marker AND the Plan: label bolded, and the path itself wrapped
    wrapped_text, wrapped_ok = validate_planner_plan(
        f"**PLAN-READY:** ready\n**Plan:** **{plan}**"
    )
    assert wrapped_ok is True  # same plan path resolved, same acceptance as bare
    # backticked forms too
    _, code_ok = validate_planner_plan(f"`PLAN-READY:` ready\n`Plan: {plan}`")
    assert code_ok is True


# --- escape site (distinct vocabulary, first-non-blank-line-only) -------------

def _load_escape_module():
    path = SCRIPTS_DIR / "spawn-cursor-escape.py"
    spec = importlib.util.spec_from_file_location("spawn_cursor_escape", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("marker", ["RESOLVED", "INVESTIGATION", "LOOP_DETECTED"])
def test_escape_site_accepts_wrapped_markers(marker):
    escape = _load_escape_module()
    for wrapped in (f"**{marker}:** x", f"`{marker}: x`", f"__{marker}:__ x"):
        _, ok = escape.validate_marker(wrapped)
        assert ok is True, wrapped
    # and the bare form still passes
    _, ok = escape.validate_marker(f"{marker}: x")
    assert ok is True


# --- negatives: the ^MARKER: anchor still holds ------------------------------

def test_marker_word_mid_sentence_still_not_matched():
    assert extract_marker("I considered whether to ESCALATE this but did not") is None
    assert parse_marker("I considered whether to ESCALATE this but did not") == (None, "")


def test_emphasis_before_prose_marker_does_not_match():
    # stripping the leading '*' run leaves "note* ESCALATE: x", which does not match
    assert extract_marker("*note* ESCALATE: x") is None
    assert parse_marker("*note* ESCALATE: x") == (None, "")


def test_line_of_only_emphasis_chars_does_not_match():
    assert extract_marker("***\n___\n```") is None
    assert parse_marker("***") == (None, "")
