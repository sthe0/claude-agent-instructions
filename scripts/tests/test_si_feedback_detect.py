"""Tests for si_feedback_detect — the shared DETERMINISTIC feedback detector.

`find_signals` now fires ONLY on Tier 1: the explicit 'self-improvement'
proper-name mention (after excising harness-injected context). The former Tier-2
natural-language corrective cues were retired from regex and moved to
semantic_judge.py ('si_feedback'); the Stop shell consults that judge behind a
precondition gate, and the instant reminder deliberately does not. So this file
pins (a) the Tier-1 literal, (b) that former Tier-2 NL corrections are now SILENT
here, (c) the injection-excision, and (d) `is_neutral_affirmation`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS_DIR / "si_feedback_detect.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("si_feedback_detect", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# --- Tier 1: explicit self-improvement mention fires (deterministic) --------

@pytest.mark.parametrize(
    "prompt",
    [
        "did you run self-improvement?",
        "запусти self-improvement по этому",
        "the self improvement skill should have run",
        "selfimprovement was skipped",
    ],
)
def test_tier1_mention_fires(prompt):
    assert _mod.find_signals(prompt) == ["explicit self-improvement mention"]


# --- former Tier-2 NL corrections are now SILENT here (judge's domain) ------

@pytest.mark.parametrize(
    "prompt",
    [
        "don't do that again",
        "stop doing the extra commits",
        "перестань так делать",
        "не делай так больше",
        "я же просил не коммитить",
        "you shouldn't have pushed",
        "why did you skip the tests",
        "next time ask me first, you keep forgetting",
        "почему ты не спросил",
        "you used a loop instead of a map",
    ],
)
def test_former_tier2_now_silent(prompt):
    # These carry no 'self-improvement' literal — the semantic judge, not this
    # deterministic detector, now classifies them. `find_signals` stays silent.
    assert _mod.find_signals(prompt) == [], f"unexpected signal for: {prompt!r}"


# --- plain task text is silent (unchanged) ----------------------------------

@pytest.mark.parametrize(
    "prompt",
    [
        "add a function to parse the config file",
        "always validate input before writing",
        "never commit secrets to the repo",
        "refactor this module instead of rewriting it",
        "run the tests and report the result",
        "",
    ],
)
def test_plain_task_text_silent(prompt):
    assert _mod.find_signals(prompt) == [], f"unexpected signal for: {prompt!r}"


def test_non_string_input_is_safe():
    assert _mod.find_signals(None) == []  # type: ignore[arg-type]
    assert _mod.find_signals(123) == []  # type: ignore[arg-type]


# --- is_neutral_affirmation -------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["ok", "OK", "спасибо", "thanks", "thank you.", "да", "lgtm", "perfect!", "", "  "],
)
def test_neutral_affirmations(text):
    assert _mod.is_neutral_affirmation(text) is True


@pytest.mark.parametrize(
    "text",
    ["ok but fix the tests", "спасибо, но переделай", "why did you skip the tests"],
)
def test_non_affirmations(text):
    assert _mod.is_neutral_affirmation(text) is False


def test_is_neutral_affirmation_non_string():
    assert _mod.is_neutral_affirmation(None) is False  # type: ignore[arg-type]


# --- injection excision (Tier-1 must survive / not fire on injected text) ----

def test_system_reminder_span_is_excised():
    injected = (
        "<system-reminder>Remember to run self-improvement when the user "
        "corrects behavior.</system-reminder> ok thanks"
    )
    assert _mod.find_signals(injected) == []


def test_multiline_system_reminder_span_is_excised():
    injected = (
        "<system-reminder>\nSkill: self-improvement\nUse the self improvement "
        "skill on feedback.\n</system-reminder>\nlooks good, ship it"
    )
    assert _mod.find_signals(injected) == []


def test_multiple_reminder_spans_excised_but_human_text_still_scanned():
    text = (
        "<system-reminder>self-improvement context A</system-reminder>"
        "did you run self-improvement on this?"
        "<system-reminder>self-improvement context B</system-reminder>"
    )
    assert _mod.find_signals(text) == ["explicit self-improvement mention"]


def test_strip_injected_context_helper():
    assert (
        _mod.strip_injected_context("<system-reminder>x</system-reminder>ab").strip()
        == "ab"
    )
    assert _mod.strip_injected_context("plain text") == "plain text"


def test_task_notification_span_is_excised():
    injected = (
        "<task-notification>\n<task-id>abc</task-id>\nrun the self-improvement "
        "skill\n</task-notification>"
    )
    assert _mod.find_signals(injected) == []


def test_continuation_summary_is_excised():
    injected = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers earlier work.\n\n"
        "Queued self-improvement items: (a) atomic timer-arming; (b) budget-death "
        "forensics. Deferred to a post-resolution batch."
    )
    assert _mod.find_signals(injected) == []


def test_skill_replay_block_is_excised():
    injected = (
        "The following skills were invoked EARLIER in this session, not on the "
        "current turn.\n\n### Skill: self-improvement\nARGUMENTS: Behavioral "
        "correction about self-improvement timing."
    )
    assert _mod.find_signals(injected) == []


def test_trailing_injection_does_not_suppress_prior_human_tier1():
    # Human Tier-1 feedback authored BEFORE a trailing injected block still fires.
    text = (
        "did you run self-improvement on this?\n\n"
        "The following skills were invoked EARLIER in this session\n"
        "### Skill: self-improvement\nfull body ..."
    )
    assert _mod.find_signals(text) == ["explicit self-improvement mention"]
