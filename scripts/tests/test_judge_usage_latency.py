"""judge-usage-report.py --latency — the rate side of the judge execution ledger.

The outcome report says how often a judge failed open. It cannot say whether the
CEILING is the reason, because it pools a judge's whole history: this ledger
spans a ceiling change, and one pooled rate describes neither regime. These tests
pin the four ways the new view could lie about that:

  * pooling two ceilings, which turns "94% under the old ceiling, 7% under the
    new one" into one number that was never true of anything;
  * quietly growing a second population — a filter like ``stage == "call"``
    reads as a synonym for outcomes 4 and 5 and is not one, so the rate printed
    here would disagree with the duration table printed above it;
  * folding budget skips into the calls, which is how the repair this view
    exists to verify could re-achieve "the verdict silently stopped existing"
    through a channel no rate over calls can see;
  * re-implementing the percentiles, so the p90 that sizes a ceiling and the p90
    that reports on it become two different numbers under one name.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "judge_usage_report", SCRIPTS / "judge-usage-report.py"
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["judge_usage_report"] = mod  # dataclass string-annotation resolution needs this
_SPEC.loader.exec_module(mod)

from lib import judge_latency  # noqa: E402
from lib import judge_ledger  # noqa: E402

DAY = 86400.0
T0 = 1_755_000_000.0  # an arbitrary fixed epoch; nothing here depends on the date


def _point(judge, outcome_id, duration, ceiling, ts=T0, hook="turn_end"):
    return mod.JudgePoint(hook, judge, outcome_id, duration, ceiling, ts)


def _completed(judge, duration, ceiling, **kw):
    return _point(judge, "4", duration, ceiling, **kw)


def _timed_out(judge, ceiling, **kw):
    """A timeout is recorded at its ceiling: that is what killed it."""
    return _point(judge, "5", float(ceiling), ceiling, **kw)


def _skipped(judge, ceiling, **kw):
    return _point(judge, mod.BUDGET_SKIP_OUTCOME_ID, None, ceiling, **kw)


def test_two_ceilings_for_one_judge_do_not_pool():
    """The measurement this whole view exists for. approval_ask ran at ceiling 13
    and, after 2026-08-19, at ceiling 30; pooled, its timeout rate is a number
    that describes neither regime — and specifically hides that the fix worked."""
    points = (
        [_timed_out("approval_ask", 13) for _ in range(9)]
        + [_completed("approval_ask", 11.0, 13)]
        + [_timed_out("approval_ask", 30)]
        + [_completed("approval_ask", 17.0, 30) for _ in range(9)]
    )
    groups = mod.latency_by_judge(points)

    assert set(groups) == {("approval_ask", 13), ("approval_ask", 30)}
    assert groups[("approval_ask", 13)].n == 10
    assert groups[("approval_ask", 13)].rate == pytest.approx(0.9)
    assert groups[("approval_ask", 30)].n == 10
    assert groups[("approval_ask", 30)].rate == pytest.approx(0.1)

    # The pooled number — 10 timeouts in 20 calls — is what a judge-only key
    # would have printed, and it is true of neither ceiling.
    pooled = sum(g.timeouts for g in groups.values()) / sum(g.n for g in groups.values())
    assert pooled == pytest.approx(0.5)
    assert all(g.rate != pytest.approx(pooled) for g in groups.values())


def test_a_window_cuts_by_recorded_time_and_refuses_to_guess():
    """"A fresh post-fix window" is the unit the repair is measured in, so the
    cut has to be on the ledger's own timestamps. A point carrying no ts cannot
    be SHOWN to fall inside the window, so it is excluded rather than assumed
    in — including it would let unstamped rows from the sick regime count as
    evidence that the sick regime is over."""
    points = [
        _completed("binary_ask", 5.0, 13, ts=T0 - 10 * DAY),
        _timed_out("binary_ask", 13, ts=T0 - 10 * DAY),
        _completed("binary_ask", 6.0, 13, ts=T0),
        mod.JudgePoint("turn_end", "binary_ask", "4", 7.0, 13, None),
    ]
    assert mod.latency_by_judge(points)[("binary_ask", 13)].n == 4

    windowed = mod.latency_by_judge(points, since=T0 - DAY)
    assert windowed[("binary_ask", 13)].n == 1
    assert windowed[("binary_ask", 13)].timeouts == 0
    assert windowed[("binary_ask", 13)].durations == [6.0]


def test_the_population_is_the_pinned_one_and_not_a_second_filter():
    """Every outcome that is not 4 or 5 (and is not the skip column) contributes
    nothing to n, to the timeouts or to the rate. The fast-failure outcomes are
    the sharp case: they carry a duration two orders of magnitude below a real
    call, so admitting them makes a deader judge look faster AND drags the rate
    down by inflating its denominator."""
    real = [
        _completed("feedback_signal", 12.0, 16),
        _timed_out("feedback_signal", 16),
    ]
    intruders = [
        _point("feedback_signal", "2", None, 16),
        _point("feedback_signal", "6", None, 16),
        _point("feedback_signal", "7", 0.05, 16),
        _point("feedback_signal", "7a", 0.06, 16),
        _point("feedback_signal", "7b", 0.04, 16),
        _point("feedback_signal", "7c", None, 16),
    ]
    clean = mod.latency_by_judge(real)[("feedback_signal", 16)]
    widened = mod.latency_by_judge(real + intruders)[("feedback_signal", 16)]

    assert (clean.n, clean.timeouts) == (2, 1)
    assert clean.rate == pytest.approx(0.5)
    assert (widened.n, widened.timeouts, widened.rate) == (clean.n, clean.timeouts, clean.rate)
    assert widened.durations == clean.durations

    # And the exclusion is load-bearing rather than a no-op on this fixture: had
    # the fast failures been admitted, the rate would have been a fifth of what
    # it is, and the median an order of magnitude lower.
    assert 1 / 5 != pytest.approx(clean.rate)
    assert judge_latency.median([12.0, 16.0, 0.05, 0.06, 0.04]) < clean.median_s


def test_the_estimators_are_the_ones_that_size_the_ceilings():
    """A report whose p90 disagrees with the module that SETS the ceilings would
    be comparing two different numbers under one name. Nearest rank, not
    interpolation and not truncation — the difference is visible on this list."""
    # n=11, chosen so nearest rank (ceil(0.9n)) and truncation (int(0.9n)) pick
    # different ranks — at n=10 they coincide and the contrast below is vacuous.
    durations = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 100.0]
    stats = mod.latency_by_judge(
        [_completed("outage_escalation", d, 26) for d in durations]
    )[("outage_escalation", 26)]

    assert stats.p90_s == judge_latency.p90(durations)
    assert stats.median_s == judge_latency.median(durations)
    assert stats.min_s == min(durations)
    assert stats.max_s == max(durations)
    # The estimator that a re-implementation would plausibly have picked.
    assert stats.p90_s != sorted(durations)[int(0.9 * len(durations)) - 1]


def test_a_budget_skip_is_counted_apart_from_every_number_about_calls():
    """The channel that would let this plan's own failure mode return with all
    its other numbers green: raising the two earliest judges' ceilings under one
    FIXED shared budget spends what the last judge needs, and a judge skipped for
    want of budget produced no verdict just as surely as one killed on its
    ceiling — while being invisible to every statistic over calls."""
    calls = [
        _completed("outage_escalation", 9.0, 26),
        _completed("outage_escalation", 11.0, 26),
    ]
    before = mod.latency_by_judge(calls)[("outage_escalation", 26)]
    after = mod.latency_by_judge(
        calls + [_skipped("outage_escalation", 26) for _ in range(7)]
    )[("outage_escalation", 26)]

    assert before.budget_skips == 0
    assert after.budget_skips == 7
    assert (after.n, after.timeouts, after.rate) == (before.n, before.timeouts, before.rate)
    assert after.durations == before.durations

    # A judge that was ONLY ever skipped still gets a row — that is the whole
    # point of the column — and it reports no rate rather than a flattering 0%.
    only = mod.latency_by_judge([_skipped("binary_ask", 12)])[("binary_ask", 12)]
    assert (only.n, only.timeouts, only.budget_skips) == (0, 0, 1)
    assert only.rate is None
    line = mod._latency_row("binary_ask", 12, only, width=20)
    assert "0.0%" not in line
    assert "budget-skips=1" in line


def test_the_latency_view_and_the_duration_table_share_one_population(tmp_path):
    """The test that keeps one population from becoming two. Both surfaces are
    printed by the same script, and a reader compares them; if their membership
    can drift, the report contradicts itself about the same judge."""
    records = [
        _decided_record("r1", judge="feedback_signal", reason="", duration=12.0, ceiling=16),
        _decided_record("r2", judge="feedback_signal", timed_out=True,
                        reason="judge timed out (fail-open)", duration=16.0, ceiling=16),
        _decided_record("r3", judge="approval_ask", reason="", duration=17.0, ceiling=30),
        _decided_record("r4", judge="approval_ask", timed_out=True,
                        reason="judge timed out (fail-open)", duration=13.0, ceiling=13),
        # Everything the population excludes, each carrying a plausible duration.
        _decided_record("r5", judge="feedback_signal", stage="budget",
                        reason="budget exhausted before call (fail-open)", ceiling=16),
        _decided_record("r6", judge="feedback_signal",
                        reason="judge exited non-zero (fail-open)", duration=0.05, ceiling=16),
        _decided_record("r7", judge="approval_ask", malformed=True,
                        reason="unparseable (fail-open)", duration=0.06, ceiling=30),
        _decided_record("r8", judge="binary_ask", stage="killswitch",
                        reason="judge disabled (fail-open)", ceiling=13),
    ]
    path = tmp_path / "judge-usage-ledger.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    result = mod.tally(judge_ledger.read_ledger(path), path)

    from_table = result.durations_by_judge()
    from_view = {}
    for (judge, _ceiling), stats in mod.latency_by_judge(result.judge_points).items():
        from_view.setdefault(judge, []).extend(stats.durations)

    assert {j: sorted(v) for j, v in from_view.items()} == {
        j: sorted(v) for j, v in from_table.items()
    }
    # …and it is not vacuous: the excluded rows exist and would have shown up.
    assert sum(len(v) for v in from_table.values()) == 4
    assert len(result.judge_points) == len(records)


def _decided_record(invocation_id, *, judge, stage="call", ts=T0, **overrides):
    """A `decided` line with every field lib/judge_ledger.decided() writes, so a
    fixture cannot accidentally pass by omitting the field a branch reads."""
    fields = {
        "judge": judge,
        "stage": stage,
        "verdict": False,
        "reason": "",
        "timed_out": False,
        "malformed": False,
        "runner_legacy": False,
        "remaining": None,
        "threshold": None,
        "ceiling": None,
        "duration": None,
    }
    fields.update(overrides)
    return {
        "ts": ts,
        "kind": "decided",
        "invocation_id": invocation_id,
        "hook": "turn_end",
        "source": "sess-under-test",
        **fields,
    }


def test_a_window_argument_is_a_date_a_datetime_or_n_days_back():
    now = T0
    assert mod.parse_since("7d", now=now) == now - 7 * DAY
    assert mod.parse_since("0d", now=now) == now
    from datetime import datetime as _dt

    assert mod.parse_since("2026-08-19") == _dt(2026, 8, 19).timestamp()
    assert mod.parse_since("2026-08-19T16:08:00") == _dt(2026, 8, 19, 16, 8).timestamp()
    for bad in ("yesterday", "7 days", "d", "-3d", ""):
        with pytest.raises(Exception):
            mod.parse_since(bad, now=now)


def test_the_cli_prints_the_view_and_leaves_the_default_report_untouched(tmp_path, capsys):
    """The argparse wiring and the print path — the only part an operator
    touches. The default report is the same bytes it was before this view
    existed, which is what makes this an added view rather than a changed one."""
    records = [
        {"ts": T0, "kind": "hook_start", "invocation_id": "c1", "hook": "turn_end",
         "source": "s"},
        _decided_record("c1", judge="feedback_signal", reason="", duration=12.0, ceiling=16),
        _decided_record("c1", judge="binary_ask", timed_out=True,
                        reason="judge timed out (fail-open)", duration=13.0, ceiling=13),
        _decided_record("c1", judge="outage_escalation", stage="budget",
                        reason="budget exhausted before call (fail-open)", ceiling=26),
    ]
    path = tmp_path / "judge-usage-ledger.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )

    assert mod.main(["--ledger", str(path), "--latency"]) == 0
    latency_out = capsys.readouterr().out
    assert "feedback_signal @ 16s" in latency_out
    assert "binary_ask @ 13s" in latency_out
    assert "rate=100.0%" in latency_out
    assert "outage_escalation @ 26s" in latency_out
    assert "budget-skips=1" in latency_out

    assert mod.main(["--ledger", str(path)]) == 0
    default_out = capsys.readouterr().out
    result = mod.tally(judge_ledger.read_ledger(path), path)
    assert default_out == "\n".join(mod.format_report(result)) + "\n"
    assert "budget-skips" not in default_out
    assert "@ 16s" not in default_out


def test_a_window_without_the_view_is_refused_rather_than_ignored(tmp_path):
    """--since silently doing nothing would let a verifier believe a lifetime
    number was a windowed one — the exact confusion this view exists to end."""
    path = tmp_path / "judge-usage-ledger.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.main(["--ledger", str(path), "--since", "7d"])


def test_an_unreadable_ledger_does_not_print_as_a_judge_that_never_times_out(tmp_path):
    """An empty table over an unreadable file reads as "no judge has a problem".
    The view has to say it knows nothing instead."""
    missing = tmp_path / "never-written.jsonl"
    text = "\n".join(mod.format_latency(mod.tally(judge_ledger.read_ledger(missing), missing)))
    assert "NO DATA" in text

    unreadable = tmp_path / "locked"
    unreadable.mkdir()  # a directory: open() fails with an OSError that is not ENOENT
    read = judge_ledger.read_ledger(unreadable)
    text = "\n".join(mod.format_latency(mod.tally(read, unreadable)))
    assert "Verdict: UNKNOWN" in text
