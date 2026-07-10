"""Tests for si_feedback_detect.find_signals — the shared feedback detector.

The reminder hook and the Stop gate both import this module, so the tier
behavior is verified once here (the reminder-hook test file covers the hook's
stdin/stdout wrapper separately).
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


@pytest.mark.parametrize(
    "prompt",
    [
        "did you run self-improvement?",
        "запусти self-improvement по этому",
        "the self improvement skill should have run",
        "selfimprovement was skipped",
        "don't do that again",
        "stop doing the extra commits",
        "перестань так делать",
        "не делай так больше",
        "я же просил не коммитить",
        "you shouldn't have pushed",
        "why did you skip the tests",
        "next time ask me first, you keep forgetting",
        "почему ты не спросил",
    ],
)
def test_positive_signals_fire(prompt):
    assert _mod.find_signals(prompt), f"expected a signal for: {prompt!r}"


@pytest.mark.parametrize(
    "prompt",
    [
        "add a function to parse the config file",
        "always validate input before writing",
        "never commit secrets to the repo",
        "всегда валидируй ввод перед записью",
        "refactor this module instead of rewriting it",
        "you should review the README too",
        "run the tests and report the result",
        "",
    ],
)
def test_negative_prompts_silent(prompt):
    assert _mod.find_signals(prompt) == [], f"unexpected signal for: {prompt!r}"


def test_tier2b_needs_agent_ref():
    assert _mod.find_signals("use a map instead of a loop") == []
    assert _mod.find_signals("you used a loop instead of a map")


def test_non_string_input_is_safe():
    assert _mod.find_signals(None) == []  # type: ignore[arg-type]
    assert _mod.find_signals(123) == []  # type: ignore[arg-type]


def test_system_reminder_span_is_excised():
    # A single injected span that mentions self-improvement must NOT fire.
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
    # Two injected spans wrap genuine user feedback — the feedback must survive.
    text = (
        "<system-reminder>self-improvement context A</system-reminder>"
        "did you run self-improvement on this?"
        "<system-reminder>self-improvement context B</system-reminder>"
    )
    assert _mod.find_signals(text) == ["explicit self-improvement mention"]


def test_reminder_excision_does_not_suppress_human_tier2():
    text = (
        "<system-reminder>self-improvement skill list</system-reminder>"
        " you shouldn't have pushed"
    )
    assert _mod.find_signals(text)


def test_strip_injected_context_helper():
    assert (
        _mod.strip_injected_context("<system-reminder>x</system-reminder>ab").strip()
        == "ab"
    )
    assert _mod.strip_injected_context("plain text") == "plain text"
