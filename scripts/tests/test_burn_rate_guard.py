"""Stage 3: the fast-burn guard — two-window conjunction, throttled, fail-open.

The four properties pinned here are the four that make it safe to run on every
prompt of every session: it stays quiet unless BOTH windows are hot (so a
one-off expensive turn and a long-finished spike are both silent), it measures
against wall-clock NOW rather than the transcript's own last message (so a
resumed session is not warned about last night), it says a thing at most once
per band per session, and it never fails loudly.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "hook_burn_rate_guard", SCRIPTS_DIR / "hook-burn-rate-guard.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

from lib import transcript_cost as tc  # noqa: E402

UTC = dt.timezone.utc
USD_PER_TOKEN = tc.PRICING_USD_PER_MTOK["opus"]["input"] / 1_000_000


def _transcript(tmp_path, spend, name="transcript.jsonl", *, age_min=0.0):
    """A transcript from ``[(minutes_before_now, usd), ...]``.

    Timestamps are relative to REAL wall-clock now, because that is what the
    guard anchors its windows on. ``age_min`` pushes the whole transcript that
    many minutes further into the past, which is how a resumed or long-idle
    session is expressed.

    Priced through the real table (opus base input) rather than a fabricated
    number, so the fixtures move with the rates instead of pinning a stale copy.
    """
    anchor = dt.datetime.now(UTC) - dt.timedelta(minutes=age_min)
    lines = []
    for i, (minutes, usd) in enumerate(spend):
        ts = (anchor - dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        lines.append(json.dumps({
            "timestamp": ts,
            "message": {
                "id": f"msg_{i}",
                "model": "claude-opus-5",
                "usage": {"input_tokens": round(usd / USD_PER_TOKEN)},
            },
        }))
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# Fast window is 15 min with a 7.5 min floor; slow is 3 h with a floor of one
# declared medium-tier stage (25 min at the time of writing). Every fixture below
# spans well past both floors, so these tests exercise the conjunction rather
# than the abstention rules — those have their own tests in test_transcript_cost.
# BOTH_HOT: fast = $3.00 / (10 min) = $18/h, slow = $9.00 / 1.5 h = $6/h.
BOTH_HOT = [(90, 6.00), (10, 1.00), (0, 2.00)]
# FAST_ONLY: the same recent burst, but the hours behind it were nearly free.
FAST_ONLY = [(170, 0.10), (10, 1.00), (0, 2.00)]
# SLOW_ONLY: an expensive stretch that has already stopped.
SLOW_ONLY = [(170, 20.00), (160, 1.00), (10, 0.01), (0, 0.01)]
# BOTH_VERY_HOT: fast = $36/h, slow = $17.33/h.
BOTH_VERY_HOT = [(90, 20.00), (10, 2.00), (0, 4.00)]


def _run(monkeypatch, capsys, tmp_path, transcript, *, session=None,
         warn=5.0, escalate=10.0):
    """Invoke main() with fixed thresholds and an isolated state dir.

    The thresholds are pinned so these tests assert the CONJUNCTION LOGIC rather
    than today's config.md numbers; that the real ones are derived and not
    invented is a separate assertion below.
    """
    monkeypatch.setenv(mod.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(mod, "thresholds", lambda: (warn, escalate))
    sid = session or f"s-{uuid.uuid4().hex[:8]}"
    payload = {"session_id": sid, "transcript_path": str(transcript)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out, sid


# --- the conjunction ----------------------------------------------------------

def test_both_windows_hot_warns_once(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, BOTH_HOT))
    assert rc == 0
    assert "burn-rate" in out
    assert out.count("burn-rate") == 1


def test_fast_window_alone_is_silent(monkeypatch, capsys, tmp_path):
    """One expensive turn on top of cheap hours is budget consumption, not a
    burn — policy-scorecard.py's own recorded disposition (conjunction mode)."""
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, FAST_ONLY))
    assert rc == 0
    assert out == ""


def test_slow_window_alone_is_silent(monkeypatch, capsys, tmp_path):
    """An expensive stretch that has already stopped needs no live warning; the
    periodic scorecard is the right instrument for it."""
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, SLOW_ONLY))
    assert rc == 0
    assert out == ""


def test_escalation_band_asks_for_a_user_decision(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, BOTH_VERY_HOT))
    assert rc == 0
    assert "AskUserQuestion" in out


def test_band_for_requires_both_and_treats_abstention_as_silence():
    assert mod.band_for(20.0, 20.0, 5.0, 10.0) == 2
    assert mod.band_for(6.0, 6.0, 5.0, 10.0) == 1
    assert mod.band_for(20.0, 6.0, 5.0, 10.0) == 1   # escalate needs both
    assert mod.band_for(20.0, 1.0, 5.0, 10.0) == 0
    assert mod.band_for(1.0, 20.0, 5.0, 10.0) == 0
    assert mod.band_for(None, 20.0, 5.0, 10.0) == 0  # abstain != zero, and != hot
    assert mod.band_for(20.0, None, 5.0, 10.0) == 0


# --- the windows end at NOW, not at the last message --------------------------

def test_a_stale_burst_on_a_resumed_session_is_silent(monkeypatch, capsys, tmp_path):
    """The same burst that fires while it is happening must not fire an hour
    later. Anchoring the windows on the transcript's own last message instead of
    on wall-clock now re-prices last night's session as though it were live —
    every resumed session would open with a burn warning about finished work."""
    fresh = _transcript(tmp_path, BOTH_HOT, name="fresh.jsonl")
    _, out_fresh, _ = _run(monkeypatch, capsys, tmp_path, fresh)
    assert "burn-rate" in out_fresh  # the burst is real while it is live

    stale = _transcript(tmp_path, BOTH_HOT, name="stale.jsonl", age_min=60)
    rc, out_stale, _ = _run(monkeypatch, capsys, tmp_path, stale)
    assert rc == 0
    assert out_stale == ""


def test_a_long_idle_session_is_silent(monkeypatch, capsys, tmp_path):
    """Ten hours idle puts every message outside even the slow window."""
    rc, out, _ = _run(
        monkeypatch, capsys, tmp_path,
        _transcript(tmp_path, BOTH_VERY_HOT, age_min=600))
    assert rc == 0
    assert out == ""


def test_a_stale_burst_does_not_consume_the_throttle(monkeypatch, capsys, tmp_path):
    """The costly half of the same defect: firing on a resumed session's stale
    burst also STAMPS the band, so the genuine burn that follows in that session
    is the one that goes unwarned."""
    stale = _transcript(tmp_path, BOTH_HOT, name="stale.jsonl", age_min=60)
    _, out_stale, sid = _run(monkeypatch, capsys, tmp_path, stale)
    assert out_stale == ""

    live = _transcript(tmp_path, BOTH_HOT, name="live.jsonl")
    _, out_live, _ = _run(monkeypatch, capsys, tmp_path, live, session=sid)
    assert "burn-rate" in out_live


def test_evaluate_anchors_on_wall_clock_now(tmp_path, monkeypatch):
    """Stated directly against evaluate(), so the property survives a rewrite of
    main(): moving `now` forward past the window silences a hot transcript."""
    monkeypatch.setattr(mod, "thresholds", lambda: (5.0, 10.0))
    t = _transcript(tmp_path, BOTH_HOT)
    assert mod.evaluate(str(t)) is not None
    later = dt.datetime.now(UTC) + dt.timedelta(hours=6)
    assert mod.evaluate(str(t), now=later) is None


# --- throttling ---------------------------------------------------------------

def test_second_invocation_in_the_same_band_is_silent(monkeypatch, capsys, tmp_path):
    t = _transcript(tmp_path, BOTH_HOT)
    _, out1, sid = _run(monkeypatch, capsys, tmp_path, t)
    assert "burn-rate" in out1
    rc, out2, _ = _run(monkeypatch, capsys, tmp_path, t, session=sid)
    assert rc == 0
    assert out2 == ""


def test_escalation_still_speaks_after_a_warning(monkeypatch, capsys, tmp_path):
    """Throttling is per band, not per session: the situation getting worse is
    news even though the warning band already fired."""
    _, out1, sid = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, BOTH_HOT))
    assert "burn-rate" in out1
    _, out2, _ = _run(
        monkeypatch, capsys, tmp_path,
        _transcript(tmp_path, BOTH_VERY_HOT, name="hotter.jsonl"), session=sid)
    assert "AskUserQuestion" in out2


def test_a_different_session_is_not_throttled(monkeypatch, capsys, tmp_path):
    t = _transcript(tmp_path, BOTH_HOT)
    _run(monkeypatch, capsys, tmp_path, t)
    _, out2, _ = _run(monkeypatch, capsys, tmp_path, t)
    assert "burn-rate" in out2


# --- fail-open ----------------------------------------------------------------

def test_missing_transcript_is_silent_and_exits_zero(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, tmp_path / "nope.jsonl")
    assert rc == 0
    assert out == ""


def test_unreadable_transcript_is_silent_and_exits_zero(monkeypatch, capsys, tmp_path):
    unreadable = tmp_path / "a-directory.jsonl"
    unreadable.mkdir()
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, unreadable)
    assert rc == 0
    assert out == ""


def test_malformed_transcript_is_silent_and_exits_zero(monkeypatch, capsys, tmp_path):
    p = tmp_path / "junk.jsonl"
    p.write_text("not json\n{\x00 broken\n[]\n", encoding="utf-8")
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, p)
    assert rc == 0
    assert out == ""


def test_absent_transcript_path_key_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s"})))
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


def test_non_json_stdin_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("<<<not json>>>"))
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


def test_unavailable_thresholds_are_silent(monkeypatch, capsys, tmp_path):
    """If policy-scorecard.py or config.md cannot be read there is no calibrated
    number to compare against, and inventing one here is exactly what this hook
    exists to avoid."""
    monkeypatch.setattr(mod, "thresholds", lambda: None)
    monkeypatch.setenv(mod.STATE_DIR_ENV, str(tmp_path / "state"))
    payload = {"session_id": "s", "transcript_path": str(_transcript(tmp_path, BOTH_HOT))}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


def test_unwritable_state_dir_still_warns(monkeypatch, capsys, tmp_path):
    """Losing the throttle stamp must degrade to a repeated warning, never to an
    exception on a user's prompt."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(mod.STATE_DIR_ENV, str(blocker / "sub"))
    monkeypatch.setattr(mod, "thresholds", lambda: (5.0, 10.0))
    payload = {"session_id": "s", "transcript_path": str(_transcript(tmp_path, BOTH_HOT))}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert mod.main() == 0
    assert "burn-rate" in capsys.readouterr().out


def test_unreadable_config_tier_leaves_the_hook_silent(monkeypatch):
    """config.md is a file an operator edits; a reshaped table must abstain, not
    raise on a prompt."""
    monkeypatch.setattr(mod, "NORMAL_TIER", "no-such-tier")
    assert mod.normal_profile() is None
    assert mod.normal_rate_usd_per_h() is None
    assert mod.slow_min_span_h() is None
    assert mod.thresholds() is None


def test_unloadable_scorecard_leaves_the_hook_silent(monkeypatch):
    monkeypatch.setattr(mod, "load_policy_scorecard", lambda: None)
    assert mod.thresholds() is None


def test_unknown_slow_floor_leaves_the_hook_silent(monkeypatch, tmp_path):
    """A threshold with no matching observation floor is half a comparison; the
    hook abstains rather than falling back to a number of its own."""
    monkeypatch.setattr(mod, "thresholds", lambda: (5.0, 10.0))
    monkeypatch.setattr(mod, "slow_min_span_h", lambda: None)
    assert mod.evaluate(str(_transcript(tmp_path, BOTH_HOT))) is None


def test_deep_history_before_the_window_does_not_hide_a_live_burn(
        monkeypatch, capsys, tmp_path):
    """The hook reads only the transcript's tail. A long, cheap history in front
    of a hot window must neither be scanned into the answer nor cause the tail
    read to stop short of the window's far edge."""
    ancient = [(1200 + i, 0.001) for i in range(400)]
    rc, out, _ = _run(
        monkeypatch, capsys, tmp_path, _transcript(tmp_path, ancient + BOTH_HOT))
    assert rc == 0
    assert "burn-rate" in out


def test_never_emits_a_permission_decision(monkeypatch, capsys, tmp_path):
    """UserPromptSubmit stdout is context, not control. Anything shaped like a
    hook decision would make an advisory into a block."""
    _, out, _ = _run(monkeypatch, capsys, tmp_path, _transcript(tmp_path, BOTH_VERY_HOT))
    assert "permissionDecision" not in out
    assert "hookSpecificOutput" not in out


# --- calibration is imported, not invented ------------------------------------

def test_thresholds_derive_from_the_scorecard_factor_and_config(monkeypatch):
    scorecard = mod.load_policy_scorecard()
    assert scorecard is not None
    factor = scorecard.SPEND_RATE_FACTOR
    normal = mod.normal_rate_usd_per_h()
    warn, escalate = mod.thresholds()
    assert warn == normal * factor
    assert escalate == normal * factor ** 2
    assert escalate > warn > normal


def test_normal_rate_is_the_declared_medium_tier_rate():
    from agentctl.config import Thresholds

    thr = Thresholds()
    expected = thr.budget_usd_float(mod.NORMAL_TIER) / (
        thr.effort_stage_minutes(mod.NORMAL_TIER) / 60.0)
    assert mod.normal_rate_usd_per_h() == expected


def test_slow_floor_is_the_declared_stage_length_not_a_literal():
    """Threshold and observation floor are read from the SAME config row, so the
    comparison stays one statement: B dollars per M active minutes, judged after
    M active minutes. A flat 1 h floor here is what real transcripts refuted —
    it keeps the guard silent through the first hour of every session."""
    from agentctl.config import Thresholds

    expected = Thresholds().effort_stage_minutes(mod.NORMAL_TIER) / 60.0
    assert mod.slow_min_span_h() == expected
    assert mod.slow_min_span_h() < 1.0
    assert not hasattr(mod, "SLOW_MIN_SPAN_H")


def test_fast_window_is_derived_from_the_slow_one():
    """The SRE ~1/12 ratio, as an expression rather than two literals that could
    drift apart."""
    assert mod.FAST_WINDOW_H == mod.SLOW_WINDOW_H / 12.0
    assert 0 < mod.FAST_MIN_SPAN_H <= mod.FAST_WINDOW_H
    assert mod.FAST_WINDOW_H < mod.slow_min_span_h() <= mod.SLOW_WINDOW_H
