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

from lib import host_llm, marker_extract
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


# --- decoration stripping (issue #79) and multi-marker resolution ------------

@pytest.mark.parametrize(
    "decorated",
    [
        "**COMPLETED:** shipped it",
        "__COMPLETED:__ shipped it",
        "`COMPLETED:` shipped it",
        "## COMPLETED: shipped it",
        "> COMPLETED: shipped it",
        "- COMPLETED: shipped it",
    ],
    ids=["bold", "underscore", "backtick", "heading", "blockquote", "bullet"],
)
def test_validate_marker_accepts_each_decoration_shape(decorated):
    text, ok = MOD.validate_marker(decorated)
    assert ok is True
    assert text == decorated  # unchanged on success


def test_validate_marker_accepts_agreeing_terminal_markers():
    text, ok = MOD.validate_marker("COMPLETED: draft\nmore\nCOMPLETED: final")
    assert ok is True


def test_validate_marker_terminal_marker_wins_over_an_emphasised_verdict_headline():
    # Corpus-dominant shape: a code-reviewer's emphasis-wrapped verdict headline
    # precedes its true terminal marker. The headline must not beat the terminal
    # line, and the message still passes.
    text = "**REVIEW: revise**\nprose about the diff\nCOMPLETED: reviewed the stage diff"
    assert MOD.extract_marker(text) == "COMPLETED"
    result, ok = MOD.validate_marker(text)
    assert ok is True
    assert result == text  # unchanged on success


def test_validate_marker_accepts_a_later_decoy_marker_line_as_terminal():
    # The accepted cost of last-hit resolution: a correct marker followed later
    # by a decoy marker line resolves to that LATER line rather than MALFORMED —
    # this scan refuses to discard the output, not to pick between two readings.
    text = (
        "COMPLETED: shipped it, tests pass.\n\n"
        "(Had the tests failed I would have returned\n"
        "REPLAN: revise the approach\n"
        "but they passed.)"
    )
    assert MOD.extract_marker(text) == "REPLAN"
    result, ok = MOD.validate_marker(text)
    assert ok is True
    assert result == text


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


@pytest.mark.parametrize("wrapper", ["spawn-specialist.py", "spawn-cursor-specialist.py"])
def test_wrapper_binds_the_shared_marker_extract_module(wrapper):
    # Both wrappers import the SAME marker_extract module object. Claude-side
    # spawn-specialist invokes it; Cursor wrappers keep the import for typing /
    # telemetry but _build_extraction returns None (hard gate: no claude -p).
    mod = _load_wrapper(wrapper)
    assert mod.marker_extract is marker_extract


# --- call-site guard: Claude wrappers run extractor unconditionally ------------
#
# These drive each wrapper's own `_build_extraction` helper. Claude-side
# spawn-specialist delegates to marker_extract.build_extraction (unconditional,
# not rescue-only). Cursor wrappers deliberately return None without calling
# claude (hard gate) — covered by the cursor-specific tests below.

def _spy_runner(marker: str = "COMPLETED", returncode: int = 0):
    calls = []
    stdout = f"MARKER: {marker}\nDIGEST: did the thing\nPLAN: NONE\n"

    def run(argv, **kwargs):
        calls.append(argv)
        return marker_extract.RunResult(returncode, stdout, "")

    return calls, run


def test_build_extraction_not_invoked_when_kill_switch_off(monkeypatch):
    mod = _load_wrapper("spawn-specialist.py")
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    calls, spy = _spy_runner()
    monkeypatch.setattr(mod.marker_extract, "subprocess_runner", spy)

    extraction = mod._build_extraction("COMPLETED: shipped it, tests green.\n", "developer")

    assert extraction is None
    assert calls == []  # the injected runner was NEVER called


def test_build_extraction_invoked_unconditionally_on_clean_marker(monkeypatch):
    # A clean, unambiguous marker: the legacy any-line regex scan would
    # already succeed on this text. Under the old rescue-only wiring the
    # extractor would never run here. It must run anyway (Claude path only).
    mod = _load_wrapper("spawn-specialist.py")
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude")
    calls, spy = _spy_runner()
    monkeypatch.setattr(mod.marker_extract, "subprocess_runner", spy)

    extraction = mod._build_extraction("COMPLETED: shipped it, tests green.\n", "developer")

    assert len(calls) == 1  # the injected runner WAS called — unconditional, not rescue-only
    assert extraction is not None
    assert extraction.marker == "COMPLETED"
    assert extraction.degraded is False


@pytest.mark.parametrize("wrapper", ["spawn-cursor-specialist.py", "spawn-cursor-escape.py"])
def test_cursor_build_extraction_never_invokes_claude(wrapper, monkeypatch):
    """Cursor hard gate: _build_extraction must not shell out to claude -p."""
    mod = _load_wrapper(wrapper)
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude")
    calls, spy = _spy_runner("RESOLVED" if "escape" in wrapper else "COMPLETED")
    monkeypatch.setattr(mod.marker_extract, "subprocess_runner", spy)

    if "escape" in wrapper:
        extraction = mod._build_extraction("RESOLVED: root-caused and fixed.\n")
    else:
        extraction = mod._build_extraction("COMPLETED: shipped it, tests green.\n", "developer")

    assert extraction is None
    assert calls == []


def test_escape_build_extraction_not_invoked_when_kill_switch_off(monkeypatch):
    # Retained: Cursor escape always returns None; kill-switch off is redundant
    # but must not regress into calling claude.
    mod = _load_wrapper("spawn-cursor-escape.py")
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    calls, spy = _spy_runner("RESOLVED")
    monkeypatch.setattr(mod.marker_extract, "subprocess_runner", spy)

    extraction = mod._build_extraction("RESOLVED: root-caused and fixed.\n")

    assert extraction is None
    assert calls == []
