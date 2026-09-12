"""Pins `lib.marker_extract._EXTRACT_TIMEOUT_S` to the committed calibration
dataset (`docs/operations/marker-extractor-calibration.jsonl`), so an edit to
`docs/operations/marker-extractor-calibration.md` can never silently drift the
shipped timeout. Sibling of `test_enumerate_detach.py::TestCalibrationConstant`,
same method (`max within-size max/min spread` x `min elapsed at the largest
sampled size`) applied to a different, POOLED (idle+loaded) dataset.

`load_rows()` and `derive_constant(rows)` are importable on purpose: the gate
in the plan's stage-4 `Verify command` calls them directly on the pooled set
and on an idle-only filter of the same rows, so the pooled-vs-idle-only
requirement is checked mechanically rather than trusted from prose.
"""
from __future__ import annotations

import json
import math
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CALIBRATION_PATH = REPO_ROOT / "docs" / "operations" / "marker-extractor-calibration.jsonl"

# Granularity of the rounding step: the shipped constant lives in the tens of
# seconds (unlike advisor.py's `_ENUMERATE_TIMEOUT_S_DEFAULT`, which lives in
# minutes and rounds to the minute) so rounding up to the nearest 5s keeps a
# proportionally similar rounding-to-value ratio without manufacturing false
# precision.
_ROUND_TO_S = 5


def load_rows() -> list[dict]:
    return [
        json.loads(line)
        for line in CALIBRATION_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def derive_constant(rows: list[dict]) -> int:
    """`ceil_to_5(max_size(max/min spread) * min(elapsed_s at the largest
    sampled input_chars))` — the same shape as advisor.py's derivation, over
    whichever subset of rows it is handed."""
    by_size: dict[int, list[float]] = {}
    for row in rows:
        by_size.setdefault(row["input_chars"], []).append(row["elapsed_s"])
    max_spread = max(max(vals) / min(vals) for vals in by_size.values())
    largest_size = max(by_size)
    min_elapsed_at_largest = min(by_size[largest_size])
    raw = max_spread * min_elapsed_at_largest
    return math.ceil(raw / _ROUND_TO_S) * _ROUND_TO_S


def _idle_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["loadavg"] < 2.0]


class TestDatasetShape:
    def test_at_least_four_sizes_with_three_idle_and_three_loaded_repeats_each(self):
        rows = load_rows()
        assert len(rows) >= 24
        sizes = {r["input_chars"] for r in rows}
        assert len(sizes) >= 4
        for size in sizes:
            idle = [r for r in rows if r["input_chars"] == size and r["loadavg"] < 2.0]
            loaded = [r for r in rows if r["input_chars"] == size and r["loadavg"] >= 5.0]
            assert len(idle) >= 3, (size, "idle repeats")
            assert len(loaded) >= 3, (size, "loaded repeats")


class TestCalibrationConstant:
    def test_default_timeout_matches_calibration_dataset_formula(self):
        from lib.marker_extract import _EXTRACT_TIMEOUT_S

        rows = load_rows()
        expected = derive_constant(rows)
        assert expected == _EXTRACT_TIMEOUT_S

    def test_idle_only_derivation_is_strictly_below_the_pooled_one(self):
        """The pooled-derivation requirement is only checkable if the loaded
        rows actually MOVED the answer — this is what makes that mechanical
        rather than trusted from the note's prose."""
        rows = load_rows()
        pooled = derive_constant(rows)
        idle_only = derive_constant(_idle_rows(rows))
        assert idle_only < pooled

    def test_shipped_constant_is_not_reproducible_from_idle_rows_alone(self):
        from lib.marker_extract import _EXTRACT_TIMEOUT_S

        rows = load_rows()
        idle_only = derive_constant(_idle_rows(rows))
        assert _EXTRACT_TIMEOUT_S != idle_only
