"""hook-si-freetext-answer.py: nudge self-improvement on a free-text
AskUserQuestion answer, silent when the user picked an offered option."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook-si-freetext-answer.py"

QUESTIONS_INPUT = {
    "questions": [
        {
            "question": "Применить эту правку?",
            "header": "Действие",
            "multiSelect": False,
            "options": [
                {"label": "Применить", "description": "..."},
                {"label": "Не применять", "description": "..."},
            ],
        }
    ]
}

MULTI_INPUT = {
    "questions": [
        {
            "question": "Какие файлы включить?",
            "header": "Область",
            "multiSelect": True,
            "options": [
                {"label": "a.py", "description": "..."},
                {"label": "b.py", "description": "..."},
            ],
        }
    ]
}


def _run(payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_option_answer_silent():
    r = _run({
        "tool_name": "AskUserQuestion",
        "tool_input": QUESTIONS_INPUT,
        "tool_response": 'Your questions have been answered: "Применить эту правку?"="Применить". '
                          "You can now continue with these answers in mind.",
    })
    assert r.returncode == 0
    assert r.stderr == ""


def test_multiselect_option_answers_silent():
    r = _run({
        "tool_name": "AskUserQuestion",
        "tool_input": MULTI_INPUT,
        "tool_response": 'Your questions have been answered: "Какие файлы включить?"="a.py, b.py". '
                          "You can now continue with these answers in mind.",
    })
    assert r.returncode == 0
    assert r.stderr == ""


def test_freetext_answer_nudges():
    # Real-world precedent (2026-07-02): a correction delivered as an
    # AskUserQuestion answer instead of picking "Применить"/"Не применять".
    r = _run({
        "tool_name": "AskUserQuestion",
        "tool_input": QUESTIONS_INPUT,
        "tool_response": 'Your questions have been answered: "Применить эту правку?"='
                          '"Почему снова по-английски? Напиши по-русски.". '
                          "You can now continue with these answers in mind.",
    })
    assert r.returncode == 0
    assert "self-improvement" in r.stderr.lower()
    assert "free text" in r.stderr.lower()


def test_freetext_partial_multiselect_nudges():
    r = _run({
        "tool_name": "AskUserQuestion",
        "tool_input": MULTI_INPUT,
        "tool_response": 'Your questions have been answered: "Какие файлы включить?"="a.py, c.py". '
                          "You can now continue with these answers in mind.",
    })
    assert r.returncode == 0
    assert "self-improvement" in r.stderr.lower()


def test_other_tool_silent():
    r = _run({
        "tool_name": "Bash",
        "tool_input": QUESTIONS_INPUT,
        "tool_response": 'Your questions have been answered: "Применить эту правку?"="Что угодно". ',
    })
    assert r.returncode == 0
    assert r.stderr == ""


def test_uncorrelated_question_silent():
    # Answered text doesn't match any question in tool_input -> can't
    # correlate to an offered label set, so never flagged.
    r = _run({
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Другой вопрос?", "options": [{"label": "Да"}]}]},
        "tool_response": 'Your questions have been answered: "Применить эту правку?"="Свободный текст". ',
    })
    assert r.returncode == 0
    assert r.stderr == ""


def test_malformed_payload_and_missing_fields_silent():
    r = _run("not json")
    assert r.returncode == 0
    assert r.stderr == ""

    r2 = _run({"tool_name": "AskUserQuestion"})
    assert r2.returncode == 0
    assert r2.stderr == ""

    r3 = _run({"tool_name": "AskUserQuestion", "tool_input": QUESTIONS_INPUT})
    assert r3.returncode == 0
    assert r3.stderr == ""

    r4 = _run({"tool_name": "AskUserQuestion", "tool_input": "not-a-dict", "tool_response": "x"})
    assert r4.returncode == 0
    assert r4.stderr == ""


def test_registered_in_installer_desired():
    installer = Path(__file__).resolve().parent.parent / "install-reminder-hooks.sh"
    text = installer.read_text(encoding="utf-8")
    assert '"PostToolUse",      "AskUserQuestion", "hook-si-freetext-answer.py"' in text
