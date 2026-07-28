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
    tmp_path, so no test reads or mutates this machine's ledgers."""
    mod = _load_module()
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(mod, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
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


# ----------------------------------------------- 2. the two opposed flag tests
#
# The measured W29→W30 shape, from the plan's own figures: 452M tok / $456 over
# 7 active days → 912M / $865 over 5 active days, with $/prompt FALLING
# 3.38 → 2.59. Prompt counts are chosen so the fixture's own cost/prompts
# reproduces those two $/prompt figures exactly rather than asserting them.
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


# ------------------------------------------------------------ 3. the baseline

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


# ------------------------------------------------------------- 4. the render

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


# --------------------------------------------------------- 5. the calibrator

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
