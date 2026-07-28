"""spend-regression-investigate.py: driver ranking by movement (not absolute
size), the OLS-slope dollar-impact pricing for count drivers, the direct-delta
pricing for the dollar-denominated driver, and the insufficient-history error
paths.

Follows test_quality_regression_investigate.py's shape: a `_load_module()`
by-path loader, hand-built fixture rows (here: raw policy-ledger session rows
rather than a git repo), and a `main()` CLI section using capsys.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "spend-regression-investigate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("spend_regression_investigate_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sri():
    return _load_module()


# ------------------------------------------------------------- row fixture

def _row(session_id: str, last_ts: str, *, cost_usd: float = 0.0,
        cache_read_usd: float = 0.0, inherit_opus: int = 0, clusters: int = 0,
        rework_edits: int = 0, replans: int = 0, subagent_failures: int = 0) -> dict:
    return {
        "session_id": session_id,
        "last_ts": last_ts,
        "cost_usd": cost_usd,
        "cache_read_usd": cache_read_usd,
        "agent_spawns": {"inherit_opus": inherit_opus},
        "missed_delegation_clusters": clusters,
        "effectiveness": {
            "rework_edits": rework_edits,
            "replans": replans,
            "subagent_failures": subagent_failures,
        },
    }


NOW = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)


# --------------------------------------------------------- 1. movement ranking

def test_ranks_a_moving_driver_and_omits_a_flat_one(sri):
    # inherit_opus rises 1->7 across the two windows and correlates perfectly
    # with cost_usd across all four rows (slope exactly 1.0, cost = 1 + count):
    # a real, priceable driver. rework_edits is 5 in every row -- large in
    # absolute terms but flat, so it carries no movement to report.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0,
                  inherit_opus=0, rework_edits=5),
        "p2": _row("p2", "2026-07-15T00:00:00+00:00", cost_usd=2.0,
                  inherit_opus=1, rework_edits=5),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=4.0,
                  inherit_opus=3, rework_edits=5),
        "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=5.0,
                  inherit_opus=4, rework_edits=5),
    }

    result = sri.investigate(rows, days=7, now=NOW)

    ranked_keys = [r["key"] for r in result["ranked"]]
    assert "inherit-opus" in ranked_keys
    assert "rework-edits" not in ranked_keys
    inherit = next(r for r in result["ranked"] if r["key"] == "inherit-opus")
    assert inherit["delta"] == pytest.approx(6.0)
    assert inherit["slope"] == pytest.approx(1.0)
    assert inherit["dollar_impact"] == pytest.approx(6.0)


def test_flat_driver_is_reported_omitted_with_a_reason(sri):
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0,
                  inherit_opus=0, rework_edits=5),
        "p2": _row("p2", "2026-07-15T00:00:00+00:00", cost_usd=2.0,
                  inherit_opus=1, rework_edits=5),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=4.0,
                  inherit_opus=3, rework_edits=5),
        "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=5.0,
                  inherit_opus=4, rework_edits=5),
    }

    result = sri.investigate(rows, days=7, now=NOW)

    omitted = {o["key"]: o["reason"] for o in result["omitted"]}
    assert omitted["rework-edits"] == "no movement"


def test_ranking_is_by_dollar_impact_descending(sri):
    # inherit_opus moves a lot (delta 30) but correlates weakly with cost
    # (slope 0.2 -> impact 6); missed-delegation moves a little (delta 1) but
    # correlates strongly (slope 47 -> impact 47) -- the winner must be
    # decided by the priced impact, not by raw movement size. The single
    # clusters=1 row carries inherit_opus=10, the exact mean of the other five
    # rows' inherit_opus values, so its cost jump contributes zero to
    # inherit-opus's own slope (its (x - xbar) term is exactly 0) and the two
    # drivers' prices stay independently attributable.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0, inherit_opus=0),
        "p2": _row("p2", "2026-07-15T00:00:00+00:00", cost_usd=2.0, inherit_opus=5),
        "p3": _row("p3", "2026-07-16T00:00:00+00:00", cost_usd=3.0, inherit_opus=10),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=4.0, inherit_opus=15),
        "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=5.0, inherit_opus=20),
        "c3": _row("c3", "2026-07-24T00:00:00+00:00", cost_usd=50.0,
                  inherit_opus=10, clusters=1),
    }

    result = sri.investigate(rows, days=7, now=NOW)

    assert [r["key"] for r in result["ranked"]] == ["missed-delegation", "inherit-opus"]
    by_key = {r["key"]: r for r in result["ranked"]}
    assert by_key["inherit-opus"]["delta"] == pytest.approx(30.0)
    assert by_key["missed-delegation"]["delta"] == pytest.approx(1.0)
    assert by_key["inherit-opus"]["dollar_impact"] == pytest.approx(6.0)
    assert by_key["missed-delegation"]["dollar_impact"] == pytest.approx(47.0)


# --------------------------------------------------- 2. dollar-denominated driver

def test_cache_read_cost_is_priced_by_raw_delta_not_regression(sri):
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cache_read_usd=1.0),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cache_read_usd=9.0),
    }

    result = sri.investigate(rows, days=7, now=NOW)

    cache = next(r for r in result["ranked"] if r["key"] == "cache-read-share")
    assert cache["dollar_impact"] == pytest.approx(8.0)
    assert cache["slope"] is None


# ------------------------------------------------------ 3. no positive slope

def test_driver_with_no_positive_cost_relationship_is_omitted(sri):
    # replans rises but cost_usd is identical (and non-zero) in every row --
    # zero cost variance means the slope is undefined, not merely small.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=3.0, replans=0),
        "p2": _row("p2", "2026-07-15T00:00:00+00:00", cost_usd=3.0, replans=0),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=3.0, replans=5),
        "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=3.0, replans=6),
    }

    result = sri.investigate(rows, days=7, now=NOW)

    ranked_keys = [r["key"] for r in result["ranked"]]
    assert "replans" not in ranked_keys
    omitted = {o["key"]: o["reason"] for o in result["omitted"]}
    assert "no measurable positive cost relationship" in omitted["replans"]


# --------------------------------------------------- 4. insufficient history

def test_empty_ledger_raises_insufficient_history(sri):
    with pytest.raises(sri.InvestigationError, match="no rows"):
        sri.investigate({}, days=7, now=NOW)


def test_single_window_ledger_raises_insufficient_history(sri):
    # every row falls in the current window; the previous window is empty.
    rows = {
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=1.0),
        "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=1.0),
    }

    with pytest.raises(sri.InvestigationError, match="only one of the two"):
        sri.investigate(rows, days=7, now=NOW)


# -------------------------------------------------------------- 5. format_report

# Both rows below make the fixture used by the two tests: inherit-opus and
# missed-delegation both move and both price out positive, and their combined
# dollar_impact (~$11.93) genuinely exceeds the window's actual cost_usd
# movement (+$6.00) -- the double-counting the module's own docstring warns
# about (independent whole-ledger slopes fit against one shared cost_usd).
# If this over-claim ever stopped holding for these rows, the fixture itself
# would need to change; the two tests below are what would go RED, not this
# comment.
_REPORT_FIXTURE_ROWS = {
    "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0, inherit_opus=0, clusters=0),
    "p2": _row("p2", "2026-07-15T00:00:00+00:00", cost_usd=2.0, inherit_opus=1, clusters=0),
    "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=4.0, inherit_opus=3, clusters=2),
    "c2": _row("c2", "2026-07-23T00:00:00+00:00", cost_usd=5.0, inherit_opus=4, clusters=3),
}


def test_report_header_carries_the_actual_cost_delta(sri):
    result = sri.investigate(_REPORT_FIXTURE_ROWS, days=7, now=NOW)
    assert result["prev_cost"] == pytest.approx(3.0)
    assert result["cur_cost"] == pytest.approx(9.0)

    report = sri.format_report(result, days=7)

    assert "Actual cost_usd movement: $3.00" in report
    assert "$9.00" in report
    assert "+$6.00" in report


def test_report_carries_a_non_additivity_caveat_when_impacts_are_ranked(sri):
    result = sri.investigate(_REPORT_FIXTURE_ROWS, days=7, now=NOW)
    ranked_sum = sum(r["dollar_impact"] for r in result["ranked"])
    actual_delta = result["cur_cost"] - result["prev_cost"]
    # sanity: this fixture must genuinely over-claim, or the caveat it drives
    # isn't testing what it claims to.
    assert ranked_sum > actual_delta

    report = sri.format_report(result, days=7)

    assert "not additive" in report.lower()


def test_count_driver_comma_grouped_and_usd_driver_carries_dollar_sign(sri):
    # inherit_opus's cur value (1,201) crosses 1000, so a reverted {:.3g}
    # format would render it in scientific notation (1.2e+03) instead of
    # comma-grouped -- a three-digit fixture would pass under both the old
    # and new formatting and prove nothing.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0,
                  inherit_opus=1, cache_read_usd=1.0),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=3000.0,
                  inherit_opus=1201, cache_read_usd=21.0),
    }

    result = sri.investigate(rows, days=7, now=NOW)
    ranked_keys = [r["key"] for r in result["ranked"]]
    assert "inherit-opus" in ranked_keys
    assert "cache-read-share" in ranked_keys

    report = sri.format_report(result, days=7)

    assert "1→1,201" in report
    assert "$1.00→$21.00" in report


def test_report_header_signs_a_negative_cost_delta(sri):
    # spend FELL between the two windows. The header builds its sign by hand
    # (delta_sign plus abs(cost_delta)) rather than with a `:+` format, so the
    # negative branch is a path of its own -- hardcoding delta_sign = "+",
    # which would silently mis-sign every drop, leaves every other test in
    # this file green.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=9.0),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=3.0),
    }

    result = sri.investigate(rows, days=7, now=NOW)
    assert result["prev_cost"] == pytest.approx(9.0)
    assert result["cur_cost"] == pytest.approx(3.0)

    report = sri.format_report(result, days=7)

    assert "Actual cost_usd movement: $9.00 → $3.00 (-$6.00)" in report
    assert "(+$6.00)" not in report


def test_format_report_no_ranked_impacts_branch(sri):
    # every driver's per-session value is identical across both windows --
    # zero movement for all six, so nothing is ranked. The header and the
    # no-candidate line must still print, the non-additivity caveat must NOT
    # (there is nothing to caveat), and the omitted section must still list
    # all six drivers. Today's behaviour is already correct -- this pins it,
    # not changes it.
    rows = {
        "p1": _row("p1", "2026-07-14T00:00:00+00:00", cost_usd=1.0),
        "c1": _row("c1", "2026-07-22T00:00:00+00:00", cost_usd=1.0),
    }

    result = sri.investigate(rows, days=7, now=NOW)
    assert result["ranked"] == []

    report = sri.format_report(result, days=7)

    assert "Actual cost_usd movement: $1.00" in report
    assert "(no candidate driver rose between the two windows)" in report
    assert "not additive" not in report.lower()
    assert f"Not ranked ({len(result['omitted'])}): " in report


# --------------------------------------------------------------------- 6. CLI

def test_main_help_exits_zero(sri):
    with pytest.raises(SystemExit) as exc:
        sri.main(["--help"])
    assert exc.value.code == 0


def test_main_empty_ledger_exits_nonzero_with_message(tmp_path, sri, capsys):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")

    rc = sri.main(["--ledger", str(ledger)])

    assert rc == 1
    assert "no rows" in capsys.readouterr().err


def test_main_missing_ledger_file_exits_nonzero_with_message(tmp_path, sri, capsys):
    rc = sri.main(["--ledger", str(tmp_path / "does-not-exist.jsonl")])

    assert rc == 1
    assert "no rows" in capsys.readouterr().err


def test_main_never_touches_the_real_ledger_path(tmp_path, sri, monkeypatch):
    # --ledger must fully override the module-level LEDGER global rather than
    # being read alongside it -- guard against a future refactor that keeps a
    # stray reference to the real path.
    real_ledger = tmp_path / "real-should-not-be-touched.jsonl"
    monkeypatch.setattr(sri.policy_scorecard, "LEDGER", real_ledger)
    fixture_ledger = tmp_path / "fixture.jsonl"
    fixture_ledger.write_text(
        json.dumps({"session_id": "s1", "last_ts": "2026-07-22T00:00:00+00:00",
                    "cost_usd": 1.0}) + "\n",
        encoding="utf-8",
    )

    sri.main(["--ledger", str(fixture_ledger)])

    assert not real_ledger.exists()
    assert sri.policy_scorecard.LEDGER == fixture_ledger
