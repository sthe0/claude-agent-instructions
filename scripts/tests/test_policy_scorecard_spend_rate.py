"""policy-scorecard.py Stage 7: the spend-RATE (burn-rate) axis.

Every pre-existing scorecard metric is normalised per session or per prompt. In
the 2026-W29→W30 event all of them improved while total consumption roughly
doubled, so none of them could fire; the un-normalised per-active-day rate is
the axis that makes such an event visible.

The two load-bearing tests here are OPPOSED on purpose:
`test_benign_volume_growth_stays_silent` replays the measured event, which the
session's own analysis established was volume growth and not degradation, and
`test_real_degradation_fires` replays the same rate movement with a
non-improving $/prompt. A single silence test would be satisfied by a flag that
never fires at all; a single firing test would certify a known-benign event as
the specification.

Every test runs against a synthetic fixture ledger in tmp_path — never the
machine's real ledger, which a bare `policy-scorecard.py` run would upsert.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "policy-scorecard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("policy_scorecard_spend_rate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ps(monkeypatch, tmp_path):
    """A fresh module instance with every real-machine path redirected into
    tmp_path, so no test reads or mutates this machine's ledgers.

    SPAWN_LEDGER included: `scorecard()` defaults `spawn_rows` to reading it, and
    the tests below assert on the whole rendered Flags section. Left live, a real
    failure-rate flag on this machine's telemetry would turn them red — the suite
    would break precisely when the instrument works."""
    mod = _load_module()
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(mod, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(mod, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")
    return mod


NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc)


def _row(session_id: str, first_ts: str, last_ts: str, *,
         cost: float = 0.0, prompts: int = 0, tokens: int = 0) -> dict:
    """One ledger row. `tokens` lands in the opus `in` bucket; the rate sums
    every bucket and field, so which one it lands in does not matter."""
    return {
        "session_id": session_id,
        "project": "proj",
        "date": last_ts[:10],
        "first_ts": first_ts,
        "last_ts": last_ts,
        "instructions_head": None,
        "mtime": 0.0,
        "model_tokens": {"opus": {"in": tokens, "out": 0, "cache_read": 0, "cache_create": 0},
                         "sonnet": {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0},
                         "haiku": {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0}},
        "cost_usd": cost,
        "cache_read_usd": 0.0,
        "main_read_bash": 0,
        "agent_spawns": {"total": 0, "opus": 0, "sonnet": 0, "haiku": 0,
                         "no_explicit_model": 0, "inherit_opus": 0},
        "missed_delegation_clusters": 0,
        "attention": {"askq": 0, "prompts": prompts, "interrupts": 0, "corrections": 0},
        "user_signals": {"n_user_corrections": 0, "n_user_questions": 0,
                         "n_freetext_askuser_answers": 0, "n_interrupts": 0},
        "effectiveness": {"resolution_confirmed": 0, "replans": 0, "overcome_difficulty": 0,
                          "subagent_failures": 0, "rework_edits": 0},
        "quality_rating": None,
        "quality_note": None,
    }


def _at(days_ago: float, hour: int = 12) -> str:
    t = (NOW - dt.timedelta(days=days_ago)).replace(hour=hour, minute=0,
                                                    second=0, microsecond=0)
    return t.isoformat()


# ------------------------------------------------------- 1. active-day counting

def test_active_days_counts_only_days_with_a_session(ps):
    """Empty days inside the window are not active days: three sessions on two
    calendar days are two active days, not the window's seven."""
    rows = [_row("a", _at(3), _at(3)), _row("b", _at(3, hour=18), _at(3, hour=19)),
            _row("c", _at(1), _at(1))]

    assert ps._active_days(rows) == 2


def test_active_days_uses_message_timestamps_not_the_row_date(ps):
    """A session running 23:00→01:00 spans two days of activity but carries a
    single `date` (= last_ts.date()). Counting off `date` would say 1."""
    row = _row("overnight",
               "2026-07-20T23:00:00+00:00", "2026-07-21T01:00:00+00:00")
    assert row["date"] == "2026-07-21"

    assert ps._active_days([row]) == 2


def test_active_days_clamped_to_the_window(ps):
    """A session that began before the window contributes only its in-window
    days — which is what makes `active_days <= window length` an invariant."""
    lo, hi = NOW - dt.timedelta(days=2), NOW
    row = _row("long", (NOW - dt.timedelta(days=30)).isoformat(),
               (NOW - dt.timedelta(hours=1)).isoformat())

    # Without the clamp this row alone would contribute 31 days.
    assert ps._active_days([row], lo, hi) == ps._window_span_days(lo, hi) == 3


def test_active_days_never_exceeds_the_window_span(ps):
    """The sanity invariant. A rate whose denominator exceeds its own window is
    impossible by construction; asserting it is what would have caught the
    neighbouring subagent_failures defect in its first week (that counter's
    "rate" reached 2.65 and nobody noticed for six weeks).

    The ceiling is the window's calendar SPAN, not `--days`: a 7×24h window
    ending mid-afternoon straddles 8 dates. Sessions here all start 60 days
    before the window, so without the clamp `active_days` would be ~61."""
    days = 7
    lo, hi = NOW - dt.timedelta(days=days), NOW
    rows = [_row(f"s{i}", (NOW - dt.timedelta(days=60)).isoformat(),
                 (NOW - dt.timedelta(hours=i)).isoformat()) for i in range(5)]

    span = ps._window_span_days(lo, hi)

    assert span == days + 1
    assert ps._active_days(rows, lo, hi) == span
    assert ps._aggregate(rows, lo, hi)["active_days"] <= span


def test_window_span_is_exact_when_the_window_is_midnight_aligned(ps):
    lo = dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc)
    hi = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)

    assert ps._window_span_days(lo, hi) == 7


def test_active_days_empty_window_is_zero_and_rates_do_not_divide(ps):
    agg = ps._aggregate([], NOW - dt.timedelta(days=7), NOW)

    assert agg["active_days"] == 0
    assert agg["window_days"] == 8
    assert agg["cost_per_active_day"] == 0.0
    assert agg["tokens_per_active_day"] == 0.0


def test_aggregate_reports_rate_over_the_same_population(ps):
    """cost, prompts and active_days all come from the rows of this window."""
    lo, hi = NOW - dt.timedelta(days=7), NOW
    rows = [_row("a", _at(3), _at(3), cost=10.0, prompts=4, tokens=1_000_000),
            _row("b", _at(1), _at(1), cost=30.0, prompts=6, tokens=3_000_000)]

    agg = ps._aggregate(rows, lo, hi)

    assert agg["active_days"] == 2
    assert agg["cost_usd"] == 40.0
    assert agg["cost_per_active_day"] == 20.0
    assert agg["tokens_per_active_day"] == 2_000_000
    assert agg["cost_per_prompt"] == 4.0


# ------------------------------ 2. numerator and denominator share one extent

def test_rate_numerator_excludes_spend_earned_before_the_window(ps):
    """The MF-1 defect. `active_days` is clamped to the window, so a session
    that began before `lo` contributes only its in-window days to the
    DENOMINATOR — but the whole of its `cost_usd` used to land in the
    NUMERATOR. Same rows, two different time extents: the rate then reports
    weeks of spending as if it had all happened inside a 7-day window.

    Here a 22-day session carrying $1000 has 2 of its days inside the window,
    so $90.91 of it belongs to this window's rate — not the full $1000.
    """
    lo, hi = NOW - dt.timedelta(days=7), NOW
    straddler = _row("straddler", _at(27), _at(6), cost=1000.0, prompts=50,
                     tokens=22_000_000)
    inside = _row("inside", _at(1), _at(1), cost=100.0, prompts=10,
                  tokens=1_000_000)

    agg = ps._aggregate([straddler, inside], lo, hi)

    assert agg["active_days"] == 3            # 07-21, 07-22 from the straddler; 07-27
    assert agg["cost_per_active_day"] == pytest.approx((1000.0 * 2 / 22 + 100.0) / 3)
    assert agg["tokens_per_active_day"] == pytest.approx((22_000_000 * 2 / 22
                                                          + 1_000_000) / 3)
    assert agg["cost_usd"] == 1100.0          # whole-session sum: unchanged
    assert agg["cost_usd_in_window"] == pytest.approx(1000.0 * 2 / 22 + 100.0)


def test_a_session_wholly_inside_the_window_is_apportioned_in_full(ps):
    """Apportionment must be a no-op for the ordinary case, or every rate in
    the ledger's history would shift."""
    lo, hi = NOW - dt.timedelta(days=7), NOW
    rows = [_row("a", _at(3), _at(3), cost=10.0, prompts=4, tokens=1_000_000),
            _row("b", _at(2, hour=23), _at(1, hour=1), cost=30.0, prompts=6)]

    agg = ps._aggregate(rows, lo, hi)

    assert agg["cost_per_active_day"] == 40.0 / agg["active_days"]
    assert agg["cost_usd_in_window"] == agg["cost_usd"] == 40.0


def test_in_window_cost_never_exceeds_the_whole_session_cost(ps):
    """The share is a fraction of the row's own days, so it cannot manufacture
    spend. Swept over every start offset that straddles the window edge."""
    lo, hi = NOW - dt.timedelta(days=7), NOW

    for start in range(0, 30):
        row = _row(f"s{start}", _at(start), _at(1), cost=100.0, prompts=1)
        agg = ps._aggregate([row], lo, hi)

        assert 0.0 < agg["cost_usd_in_window"] <= agg["cost_usd"] == 100.0


def test_the_further_back_a_session_began_the_less_of_it_is_this_window(ps):
    """The same $600 spread over more days is a lower daily burn, so the share
    landing inside a fixed window must FALL as `first_ts` is dragged earlier.
    Under the defect it was constant: the numerator ignored `first_ts`
    entirely, and a month-long session's whole cost counted as this week's."""
    lo, hi = NOW - dt.timedelta(days=7), NOW
    # last_ts pinned, so the in-window days (and the denominator) never change.
    rates = [ps._aggregate([_row(f"s{start}", _at(start), _at(6), cost=600.0,
                                 prompts=1)], lo, hi)["cost_per_active_day"]
             for start in (8, 15, 30, 60)]

    assert rates == sorted(rates, reverse=True)
    assert len(set(rates)) == len(rates)


# ----------------------------------------------- 3. the two opposed flag tests
#
# The measured W29→W30 shape, from the plan's own figures: 452M tok / $456 over
# 7 active days → 912M / $865 over 5 active days, with $/prompt FALLING
# 3.38 → 2.59. Prompt counts are chosen so the fixture's own cost/prompts
# reproduces those two $/prompt figures exactly rather than asserting them.
# These are pre-apportionment figures — they were measured before the rate's
# numerator was clamped to the window. They are kept verbatim because what they
# exercise is the flag PREDICATE over a fixed pair of aggregates; the tests
# below pass the aggregates in directly and never re-derive them from a ledger.
W29 = dict(cost=456.0, tokens=452_000_000, active_days=7, prompts=135)   # $3.378/prompt
W30 = dict(cost=865.0, tokens=912_000_000, active_days=5, prompts=334)   # $2.590/prompt


def _neutral() -> dict:
    """The keys the other, pre-existing flags read — so `_flags` can be called
    with a spend-rate aggregate without any of them firing."""
    return {
        "sessions": 10, "spawns_total": 0, "inherit_opus_rate": 0.0, "inherit_opus": 0,
        "clusters_per_session": 0.0, "clusters": 0, "cost_per_session": 1.0,
        "resolution_rate": 0.9, "avg_quality": None, "n_rated": 0,
    }


def _agg(spec: dict) -> dict:
    """A window aggregate carrying only what the spend-rate flag reads."""
    return {
        "active_days": spec["active_days"],
        "cost_usd": spec["cost"],
        "cost_per_active_day": spec["cost"] / spec["active_days"],
        "tokens_per_active_day": spec["tokens"] / spec["active_days"],
        "cost_per_prompt": spec["cost"] / spec["prompts"],
    }


def test_the_replay_really_is_a_rate_rise(ps):
    """Guards the two tests below: if the fixture stopped clearing the rate
    threshold, `stays_silent` would pass for the wrong reason."""
    cur, prev = _agg(W30), _agg(W29)
    baseline = prev["cost_per_active_day"]

    assert cur["cost_per_active_day"] / baseline > ps.SPEND_RATE_FACTOR
    assert cur["cost_per_prompt"] < prev["cost_per_prompt"]  # and $/prompt fell


def test_benign_volume_growth_stays_silent(ps):
    """The measured W29→W30 event. The rate roughly doubled and the session's
    own diagnosis was volume, not degradation — $/prompt fell 3.38→2.59. A
    rate-only predicate calls that a problem; the conjunction does not."""
    cur, prev = _agg(W30), _agg(W29)

    flag = ps._spend_rate_flag(cur, prev, prev["cost_per_active_day"])

    assert flag is None
    assert ps._flags(cur | _neutral(), prev | _neutral(),
                     spend_baseline=prev["cost_per_active_day"]) == []


def test_real_degradation_fires(ps):
    """The same rate movement with $/prompt flat instead of falling."""
    prev = _agg(W29)
    cur = _agg(W30) | {"cost_per_prompt": prev["cost_per_prompt"]}

    flag = ps._spend_rate_flag(cur, prev, prev["cost_per_active_day"])

    assert flag is not None
    assert "per active day" in flag
    assert "$/prompt is not improving" in flag


def test_real_degradation_fires_when_cost_per_prompt_rises(ps):
    prev = _agg(W29)
    cur = _agg(W30) | {"cost_per_prompt": prev["cost_per_prompt"] * 1.4}

    assert ps._spend_rate_flag(cur, prev, prev["cost_per_active_day"]) is not None


def test_rate_only_mode_fires_on_the_benign_replay(ps, monkeypatch):
    """SPEND_RATE_FLAG_MODE is a real switch, not decoration: under 'rate_only'
    the same benign replay that stays silent above does fire."""
    monkeypatch.setattr(ps, "SPEND_RATE_FLAG_MODE", "rate_only")
    cur, prev = _agg(W30), _agg(W29)

    flag = ps._spend_rate_flag(cur, prev, prev["cost_per_active_day"])

    assert flag is not None
    assert "$/prompt" not in flag


def test_rate_below_the_factor_never_fires(ps):
    prev = _agg(W29)
    cur = _agg(W29) | {"cost_per_active_day": prev["cost_per_active_day"] * 1.2,
                       "cost_per_prompt": prev["cost_per_prompt"] * 2}

    assert ps._spend_rate_flag(cur, prev, prev["cost_per_active_day"]) is None


# ------------------------------------------------------------ 4. the baseline

def _ledger(ps, rows: list[dict]) -> dict[str, dict]:
    ps.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ps.LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return ps.load_ledger()


def test_baseline_is_none_with_too_little_history(ps):
    """A window with fewer than SPEND_RATE_MIN_BASELINE_WINDOWS trailing windows
    of activity produces no baseline, so no flag — an honest silence rather than
    a spurious ranking off one data point."""
    rows = _ledger(ps, [_row("a", _at(2), _at(2), cost=100.0, prompts=10)])

    assert ps._spend_rate_baseline(rows, NOW, 7) is None


def test_baseline_is_the_median_of_the_trailing_windows(ps):
    """Four trailing 7d windows at $10/$20/$30/$40 per active day (one active
    day each) -> median 25."""
    rows = _ledger(ps, [
        _row("w1", _at(8), _at(8), cost=10.0, prompts=1),
        _row("w2", _at(15), _at(15), cost=20.0, prompts=1),
        _row("w3", _at(22), _at(22), cost=30.0, prompts=1),
        _row("w4", _at(29), _at(29), cost=40.0, prompts=1),
    ])

    assert ps._spend_rate_baseline(rows, NOW, 7) == 25.0


def test_baseline_ignores_windows_with_no_activity(ps):
    """Three active trailing windows still make a baseline; two do not."""
    rows = _ledger(ps, [
        _row("w1", _at(8), _at(8), cost=10.0, prompts=1),
        _row("w2", _at(15), _at(15), cost=20.0, prompts=1),
    ])
    assert ps._spend_rate_baseline(rows, NOW, 7) is None

    rows = _ledger(ps, [
        _row("w1", _at(8), _at(8), cost=10.0, prompts=1),
        _row("w2", _at(15), _at(15), cost=20.0, prompts=1),
        _row("w3", _at(22), _at(22), cost=30.0, prompts=1),
    ])
    assert ps._spend_rate_baseline(rows, NOW, 7) == 20.0


def test_flag_absent_without_a_baseline(ps):
    cur, prev = _agg(W30), _agg(W29)

    assert ps._spend_rate_flag(cur, prev, None) is None


# ------------------------------------------------------------- 5. the render

def test_scorecard_renders_the_rate_unconditionally(ps):
    """The figure is informational and always shown; only the flag is gated."""
    rows = _ledger(ps, [_row("a", _at(2), _at(2), cost=12.0, prompts=3,
                             tokens=5_000_000)])

    out = ps.scorecard(rows, days=7, project=None)

    assert "per active day" in out
    assert "- none past threshold this window." in out  # no baseline -> no flag


def test_scorecard_renders_the_rate_on_an_empty_ledger(ps):
    out = ps.scorecard({}, days=7, project=None)

    assert "per active day" in out


def test_existing_flags_unaffected_by_the_new_signature(ps):
    """Backward compatibility: `_flags` still works with its old arities."""
    assert ps._flags(_neutral(), _neutral()) == []
    assert ps._flags(_neutral(), _neutral(), None, None) == []


# --------------------------------------------------------- 6. the calibrator

def test_calibrate_reports_insufficient_history_honestly(ps):
    rows = _ledger(ps, [_row("a", _at(2), _at(2), cost=1.0, prompts=1)])

    out = ps.calibrate_spend_rate(rows, 7)

    assert "too short" in out


def test_calibrate_reports_the_ratio_distribution(ps):
    rows = _ledger(ps, [
        _row(f"s{i}", _at(i), _at(i), cost=10.0 + i, prompts=2)
        for i in range(1, 45)
    ])

    out = ps.calibrate_spend_rate(rows, 7)

    assert "rolling samples" in out
    # The shipped constant must be locatable in the printed distribution —
    # a calibration whose own threshold is not on the chart cannot be checked.
    assert f"factor {ps.SPEND_RATE_FACTOR}:" in out
    assert "<- shipped" in out
    assert "distinct episode(s)" in out


def test_calibrate_counts_episodes_not_overlapping_samples(ps):
    """Adjacent rolling windows overlap by days-1, so one spending episode
    produces a run of adjacent firing dates. Reporting the raw sample count as
    the firing frequency would over-state it several-fold."""
    rows = _ledger(ps, [
        # A quiet baseline, then one short expensive burst.
        *[_row(f"q{i}", _at(i), _at(i), cost=1.0, prompts=1) for i in range(8, 45)],
        _row("burst", _at(12), _at(12), cost=500.0, prompts=1),
    ])

    out = ps.calibrate_spend_rate(rows, 7)
    line = next(l for l in out.splitlines() if l.strip().startswith("factor 2.0:"))

    n_samples = int(line.split()[2].split("/")[0])
    n_episodes = int(line.split("in")[1].split()[0])
    assert n_samples > n_episodes == 1


# ------------------------------------------- 7. the selection rule (MF-2)

def test_the_shipped_factor_is_what_the_rule_picks(ps):
    """The constant is derived, not chosen: whatever `_factor_by_quantile`
    returns for the snapshot named in its comment is what ships. This guards
    the rule and the constant against drifting apart in either direction."""
    # The pinned snapshot's own distribution, from --calibrate-until 2026-07-28.
    pinned = [0.65, 0.71, 0.78, 0.80, 0.85, 0.87, 0.88, 0.92, 1.09, 1.24,
              1.29, 1.49, 1.51, 1.84, 1.96, 1.99, 2.06, 2.17, 2.44]

    assert ps._factor_by_quantile(pinned) == ps.SPEND_RATE_FACTOR


def test_the_rule_rounds_up_onto_the_grid(ps):
    """Rounding UP, never down: rounding to nearest would put the threshold
    below the quantile it was calibrated to and raise the firing rate above the
    budget the rule exists to hold."""
    ratios = [1.0] * 9 + [2.01]

    picked = ps._factor_by_quantile(ratios)

    assert picked >= ps._quantile(sorted(ratios), ps.SPEND_RATE_TARGET_QUANTILE)
    assert picked % ps.SPEND_RATE_FACTOR_GRID == pytest.approx(0.0)


def test_both_rules_survive_a_single_sample(ps):
    """A ledger just long enough for one rolling window has no adjacent pair to
    step between. Both rules must still answer, because the calibrator prints
    both on every run and a crash there is a crash of the reproducer the
    SPEND_RATE_FACTOR comment tells the reader to run."""
    assert ps._factor_by_largest_gap([1.7]) == pytest.approx(1.7)
    assert ps._factor_by_quantile([1.7]) == pytest.approx(1.75)


def test_the_quantile_rule_moves_at_most_one_grid_step_per_day(ps):
    """MF-2's real finding: 'largest gap' swung by more than half a point on one
    day of extra data. The shipped rule is a quantile, which by construction
    cannot move more than one grid step when a single sample is appended."""
    base = [0.6, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9, 2.0, 2.1, 2.2]

    for extra in (0.1, 1.0, 2.5, 9.9):
        before = ps._factor_by_quantile(base)
        after = ps._factor_by_quantile(base + [extra])

        assert abs(after - before) <= ps.SPEND_RATE_FACTOR_GRID + 1e-9


def test_calibrate_prints_the_stability_evidence_and_the_rule(ps):
    """The comment claims a rule, a quantile and a stability comparison. A
    reproducer that does not print them leaves the claims uncheckable — which
    is the defect this section exists to close."""
    rows = _ledger(ps, [
        _row(f"s{i}", _at(i), _at(i), cost=10.0 + (i % 5) * 20, prompts=2)
        for i in range(1, 45)
    ])

    out = ps.calibrate_spend_rate(rows, 7)

    assert "selection rule:" in out
    assert f"q{int(ps.SPEND_RATE_TARGET_QUANTILE * 100)}" in out
    assert "widest step between adjacent samples" in out
    assert "largest-gap rule" in out
    assert "largest 1-day move" in out


def test_calibrate_until_truncates_to_a_reproducible_snapshot(ps):
    """The pin. The same cutoff must give the same figures however much the
    ledger has grown past it — that is what makes a quoted number checkable."""
    old = [_row(f"s{i}", _at(i), _at(i), cost=10.0 + (i % 5) * 20, prompts=2)
           for i in range(1, 45)]
    cutoff = dt.date.fromisoformat(_at(1)[:10])

    before = ps.calibrate_spend_rate(ps._truncated(_ledger(ps, old), cutoff), 7)
    grown = old + [_row("late", _at(0.5), _at(0.5), cost=9_999.0, prompts=1)]
    after = ps.calibrate_spend_rate(ps._truncated(_ledger(ps, grown), cutoff), 7)

    assert before == after
    assert ps._truncated(_ledger(ps, grown), cutoff).keys() < set(r["session_id"] for r in grown)


# --------------------------------------------- 8. inherit→opus rate (Stage 9)
#
# The prior threshold, 0.5, sat AT `delegatable-work-patterns.md`'s founding
# audit figure (~48%) rather than below it — a threshold at the largest
# instance a policy already produced has decided that instance is acceptable.
# INHERIT_OPUS_RATE_THRESHOLD's derivation (rolling 7d windows over the
# ledger, current-regime q50 grid-rounded to 0.30) lives in the module
# constant's own comment in policy-scorecard.py. The two tests below are
# opposed on purpose, mirroring test_scorecard_flag_routing.py's
# `test_the_threshold_is_where_the_constant_says_it_is` pattern: a single
# firing test would be satisfied by a flag that always fires, and a single
# silent test by one that never does.
#
# Both replay REAL rolling 7d windows from the ledger snapshot the constant
# was derived against, not invented ratios: 67/200 (2026-07-20 window, the
# cited leak, 33.5%) and 34/122 (2026-07-21 window, 27.9% — just under the
# threshold, not zero).

def test_inherit_opus_threshold_fires_on_measured_leak(ps):
    """The measured leak, 67/200 = 33.5%, must fire under the recalibrated
    threshold — the whole point of lowering it off the prior 0.5."""
    cur = _neutral() | {"spawns_total": 200, "inherit_opus": 67,
                        "inherit_opus_rate": 67 / 200}

    flags = ps._flags(cur, _neutral())

    matches = [f for f in flags if f.key == "inherit-opus/7d"]
    assert len(matches) == 1
    assert "67/200" in matches[0].message
    assert "policy says name the tier (delegatable-work-patterns)" in matches[0].message


def test_inherit_opus_rate_just_below_threshold_stays_silent(ps):
    """A real recent window, 34/122 = 27.9%, sits just under the threshold —
    not at zero. A flag that fires on any nonzero rate would pass the test
    above for the wrong reason: it would never have to find the line."""
    cur = _neutral() | {"spawns_total": 122, "inherit_opus": 34,
                        "inherit_opus_rate": 34 / 122}
    assert 0 < cur["inherit_opus_rate"] < ps.INHERIT_OPUS_RATE_THRESHOLD

    flags = ps._flags(cur, _neutral())

    assert not any(f.key == "inherit-opus/7d" for f in flags)
