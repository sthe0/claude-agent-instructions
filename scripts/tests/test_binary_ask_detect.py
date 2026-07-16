"""Tests for binary_ask_detect.detect — the precision-first perception half of the
prose_binary_ask turn guardian (does this turn END with a binary / confirm question
posed in prose instead of via an AskUserQuestion click-gate?).

The fires / doesn't-fire rows are precision TARGETS, not guarantees: this is a
heuristic backstop and paraphrase misses are documented, not bugs. What the tests
PIN is the conjunction discipline (final utterance is a question AND carries a
confirm/action cue AND does not open with a wh-word) and the named precision
targets from the thinker plan-review (substring guards, wh-suppression, the
готов/счита FP-shape regression pins).
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
detect = _mod.detect


# --- fires: trailing binary / confirm question ------------------------------

def test_fires_ru_zapisat():
    assert len(detect("Записать?")) == 1


def test_fires_ru_publish():
    assert len(detect("Готов черновик v11. Публикуем v11?")) == 1


def test_fires_ru_resolution_ask():
    # The headline resolution ask this mechanism most exists to catch.
    assert len(detect("Requested: X. Delivered: X. Считаем решённой?")) == 1


def test_fires_ru_landing_ask():
    assert len(detect("Коммит готов. Вливаем в транк?")) == 1


def test_fires_en_should_i_push():
    assert len(detect("Tests are green. should I push?")) == 1


def test_fires_en_land_it():
    assert len(detect("The branch is ready. Land it?")) == 1


def test_fires_after_leading_prose_paragraph():
    text = (
        "Я доисследовал провенанс judge_score_gt по коду: это ручная YAML-разметка.\n"
        "Записать вывод в system-knowledge лист?"
    )
    assert len(detect(text)) == 1


# --- precision targets: SHOULD NOT fire -------------------------------------

def test_open_wh_ru_chto_zapisat_does_not_fire():
    # Confirm verb present ("записать") but opens with the open wh-word "что" ->
    # genuinely open, free-text answer -> out of scope (rule 3).
    assert detect("Что записать в лист?") == []


def test_open_wh_en_how_should_i_proceed_does_not_fire():
    assert detect("how should I proceed?") == []


def test_substring_guard_udalos_does_not_fire():
    # "удалось" must NOT match the delete cue (narrowed to удали|удаля).
    assert detect("Это удалось?") == []


def test_non_question_does_not_fire():
    assert detect("Всё готово, опубликовал v11.") == []


def test_confirm_then_prose_not_ending_in_question_does_not_fire():
    # Rule 1: the text must END with '?'. A confirm followed by more prose misses
    # (deliberate under-fire).
    assert detect("Записать? Ниже детали правок и следующий шаг.") == []


def test_empty_and_non_string():
    assert detect("") == []
    assert detect(None) == []  # type: ignore[arg-type]


# --- thinker NIT regression pins: готов / счита FP shapes --------------------
# These open with "Есть"/"Ты" (not wh-words) and carry the loosest cues
# (готов\\w* / счита\\w*). They are the shapes most likely to false-positive; pin
# them so a future lexicon change that would fire on them is caught in CI. Current
# behaviour: they DO fire (documented loosest-cue cost) — the pin asserts the
# CURRENT verdict so any change is deliberate, and the surrounding conjunction
# (final-segment-only, ends-'?', not-wh) is what bounds the blast radius.

def test_pin_est_li_gotovoe_reshenie():
    # "Есть ли готовое решение?" — informational, but "готов" is a cue. Pinned as
    # a KNOWN loose-cue fire so a lexicon tweak that changes it is intentional.
    assert len(detect("Есть ли готовое решение?")) == 1


def test_pin_ty_tak_schitaesh():
    # "Ты так считаешь?" — same loose-cue shape via "счита".
    assert len(detect("Ты так считаешь?")) == 1


# --- shape ------------------------------------------------------------------

def test_returns_at_most_one_signal():
    out = detect("Публикуем и вливаем в транк?")
    assert len(out) == 1
    assert isinstance(out[0], str)
