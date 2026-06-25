"""Tests for hook-self-improvement-reminder.py — feedback-trigger detector.

Covers:
- Tier 1: explicit self-improvement mention fires (spacing/hyphen variants, RU+EN).
- Tier 2(a): strong imperatives fire on their own (no agent-ref needed).
- Tier 2(b): context-dependent corrections fire only with an agent-ref cue.
- Negatives: neutral task prompts stay silent, incl. bare always/never/всегда.
- main(): emits exactly one reminder line on a signal; nothing otherwise.
- Robustness: malformed / empty stdin -> exit 0, no output.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-self-improvement-reminder.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_si_reminder", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# find_signals — positives
# ---------------------------------------------------------------------------
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
        "я же говорил про язык",
        "you shouldn't have pushed",
        "why did you skip the tests",
        "next time ask me first, you keep forgetting",
        "ты сделал не так",
        "почему ты не спросил",
        "в следующий раз ты должен спросить",
    ],
)
def test_positive_signals_fire(prompt):
    assert _mod.find_signals(prompt), f"expected a signal for: {prompt!r}"


# ---------------------------------------------------------------------------
# find_signals — negatives
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prompt",
    [
        "add a function to parse the config file",
        "always validate input before writing",  # bare 'always', no agent-ref
        "never commit secrets to the repo",       # bare 'never'
        "всегда валидируй ввод перед записью",     # bare 'всегда'
        "refactor this module instead of rewriting it",  # 'instead of' but no agent-ref
        "you should review the README too",        # agent-ref but no corrective pattern
        "run the tests and report the result",
        "",
    ],
)
def test_negative_prompts_silent(prompt):
    assert _mod.find_signals(prompt) == [], f"unexpected signal for: {prompt!r}"


def test_tier2b_needs_agent_ref():
    # 'instead of' is a Tier 2(b) pattern: silent without an agent-ref cue,
    # fires once one co-occurs.
    assert _mod.find_signals("use a map instead of a loop") == []
    assert _mod.find_signals("you used a loop instead of a map")


# ---------------------------------------------------------------------------
# main() via subprocess — exactly one line / silence / exit 0 always
# ---------------------------------------------------------------------------
def _run(stdin_bytes: bytes):
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_bytes,
        capture_output=True,
    )


def test_main_emits_one_line_on_signal():
    p = _run(json.dumps({"prompt": "did you run self-improvement?"}).encode())
    assert p.returncode == 0
    lines = [l for l in p.stdout.decode().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("[self-improvement-reminder]")


def test_main_silent_on_neutral():
    p = _run(json.dumps({"prompt": "add a parser for the config"}).encode())
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


def test_main_malformed_stdin_exit_0():
    p = _run(b"not json at all")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


def test_main_empty_stdin_exit_0():
    p = _run(b"")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


def test_main_empty_prompt_exit_0():
    p = _run(json.dumps({"prompt": "   "}).encode())
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""
