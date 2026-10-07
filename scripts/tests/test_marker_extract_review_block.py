"""A message whose terminal block is a verdict-bearing REVIEW block is labelled
REVIEW by structure, never by the extractor model — zero real model calls.

Each fixture under fixtures/marker_extract_review/ is a plan-review reply the
model once labelled otherwise (the header line names the wrong label). The model
runner is replaced by one that returns that recorded wrong label and counts its
calls.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lib import marker_extract

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marker_extract_review"
HEADER = "# wrong-label:"
FIXTURE_PATHS = sorted(FIXTURES.glob("*.txt"))
DIGEST = "a" * 64


def _fixture(path: Path) -> tuple[str, str]:
    header, _, body = path.read_text(encoding="utf-8").partition("\n")
    assert header.startswith(HEADER), path.name
    return header[len(HEADER):].split()[0], body


class _Model:
    def __init__(self, marker: str):
        self.marker = marker
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        return marker_extract.RunResult(0, f"MARKER: {self.marker}\nDIGEST: d\nPLAN: NONE\n")


def _driver():
    spec = importlib.util.spec_from_file_location(
        "plan_review_topological", SCRIPTS / "plan-review-topological.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_review_topological"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fixtures_cover_every_recorded_wrong_label():
    labels = {_fixture(p)[0] for p in FIXTURE_PATHS}
    assert labels == {"PLAN-READY", "REPLAN", "COMPLETED", "MALFORMED"}


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_fixture_extract_returns_review_without_the_model(path):
    wrong, body = _fixture(path)
    model = _Model("NONE" if wrong == "MALFORMED" else wrong)
    result = marker_extract.extract(body, runner=model)
    assert model.calls == 0
    assert result.marker == "REVIEW"
    assert result.outcome == marker_extract.OUTCOME_CLASSIFIED
    assert result.degraded is False


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_fixture_driver_parses_the_same_block(path):
    _, body = _fixture(path)
    parsed = _driver().parse_review_output(body)
    assert parsed.verdict in ("pass", "revise")
    assert (parsed.concerns != []) == (parsed.verdict == "revise")


def test_fixture_two_verdict_lines_use_the_terminal_blocks_verdict():
    body = _fixture(FIXTURES / "02-replan-two-verdict-lines.txt")[1]
    assert body.lstrip().startswith("Verdict: **revise**")
    from lib import review_block

    assert review_block.find_terminal_review_block(body).verdict == "revise"


def test_terminal_other_marker_quoting_an_earlier_review_block_goes_to_the_model():
    text = (
        "Earlier I wrote this review:\n\n"
        f"REVIEW:\nVerdict: pass\nPlan digest: {DIGEST}\n\n"
        "COMPLETED: the stage is done and tests are green.\n"
    )
    model = _Model("COMPLETED")
    result = marker_extract.extract(text, runner=model)
    assert model.calls == 1
    assert result.marker == "COMPLETED"


def test_review_block_without_a_verdict_line_goes_to_the_model():
    text = f"Findings follow.\n\nREVIEW:\nPlan digest: {DIGEST}\nC1: holds.\n"
    model = _Model("INCOMPLETE")
    result = marker_extract.extract(text, runner=model)
    assert model.calls == 1
    assert result.marker == "INCOMPLETE"


def test_review_word_inside_prose_goes_to_the_model():
    text = "I may return REVIEW: pass later, Verdict: pass is not decided yet.\n"
    model = _Model("NONE")
    result = marker_extract.extract(text, runner=model)
    assert model.calls == 1
    assert result.marker is None


def test_vocabulary_without_review_never_short_circuits():
    _, body = _fixture(FIXTURE_PATHS[0])
    model = _Model("RESOLVED")
    result = marker_extract.extract(body, allowed=("RESOLVED", "INVESTIGATION"), runner=model)
    assert model.calls == 1
    assert result.marker == "RESOLVED"


def test_driver_and_extractor_share_one_parse_function(monkeypatch):
    from lib import review_block

    original = review_block.find_terminal_review_block
    seen: list[str] = []

    def spy(text):
        seen.append(text)
        return original(text)

    monkeypatch.setattr(review_block, "find_terminal_review_block", spy)
    body = _fixture(FIXTURE_PATHS[0])[1]
    drv = _driver()
    assert drv.review_block is review_block
    drv.parse_review_output(body)
    marker_extract.extract(body, runner=_Model("COMPLETED"))
    assert len(seen) == 2
