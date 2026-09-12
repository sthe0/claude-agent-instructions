"""Tests for improvement-scan.py's `report` subcommand (Stage 5): one
cost-first ranking over both producers' findings, with an explicit
unmeasured band and a recommended next step per finding.

Loaded by path exactly like test_improvement_scan.py; see that file's
docstring for why (the module's filename carries a dash).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from ast_purity import impure_names  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()


def _cost(measured=False, usd=None, attention=None, stability=None, basis=""):
    return scan.CostSignal(
        usd_per_week=usd, attention_per_week=attention, stability_per_week=stability,
        basis=basis or ("measured" if measured else "unmeasured"), measured=measured,
    )


def _finding(kind="backlog-item", signal="sig-1", **kw):
    defaults = dict(
        kind=kind, signal=signal, title="a title", functional_ground="a ground",
        evidence=("ev-1",), cost_signal=_cost(), source_ref="ref-1",
        recommended_next_step="planner",
    )
    defaults.update(kw)
    return scan.Finding(**defaults)


def _report_dict(finding: "scan.Finding") -> dict:
    """A `_report_findings`-shaped dict built directly from a Finding, so
    ranking tests don't need to round-trip through the store."""
    return {
        "key": finding.signal,
        "source": finding.kind,
        "path": finding.signal,
        "title": finding.title,
        "functional_ground": finding.functional_ground,
        "evidence": list(finding.evidence),
        "cost_signal": {
            "usd_per_week": finding.cost_signal.usd_per_week,
            "attention_per_week": finding.cost_signal.attention_per_week,
            "stability_per_week": finding.cost_signal.stability_per_week,
            "basis": finding.cost_signal.basis,
            "measured": finding.cost_signal.measured,
        },
        "proxy_score": finding.proxy_score,
        "source_ref": finding.source_ref,
        "recommended_next_step": finding.recommended_next_step,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "times_surfaced": 1,
        "status": "open",
    }


# --- (a) three-term lexicographic order, ties broken correctly --------------

def test_measured_band_is_ranked_cost_then_attention_then_stability():
    high_cost = _report_dict(_finding(signal="a", cost_signal=_cost(measured=True, usd=50)))
    low_cost = _report_dict(_finding(signal="b", cost_signal=_cost(measured=True, usd=10)))
    tie_cost_high_attn = _report_dict(
        _finding(signal="c", cost_signal=_cost(measured=True, usd=10, attention=9))
    )
    tie_cost_tie_attn_high_stab = _report_dict(
        _finding(signal="d", cost_signal=_cost(measured=True, usd=10, attention=9, stability=5))
    )

    ranked = scan._rank_findings([low_cost, high_cost, tie_cost_high_attn, tie_cost_tie_attn_high_stab])

    assert [f["key"] for f in ranked] == ["a", "d", "c", "b"]


# --- (b) unmeasured band never interleaves with measured ---------------------

def test_unmeasured_band_never_interleaves_even_with_a_high_proxy_score():
    cheap_measured = _report_dict(_finding(signal="measured-low", cost_signal=_cost(measured=True, usd=1)))
    huge_proxy = _report_dict(_finding(signal="unmeasured-huge-proxy", proxy_score=999.0))

    ranked = scan._rank_findings([huge_proxy, cheap_measured])

    assert [f["key"] for f in ranked] == ["measured-low", "unmeasured-huge-proxy"]


def test_unmeasured_band_orders_by_proxy_score_descending():
    low_proxy = _report_dict(_finding(signal="low", proxy_score=1.0))
    high_proxy = _report_dict(_finding(signal="high", proxy_score=9.0))
    no_proxy = _report_dict(_finding(signal="none", proxy_score=None))

    ranked = scan._rank_findings([low_proxy, no_proxy, high_proxy])

    assert [f["key"] for f in ranked] == ["high", "low", "none"]


# --- (c) backlog cost_estimate rate parsing, incl. monthly -> weekly ---------

@pytest.mark.parametrize(
    "text, expected_weekly",
    [
        ("$40/week", 40.0),
        ("40/week", 40.0),
        ("$40/wk", 40.0),
        ("$434.5/month", 100.0),
        ("120 tokens/week", 120.0),
    ],
)
def test_backlog_cost_rate_parses_a_matching_rate(text, expected_weekly):
    cost = scan.parse_backlog_cost_rate(text)
    assert cost.measured
    assert cost.usd_per_week == pytest.approx(expected_weekly, rel=1e-3)


def test_backlog_cost_rate_token_rate_basis_names_the_unit():
    cost = scan.parse_backlog_cost_rate("120 tokens/week")
    assert cost.measured
    assert "tokens" in cost.basis


# --- (d) a non-matching value never yields a guessed measurement ------------

@pytest.mark.parametrize(
    "text",
    [
        "not estimable: n/a",
        "somewhere between $20 and $80 a week",
        "roughly significant, hard to quantify",
        "40 EUR/week",  # non-USD currency: not the pinned pattern
        "",
        None,
    ],
)
def test_backlog_cost_rate_non_matching_text_is_never_measured(text):
    cost = scan.parse_backlog_cost_rate(text)
    assert cost.measured is False
    assert cost.usd_per_week is None


# --- (e) recommended-next-step vocabulary is closed and rendered as text ----

def test_render_next_step_covers_the_closed_vocabulary_as_text_only():
    f = _report_dict(_finding())
    assert scan._render_next_step("self-improvement", f) == "Skill(self-improvement)"
    assert scan._render_next_step("planner", f) == "Skill(planner)"
    file_diff = scan._render_next_step("file-difficulty", f)
    assert file_diff.startswith("scripts/file-difficulty.py")
    assert "--target" in file_diff and "--ground" in file_diff and "--cost" in file_diff


def test_render_next_step_only_ever_returns_a_string():
    for step in scan.RECOMMENDED_NEXT_STEPS:
        assert isinstance(scan._render_next_step(step, _report_dict(_finding())), str)


# --- end-to-end store -> CLI, both producers, both bands --------------------

def test_report_json_spans_both_producers_and_both_bands(capsys):
    rc = scan.main([
        "report", "--store", str(FIXTURES / "improvement_scan_mixed_store.jsonl"),
        "--format", "json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    findings = result["findings"]

    measured = [f for f in findings if f["cost_signal"]["measured"]]
    unmeasured = [f for f in findings if not f["cost_signal"]["measured"]]
    assert measured and unmeasured
    assert all(findings.index(a) < findings.index(b) for a in measured for b in unmeasured)

    keys = [
        (f["cost_signal"]["usd_per_week"] or 0, f["cost_signal"]["attention_per_week"] or 0,
         f["cost_signal"]["stability_per_week"] or 0)
        for f in measured
    ]
    assert keys == sorted(keys, reverse=True)

    assert {f["recommended_next_step"] for f in findings} <= set(scan.RECOMMENDED_NEXT_STEPS)
    assert {f["source"] for f in findings} >= {"backlog-item", "telemetry-pattern"}


def test_report_markdown_never_interleaves_bands_and_follows_outcome_format(capsys):
    rc = scan.main([
        "report", "--store", str(FIXTURES / "improvement_scan_mixed_store.jsonl"),
        "--format", "md",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    measured_at = out.index("## Measured cost signal")
    unmeasured_at = out.index("## No measured cost signal")
    assert measured_at < unmeasured_at
    assert "\U0001F600" not in out  # no emoji
    assert "**" in out  # load-bearing figures are bold


def test_report_is_deterministic_given_the_same_store(capsys):
    args = ["report", "--store", str(FIXTURES / "improvement_scan_mixed_store.jsonl"), "--format", "json"]
    scan.main(args)
    first = capsys.readouterr().out
    scan.main(args)
    second = capsys.readouterr().out
    assert first == second


# --- (f) no filing, no dispatch, no network from the report code path -------

def test_report_functions_never_shell_out_or_reach_the_network():
    for fn in (
        scan._report_findings,
        scan._decode_finding_detail,
        scan._cost_sort_key,
        scan._proxy_sort_key,
        scan._rank_findings,
        scan._render_next_step,
        scan._format_cost,
        scan._render_finding_md,
        scan._render_markdown,
        scan._render_json,
        scan._cmd_report,
    ):
        assert impure_names(fn) == set()
