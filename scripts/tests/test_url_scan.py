"""Shared URL scan: one home for the http(s) regex and the trailing-sentence-
punctuation trim both link-reasoning hooks need."""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "url_scan", Path(__file__).resolve().parents[1] / "url_scan.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

_URL = "https://ci.example.com/runs/4271"


def test_finds_a_bare_url():
    assert list(mod.iter_urls(f"see {_URL} now")) == [_URL]


@pytest.mark.parametrize("text", [
    f"launched {_URL}.",
    f"{_URL}: running",
    f"created {_URL}, watching it",
    f"done? {_URL}!",
])
def test_trailing_sentence_punctuation_is_trimmed(text):
    assert list(mod.iter_urls(text)) == [_URL]


def test_punctuation_inside_a_path_survives():
    # The trim is END-anchored: a URL may legally carry these mid-path.
    url = "https://ci.example.com/runs/v1.2.3/logs"
    assert list(mod.iter_urls(f"see {url} now")) == [url]


def test_markdown_and_quote_wrappers_are_excluded():
    assert list(mod.iter_urls(f"[link]({_URL})")) == [_URL]
    assert list(mod.iter_urls(f"`{_URL}`")) == [_URL]


def test_several_urls_in_order():
    other = "https://ci.example.com/runs/9008"
    assert list(mod.iter_urls(f"{_URL}, then {other}.")) == [_URL, other]


@pytest.mark.parametrize("text", ["", None, 17, ["not", "a", "string"], "no links here"])
def test_total_on_bad_or_empty_input(text):
    assert list(mod.iter_urls(text)) == []
