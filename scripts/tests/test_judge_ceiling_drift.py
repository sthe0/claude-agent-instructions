"""judge-usage-report.py --check-drift — the ceiling half of the loop.

A ceiling raised from a re-sample answers "is this correct right now". Nothing
so far answers "is it still correct" once the call population it was sized
against keeps growing. These tests pin the detector that closes that gap:

  * it must never restate a ceiling — a constant and its detector that could
    independently drift apart is the exact defect this plan repairs;
  * it must report one finding per (hook, judge) PAIR, not per judge, because
    outage_escalation is called by two hooks and pooling them is precisely the
    error the (judge, ceiling) partition already guards against elsewhere;
  * it must distinguish "not enough data yet" from a verdict, so a freshly
    raised ceiling with few post-fix calls reads as silence, not as health;
  * --strict must promote exactly the three WARN conditions to FAIL and leave
    the FAIL/INSUFFICIENT DATA classification of everything else untouched.
"""
import importlib.util
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

DAY = 86400.0
T0 = 1_755_000_000.0  # an arbitrary fixed epoch; nothing here depends on the date

# The two hooks that call outage_escalation, at their actual ledger hook names
# and script basenames — needed to build the collision fixture (test item h).
TURN_END_HOOK = "turn_end"
TURN_END_BASENAME = "hook-turn-end-gate.py"
ESCALATION_HOOK = "escalation_diagnosis"
ESCALATION_BASENAME = "hook-escalation-diagnosis-gate.py"


def _point(hook, judge, outcome_id, duration, ceiling, ts=T0):
    return mod.JudgePoint(hook, judge, outcome_id, duration, ceiling, ts)


def _completed(hook, judge, duration, ceiling, ts=T0):
    return _point(hook, judge, "4", duration, ceiling, ts)


def _timed_out(hook, judge, ceiling, ts=T0):
    """A timeout is recorded at its ceiling: that is what killed it."""
    return _point(hook, judge, "5", float(ceiling), ceiling, ts)


def _skipped(hook, judge, ceiling, ts=T0):
    return _point(hook, judge, mod.BUDGET_SKIP_OUTCOME_ID, None, ceiling, ts)


def _healthy_points(hook, judge, ceiling, n=30, duration=None, ts=T0):
    duration = duration if duration is not None else ceiling / 2
    return [_completed(hook, judge, duration, ceiling, ts) for _ in range(n)]


def _finding_for(findings, hook_basename, judge):
    matches = [f for f in findings if f.hook == hook_basename and f.judge == judge]
    assert len(matches) == 1, f"expected exactly one finding for {hook_basename}/{judge}"
    return matches[0]


def test_a_synthetic_drifted_judge_fails():
    """Median sitting AT the declared ceiling is the structural drift signal —
    not a rate, not a WARN, a FAIL in both modes."""
    ceiling = judge_latency.call_ceiling_s("binary_ask")
    points = _healthy_points(
        TURN_END_HOOK, "binary_ask", ceiling, n=30, duration=ceiling
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "binary_ask")

    assert finding.status == mod.DRIFT_FAIL
    assert mod._drift_fails(finding, strict=False)
    assert mod._drift_fails(finding, strict=True)


def test_a_healthy_judge_is_ok():
    ceiling = judge_latency.call_ceiling_s("feedback_signal")
    points = _healthy_points(
        TURN_END_HOOK, "feedback_signal", ceiling, n=30, duration=ceiling / 3
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "feedback_signal")

    assert finding.status == mod.DRIFT_OK
    assert not mod._drift_fails(finding, strict=False)
    assert not mod._drift_fails(finding, strict=True)


def test_below_the_minimum_is_insufficient_data_not_a_fail_in_either_mode():
    """A judge with too little post-fix data must never be judged healthy OR
    unhealthy — reported, and zero exit, in BOTH modes."""
    ceiling = judge_latency.call_ceiling_s("feedback_signal")
    points = _healthy_points(
        TURN_END_HOOK, "feedback_signal", ceiling, n=mod.MIN_DRIFT_CALLS - 1
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "feedback_signal")

    assert finding.status == mod.DRIFT_INSUFFICIENT
    assert not mod._drift_fails(finding, strict=False)
    assert not mod._drift_fails(finding, strict=True)


def test_exactly_the_minimum_is_judged_not_deferred():
    """The boundary case: n == MIN_DRIFT_CALLS is judged, not treated as
    insufficient — the check must use >=, not >."""
    ceiling = judge_latency.call_ceiling_s("feedback_signal")
    points = _healthy_points(
        TURN_END_HOOK, "feedback_signal", ceiling, n=mod.MIN_DRIFT_CALLS
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "feedback_signal")

    assert finding.n == mod.MIN_DRIFT_CALLS
    assert finding.status != mod.DRIFT_INSUFFICIENT


def test_high_timeout_rate_warns_by_default_and_fails_under_strict():
    ceiling = judge_latency.call_ceiling_s("binary_ask")
    n_timeouts = 6  # 6 of 30 = 20% >= the 15% bar
    points = (
        [_timed_out(TURN_END_HOOK, "binary_ask", ceiling) for _ in range(n_timeouts)]
        + _healthy_points(
            TURN_END_HOOK, "binary_ask", ceiling,
            n=30 - n_timeouts, duration=ceiling / 3,
        )
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "binary_ask")

    assert finding.status == mod.DRIFT_WARN
    assert any("timeout rate" in r for r in finding.reasons)
    assert not mod._drift_fails(finding, strict=False)
    assert mod._drift_fails(finding, strict=True)


def test_ceiling_clustered_survivors_warn_even_at_a_low_timeout_rate():
    """A rate under the bar achieved by calls that still all die AT the ceiling
    is the censoring signature returning, not a healthy judge: one timeout in
    thirty (3.3%, well under 15%) that lands at the ceiling must still WARN."""
    ceiling = judge_latency.call_ceiling_s("outage_escalation")
    points = (
        [_timed_out(TURN_END_HOOK, "outage_escalation", ceiling)]
        + _healthy_points(
            TURN_END_HOOK, "outage_escalation", ceiling, n=29, duration=ceiling / 3
        )
    )
    findings = mod.check_drift(points)
    finding = _finding_for(findings, TURN_END_BASENAME, "outage_escalation")

    assert finding.timeout_rate < mod.DRIFT_TIMEOUT_RATE_WARN
    assert finding.status == mod.DRIFT_WARN
    assert any("within" in r and "ceiling" in r for r in finding.reasons)
    assert not mod._drift_fails(finding, strict=False)
    assert mod._drift_fails(finding, strict=True)


def test_budget_skip_rate_warns_with_no_timeouts_at_all():
    ceiling = judge_latency.call_ceiling_s("deferring_disposition")
    points = (
        _healthy_points(
            "deferring_disposition", "deferring_disposition", ceiling,
            n=30, duration=ceiling / 3,
        )
        + [_skipped("deferring_disposition", "deferring_disposition", ceiling) for _ in range(2)]
    )
    findings = mod.check_drift(points)
    finding = _finding_for(
        findings, "hook-deferring-disposition-gate.py", "deferring_disposition"
    )

    # 2 skips over 32 decision points = 6.25% > the 5% bar.
    assert finding.skip_rate > mod.DRIFT_SKIP_RATE_WARN
    assert finding.status == mod.DRIFT_WARN
    assert any("budget-skip rate" in r for r in finding.reasons)
    assert not mod._drift_fails(finding, strict=False)
    assert mod._drift_fails(finding, strict=True)


def test_ceilings_are_read_from_judge_latency_never_restated(monkeypatch):
    """Patch the one function every ceiling comes from and every reference this
    check prints must move with it — proof it holds no copy of its own."""
    monkeypatch.setattr(judge_latency, "call_ceiling_s", lambda judge: 12345.0)
    findings = mod.check_drift([])
    assert findings  # one per HOOK_CALL_SEQUENCE pair, even with no ledger data
    assert all(f.reference_ceiling == 12345.0 for f in findings)


def test_one_judge_under_two_hooks_at_different_ceilings_yields_two_findings():
    """outage_escalation is declared by both hooks. Different data, different
    ceilings, must never be pooled into one row."""
    reference = judge_latency.call_ceiling_s("outage_escalation")
    turn_end_ceiling = reference  # exactly at the reference: healthy population
    escalation_ceiling = reference + 5  # a hook with real padding above it

    points = (
        _healthy_points(
            TURN_END_HOOK, "outage_escalation", turn_end_ceiling,
            n=30, duration=turn_end_ceiling / 3,
        )
        + _healthy_points(
            ESCALATION_HOOK, "outage_escalation", escalation_ceiling,
            n=30, duration=escalation_ceiling,  # drifted under ITS OWN ceiling
        )
    )
    findings = mod.check_drift(points)

    turn_end_finding = _finding_for(findings, TURN_END_BASENAME, "outage_escalation")
    escalation_finding = _finding_for(findings, ESCALATION_BASENAME, "outage_escalation")

    assert turn_end_finding.chosen_ceiling == turn_end_ceiling
    assert turn_end_finding.status == mod.DRIFT_OK
    assert escalation_finding.chosen_ceiling == escalation_ceiling
    assert escalation_finding.status == mod.DRIFT_FAIL


def test_two_hooks_declaring_the_identical_ceiling_still_report_two_lines():
    """The collision case: both hooks happen to enforce the SAME numeric
    ceiling for outage_escalation. Their rows must still be kept apart —
    pooling them by (judge, ceiling) alone would be exactly the bug the
    hook-first filter in check_drift exists to prevent."""
    reference = judge_latency.call_ceiling_s("outage_escalation")

    points = (
        _healthy_points(
            TURN_END_HOOK, "outage_escalation", reference,
            n=30, duration=reference / 3,
        )
        + _healthy_points(
            ESCALATION_HOOK, "outage_escalation", reference,
            n=30, duration=reference,  # same ceiling, drifted population
        )
    )
    findings = mod.check_drift(points)

    turn_end_finding = _finding_for(findings, TURN_END_BASENAME, "outage_escalation")
    escalation_finding = _finding_for(findings, ESCALATION_BASENAME, "outage_escalation")

    assert turn_end_finding.chosen_ceiling == escalation_finding.chosen_ceiling == reference
    assert turn_end_finding.status == mod.DRIFT_OK
    assert escalation_finding.status == mod.DRIFT_FAIL
    assert turn_end_finding.n == 30 and escalation_finding.n == 30  # never pooled


def test_since_window_composes_with_check_drift_in_both_modes():
    ceiling = judge_latency.call_ceiling_s("feedback_signal")
    stale = _healthy_points(
        TURN_END_HOOK, "feedback_signal", ceiling,
        n=30, duration=ceiling / 3, ts=T0 - 10 * DAY,
    )
    findings_all = mod.check_drift(stale)
    findings_windowed = mod.check_drift(stale, since=T0 - DAY)

    assert _finding_for(findings_all, TURN_END_BASENAME, "feedback_signal").n == 30
    assert (
        _finding_for(findings_windowed, TURN_END_BASENAME, "feedback_signal").status
        == mod.DRIFT_INSUFFICIENT
    )


def test_format_drift_exit_code_matches_default_and_strict():
    ceiling = judge_latency.call_ceiling_s("binary_ask")
    healthy = _healthy_points(TURN_END_HOOK, "binary_ask", ceiling, n=30, duration=ceiling / 3)
    warn_points = (
        [_timed_out(TURN_END_HOOK, "outage_escalation", judge_latency.call_ceiling_s("outage_escalation"))]
        + _healthy_points(
            TURN_END_HOOK, "outage_escalation",
            judge_latency.call_ceiling_s("outage_escalation"), n=29,
            duration=judge_latency.call_ceiling_s("outage_escalation") / 3,
        )
    )
    findings = mod.check_drift(healthy + warn_points)

    assert not any(mod._drift_fails(f, strict=False) for f in findings)
    assert any(mod._drift_fails(f, strict=True) for f in findings)

    default_lines = mod.format_drift(findings, strict=False)
    strict_lines = mod.format_drift(findings, strict=True)
    assert "no ceiling needs re-deriving" in default_lines[-1]
    assert "FAIL" in strict_lines[-1]
