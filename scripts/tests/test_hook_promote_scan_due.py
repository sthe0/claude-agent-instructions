"""Unit tests for hook-promote-scan-due.py: throttle bookkeeping, fail-open
scanner invocation, flagged-cluster reporting, and the --dry-run/--force-run modes.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-promote-scan-due.py"
_spec = importlib.util.spec_from_file_location("promote_scan_due", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
sys.modules["promote_scan_due"] = hook
_spec.loader.exec_module(hook)


def _make_script(tmp_path, name, body):
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


_FLAGGED_CLUSTER = {
    "occurrences_total": 3,
    "members": ["2026-08-11-loop.md", "2026-08-17-loop.md", "2026-08-20-loop.md"],
    "flagged": True,
    "fragmented": True,
}
_UNFLAGGED_CLUSTER = {
    "occurrences_total": 2,
    "members": ["2026-07-01-foo.md", "2026-07-10-foo.md"],
    "flagged": False,
    "fragmented": True,
}


# ── run_scanner ───────────────────────────────────────────────────────────────

def test_run_scanner_returns_clusters(tmp_path, monkeypatch):
    clusters = [_FLAGGED_CLUSTER]
    script = _make_script(
        tmp_path, "fake.py",
        f"import json, sys; print(json.dumps({clusters!r})); sys.exit(0)",
    )
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    assert hook.run_scanner() == clusters


def test_run_scanner_missing_script_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", tmp_path / "does-not-exist.py")
    assert hook.run_scanner() is None


def test_run_scanner_crash_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "crash.py", "raise RuntimeError('boom')")
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    assert hook.run_scanner() is None


def test_run_scanner_timeout_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "slow.py", "import time; time.sleep(5)")
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    monkeypatch.setattr(hook, "SCAN_TIMEOUT_S", 0.2)
    assert hook.run_scanner() is None


def test_run_scanner_bad_json_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "bad.py", "print('not json')")
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    assert hook.run_scanner() is None


def test_run_scanner_non_list_json_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "bad.py", "print('{\"flagged\": true}')")
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    assert hook.run_scanner() is None


def test_run_scanner_nonzero_exit_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "err.py", "import sys; sys.exit(2)")
    monkeypatch.setattr(hook, "RECORD_EXPERIENCE", script)
    assert hook.run_scanner() is None


# ── stamp bookkeeping ─────────────────────────────────────────────────────────

def test_stamp_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "state" / "stamp")
    assert hook.last_run() is None
    hook.record_run(1000.0)
    assert hook.last_run() == 1000.0


def test_last_run_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "does-not-exist")
    assert hook.last_run() is None


# ── main() CLI wiring ─────────────────────────────────────────────────────────

def test_main_throttled_within_7_days_skips_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    calls = []
    monkeypatch.setattr(hook, "run_scanner", lambda: calls.append(1) or [])
    assert hook.main([]) == 0
    assert calls == []


def test_main_past_throttle_window_scans_and_restamps(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time() - hook.THROTTLE_DAYS * 86400.0 - 1.0)
    monkeypatch.setattr(hook, "run_scanner", lambda: [])
    before = hook.last_run()
    assert hook.main([]) == 0
    assert hook.last_run() > before


def test_main_force_run_bypasses_throttle_without_consuming(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.record_run(time.time())
    before = hook.last_run()
    monkeypatch.setattr(hook, "run_scanner", lambda: [_FLAGGED_CLUSTER])
    assert hook.main(["--force-run"]) == 0
    assert hook.last_run() == before


def test_main_dry_run_never_touches_stamp(tmp_path, monkeypatch):
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(hook, "STAMP", stamp)
    monkeypatch.setattr(hook, "run_scanner", lambda: [])
    assert hook.main(["--dry-run"]) == 0
    assert not stamp.exists()


def test_main_reports_flagged_cluster_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(hook, "run_scanner", lambda: [_FLAGGED_CLUSTER])
    assert hook.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "1 principle-induction candidate(s)" in err
    assert "2026-08-11-loop.md" in err


def test_main_unflagged_only_result_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(hook, "run_scanner", lambda: [_UNFLAGGED_CLUSTER])
    assert hook.main(["--dry-run"]) == 0
    assert capsys.readouterr().err == ""


def test_main_scanner_failure_is_silent_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(hook, "run_scanner", lambda: None)
    assert hook.main(["--force-run"]) == 0
    assert capsys.readouterr().err == ""


def test_report_caps_printed_clusters(capsys):
    many_flagged = [
        {
            "occurrences_total": 3,
            "members": [f"leaf{i}.md"],
            "flagged": True,
            "fragmented": False,
        }
        for i in range(hook.MAX_PRINTED_CLUSTERS + 4)
    ]
    hook.report(many_flagged)
    err = capsys.readouterr().err
    assert "... and 4 more" in err
