"""Every plan-dashboard mutation turns its tests red; with none installed they stay green.

Each case runs a test file in a subprocess with one mutation installed, so a mutation that no
test notices fails here: ``MUTATIONS`` against the model tests, the standard-library rows of
``TUI_MUTATIONS`` against ``test_plan_dashboard_tui.py``, ``WEB_MUTATIONS`` against
``test_plan_dashboard_web.py``, ``WEB_PAGE_MUTATIONS`` against ``test_plan_dashboard_web_dom.py``
(node is required as that file requires it: ``PLAN_DASHBOARD_REQUIRE_NODE=1`` fails without it, otherwise
the cases skip), and the rows that target the textual layer against the pilot file, when textual can be
imported (see ``pilot_interpreter_has_textual``).
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
from test_plan_dashboard_web_dom import find_node

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
MODEL_TESTS = TESTS / "test_plan_dashboard_model.py"
TUI_TESTS = TESTS / "test_plan_dashboard_tui.py"
PILOT_TESTS = TESTS / "test_plan_dashboard_tui_app.py"
WEB_TESTS = TESTS / "test_plan_dashboard_web.py"
DOM_TESTS = TESTS / "test_plan_dashboard_web_dom.py"
WORKERS = 4
RUN_TIMEOUT_S = 300
PILOT_MUTATIONS = sorted(name for name, (module, _, _) in mutations.TUI_MUTATIONS.items()
                         if module == mutations.TUI_APP)
STDLIB_TUI_MUTATIONS = sorted(set(mutations.TUI_MUTATIONS) - set(PILOT_MUTATIONS))
REQUIRED = os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1"


def pilot_interpreter_has_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


def run_tests(test_file: Path, mutation: str) -> subprocess.CompletedProcess:
    env = {**os.environ, mutations.ENV: mutation, "PYTHONPATH": f"{TESTS}{os.pathsep}{SCRIPTS}"}
    command = [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", "-p", "plan_dashboard_mutations",
               str(test_file)]
    try:
        return subprocess.run(command, cwd=SCRIPTS.parent, env=env, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as hung:
        out = hung.output.decode() if isinstance(hung.output, bytes) else (hung.output or "")
        return subprocess.CompletedProcess(command, -9, f"{out}\nTIMEOUT after {RUN_TIMEOUT_S}s, mutation: {mutation or 'none'}", "")


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
def web_results():
    return run_all(WEB_TESTS, sorted(mutations.WEB_MUTATIONS))


@pytest.fixture(scope="module")
def dom_results():
    if find_node() is None:
        if os.environ.get("PLAN_DASHBOARD_REQUIRE_NODE") == "1":
            pytest.fail("PLAN_DASHBOARD_REQUIRE_NODE=1 but node was not found (PLAN_DASHBOARD_NODE or PATH)")
        pytest.skip("node not found")
    return run_all(DOM_TESTS, sorted(mutations.WEB_PAGE_MUTATIONS))


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


def test_the_web_tests_are_green_with_no_mutation(web_results):
    assert_green(web_results[""])


@pytest.mark.parametrize("name", sorted(mutations.WEB_MUTATIONS))
def test_a_web_mutation_turns_a_web_test_red(name, web_results):
    assert_red(name, web_results[name], "web tests")


def test_the_page_tests_are_green_with_no_mutation(dom_results):
    assert_green(dom_results[""])


@pytest.mark.parametrize("name", sorted(mutations.WEB_PAGE_MUTATIONS))
def test_a_page_mutation_turns_a_page_test_red(name, dom_results):
    assert_red(name, dom_results[name], "page tests")


def test_the_pilot_tests_are_green_with_no_mutation(pilot_results):
    assert_green(pilot_results[""])


@pytest.mark.parametrize("name", PILOT_MUTATIONS)
def test_a_console_mutation_turns_a_pilot_test_red(name, pilot_results):
    assert_red(name, pilot_results[name], "pilot tests")
