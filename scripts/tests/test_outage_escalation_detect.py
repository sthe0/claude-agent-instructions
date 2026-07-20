"""Tests for outage_escalation_detect.protocol_prefilter — the language-agnostic
PROTOCOL-TOKEN pre-filter of the escalation-without-diagnosis gate.

The per-language "this reads like an un-diagnosed outage escalation" cue
conjunction was retired and moved to semantic_judge.py ('outage_escalation' kind).
What remains here — and what these tests PIN — is only the deterministic
precondition: does the text carry an external-service-failure protocol token (an
HTTP 4xx/5xx status run, or a fixed English outage token)? A pure natural-language
outage escalation with NO protocol token no longer fires this pre-filter (the
recall narrowing documented in the module); the semantic judge is the meaning-level
backstop on the Stop path.
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
protocol_prefilter = _mod.protocol_prefilter


# --- fires: a protocol token is present -------------------------------------

def test_fires_on_5xx_status():
    assert protocol_prefilter("The upstream returned 504 no upstreams.") is True


def test_fires_on_4xx_status():
    assert protocol_prefilter("Got a 403 from the API.") is True


def test_fires_on_timeout():
    assert protocol_prefilter("The request timed out.") is True
    assert protocol_prefilter("connection timeout") is True


def test_fires_on_unreachable():
    assert protocol_prefilter("host unreachable") is True


def test_fires_on_no_upstreams():
    assert protocol_prefilter("502 Bad Gateway: no upstreams") is True


def test_fires_language_agnostic_when_token_present():
    # A Russian body still fires when it carries a bare protocol token.
    assert protocol_prefilter("Сервис вернул 504 no upstreams — что делать?") is True


# --- does NOT fire: no protocol token (now the judge's domain) --------------

def test_pure_nl_outage_without_token_is_silent():
    # The recall narrowing: a natural-language escalation with no protocol token no
    # longer fires the pre-filter — the semantic judge is the Stop-path backstop.
    assert protocol_prefilter("Сервис не отвечает, endpoint недоступен — что делать?") is False


def test_plain_prose_is_silent():
    assert protocol_prefilter("Scroll down to the config section and add a parser.") is False


def test_bare_number_not_in_4xx_5xx_range_is_silent():
    # The status regex is [45]\d\d: a 2xx/3xx code or an unrelated number is silent.
    assert protocol_prefilter("HTTP 302 redirect handled.") is False
    assert protocol_prefilter("We processed 128 items.") is False
    assert protocol_prefilter("200 OK, all good.") is False


def test_empty_and_non_string():
    assert protocol_prefilter("") is False
    assert protocol_prefilter(None) is False  # type: ignore[arg-type]
