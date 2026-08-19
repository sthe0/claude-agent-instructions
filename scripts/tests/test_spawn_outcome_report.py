"""Tests for scripts/spawn-outcome-report.py.

Every test drives the instrument against a SYNTHETIC ledger under tmp_path. None
reads ~/.local/log/claude-spawn-costs.jsonl: that file is the evidence base for
the frozen baseline and it keeps growing, so a test that read it would be a test
whose expected values move under it.

The statistical constants pinned here (203, 396, 239, 464, 578, 1138, the two
cause-level ceilings and their Wilson trip counts) are pinned at FIXED INPUTS.
They pin the FUNCTIONS. They are deliberately not the numbers any frozen
baseline must carry — the freeze recomputes those from its own rows.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "spawn-outcome-report.py"
_spec = importlib.util.spec_from_file_location("spawn_outcome_report", _SRC)
sor = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sor)


# ── fixture helpers ─────────────────────────────────────────────────────────


def row(ts, kind="thinker", *, exit_code=0, malformed=False, reason=None, cost=1.0, **extra):
    r = {
        "ts": ts if isinstance(ts, str) else ts.isoformat(),
        "event": "spawn",
        "kind": kind,
        "exit_code": exit_code,
        "malformed": malformed,
        "cost_usd": cost,
    }
    if reason is not None:
        r["extraction_reason"] = reason
    r.update(extra)
    return r


def write_ledger(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def at(base: str, seconds: int) -> str:
    return (sor.parse_ts(base) + dt.timedelta(seconds=seconds)).isoformat()


TIMEOUT = "extractor process failed (exit 1): marker extractor timed out after 30s"
NO_MARKER = "extractor found no marker"
UNRECOGNISED = "extractor returned an unrecognised token: 'REVISE'"

POST_BASE = "2026-09-01T00:00:00+00:00"


def build_post(n_total: int, malformed_reasons: list[str], n_respawn: int) -> list[dict]:
    """Post-arm rows: one exit-0-malformed row per reason, `n_respawn` of them
    followed by a same-kind clean spawn 600 s later, the rest filled with clean
    rows of a different kind spaced far enough apart to pair with nothing."""
    rows: list[dict] = []
    t = 0
    for i, reason in enumerate(malformed_reasons):
        rows.append(row(at(POST_BASE, t), "thinker", malformed=True, reason=reason,
                        outcome_class="malformed"))
        if i < n_respawn:
            rows.append(row(at(POST_BASE, t + 600), "thinker", reason="ok",
                            outcome_class="ok"))
        t += 7200
    while len(rows) < n_total:
        rows.append(row(at(POST_BASE, t), "developer", reason="ok", outcome_class="ok"))
        t += 7200
    assert len(rows) == n_total, (len(rows), n_total)
    return rows


def make_baseline(**overrides) -> dict:
    b = {
        "window_start": sor.DEPLOY_TS,
        "window_end": "2026-08-01T00:00:00+00:00",
        "ledger_sha256": "0" * 64,
        "window_rows": 1000,
        "exit0_malformed_n": 230,
        "exit0_malformed_rate": 0.23,
        "exit0_malformed_cost_usd": 500.0,
        "respawn_n": 130,
        "respawn_rate": 0.13,
        "respawn_cost_usd": 200.0,
        "respawn_denominator": "all_spawn_rows_in_window",
        "respawn_numerator": "exit0_and_malformed_rows_in_window",
        "cause_breakdown": {"extractor_timeout": 170, "no_marker_found": 40},
        "attributable_n": 210,
        "assumed_recovery": 0.60,
        "timeout_class_n": 170,
        "timeout_class_respawn_n": 90,
        "timeout_class_residual_share": 0.068,
        "substitution_ceiling_share_of_attributable": 40 / 210,
        "stopping_rule_nominal_alpha": sor.NOMINAL_ALPHA,
        "stopping_rule_boundary_z": 2.1922,
        "stopping_rule_n_malformed_rate": 60,
        "stopping_rule_n_respawn_rate": 100,
        "stopping_rule_min_n": 100,
        "stopping_rule_look2_n_malformed_rate": 250,
        "stopping_rule_look2_n_respawn_rate": 400,
        "stopping_rule_look2_min_n": 400,
        "look_window_rows": 20,
    }
    b.update(overrides)
    return b


def run_delta(tmp_path, post_rows, baseline, post_start=None):
    """Drive --check-delta through the CLI against a synthetic ledger."""
    pre = [
        row("2026-07-28T00:00:00+00:00", "thinker", reason=TIMEOUT, malformed=True),
        row("2026-08-15T00:00:00+00:00", "developer", reason="ok"),  # freeze-to-landing gap
    ]
    ledger = write_ledger(tmp_path / "ledger.jsonl", pre + post_rows)
    bpath = tmp_path / "baseline.json"
    bpath.write_text(json.dumps(baseline), encoding="utf-8")
    argv = ["--ledger", str(ledger), "--check-delta", "--baseline", str(bpath)]
    if post_start:
        argv += ["--post-start", post_start]
    return sor.main(argv)


# ── row reading ─────────────────────────────────────────────────────────────


def test_heterogeneous_schema_rows_load(tmp_path):
    """The ledger's schema is era-dependent; the instrument reads that as a fact
    about the data rather than crashing or imputing."""
    rows = [
        # oldest era: no `event` field at all, no cause field
        {"ts": "2026-06-01T00:00:00+00:00", "kind": "thinker", "exit_code": 0,
         "malformed": True, "cost_usd": 2.0},
        # middle era: event, still no extraction_reason
        row("2026-07-01T00:00:00+00:00", "developer", malformed=True),
        # current era: everything
        row("2026-08-01T00:00:00+00:00", "planner", malformed=True, reason=NO_MARKER,
            session_id="s", ticket="T-1", stage_index=2),
        # not spawns
        {"ts": "2026-08-01T01:00:00+00:00", "event": "refused", "reason": "cap"},
        {"ts": "2026-08-01T02:00:00+00:00", "event": "spawn_start", "kind": "thinker"},
    ]
    ledger = write_ledger(tmp_path / "l.jsonl", rows)
    loaded = sor.load_rows(ledger)
    assert len(loaded) == 3
    assert [r["kind"] for r in loaded] == ["thinker", "developer", "planner"]
    # the two rows with no cause field are counted but not attributed
    s = sor.summarize(loaded)
    assert s["exit0_malformed_n"] == 3
    assert s["attributable_n"] == 1
    assert s["cause_breakdown"] == {"no_marker_found": 1}


def test_null_cost_usd_is_not_a_crash(tmp_path):
    rows = [
        row("2026-08-01T00:00:00+00:00", malformed=True, reason=NO_MARKER, cost=None),
        row("2026-08-01T01:00:00+00:00", reason="ok", cost=3.0),
    ]
    ledger = write_ledger(tmp_path / "l.jsonl", rows)
    s = sor.summarize(sor.load_rows(ledger))
    assert s["exit0_malformed_cost_usd"] == 0.0
    assert s["window_cost_usd"] == 3.0


def test_non_zero_exit_is_excluded(tmp_path):
    """A child that exited non-zero has an independent failure and belongs to a
    different question."""
    rows = [
        row("2026-08-01T00:00:00+00:00", malformed=True, exit_code=1, reason=TIMEOUT),
        row("2026-08-01T01:00:00+00:00", malformed=True, exit_code=0, reason=TIMEOUT),
    ]
    loaded = sor.load_rows(write_ledger(tmp_path / "l.jsonl", rows))
    assert [sor.is_target(r) for r in loaded] == [False, True]


def test_window_start_filter_excludes_a_pre_boundary_row(tmp_path):
    rows = [
        row("2026-07-27T14:26:42+00:00", malformed=True),   # one second early
        row("2026-07-27T14:26:43+00:00", malformed=True, reason=TIMEOUT),
    ]
    loaded = sor.load_rows(write_ledger(tmp_path / "l.jsonl", rows))
    windowed = sor.in_window(loaded, sor.parse_ts(sor.DEPLOY_TS))
    assert len(windowed) == 1
    assert windowed[0]["extraction_reason"] == TIMEOUT


# ── the re-spawn predicate ──────────────────────────────────────────────────


def test_respawn_window_boundary_at_exactly_1800s(tmp_path):
    base = "2026-08-01T00:00:00+00:00"
    on_time = [
        row(base, "thinker", malformed=True, reason=TIMEOUT),
        row(at(base, 1800), "thinker"),
    ]
    late = [
        row(base, "thinker", malformed=True, reason=TIMEOUT),
        row(at(base, 1801), "thinker"),
    ]
    assert len(sor.respawn_pairs(on_time)) == 1, "exactly 1800 s is inside the window"
    assert len(sor.respawn_pairs(late)) == 0, "1801 s is outside it"


def test_restricted_numerator_is_what_is_scored(tmp_path):
    """A fixture where the unrestricted and restricted numerators disagree.

    Six clean same-kind rows 600 s apart: under a predicate that lets ANY row be
    a candidate, five of them are 'followed within 1800 s by a same-kind clean
    spawn'. Only the one exit-0-and-malformed row may actually count.
    """
    base = "2026-08-01T00:00:00+00:00"
    rows = [row(at(base, 600 * i), "thinker") for i in range(6)]
    rows.insert(0, row(base, "thinker", malformed=True, reason=TIMEOUT))
    rows = sorted(rows, key=lambda r: r["ts"])

    unrestricted = sum(
        1
        for i, a in enumerate(rows)
        for b in rows[i + 1:]
        if 0 < (sor.parse_ts(b["ts"]) - sor.parse_ts(a["ts"])).total_seconds() <= 1800
        and b.get("kind") == a.get("kind") and not b.get("malformed")
    )
    assert unrestricted > 1, "the fixture must be one where the two readings disagree"

    pairs = sor.respawn_pairs(rows)
    assert len(pairs) == 1
    s = sor.summarize(rows)
    assert s["respawn_n"] == 1
    assert s["respawn_n"] <= s["exit0_malformed_n"]


# ── the pinned statistical functions ────────────────────────────────────────


def test_sample_size_single_look_pins():
    """Each stopping rule is recomputed from its OWN test's parameters."""
    assert sor.sample_size_two_proportion(0.2331, 0.1266, 0.05) == 203
    assert sor.sample_size_two_proportion(0.1266, 0.0677, 0.05) == 396


def test_sample_size_two_look_pins():
    """The two-look sizes at the nominal alpha, from the planning-time counts.

    845 window rows, 197 exit-0-malformed, 107 re-spawns, 150 extractor timeouts
    of which 83 were re-spawned.
    """
    W, MAL, RES, TO, TO_RES = 845, 197, 107, 150, 83
    p0_m, p0_r = MAL / W, RES / W

    def sizes(recovery):
        return (
            sor.sample_size_two_proportion(p0_m, p0_m - recovery * TO / W, sor.NOMINAL_ALPHA),
            sor.sample_size_two_proportion(p0_r, p0_r - recovery * TO_RES / W, sor.NOMINAL_ALPHA),
        )

    assert sizes(0.60) == (239, 464)
    assert sizes(0.40) == (578, 1138)
    assert abs(sor._ND.inv_cdf(1 - sor.NOMINAL_ALPHA / 2) - 2.1922) < 1e-3


def test_timeout_ceiling_pins():
    """The residual the 60% assumption implies, plus a one-sided 95% allowance."""
    residual = 0.40 * 150 / 845
    assert abs(residual - 0.071006) < 1e-6
    assert abs(sor.timeout_ceiling(residual, 464) - 0.090618) < 1e-6
    assert abs(sor.timeout_ceiling(residual, 1138) - 0.083529) < 1e-6
    # break-even: the recovery at which the true residual meets the look-1 ceiling
    break_even = 1 - sor.timeout_ceiling(residual, 464) / (150 / 845)
    assert abs(break_even - 0.4895) < 1e-4


def test_substitution_ceiling_wilson_trip_counts():
    """A 3-row fluke must not fire; the plan's trip counts are 7/20, 16/55, 38/150."""
    ceiling = 37 / 189
    assert abs(ceiling - 0.195767) < 1e-6
    for k, n in ((7, 20), (16, 55), (38, 150)):
        assert sor.wilson_lower_bound(k, n) > ceiling, (k, n)
        assert sor.wilson_lower_bound(k - 1, n) <= ceiling, (k - 1, n)
    # a 3-row showing in a post attributable set of 20 must not fire it
    assert sor.wilson_lower_bound(3, 20) <= ceiling


# ── freeze ──────────────────────────────────────────────────────────────────


def _freezable(base="2026-08-01T00:00:00+00:00"):
    """40 rows: 8 exit-0-malformed (5 timeouts, 3 no-marker), 3 of them re-spawned."""
    reasons = [TIMEOUT] * 5 + [NO_MARKER] * 3
    rows: list[dict] = []
    t = 0
    for i, reason in enumerate(reasons):
        rows.append(row(at(base, t), "thinker", malformed=True, reason=reason, cost=2.0))
        if i < 3:
            rows.append(row(at(base, t + 600), "thinker", reason="ok", cost=1.5))
        t += 7200
    while len(rows) < 40:
        rows.append(row(at(base, t), "developer", reason="ok"))
        t += 7200
    return rows


def test_freeze_refuses_an_existing_path_and_succeeds_with_force(tmp_path):
    base = "2026-08-01T00:00:00+00:00"
    ledger = write_ledger(tmp_path / "l.jsonl", _freezable(base))
    target = tmp_path / "baseline.json"
    argv = ["--ledger", str(ledger), "--window-start", base, "--freeze-baseline", str(target)]

    assert sor.main(argv) == 0
    first = target.read_text(encoding="utf-8")

    assert sor.main(argv) == 3, "a baseline is frozen once"
    assert target.read_text(encoding="utf-8") == first, "the refused run wrote nothing"

    assert sor.main(argv + ["--force"]) == 0

    b = json.loads(target.read_text(encoding="utf-8"))
    assert b["window_rows"] == 40
    assert b["exit0_malformed_n"] == 8
    assert b["respawn_n"] == 3
    assert b["respawn_numerator"] == "exit0_and_malformed_rows_in_window"
    assert b["respawn_denominator"] == "all_spawn_rows_in_window"
    assert b["cause_breakdown"] == {"extractor_timeout": 5, "no_marker_found": 3}
    assert b["timeout_class_n"] == 5
    assert b["stopping_rule_min_n"] == max(
        b["stopping_rule_n_malformed_rate"], b["stopping_rule_n_respawn_rate"]
    )
    assert b["stopping_rule_look2_min_n"] > b["stopping_rule_min_n"]
    assert abs(
        b["timeout_class_residual_share"]
        - (1 - b["assumed_recovery"]) * b["timeout_class_n"] / b["window_rows"]
    ) < 1e-12


def test_report_mode_banners_a_mixed_regime_window(tmp_path, capsys):
    ledger = write_ledger(tmp_path / "l.jsonl", _freezable())
    assert sor.main(["--ledger", str(ledger), "--window-start", "all"]) == 0
    out = capsys.readouterr().out
    assert "MIXES TWO REGIMES" in out


# ── --check-delta ───────────────────────────────────────────────────────────


def test_check_delta_below_the_first_look_window(tmp_path, capsys):
    code = run_delta(tmp_path, build_post(50, [TIMEOUT] * 2, 1), make_baseline())
    out = capsys.readouterr().out
    assert code == 2
    assert "insufficient evidence" in out
    assert "50 more to go" in out


def test_check_delta_between_the_two_look_windows_is_a_third_look(tmp_path, capsys):
    code = run_delta(tmp_path, build_post(200, [TIMEOUT] * 4, 2), make_baseline())
    out = capsys.readouterr().out
    assert code == 2
    assert "third look" in out


def test_check_delta_refuses_a_rate_that_rose(tmp_path, capsys):
    code = run_delta(
        tmp_path,
        build_post(100, [TIMEOUT] * 2 + [NO_MARKER] * 38, 30),
        make_baseline(),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "did not fall" in out


def test_check_delta_refuses_a_post_start_earlier_than_the_landing(tmp_path, capsys):
    code = run_delta(
        tmp_path,
        build_post(100, [TIMEOUT] * 3 + [NO_MARKER] * 2, 2),
        make_baseline(),
        post_start="2026-08-20T00:00:00+00:00",
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "may only be moved later" in out


def test_check_delta_fails_closed_before_unit_1_lands(tmp_path, capsys):
    """No row carries outcome_class yet: the honest answer is 'not yet
    measurable', never a pass by default."""
    post = [row(at(POST_BASE, 7200 * i), "developer", reason="ok") for i in range(100)]
    code = run_delta(tmp_path, post, make_baseline())
    out = capsys.readouterr().out
    assert code == 2
    assert "not reached production traffic" in out.lower() or "Not yet measurable" in out


def test_check_delta_fails_on_the_timeout_ceiling_alone(tmp_path, capsys):
    """12 of 100 post rows are still the extractor's own timeout — above the
    0.1094 the 60% recovery assumption allows at n=100 — while both rates fell
    significantly and no new cause class appeared."""
    code = run_delta(tmp_path, build_post(100, [TIMEOUT] * 12, 2), make_baseline())
    out = capsys.readouterr().out
    assert code == 1
    assert "extractor-timeout class still holds" in out
    assert "did not fall" not in out
    assert "substituted one failure class" not in out


def test_check_delta_fails_on_the_substitution_ceiling_alone(tmp_path, capsys):
    """A cause class absent from the frozen breakdown holds 7 of 20 post
    attributable rows — a Wilson one-sided 95% lower bound above the ceiling —
    while both rates fell and the timeout class is gone entirely."""
    reasons = [UNRECOGNISED] * 7 + [NO_MARKER] * 13
    code = run_delta(tmp_path, build_post(450, reasons, 2), make_baseline())
    out = capsys.readouterr().out
    assert code == 1
    assert "substituted one failure class" in out
    assert "did not fall" not in out
    assert "extractor-timeout class still holds" not in out


def test_check_delta_passes_when_every_condition_holds(tmp_path, capsys):
    code = run_delta(
        tmp_path,
        build_post(100, [TIMEOUT] * 3 + [NO_MARKER] * 2, 2),
        make_baseline(),
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS" in out
    assert "rows excluded in the freeze-to-landing gap: 1" in out
