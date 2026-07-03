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
