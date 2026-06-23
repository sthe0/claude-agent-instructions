"""Make the `agentctl` package importable and provide shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.store import FileStateStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return FileStateStore(tmp_path / "state")


@pytest.fixture
def fixtures_dir():
    return Path(__file__).resolve().parent / "fixtures"
