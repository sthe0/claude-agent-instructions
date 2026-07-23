"""Unit tests for hook-phase3-due.py: baseline establishment (write-once),
throttle bookkeeping (stamped regardless of verdict), fail-open subprocess
invocation, and the emit-on-DUE / no-op-on-NOT-DUE / --dry-run / --force-run
CLI modes.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-phase3-due.py"
_spec = importlib.util.spec_from_file_location("phase3_due", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
sys.modules["phase3_due"] = hook
_spec.loader.exec_module(hook)

_DUE_OUT = (
    "phase3-readiness: DUE - pressure: surface 90000 chars exceeds the 80000-char budget\n"
    "  pressure:         surface 90000 / budget 80000 chars (overshoot 10000)\n"
    "  data-sufficiency: 1200 session(s) / floor 30; baseline age 20.0 day(s) / window 14 day(s)\n"
    "  reclaimable:      12000 char(s) across 9 OBSERVED tier>=1 unit(s)\n"
)
_NOT_DUE_OUT = (
    "phase3-readiness: NOT-DUE - pressure: surface 70000 chars is within the 80000-char advisory budget (overshoot -10000)\n"
    "  pressure:         surface 70000 / budget 80000 chars (overshoot -10000)\n"
)


def _make_script(tmp_path, name, body):
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _wire_stamps(tmp_path, monkeypatch):
    baseline = tmp_path / "state" / "baseline"
    throttle = tmp_path / "state" / "throttle"
    monkeypatch.setattr(hook, "BASELINE_STAMP", baseline)
    monkeypatch.setattr(hook, "THROTTLE_STAMP", throttle)
    return baseline, throttle


# ── run_check ────────────────────────────────────────────────────────────────

def test_run_check_returns_stdout(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "fake.py", f"print({_DUE_OUT!r}, end='')")
    monkeypatch.setattr(hook, "CHECKER", script)
    assert hook.run_check() == _DUE_OUT.strip()


def test_run_check_missing_script_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "CHECKER", tmp_path / "does-not-exist.py")
    assert hook.run_check() is None


def test_run_check_timeout_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "slow.py", "import time\ntime.sleep(5)")
    monkeypatch.setattr(hook, "CHECKER", script)
    monkeypatch.setattr(hook, "CHECK_TIMEOUT_S", 0.2)
    assert hook.run_check() is None


def test_run_check_crash_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "crash.py", "raise RuntimeError('boom')")
    monkeypatch.setattr(hook, "CHECKER", script)
    assert hook.run_check() is None


# ── stamp bookkeeping ────────────────────────────────────────────────────────

def test_throttle_stamp_roundtrip(tmp_path, monkeypatch):
    _, throttle = _wire_stamps(tmp_path, monkeypatch)
    assert hook.last_throttle() is None
    now = dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc)
    hook.record_throttle(now)
    assert hook.last_throttle() == now


def test_last_throttle_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "THROTTLE_STAMP", tmp_path / "does-not-exist")
    assert hook.last_throttle() is None


def test_ensure_baseline_creates_when_absent(tmp_path, monkeypatch):
    baseline, _ = _wire_stamps(tmp_path, monkeypatch)
    now = dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc)
    hook.ensure_baseline(now)
    assert baseline.exists()
    assert baseline.read_text(encoding="utf-8").strip() == now.isoformat()


def test_ensure_baseline_never_overwrites(tmp_path, monkeypatch):
    baseline, _ = _wire_stamps(tmp_path, monkeypatch)
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text("2020-01-01T00:00:00+00:00", encoding="utf-8")
    hook.ensure_baseline(dt.datetime.now(dt.timezone.utc))
    assert baseline.read_text(encoding="utf-8").strip() == "2020-01-01T00:00:00+00:00"


# ── main() CLI wiring ────────────────────────────────────────────────────────

def test_main_emits_on_due(tmp_path, monkeypatch, capsys):
    _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _DUE_OUT)
    assert hook.main([]) == 0
    err = capsys.readouterr().err
    assert "[phase3-due]" in err
    assert "reclaimable" in err


def test_main_silent_on_not_due(tmp_path, monkeypatch, capsys):
    _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _NOT_DUE_OUT)
    assert hook.main([]) == 0
    assert capsys.readouterr().err == ""


def test_main_establishes_baseline_on_first_run(tmp_path, monkeypatch):
    baseline, _ = _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _NOT_DUE_OUT)
    assert not baseline.exists()
    hook.main([])
    assert baseline.exists()


def test_main_throttled_skips_check(tmp_path, monkeypatch):
    _, throttle = _wire_stamps(tmp_path, monkeypatch)
    hook.record_throttle(dt.datetime.now(dt.timezone.utc))
    calls = []
    monkeypatch.setattr(hook, "run_check", lambda: (calls.append(1), _DUE_OUT)[1])
    assert hook.main([]) == 0
    assert calls == []


def test_main_past_throttle_window_checks_and_restamps(tmp_path, monkeypatch):
    _wire_stamps(tmp_path, monkeypatch)
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=hook.THROTTLE_DAYS, seconds=1)
    hook.record_throttle(stale)
    monkeypatch.setattr(hook, "run_check", lambda: _NOT_DUE_OUT)
    hook.main([])
    assert hook.last_throttle() > stale


def test_main_throttle_stamp_written_regardless_of_verdict(tmp_path, monkeypatch):
    _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _DUE_OUT)
    hook.main([])
    assert hook.last_throttle() is not None


def test_main_force_run_bypasses_throttle_without_consuming(tmp_path, monkeypatch, capsys):
    _wire_stamps(tmp_path, monkeypatch)
    now = dt.datetime.now(dt.timezone.utc)
    hook.record_throttle(now)
    monkeypatch.setattr(hook, "run_check", lambda: _DUE_OUT)
    assert hook.main(["--force-run"]) == 0
    assert hook.last_throttle() == now
    assert "[phase3-due]" in capsys.readouterr().err


def test_main_force_run_still_establishes_baseline(tmp_path, monkeypatch):
    baseline, _ = _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _NOT_DUE_OUT)
    hook.main(["--force-run"])
    assert baseline.exists()


def test_main_dry_run_never_touches_either_stamp(tmp_path, monkeypatch):
    baseline, throttle = _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _DUE_OUT)
    assert hook.main(["--dry-run"]) == 0
    assert not baseline.exists()
    assert not throttle.exists()


def test_main_dry_run_still_reports_due(tmp_path, monkeypatch, capsys):
    _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "run_check", lambda: _DUE_OUT)
    assert hook.main(["--dry-run"]) == 0
    assert "[phase3-due]" in capsys.readouterr().err


# ── fail-open ────────────────────────────────────────────────────────────────

def test_main_fails_open_when_run_check_raises(tmp_path, monkeypatch):
    _wire_stamps(tmp_path, monkeypatch)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "run_check", _raise)
    assert hook.main([]) == 0


def test_main_fails_open_when_stamp_dir_unwritable(tmp_path, monkeypatch):
    # A path whose parent is a file, not a dir, makes mkdir raise NotADirectoryError
    # (an OSError subclass) — ensure_baseline/record_throttle must swallow it.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(hook, "BASELINE_STAMP", blocker / "baseline")
    monkeypatch.setattr(hook, "THROTTLE_STAMP", blocker / "throttle")
    monkeypatch.setattr(hook, "run_check", lambda: _NOT_DUE_OUT)
    assert hook.main([]) == 0


def test_main_missing_checker_fails_open_silently(tmp_path, monkeypatch, capsys):
    _wire_stamps(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "CHECKER", tmp_path / "does-not-exist.py")
    assert hook.main([]) == 0
    assert capsys.readouterr().err == ""


# ── report() bounding ────────────────────────────────────────────────────────

def test_report_caps_printed_lines(capsys):
    text = "phase3-readiness: DUE - x\n" + "\n".join(
        f"  detail line {i}" for i in range(hook.MAX_PRINTED_LINES + 3)
    )
    hook.report(text)
    err = capsys.readouterr().err
    assert "more line(s)" in err


# ── coupling: stamp path must match the predicate's reader ──────────────────

def test_baseline_stamp_matches_the_predicates_reader():
    """rule-salience-report.py stays write-free re: this stamp (its own module
    contract) but reads it via BASELINE_STAMP_PATH — that path must be exactly
    what this hook writes, or the predicate reads an eternally-absent baseline.
    """
    rsr_path = Path(__file__).resolve().parent.parent / "rule-salience-report.py"
    spec = importlib.util.spec_from_file_location("rule_salience_report_coupling_check", rsr_path)
    rsr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rsr)
    assert hook.BASELINE_STAMP == rsr.BASELINE_STAMP_PATH
