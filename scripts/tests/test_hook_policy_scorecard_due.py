"""Unit tests for hook-policy-scorecard-due.py: throttle bookkeeping, the
detached launch of the ledger upsert, fail-open behaviour on a raising
launcher, and the two env overrides (CLAUDE_POLICY_LEDGER /
CLAUDE_POLICY_SCORECARD_STAMP) that let this hook's own end-to-end proof run
without ever touching live state.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import sys
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-policy-scorecard-due.py"
_spec = importlib.util.spec_from_file_location("policy_scorecard_due", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
sys.modules["policy_scorecard_due"] = hook
_spec.loader.exec_module(hook)


class _Recorder:
    """Injected launcher: records every cmd it was called with, optionally
    raising on call so tests can prove the hook stays fail-open."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[list[str]] = []
        self._raise = raise_exc

    def __call__(self, cmd: list[str]) -> None:
        self.calls.append(cmd)
        if self._raise is not None:
            raise self._raise


@pytest.fixture(autouse=True)
def _isolated_stamp(tmp_path, monkeypatch):
    """Every test gets its own default STAMP path and a clean environment, so
    nothing here can ever read or write this machine's real
    ~/.local/state/claude-policy-scorecard.stamp or real ledger."""
    monkeypatch.setattr(hook, "STAMP", tmp_path / "stamp")
    monkeypatch.delenv("CLAUDE_POLICY_SCORECARD_STAMP", raising=False)
    monkeypatch.delenv("CLAUDE_POLICY_LEDGER", raising=False)
    return tmp_path


def _run_hook(monkeypatch, launch) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    return hook.main(launch=launch)


# --------------------------------------------------------------- throttle

def test_first_run_is_due_and_launches(monkeypatch, tmp_path):
    rec = _Recorder()
    rc = _run_hook(monkeypatch, rec)
    assert rc == 0
    assert len(rec.calls) == 1
    assert (tmp_path / "stamp").exists()


def test_not_due_skips_launch_and_stamp_rewrite(monkeypatch, tmp_path):
    stamp = tmp_path / "stamp"
    now = dt.datetime.now()
    stamp.write_text(now.isoformat(), encoding="utf-8")
    before = stamp.read_text(encoding="utf-8")

    rec = _Recorder()
    rc = _run_hook(monkeypatch, rec)

    assert rc == 0
    assert rec.calls == []
    assert stamp.read_text(encoding="utf-8") == before


def test_due_after_throttle_window_launches_again(monkeypatch, tmp_path):
    stamp = tmp_path / "stamp"
    old = dt.datetime.now() - dt.timedelta(days=hook.THROTTLE_DAYS + 1)
    stamp.write_text(old.isoformat(), encoding="utf-8")

    rec = _Recorder()
    rc = _run_hook(monkeypatch, rec)

    assert rc == 0
    assert len(rec.calls) == 1


def test_stamp_written_exactly_once_per_due_firing(monkeypatch, tmp_path):
    counts = {"n": 0}
    orig_record = hook.record_nudge

    def counting_record(now, stamp):
        counts["n"] += 1
        orig_record(now, stamp)

    monkeypatch.setattr(hook, "record_nudge", counting_record)
    rc = _run_hook(monkeypatch, _Recorder())
    assert rc == 0
    assert counts["n"] == 1


# ------------------------------------------------------------- fail-open

def test_launcher_raising_still_exits_zero_and_still_stamps(monkeypatch, tmp_path):
    rec = _Recorder(raise_exc=RuntimeError("boom"))
    rc = _run_hook(monkeypatch, rec)
    assert rc == 0
    assert len(rec.calls) == 1
    assert (tmp_path / "stamp").exists()


def test_malformed_stdin_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    rc = hook.main(launch=_Recorder())
    assert rc == 0


# ---------------------------------------------------------- env overrides

def test_ledger_env_override_passed_through_as_flag(monkeypatch):
    monkeypatch.setenv("CLAUDE_POLICY_LEDGER", "/tmp/fake-ledger.jsonl")
    cmd = hook.ledger_upsert_cmd()
    assert "--ledger" in cmd
    assert cmd[cmd.index("--ledger") + 1] == "/tmp/fake-ledger.jsonl"


def test_no_ledger_env_override_omits_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_POLICY_LEDGER", raising=False)
    cmd = hook.ledger_upsert_cmd()
    assert "--ledger" not in cmd


def test_stamp_env_override_redirects_and_default_stays_untouched(monkeypatch, tmp_path):
    override_stamp = tmp_path / "override-stamp"
    default_stamp = tmp_path / "default-stamp-must-stay-absent"
    monkeypatch.setattr(hook, "STAMP", default_stamp)
    monkeypatch.setenv("CLAUDE_POLICY_SCORECARD_STAMP", str(override_stamp))

    rc = _run_hook(monkeypatch, _Recorder())

    assert rc == 0
    assert override_stamp.exists()
    assert not default_stamp.exists()


def test_stamp_env_override_absent_falls_back_to_default(monkeypatch, tmp_path):
    default_stamp = tmp_path / "default-stamp"
    monkeypatch.setattr(hook, "STAMP", default_stamp)
    monkeypatch.delenv("CLAUDE_POLICY_SCORECARD_STAMP", raising=False)

    _run_hook(monkeypatch, _Recorder())

    assert default_stamp.exists()


# --------------------------------------------------------- default launch

def test_default_launch_uses_detached_supervised_popen(monkeypatch):
    """default_launch must never block session start: it hands off to
    proc_tree.launch_supervised (start_new_session=True), not
    subprocess.run or Popen(...).wait()."""
    captured = {}

    def fake_launch_supervised(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(hook, "launch_supervised", fake_launch_supervised)
    hook.default_launch([sys.executable, "-c", "pass"])

    assert captured["cmd"] == [sys.executable, "-c", "pass"]
    assert captured["kwargs"].get("stdout") is not None
    assert captured["kwargs"].get("stderr") is not None
