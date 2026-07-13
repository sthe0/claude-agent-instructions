"""Tests for outage_escalation_detect.detect — the precision-first perception half
of the escalation-without-diagnosis gate.

The fires / doesn't-fire rows are precision TARGETS, not guarantees: this is a
heuristic backstop and paraphrase misses are documented, not bugs. What the tests
PIN is the conjunction discipline (both a present-tense failure cue AND a
user-facing escalation frame must co-occur) and the named precision targets.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS_DIR / "outage_escalation_detect.py"


def _load():
    spec = importlib.util.spec_from_file_location("outage_escalation_detect", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
detect = _mod.detect


# --- fires: present-tense outage + user-facing ask --------------------------

def test_fires_ru_present_outage_with_ask():
    out = detect("Сервис не отвечает, endpoint недоступен — что делать?")
    assert len(out) == 1


def test_fires_first_person_inability_with_ask():
    out = detect("Не могу получить доступ к базе. Подскажи, к кому обратиться?")
    assert len(out) == 1


def test_fires_en_present_outage_with_ask():
    out = detect("The upstream is down — 504 no upstreams. What should I do?")
    assert len(out) == 1


def test_fires_ask_user_question_body_shape():
    # The A2 gate concatenates question + option texts; a typical outage ask body.
    body = "Сервис лежит и недоступен. К какому сервису запросить доступ?"
    assert len(detect(body)) == 1


# --- precision targets: SHOULD NOT fire -------------------------------------

def test_past_downtime_narrative_is_a_target_not_fire():
    # Documented precision target: narrative PAST downtime with a resolution.
    # `лежал` is past tense -> the present-tense `\bлежит\b` cue does not match.
    assert detect("Сервис лежал вчера час, но его уже починили.") == []


def test_plain_task_spec_with_down_in_another_sense():
    assert detect("Scroll down to the config section and add a parser.") == []


def test_outage_report_without_ask_does_not_fire():
    # A bare report with no escalation frame -> conjunction not met.
    assert detect("Сервис не отвечает на запросы.") == []


def test_ask_without_failure_cue_does_not_fire():
    # A user-facing question with no failure cue -> conjunction not met.
    assert detect("Что делать дальше по этой задаче?") == []


def test_empty_and_non_string():
    assert detect("") == []
    assert detect(None) == []  # type: ignore[arg-type]


# --- shape ------------------------------------------------------------------

def test_returns_at_most_one_signal():
    # Multiple cues present -> still exactly one aggregated signal string.
    out = detect(
        "Endpoint недоступен, сервис не отвечает, 504 no upstreams. "
        "Что делать и к кому эскалировать?"
    )
    assert len(out) == 1
    assert isinstance(out[0], str)
