"""Unit tests for hook-self-diagnose-due.py: throttle bookkeeping, fail-open
scanner invocation, and the --dry-run/--force-run CLI modes.
"""
from __future__ import annotations

import importlib.util
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-self-diagnose-due.py"
_spec = importlib.util.spec_from_file_location("self_diagnose_due", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
sys.modules["self_diagnose_due"] = hook
_spec.loader.exec_module(hook)


def _make_script(tmp_path, name, body):
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# ── run_scanner ──────────────────────────────────────────────────────────────

def test_run_scanner_returns_stdout_lines(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "fake.py", "print('a: b — c')\nprint('d: e — f')\nraise SystemExit(1)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() == ["a: b — c", "d: e — f"]


def test_run_scanner_clean_tree_is_empty(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "fake.py", "raise SystemExit(0)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() == []


def test_run_scanner_missing_script_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", tmp_path / "does-not-exist.py")
    assert hook.run_scanner() == []


def test_run_scanner_timeout_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "slow.py", "import time\ntime.sleep(5)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    monkeypatch.setattr(hook, "SCAN_TIMEOUT_S", 0.2)
    assert hook.run_scanner() == []


def test_run_scanner_crash_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "crash.py", "raise RuntimeError('boom')")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    # a non-zero exit with stderr noise but no stdout still yields no findings
    assert hook.run_scanner() == []


# ── stamp bookkeeping ────────────────────────────────────────────────────────

def test_stamp_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "state" / "stamp")
    assert hook.last_run() is None
    hook.record_run(1000.0)
    assert hook.last_run() == 1000.0


def test_last_run_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "does-not-exist")
    assert hook.last_run() is None


# ── main() CLI wiring ────────────────────────────────────────────────────────

def test_main_throttled_skips_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    calls = []
    monkeypatch.setattr(hook, "run_scanner", lambda: calls.append(1) or [])
    assert hook.main([]) == 0
    assert calls == []


def test_main_past_throttle_window_scans_and_restamps(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time() - hook.THROTTLE_HOURS * 3600.0 - 1.0)
    monkeypatch.setattr(hook, "run_scanner", lambda: [])
    before = hook.last_run()
    assert hook.main([]) == 0
    assert hook.last_run() > before


def test_main_force_run_bypasses_throttle_without_consuming(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    before = hook.last_run()
    monkeypatch.setattr(hook, "run_scanner", lambda: ["x: y — z"])
    assert hook.main(["--force-run"]) == 0
    assert hook.last_run() == before


def test_main_dry_run_never_touches_stamp(tmp_path, monkeypatch):
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(hook, "STAMP", stamp)
    monkeypatch.setattr(hook, "run_scanner", lambda: [])
    assert hook.main(["--dry-run"]) == 0
    assert not stamp.exists()


def test_main_reports_findings_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(hook, "run_scanner", lambda: ["oversized-index: foo — 300 lines > 200"])
    assert hook.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "1 self-friction item(s)" in err
    assert "oversized-index: foo" in err


def test_report_caps_printed_lines(monkeypatch, capsys):
    findings = [f"kind: path{i} — detail" for i in range(hook.MAX_PRINTED_LINES + 3)]
    hook.report(findings)
    err = capsys.readouterr().err
    assert "... and 3 more" in err
