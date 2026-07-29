"""Unit tests for hook-self-diagnose-due.py: throttle bookkeeping, fail-open
scanner invocation, and the --dry-run/--force-run CLI modes.
"""
from __future__ import annotations

import importlib.util
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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

def test_run_scanner_returns_records(tmp_path, monkeypatch):
    script = _make_script(
        tmp_path,
        "fake.py",
        "print('[{\"kind\": \"orphan-leaf\", \"path\": \"b\", \"detail\": \"c\"}]')\n"
        "raise SystemExit(1)",
    )
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() == [{"kind": "orphan-leaf", "path": "b", "detail": "c"}]


def test_run_scanner_clean_tree_is_an_empty_list(tmp_path, monkeypatch):
    """A COMPLETED scan over a clean tree — the one case that may resolve rows out."""
    script = _make_script(tmp_path, "fake.py", "print('[]')\nraise SystemExit(0)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() == []


def test_run_scanner_unparseable_output_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "fake.py", "print('not json')\nraise SystemExit(1)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() is None


def test_run_scanner_non_list_json_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "fake.py", "print('{\"kind\": \"x\"}')")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    assert hook.run_scanner() is None


def test_run_scanner_missing_script_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", tmp_path / "does-not-exist.py")
    assert hook.run_scanner() is None


def test_run_scanner_timeout_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "slow.py", "import time\ntime.sleep(5)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    monkeypatch.setattr(hook, "SCAN_TIMEOUT_S", 0.2)
    assert hook.run_scanner() is None


def test_run_scanner_argv_is_pinned(tmp_path, monkeypatch):
    """Pin the SCAN's own argv, not just its output handling.

    Every other test here stubs SELF_DIAGNOSE and inspects what run_scanner does
    with the RESULT, so all of them stay green if a future edit adds
    `--settings-path` or `--no-hooks` to this invocation. Such a flag changes what
    the scan COVERS while the hook keeps reporting a clean tree, silently
    falsifying the label any control built on this hook carries — the scan says
    "the tree is clean" when it means "the subset I still look at is clean"."""
    script = _make_script(tmp_path, "fake.py", "print('[]')")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    seen = {}

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(hook.subprocess, "run", _capture)
    hook.run_scanner()
    assert seen["argv"] == [sys.executable, str(script), "--json"]


def test_run_scanner_crash_fails_open(tmp_path, monkeypatch):
    script = _make_script(tmp_path, "crash.py", "raise RuntimeError('boom')")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)
    # a non-zero exit with stderr noise but no stdout is a FAILED scan, not a clean one
    assert hook.run_scanner() is None


# ── a failed scan is not a clean tree ────────────────────────────────────────

def _seeded_store(monkeypatch, tmp_path):
    """Two rows, one of them explicitly ACKED, in an isolated store."""
    store_file = tmp_path / "seeded.jsonl"
    monkeypatch.setenv("CLAUDE_SELF_DIAGNOSE_STORE", str(store_file))
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    hook.store.upsert_findings(
        [
            {"kind": "orphan-leaf", "path": "/mem/a.md", "detail": "d"},
            {"kind": "orphan-leaf", "path": "/mem/b.md", "detail": "d"},
        ]
    )
    hook.store.ack(hook.store.finding_key("orphan-leaf", "/mem/a.md"), "deliberate")
    return store_file


def test_a_failed_scan_leaves_the_store_byte_identical(tmp_path, monkeypatch):
    """One scanner hiccup must not wipe every first_seen, times_surfaced and
    explicit ack. This is one half of the contract; the resolve-out test below is
    the other, and a fix that satisfies only this one — never resolving anything —
    would break the store's whole closure model."""
    store_file = _seeded_store(monkeypatch, tmp_path)
    before = store_file.read_bytes()
    assert len(hook.store.load_rows()) == 2

    monkeypatch.setattr(hook, "SELF_DIAGNOSE", tmp_path / "does-not-exist.py")
    assert hook.main(["--force-run"]) == 0

    assert store_file.read_bytes() == before
    rows = {r["path"]: r for r in hook.store.load_rows()}
    assert set(rows) == {"/mem/a.md", "/mem/b.md"}
    assert rows["/mem/a.md"]["status"] == "acked"
    assert rows["/mem/b.md"]["times_surfaced"] == 1


def test_a_clean_scan_still_resolves_rows_out(tmp_path, monkeypatch):
    """A finding that disappeared from a COMPLETED scan is fixed, and closing it is
    the point — so the store must still empty out here, ack and all."""
    store_file = _seeded_store(monkeypatch, tmp_path)
    script = _make_script(tmp_path, "clean.py", "print('[]')\nraise SystemExit(0)")
    monkeypatch.setattr(hook, "SELF_DIAGNOSE", script)

    assert hook.main(["--force-run"]) == 0
    assert hook.store.load_rows() == []
    assert store_file.read_bytes() == b""


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
    monkeypatch.setattr(
        hook, "run_scanner", lambda: [{"kind": "orphan-leaf", "path": "y", "detail": "z"}]
    )
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
    monkeypatch.setattr(
        hook,
        "run_scanner",
        lambda: [{"kind": "oversized-index", "path": "foo", "detail": "300 lines > 200"}],
    )
    assert hook.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "1 self-friction item(s)" in err
    assert "oversized-index: foo" in err


def test_dry_run_reports_without_persisting(tmp_path, monkeypatch, capsys):
    """--dry-run is the verification mode; it must not perturb durable state."""
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(
        hook, "run_scanner", lambda: [{"kind": "orphan-leaf", "path": "p", "detail": "d"}]
    )
    monkeypatch.setattr(hook, "persist", lambda findings: pytest.fail("persisted on --dry-run"))
    assert hook.main(["--dry-run"]) == 0
    assert "orphan-leaf: p" in capsys.readouterr().err


def test_main_persists_the_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(
        hook, "run_scanner", lambda: [{"kind": "orphan-leaf", "path": "p", "detail": "d"}]
    )
    assert hook.main(["--force-run"]) == 0
    rows = hook.store.load_rows()
    assert [r["kind"] for r in rows] == ["orphan-leaf"]
    assert "actionable will re-surface at the turn boundary" in capsys.readouterr().err


def test_persist_fails_open_on_a_broken_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hook.store, "upsert_findings", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert hook.persist([{"kind": "orphan-leaf", "path": "p", "detail": "d"}]) == []


def test_the_summary_accounts_for_every_open_row(tmp_path, monkeypatch, capsys):
    """The line reports where each finding went, so no open row may fall between
    its buckets. A fresh actionable finding used to be counted in neither: too
    young to block, and excluded from the advisory count by its kind."""
    monkeypatch.setenv("CLAUDE_SELF_DIAGNOSE_STORE", str(tmp_path / "s.jsonl"))
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    # one aged actionable (blocks), one fresh actionable (debounced), one advisory
    old = datetime.now(timezone.utc) - timedelta(days=30)
    hook.store.upsert_findings([{"kind": "orphan-leaf", "path": "/mem/old.md", "detail": "d"}], now=old)
    monkeypatch.setattr(
        hook,
        "run_scanner",
        lambda: [
            {"kind": "orphan-leaf", "path": "/mem/old.md", "detail": "d"},
            {"kind": "orphan-leaf", "path": "/mem/new.md", "detail": "d"},
            {"kind": "near-duplicate", "path": "/mem/dup.md", "detail": "d"},
        ],
    )
    assert hook.main(["--force-run"]) == 0

    err = capsys.readouterr().err
    counts = [int(n) for n in re.findall(r"(\d+) (?:actionable|advisory)", err)]
    assert counts == [1, 1, 1]
    assert sum(counts) == len(hook.store.load_rows())


def test_report_caps_printed_lines(monkeypatch, capsys):
    findings = [
        {"kind": "near-duplicate", "path": f"path{i}", "detail": "detail"}
        for i in range(hook.MAX_PRINTED_LINES + 3)
    ]
    hook.report(findings)
    err = capsys.readouterr().err
    assert "... and 3 more" in err
