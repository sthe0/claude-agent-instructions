"""Make the `agentctl` package importable and provide shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.store import FileStateStore  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_task_quality_ledger(tmp_path, monkeypatch):
    """The scorecard aggregates the real ledger into degradation flags, so a
    test resolve must never append to it (unlike GATE_LOG, which is inert
    telemetry)."""
    from agentctl import cli
    monkeypatch.setattr(cli, "TASK_QUALITY_LOG", tmp_path / "task-quality.jsonl")


@pytest.fixture(autouse=True)
def _plan_review_gate_off_by_default(monkeypatch):
    """Default the thinker-review gate OFF for the suite at large.

    The gate blocks `approve`/`replan` on every SUBSTANTIVE session until a bound
    thinker review is recorded (gates.plan_review_blockers). The overwhelming
    majority of substantive-flow tests exercise unrelated machinery (partition,
    tracker plugin, cost, dispatch, coverage) and are not about the review gate;
    coupling them all to it would make a plan-review change cascade failures across
    a dozen unrelated modules. So we set the documented force-off knob
    (AGENTCTL_PLAN_REVIEW=0) by default — byte-identical to the gate being absent —
    exactly as the fixture above isolates the quality ledger.

    The gate's real block/pass/stale/override/scope behaviour is proven end-to-end
    by test_plan_review_gate.py and the test_spine_walk_* integration tests, which
    explicitly re-enable it (setenv "1" / a live subprocess env)."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")


@pytest.fixture(autouse=True)
def _plan_presentation_gate_off_by_default(monkeypatch):
    """Default the presentation/delivery gate OFF for the suite at large, for the
    same reason as `_plan_review_gate_off_by_default` above: the overwhelming
    majority of substantive-flow tests are not about presentation/delivery, and
    coupling them all to it would make this gate's change cascade failures across
    unrelated modules. AGENTCTL_PLAN_PRESENTATION=0 is the documented force-off
    knob (gates.plan_presentation_active) — byte-identical to the gate being
    absent. Its real block/pass/stale/override/fail-closed behaviour is proven
    end-to-end by test_plan_presentation.py, which explicitly re-enables it."""
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")


@pytest.fixture
def store(tmp_path):
    return FileStateStore(tmp_path / "state")


@pytest.fixture
def fixtures_dir():
    return Path(__file__).resolve().parent / "fixtures"
