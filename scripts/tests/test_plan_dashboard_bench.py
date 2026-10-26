"""Timing bench of the console's reads: ``plan_dashboard.bench`` and ``plan-dashboard.py bench``.

Plain pytest, standard library only. The clock is replaced by a counter, so every measure takes a known
number of seconds and the verdicts do not depend on the machine.
"""
from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

import pytest

from plan_dashboard import bench
from plan_dashboard_fixtures import build_fixture_root, read_only_guard
from plan_dashboard_support import ALPHA, BETA, NOW, isolated_home, make_state  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

MEASURES = ["live table", "live overlay", "all table", "all overlay", "detail"]


@pytest.fixture
def root(tmp_path):
    return build_fixture_root(tmp_path, [make_state(ALPHA), make_state(BETA)], now=NOW)


@pytest.fixture
def one_second_reads(monkeypatch):
    """Every measured read takes exactly one second."""
    monkeypatch.setattr(bench, "clock", itertools.count().__next__)


def run(root, **options):
    lines = []
    with read_only_guard(root.home.parent):
        code = bench.run(root.state_root, cost_log=root.cost_log, transcripts_root=root.transcripts_root, now=NOW,
                         out=lines.append, **options)
    return code, lines


def test_the_default_budgets_are_pinned():
    assert bench.BUDGETS == {"live table": 4.0, "live overlay": 3.0, "all table": 15.0, "all overlay": 30.0,
                             "detail": 5.0}


def test_a_measure_is_ok_up_to_and_including_its_budget():
    assert bench.verdict(2.9, 3.0) == "ok"
    assert bench.verdict(3.0, 3.0) == "ok"
    assert bench.verdict(3.01, 3.0) == "over"
    assert bench.line("live overlay", 3.04, 3.0) == "live overlay: 3.0s (budget 3s) over"


def test_every_measure_within_budget_prints_one_ok_line_each_and_the_volume(root, one_second_reads):
    code, lines = run(root)
    assert code == 0
    assert lines == [f"{name}: 1.0s (budget {bench.BUDGETS[name]:g}s) ok" for name in MEASURES] + [
        "volume: 2 tasks, 0 binding files"]


@pytest.mark.parametrize("over", MEASURES)
def test_any_one_measure_over_its_budget_fails_the_run(root, one_second_reads, over):
    code, lines = run(root, budgets={over: 0.5})
    assert code == bench.EXIT_OVER == 1
    assert [line.rsplit(" ", 1)[1] for line in lines[:-1]] == ["over" if name == over else "ok" for name in MEASURES]


def test_an_unknown_session_is_reported_on_stderr_with_exit_2(root, one_second_reads, capsys):
    code, lines = run(root, session="no-such-session")
    assert code == bench.EXIT_NO_SUCH_SESSION == 2
    assert lines == []
    assert capsys.readouterr().err == "bench: session no-such-session not found\n"


def test_the_command_passes_every_budget_flag_and_the_session(root, monkeypatch):
    seen = {}
    monkeypatch.setattr(bench, "run", lambda state_root, **options: seen.update(options, state_root=state_root) or 0)
    flags = [arg for name in MEASURES for arg in (bench.budget_flag(name), "0.25")]
    code = cli.main(["bench", "--state-root", str(root.state_root), "--session", ALPHA, *flags])
    assert code == 0
    assert seen["state_root"] == root.state_root and seen["session"] == ALPHA
    assert seen["budgets"] == dict.fromkeys(MEASURES, 0.25)
    assert bench.budget_flag("all overlay") == "--budget-all-overlay"
