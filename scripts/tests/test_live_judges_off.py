"""The suite-wide default that keeps every live model judge runner from launching."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentctl import advisor  # noqa: E402
from lib import marker_extract  # noqa: E402


def _boom(*_a, **_k):
    raise AssertionError("a model CLI would have been launched")


def test_conftest_defaults_live_judges_off():
    assert os.environ["AGENTCTL_LIVE_JUDGES"] == "0"


def test_advisor_runner_does_not_launch_when_switched_off(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _boom)
    result = advisor.subprocess_runner(["claude", "-p", "x"])
    assert result.returncode != 0


def test_marker_extract_runner_does_not_launch_when_switched_off(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _boom)
    result = marker_extract.subprocess_runner(["claude", "-p", "x"])
    assert result.returncode != 0
    assert "AGENTCTL_LIVE_JUDGES" in result.stderr
