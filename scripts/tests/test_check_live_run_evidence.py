"""Tests for check-live-run-evidence.py — the recount that keeps `live-run-evidence.md`
from drifting away from the samples and hook constants it claims to summarise.

A checker that only ever passes is worth nothing: the failure this whole stage
exists to prevent was a document reporting three green runs that never happened.
So the bulk of what follows is a MUTATION BATTERY — each case corrupts the real
artifact in one specific way the checker promises to catch, and asserts it does.
Each mutation also asserts that it actually changed the text, because a mutation
that silently fails to apply turns its own test into a green no-op, which is the
same defect one level up.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
ARTIFACT = SCRIPTS_DIR.parent / "live-run-evidence.md"


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_live_run_evidence", SCRIPTS_DIR / "check-live-run-evidence.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()


def _run(path: Path) -> int:
    return _mod.main(["check-live-run-evidence.py", str(path)])


# --- the real artifact --------------------------------------------------------

def test_the_committed_artifact_survives_its_own_recount():
    """The gate the stage's verify_command actually runs."""
    assert _run(ARTIFACT) == 0


def test_missing_artifact_fails_rather_than_passing_vacuously(tmp_path):
    assert _run(tmp_path / "nope.md") == 1


# --- the mutation battery -----------------------------------------------------

MUTATIONS = [
    pytest.param(
        lambda t: t.replace("| 18 | 10.29 | 17.43 | 37.58 |", "| 18 | 10.29 | 17.43 | 22.00 |"),
        "recomputes to 37.58",
        id="p90-inflated",
    ),
    pytest.param(
        lambda t: t.replace(
            "| `haiku` | `latency-sample.json:defer", "| `sonnet` | `latency-sample.json:defer"
        ),
        "model tag",
        id="model-tag-swapped",
    ),
    pytest.param(
        lambda t: t.replace("| 0 | 0.0000 | 0.1667 |", "| 0 | 0.0000 | — |"),
        "without a 95% upper bound",
        id="bound-erased-zero-rule",
    ),
    pytest.param(
        lambda t: t.replace("| 45 | 0 | 0.0000 | 0.1667 |", "| 90 | 0 | 0.0000 | 0.1667 |"),
        "ceiling claimed",
        id="ceiling-not-from-code",
    ),
    pytest.param(
        lambda t: t.replace(
            "`topup2-sample.json:binary_ask + drift-sample.json:binary_ask + "
            "drift-sample.json:not_binary_ask` | 48 |",
            "`ab-sample.json:outage_std` | 6 |",
        ),
        "below the required minimum",
        id="n-below-15",
    ),
    pytest.param(
        lambda t: re.sub(r"\n\| — \| `acceptance_judge`.*", "", t),
        "never mentions",
        id="unmeasured-row-dropped",
    ),
    pytest.param(
        lambda t: t.replace("| deny | deny | 43.09 | 45 |", "| deny | deny | 46.50 | 45 |"),
        "not below its target timeout",
        id="wall-clock-over-target",
    ),
    pytest.param(
        lambda t: t.replace("| deny | deny | 4.96 |", "| deny | allow | 4.96 |"),
        "expected verdict",
        id="verdict-mismatch",
    ),
    pytest.param(
        lambda t: t.replace("| 60 | 0 | 0.0000 | 0.0625 |", "| 60 | 2 | 0.0000 | 0.0625 |"),
        "recount that way",
        id="exceedance-miscounted",
    ),
    pytest.param(
        lambda t: t.replace("**MEASURED AND REJECTED**", "**probably slower**"),
        "MEASURED AND REJECTED",
        id="rejected-alternative-softened",
    ),
    pytest.param(
        lambda t: t.replace("| **NOT MEASURED** |", "| **fine, presumably** |"),
        "NOT MEASURED",
        id="unmeasured-alternative-softened",
    ),
    pytest.param(
        lambda t: t.split("<!-- not-checked -->")[0] + "<!-- not-checked -->\n- Latency.\n\n## x\n",
        "an stdin run misses more than that",
        id="not-checked-gutted",
    ),
]


@pytest.mark.parametrize("mutate,expected_message", MUTATIONS)
def test_mutation_is_caught(mutate, expected_message, tmp_path, capsys):
    original = ARTIFACT.read_text(encoding="utf-8")
    mutated = mutate(original)
    assert mutated != original, "mutation did not apply — this test would pass vacuously"

    path = tmp_path / "live-run-evidence.md"
    path.write_text(mutated, encoding="utf-8")

    assert _run(path) == 1
    assert expected_message in capsys.readouterr().out


# --- the bound itself ---------------------------------------------------------

def test_zero_events_uses_the_rule_of_three():
    for n in (16, 18, 26):
        assert _mod.exact_upper_bound(0, n) == pytest.approx(3.0 / n)


def test_upper_bound_is_the_largest_rate_still_consistent_with_the_count():
    """At the returned bound, seeing k or fewer in n trials sits right at 5%."""
    for k, n in ((1, 16), (2, 18), (3, 26)):
        ub = _mod.exact_upper_bound(k, n)
        assert ub > k / n, "an upper bound below the point estimate is not a bound"
        assert _mod._binom_cdf(k, n, ub) == pytest.approx(0.05, abs=1e-6)


def test_upper_bound_shrinks_as_the_sample_grows():
    bounds = [_mod.exact_upper_bound(0, n) for n in (16, 26, 100, 1000)]
    assert bounds == sorted(bounds, reverse=True)
