"""Every plan-dashboard mutation turns its tests red; with none installed they stay green.

Each case runs a test file in a subprocess with one mutation installed, so a mutation that no
test notices fails here: ``MUTATIONS`` against the model tests, the standard-library rows of
``TUI_MUTATIONS`` against ``test_plan_dashboard_tui.py``, and the row that targets the textual
layer against the pilot file, when textual can be imported (see ``pilot_interpreter_has_textual``).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import plan_dashboard_mutations as mutations

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
MODEL_TESTS = TESTS / "test_plan_dashboard_model.py"
TUI_TESTS = TESTS / "test_plan_dashboard_tui.py"
PILOT_TESTS = TESTS / "test_plan_dashboard_tui_app.py"
WORKERS = 4
PILOT_MUTATIONS = sorted(name for name, (module, _, _) in mutations.TUI_MUTATIONS.items()
                         if module == mutations.TUI_APP)
STDLIB_TUI_MUTATIONS = sorted(set(mutations.TUI_MUTATIONS) - set(PILOT_MUTATIONS))
REQUIRED = os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1"


def pilot_interpreter_has_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


def run_tests(test_file: Path, mutation: str) -> subprocess.CompletedProcess:
    env = {**os.environ, mutations.ENV: mutation, "PYTHONPATH": f"{TESTS}{os.pathsep}{SCRIPTS}"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", "-p", "plan_dashboard_mutations",
         str(test_file)],
        cwd=SCRIPTS.parent, env=env, capture_output=True, text=True, timeout=300,
    )


def run_all(test_file: Path, names: list[str]) -> dict[str, subprocess.CompletedProcess]:
    names = ["", *names]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(names, pool.map(lambda name: run_tests(test_file, name), names)))


@pytest.fixture(scope="module")
def results():
    return run_all(MODEL_TESTS, sorted(mutations.MUTATIONS))


@pytest.fixture(scope="module")
def tui_results():
    return run_all(TUI_TESTS, STDLIB_TUI_MUTATIONS)


@pytest.fixture(scope="module")
def pilot_results():
    if not (REQUIRED or pilot_interpreter_has_textual()):
        pytest.skip("textual is not importable here; run this file under the uv command of docs/components/plan-dashboard.md")
    return run_all(PILOT_TESTS, PILOT_MUTATIONS)


def assert_green(result):
    assert result.returncode == 0, result.stdout[-2000:]
    assert "plan-dashboard mutation: none" in result.stdout


def assert_red(name, result, what):
    assert f"plan-dashboard mutation: {name} applied" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, f"{name} left the {what} green:\n{result.stdout[-2000:]}"
    assert "FAILED" in result.stdout


def test_the_model_tests_are_green_with_no_mutation(results):
    assert_green(results[""])


@pytest.mark.parametrize("name", sorted(mutations.MUTATIONS))
def test_a_mutation_turns_a_model_test_red(name, results):
    assert_red(name, results[name], "model tests")


def test_the_console_tests_are_green_with_no_mutation(tui_results):
    assert_green(tui_results[""])


@pytest.mark.parametrize("name", STDLIB_TUI_MUTATIONS)
def test_a_console_mutation_turns_a_console_test_red(name, tui_results):
    assert_red(name, tui_results[name], "console tests")


def test_the_pilot_tests_are_green_with_no_mutation(pilot_results):
    assert_green(pilot_results[""])


@pytest.mark.parametrize("name", PILOT_MUTATIONS)
def test_a_console_mutation_turns_a_pilot_test_red(name, pilot_results):
    assert_red(name, pilot_results[name], "pilot tests")
