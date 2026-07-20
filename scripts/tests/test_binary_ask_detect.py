"""Tests for binary_ask_detect.final_question_segment — the language-agnostic
STRUCTURAL gate of the prose_binary_ask turn guardian (does this turn's assistant
text END with a question?).

The per-language "is that trailing question a BINARY/CONFIRM ask?" cue matching was
retired and moved to semantic_judge.py ('binary_ask' kind). What remains here — and
what these tests PIN — is only the structural precondition: the text (stripped)
ends with a question mark (ASCII '?' or full-width '？'), and the returned segment
is its final sentence (a confirm buried mid-turn, with substantive text after it,
yields None so the shell never spends a judge call on it).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS_DIR / "binary_ask_detect.py"


def _load():
    spec = importlib.util.spec_from_file_location("binary_ask_detect", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
final_question_segment = _mod.final_question_segment


# --- returns the final segment: text ends with a question -------------------

def test_ru_single_question():
    assert final_question_segment("Записать?") == "Записать"


def test_en_single_question():
    assert final_question_segment("should I push?") == "should I push"


def test_full_width_question_mark():
    # The full-width '？' counts as a question terminator (CJK input).
    assert final_question_segment("公開しますか？") == "公開しますか"


def test_returns_last_sentence_after_leading_prose():
    text = (
        "Я доисследовал провенанс по коду: это ручная YAML-разметка.\n"
        "Записать вывод в лист?"
    )
    assert final_question_segment(text) == "Записать вывод в лист"


def test_trailing_whitespace_tolerated():
    assert final_question_segment("Публикуем v11?  \n") == "Публикуем v11"


def test_segment_bounded_by_previous_terminator():
    # Only the final sentence is returned, not the whole multi-sentence body.
    assert final_question_segment("Готов черновик. Публикуем?") == "Публикуем"


# --- returns None: text does not end with a question ------------------------

def test_statement_is_none():
    assert final_question_segment("Всё готово, опубликовал v11.") is None


def test_question_then_prose_is_none():
    # A confirm followed by more prose does NOT end with '?': structurally not a
    # trailing ask, so the shell must not spend a judge call.
    assert final_question_segment("Записать? Ниже детали правок и следующий шаг.") is None


def test_empty_is_none():
    assert final_question_segment("") is None


def test_non_string_is_none():
    assert final_question_segment(None) is None  # type: ignore[arg-type]
    assert final_question_segment(123) is None  # type: ignore[arg-type]


# --- structural, not semantic: open-ended questions still pass the gate -----
# The gate is deliberately meaning-agnostic — an open wh-question ends with '?'
# too, and passes; distinguishing binary-confirm from open-ended is the judge's
# job, not this helper's. Pin that so a future change that re-adds NL suppression
# here (against the split-of-labor) is caught.

def test_open_wh_question_still_returns_segment():
    assert final_question_segment("Что записать в лист?") == "Что записать в лист"
    assert final_question_segment("how should I proceed?") == "how should I proceed"
