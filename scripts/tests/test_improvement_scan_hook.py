"""Unit tests for hook-improvement-scan-due.py: throttle bookkeeping, the
--dry-run/--force-run modes, fail-open behavior on a missing/broken store,
the nudge text naming the skill, and the never-invokes-the-scan-itself
invariant the plan's own verify command re-checks independently.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-improvement-scan-due.py"
_spec = importlib.util.spec_from_file_location("improvement_scan_due", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
sys.modules["improvement_scan_due"] = hook
_spec.loader.exec_module(hook)

_OPEN_FINDING = {"key": "abc123", "kind": "backlog-item", "source": "improvement-scan"}


# ── stamp bookkeeping / throttle ────────────────────────────────────────────

def test_main_throttled_within_window_skips_report(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    monkeypatch.setattr(hook, "open_findings", lambda: [_OPEN_FINDING])
    calls = []
    monkeypatch.setattr(hook, "report", lambda findings: calls.append(findings))
    assert hook.main([]) == 0
    assert calls == []


def test_main_past_throttle_window_reports_and_restamps(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time() - hook.THROTTLE_DAYS * 86400.0 - 1.0)
    monkeypatch.setattr(hook, "open_findings", lambda: [_OPEN_FINDING])
    before = hook.last_run()
    assert hook.main([]) == 0
    assert hook.last_run() > before


# ── --force-run bypasses the throttle without consuming it ─────────────────

def test_main_force_run_bypasses_throttle_without_consuming(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    before = hook.last_run()
    monkeypatch.setattr(hook, "open_findings", lambda: [_OPEN_FINDING])
    assert hook.main(["--force-run"]) == 0
    assert hook.last_run() == before
    assert "improvement-scan" in capsys.readouterr().err


# ── --dry-run never writes the stamp ────────────────────────────────────────

def test_main_dry_run_never_writes_stamp(tmp_path, monkeypatch):
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(hook, "STAMP", stamp)
    monkeypatch.setattr(hook, "open_findings", lambda: [_OPEN_FINDING])
    assert hook.main(["--dry-run"]) == 0
    assert not stamp.exists()


def test_main_dry_run_ignores_existing_throttle(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    monkeypatch.setattr(hook, "open_findings", lambda: [_OPEN_FINDING])
    assert hook.main(["--dry-run"]) == 0
    assert "improvement-scan" in capsys.readouterr().err


# ── fail-open on a missing/broken store ─────────────────────────────────────

def test_open_findings_returns_none_when_store_module_missing(monkeypatch):
    monkeypatch.setattr(hook, "store", None)
    assert hook.open_findings() is None


def test_open_findings_returns_none_on_broken_store(monkeypatch):
    class _BrokenStore:
        SOURCE_IMPROVEMENT_SCAN = "improvement-scan"

        def load_rows(self):
            raise OSError("store unreadable")

    monkeypatch.setattr(hook, "store", _BrokenStore())
    assert hook.open_findings() is None


def test_main_fails_open_when_store_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(hook, "store", None)
    assert hook.main(["--force-run"]) == 0
    assert hook.main(["--dry-run"]) == 0
    assert hook.main([]) == 0


# ── nudge text names the skill ──────────────────────────────────────────────

def test_report_names_the_skill_and_finding_count(capsys):
    hook.report([_OPEN_FINDING, dict(_OPEN_FINDING, key="def456")])
    err = capsys.readouterr().err
    assert "improvement-scan" in err
    assert "2" in err


def test_report_empty_findings_prints_nothing(capsys):
    hook.report([])
    assert capsys.readouterr().err == ""


# ── the hook must never invoke the scan CLI itself ──────────────────────────

def test_hook_never_mentions_the_scan_cli_by_filename():
    text = _HOOK_PATH.read_text(encoding="utf-8")
    assert "improvement-scan.py" not in text
