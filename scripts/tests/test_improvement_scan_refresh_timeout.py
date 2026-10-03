import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
_spec = importlib.util.spec_from_file_location("improvement_scan_shell", SCRIPTS / "improvement_scan_shell.py")
shell = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shell)

ENV = "IMPROVEMENT_SCAN_REFRESH_TIMEOUT_S"


def _capture(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shell.subprocess, "run", fake_run)
    return seen


def test_refresh_policy_ledger_uses_catch_up_bound(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    seen = _capture(monkeypatch)
    shell.refresh_policy_ledger(14)
    assert seen["timeout"] >= 600


def test_refresh_timeout_env_override(monkeypatch):
    monkeypatch.setenv(ENV, "1234")
    seen = _capture(monkeypatch)
    shell.refresh_policy_ledger(14)
    assert seen["timeout"] == 1234


@pytest.mark.parametrize("bad", ["abc", "0", "-5", ""])
def test_refresh_timeout_invalid_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv(ENV, bad)
    seen = _capture(monkeypatch)
    shell.refresh_policy_ledger(14)
    assert seen["timeout"] == 900


def test_search_experience_keeps_60s_bound(monkeypatch):
    monkeypatch.setenv(ENV, "1234")
    seen = _capture(monkeypatch)
    shell.search_experience(["a", "b"])
    assert seen["timeout"] == 60


def test_refresh_timeout_still_degrades(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(shell.subprocess, "run", boom)
    ok, msg = shell.refresh_policy_ledger(14)
    assert ok is False
    assert "failed to run" in msg


def test_docs_name_the_env_var():
    assert ENV in (REPO / "docs/operations/improvement-scan.md").read_text(encoding="utf-8")
