"""Tests for the Phase-3 readiness predicate in scripts/rule-salience-report.py.

Covers: each arm of `phase3_readiness` in both directions (the reason string is
pinned, not just the boolean, so an arm cannot silently start failing for the
wrong cause); `reclaimable_chars`'s conservative accounting (OBSERVED-only,
tier>=1-only, each unit counted once); that the DUE branch is reachable by
SURFACE GROWTH ALONE with no constant edited; that `--check-due` exits 0 on
every path including DUE; and the two cross-script couplings the extraction
created - `surface_breakdown()` as the single source of the surface number, and
self-diagnose.py's UNSTUBBED use of lint-prose-length.py's public names."""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: self-diagnose.py builds a dataclass with string
    # annotations, and dataclasses resolves those through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rsr = _load("rule_salience_report", "rule-salience-report.py")
lint = _load("lint_prose_length", "lint-prose-length.py")


# ---------------------------------------------------------------------------
# Fixtures — a synthetic CLAUDE.md and registry, so no test depends on the
# real kernel's current wording or size.
# ---------------------------------------------------------------------------

# `## Solo` holds ONE bold-lead unit; `## Shared` holds one bold-lead unit
# covered by TWO registry entries — the double-counting case the arm must not
# inflate. Every rule body is padded past POINTER_ALLOWANCE_CHARS so a real,
# non-zero gain survives the pointer deduction.
_PAD = " padding to clear the pointer allowance." * 6

CLAUDE_MD_TEXT = f"""# Title

## Solo

**Solo rule.** locator-solo{_PAD}

## Shared

**Shared rule.** locator-shared-one and locator-shared-two{_PAD}
"""


def _rule(rule_id: str, phrase: str, tier: int = 1) -> dict:
    return {"id": rule_id, "tier": tier, "locator_phrase": phrase}


def _units() -> list[dict]:
    return rsr.enumerate_rule_units(CLAUDE_MD_TEXT)


def _readiness_kwargs(**overrides):
    """A DUE-by-default input set; each test flips exactly the arm it tests."""
    base = dict(
        surface_chars=90_000,
        surface_budget=80_000,
        reclaimable=20_000,
        sessions_scanned=30,
        sessions_floor=30,
        baseline_age_days=14.0,
        window_days=14,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# phase3_readiness — one test per arm, both directions, reason pinned
# ---------------------------------------------------------------------------

def test_due_when_all_three_arms_pass():
    due, reason, metrics = rsr.phase3_readiness(**_readiness_kwargs())
    assert due is True
    assert "surface exceeds its budget by 10000 chars" in reason
    assert metrics["overshoot"] == 10_000


def test_not_due_when_surface_is_within_budget():
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(surface_chars=70_000))
    assert due is False
    assert reason.startswith("pressure:")


def test_not_due_when_surface_exactly_equals_budget():
    """The boundary belongs to NOT-DUE: at zero overshoot there is nothing to
    buy, so compression would be a cost with no benefit."""
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(surface_chars=80_000))
    assert due is False
    assert reason.startswith("pressure:")


def test_not_due_without_a_baseline_stamp():
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(baseline_age_days=None))
    assert due is False
    assert reason.startswith("data-sufficiency:")
    assert "no baseline stamp yet" in reason


def test_not_due_below_the_session_floor():
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(sessions_scanned=29))
    assert due is False
    assert reason.startswith("data-sufficiency:")
    assert "below the 30-session floor" in reason


def test_not_due_inside_the_day_window():
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(baseline_age_days=13.9))
    assert due is False
    assert reason.startswith("data-sufficiency:")
    assert "below the 14-day window" in reason


def test_not_due_when_reclaimable_is_below_the_overshoot():
    due, reason, _ = rsr.phase3_readiness(**_readiness_kwargs(reclaimable=9_999))
    assert due is False
    assert reason.startswith("reclaimable:")


def test_pressure_arm_is_checked_before_data_sufficiency():
    """Arm order is part of the contract: an un-pressured surface must report
    'nothing to buy', not 'come back with more data' — the second reads as a
    promise that compression starts once the window elapses."""
    due, reason, _ = rsr.phase3_readiness(
        **_readiness_kwargs(surface_chars=70_000, baseline_age_days=None, sessions_scanned=0)
    )
    assert due is False
    assert reason.startswith("pressure:")


def test_data_sufficiency_is_checked_before_reclaimable():
    """A thin sample must not be reported as 'not enough reclaimable prose':
    the reclaimable number is only as trustworthy as the firing data under it."""
    due, reason, _ = rsr.phase3_readiness(
        **_readiness_kwargs(sessions_scanned=0, reclaimable=0)
    )
    assert due is False
    assert reason.startswith("data-sufficiency:")


def test_predicate_is_pure(tmp_path, monkeypatch):
    """No clock, no config read, no filesystem: same inputs, same verdict, with
    HOME repointed at an empty directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    first = rsr.phase3_readiness(**_readiness_kwargs())
    second = rsr.phase3_readiness(**_readiness_kwargs())
    assert first == second


# ---------------------------------------------------------------------------
# reclaimable_chars — conservative, one-sided accounting
# ---------------------------------------------------------------------------

def test_observed_tier1_unit_contributes_its_length_less_the_pointer_allowance():
    rules = [_rule("solo", "locator-solo")]
    chars, detail = rsr.reclaimable_chars(rules, _units(), {"solo": "OBSERVED"})
    unit = next(u for u in _units() if "locator-solo" in u["text"])
    assert chars == len(unit["text"]) - rsr.POINTER_ALLOWANCE_CHARS
    assert [d["entries"] for d in detail] == [["solo"]]


def test_multiply_covered_unit_is_counted_once():
    rules = [_rule("shared-one", "locator-shared-one"), _rule("shared-two", "locator-shared-two")]
    chars, detail = rsr.reclaimable_chars(
        rules, _units(), {"shared-one": "OBSERVED", "shared-two": "OBSERVED"}
    )
    unit = next(u for u in _units() if "locator-shared-one" in u["text"])
    assert len(detail) == 1
    assert chars == len(unit["text"]) - rsr.POINTER_ALLOWANCE_CHARS


@pytest.mark.parametrize("state", ["NEVER-OBSERVED", "TRIGGER-ABSENT", "UNINSTRUMENTED"])
def test_non_observed_states_never_contribute(state):
    """NEVER-OBSERVED especially: it says the mechanism was watched and never
    fired, so the prose may be the only thing delivering the rule. Reading it
    as a compress signal is the regression this programme exists to prevent."""
    rules = [_rule("solo", "locator-solo")]
    chars, detail = rsr.reclaimable_chars(rules, _units(), {"solo": state})
    assert (chars, detail) == (0, [])


def test_tier0_coverage_never_contributes():
    rules = [_rule("solo", "locator-solo", tier=0)]
    chars, detail = rsr.reclaimable_chars(rules, _units(), {"solo": "OBSERVED"})
    assert (chars, detail) == (0, [])


def test_one_unproven_covering_entry_disqualifies_the_whole_unit():
    """Compressing the unit would compress BOTH rules' prose, so the weakest
    covering entry decides — the conservative direction."""
    rules = [_rule("shared-one", "locator-shared-one"), _rule("shared-two", "locator-shared-two")]
    chars, detail = rsr.reclaimable_chars(
        rules, _units(), {"shared-one": "OBSERVED", "shared-two": "NEVER-OBSERVED"}
    )
    assert (chars, detail) == (0, [])


def test_uncovered_unit_never_contributes():
    chars, detail = rsr.reclaimable_chars([], _units(), {})
    assert (chars, detail) == (0, [])


def test_unit_shorter_than_the_pointer_allowance_yields_no_gain():
    """Below the allowance the pointer form is not smaller than the prose, so
    the gain floors at zero rather than going negative."""
    text = "# T\n\n## S\n\n**Tiny.** locator-tiny\n"
    rules = [_rule("tiny", "locator-tiny")]
    chars, detail = rsr.reclaimable_chars(
        rules, rsr.enumerate_rule_units(text), {"tiny": "OBSERVED"}
    )
    assert chars == 0
    assert detail and detail[0]["chars"] == 0


# ---------------------------------------------------------------------------
# The DUE branch must be reachable by growth alone
# ---------------------------------------------------------------------------

def test_growth_alone_flips_the_verdict_with_no_constant_edited():
    """Self-scaling control: budget, floor and window are read from the REAL
    config.md and the reclaimable number from the REAL `reclaimable_chars`.
    Only the surface size moves — so this proves DUE is reachable by the
    surface growing, not by a threshold being tuned down to meet it."""
    constants = lint.parse_config_md()
    budget = int(constants["always-loaded-surface-advisory-chars"])
    floor = int(constants["phase3-due-sessions-floor"])
    window = int(constants["phase3-due-data-window-days"])

    rules = [_rule("solo", "locator-solo")]
    reclaimable, _ = rsr.reclaimable_chars(rules, _units(), {"solo": "OBSERVED"})
    assert reclaimable > 0

    settled = dict(
        surface_budget=budget,
        reclaimable=reclaimable,
        sessions_scanned=floor,
        sessions_floor=floor,
        baseline_age_days=float(window),
        window_days=window,
    )

    due, reason, _ = rsr.phase3_readiness(surface_chars=budget - 1, **settled)
    assert due is False
    assert reason.startswith("pressure:")

    due, _, metrics = rsr.phase3_readiness(surface_chars=budget + reclaimable, **settled)
    assert due is True
    assert metrics["overshoot"] == reclaimable


# ---------------------------------------------------------------------------
# baseline stamp — I/O and clock live outside the predicate
# ---------------------------------------------------------------------------

def test_baseline_age_is_none_when_the_stamp_is_absent(tmp_path):
    assert rsr.read_baseline_age_days(tmp_path / "missing.stamp") is None


def test_baseline_age_is_none_when_the_stamp_is_unparseable(tmp_path):
    stamp = tmp_path / "baseline.stamp"
    stamp.write_text("not a timestamp", encoding="utf-8")
    assert rsr.read_baseline_age_days(stamp) is None


def test_baseline_age_reads_a_stamp_written_without_a_timezone(tmp_path):
    """A naive stamp is read as UTC rather than raising: this function feeds a
    check that must never fail, and the writer is a separate script."""
    now = dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc)
    stamp = tmp_path / "baseline.stamp"
    stamp.write_text("2026-07-09T00:00:00", encoding="utf-8")
    assert rsr.read_baseline_age_days(stamp, now=now) == pytest.approx(14.0)


def test_baseline_age_counts_days_since_the_stamp(tmp_path):
    now = dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc)
    stamp = tmp_path / "baseline.stamp"
    stamp.write_text((now - dt.timedelta(days=21)).isoformat(), encoding="utf-8")
    assert rsr.read_baseline_age_days(stamp, now=now) == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# --check-due reports, never gates
# ---------------------------------------------------------------------------

def _due_inputs(**overrides) -> dict:
    inputs = dict(
        surface_chars=90_000,
        surface_budget=80_000,
        reclaimable=20_000,
        detail=[{"kind": "bold-lead", "heading": "## H", "line": 12, "chars": 20_000,
                 "entries": ["some-rule"]}],
        sessions_scanned=30,
        sessions_floor=30,
        baseline_age_days=21.0,
        window_days=14,
    )
    inputs.update(overrides)
    return inputs


def test_check_due_exits_zero_when_due(monkeypatch, capsys):
    """The DUE path is the one that must not gate: a sentinel that can fail a
    build is a sentinel someone disables the first time it fires."""
    monkeypatch.setattr(rsr, "collect_due_inputs", lambda *a, **k: _due_inputs())
    assert rsr.main(["--check-due"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("phase3-readiness: DUE - ")
    assert "exits 0 on every path" in out


def test_check_due_exits_zero_when_not_due(monkeypatch, capsys):
    monkeypatch.setattr(
        rsr, "collect_due_inputs", lambda *a, **k: _due_inputs(surface_chars=10_000)
    )
    assert rsr.main(["--check-due"]) == 0
    assert capsys.readouterr().out.startswith("phase3-readiness: NOT-DUE - pressure:")


def test_check_due_exits_zero_and_names_the_gap_when_inputs_are_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(
        rsr, "collect_due_inputs",
        lambda *a, **k: _due_inputs(surface_chars=None, sessions_floor=None),
    )
    assert rsr.main(["--check-due"]) == 0
    out = capsys.readouterr().out
    assert "NOT-DUE - inputs unavailable: surface_chars, sessions_floor" in out


def test_check_due_verdict_states_the_observed_only_rule(monkeypatch, capsys):
    monkeypatch.setattr(rsr, "collect_due_inputs", lambda *a, **k: _due_inputs())
    rsr.main(["--check-due"])
    assert "NEVER-OBSERVED is a RISK signal" in capsys.readouterr().out


def test_check_due_exits_zero_when_the_surface_source_is_broken(monkeypatch, capsys):
    """Fail open: if lint-prose-length.py cannot be loaded or raises, the
    verdict degrades to NOT-DUE naming the missing input — it does not
    traceback out of the session that called it."""
    class _Exploding:
        def parse_config_md(self):
            raise RuntimeError("config.md is unreadable")

        def surface_breakdown(self):
            raise RuntimeError("surface unmeasurable")

    monkeypatch.setattr(rsr, "_load_lint_prose_length", lambda: _Exploding())
    assert rsr.main(["--check-due"]) == 0
    assert "NOT-DUE - inputs unavailable:" in capsys.readouterr().out


def test_check_due_detail_output_is_bounded(monkeypatch, capsys):
    """The verdict is read from a hook injection, so its attribution list must
    not grow without bound; the omitted units stay counted in the total."""
    detail = [
        {"kind": "bullet", "heading": "## H", "line": n, "chars": 1_000 + n, "entries": [f"r{n}"]}
        for n in range(rsr.MAX_DETAIL_ROWS + 5)
    ]
    monkeypatch.setattr(rsr, "collect_due_inputs", lambda *a, **k: _due_inputs(detail=detail))
    assert rsr.main(["--check-due"]) == 0
    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if "chars  bullet" in ln]) == rsr.MAX_DETAIL_ROWS
    assert "and 5 smaller unit(s), not listed" in out


def test_check_due_against_the_real_repo_exits_zero(capsys):
    """End to end, no monkeypatching: whatever today's real surface, firing
    data and (absent) stamp say, the check reports and returns 0."""
    assert rsr.main(["--check-due"]) == 0
    assert capsys.readouterr().out.startswith("phase3-readiness: ")


# ---------------------------------------------------------------------------
# Cross-script couplings created by the extraction
# ---------------------------------------------------------------------------

def test_surface_breakdown_total_is_the_number_the_report_prints(capsys):
    """One surface number, one source: the predicate's pressure arm and the
    human-readable report must never be able to disagree."""
    _, total = lint.surface_breakdown()
    assert lint.main(["--surface-report"]) == 0
    printed = [
        line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("TOTAL:")
    ]
    assert printed == [f"  TOTAL: {total} chars"]


def test_surface_breakdown_rows_sum_to_its_total():
    rows, total = lint.surface_breakdown()
    assert rows
    assert sum(n for _, n in rows) == total


def test_self_diagnose_real_lint_coupling():
    """self-diagnose.py loads lint-prose-length.py BY PATH and calls
    parse_config_md / check_level / GOVERNED on it. Its own test suite stubs
    that loader, so only an unstubbed run can catch a renamed public name."""
    diag = _load("self_diagnose", "self-diagnose.py")
    mod = diag._load_lint_prose_length(REPO_ROOT)
    assert mod is not None
    for name in ("parse_config_md", "check_level", "GOVERNED"):
        assert hasattr(mod, name), f"self-diagnose.py depends on {name}"
    assert isinstance(diag.scan_ceiling_proximity(REPO_ROOT), list)
