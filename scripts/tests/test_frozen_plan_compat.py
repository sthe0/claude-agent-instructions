"""Non-regression harness over a committed corpus fixture.

Proves that tightening agentctl's plan-submission validation does not change how
an already-authored plan loads. The domain is FIXTURES_DIR/plan_corpus/, a
versioned copy of the plan corpus, deliberately NOT the author's live
~/.claude-agent/plans/ directory: a domain keyed on machine-local state is red on
every other machine (the directory is absent there) and turns red on this one the
moment an unrelated task writes a new plan into it. The live directory is still
worth watching, so `scan_live_drift` reports drift against it without ever
failing the suite — see `test_live_scan_is_advisory`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentctl.plan import PlanError, load_plan

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CORPUS_DIR = FIXTURES_DIR / "plan_corpus"
BASELINE_PATH = FIXTURES_DIR / "plan_corpus_baseline.json"

# The live plan-snapshot directory this task's fixture was copied from. Only
# `scan_live_drift` ever touches this path, and only advisory-style (never raises,
# never asserts) — see its docstring.
LIVE_PLANS_DIR = Path.home() / ".claude-agent" / "plans"


def _load_outcome(path: Path) -> str:
    try:
        load_plan(path, strict=False)
    except PlanError:
        return "PlanError"
    return "ok"


def _outcomes(directory: Path) -> dict[str, str]:
    return {p.name: _load_outcome(p) for p in sorted(directory.glob("*.toml"))}


def scan_live_drift(
    live_dir: Path = LIVE_PLANS_DIR, corpus_dir: Path = CORPUS_DIR
) -> dict[str, list[str]]:
    """Report, without ever raising or failing anything, how the live plans
    directory has drifted from the versioned fixture.

    Advisory only: the fixture is what the non-regression claim is checked
    against, and this function exists solely to surface that the live directory
    has moved on, not to gate anything. A live directory that does not exist
    (e.g. on a machine where this task's author never worked) reports empty
    rather than erroring. Checked with `is_dir()` rather than a try/except around
    `glob()`: pathlib's `glob()` silently yields nothing for a missing directory
    instead of raising, so the except branch never fires and, uncaught, this
    function would fall through to comparing the real fixture against an empty
    live set — reporting every fixture plan as a spurious 'fixture_only' drift."""
    if not live_dir.is_dir():
        return {"live_only": [], "fixture_only": []}
    live_names = {p.name for p in live_dir.glob("*.toml")}
    fixture_names = {p.name for p in corpus_dir.glob("*.toml")}
    return {
        "live_only": sorted(live_names - fixture_names),
        "fixture_only": sorted(fixture_names - live_names),
    }


def test_baseline_matches():
    """Every fixture plan loads (strict=False) with the outcome recorded at
    baseline time; a mismatch surfaces exactly which plan(s) regressed rather
    than a bare pass/fail."""
    baseline = json.loads(BASELINE_PATH.read_text())
    actual = _outcomes(CORPUS_DIR)
    assert actual == baseline, {
        "missing_from_fixture": sorted(set(baseline) - set(actual)),
        "new_in_fixture": sorted(set(actual) - set(baseline)),
        "changed_outcome": {
            name: {"baseline": baseline[name], "actual": actual[name]}
            for name in sorted(set(baseline) & set(actual))
            if baseline[name] != actual[name]
        },
    }


def test_harness_enumerates(tmp_path):
    """The harness's domain is a GLOB over the fixture directory, not a
    hand-written list of names: a plan file added to the fixture at test time
    must be reported as a divergence from the baseline — a hard-coded list of the
    current names would pass this suite without ever noticing the addition, which
    is exactly the substitution of an existential check for a universal one this
    harness exists to prevent."""
    corpus_copy = tmp_path / "plan_corpus"
    shutil.copytree(CORPUS_DIR, corpus_copy)
    baseline = json.loads(BASELINE_PATH.read_text())

    extra = corpus_copy / "zz_injected_extra_plan.toml"
    extra.write_text(
        '[meta]\n'
        'goal = "synthetic plan injected by test_harness_enumerates"\n'
        'done_criterion = "n/a"\n'
        'criterion_type = "measurable"\n'
        'weight_class = "chat"\n'
    )

    actual = _outcomes(corpus_copy)
    assert actual != baseline
    assert "zz_injected_extra_plan.toml" in (set(actual) - set(baseline))


def test_live_scan_is_advisory(tmp_path):
    """A plan present live but absent from the fixture produces a report, not a
    failure: `scan_live_drift` never raises and never asserts, so a live-only
    plan cannot turn this suite red."""
    live_dir = tmp_path / "live"
    corpus_dir = tmp_path / "corpus"
    live_dir.mkdir()
    corpus_dir.mkdir()
    (live_dir / "only_in_live.toml").write_text("[meta]\n")

    report = scan_live_drift(live_dir=live_dir, corpus_dir=corpus_dir)

    assert report["live_only"] == ["only_in_live.toml"]


def test_live_scan_tolerates_missing_live_dir(tmp_path):
    """On a machine where the live directory was never created, the scan reports
    empty rather than erroring — it must never be the reason this suite fails."""
    report = scan_live_drift(
        live_dir=tmp_path / "does-not-exist", corpus_dir=CORPUS_DIR
    )
    assert report == {"live_only": [], "fixture_only": []}
