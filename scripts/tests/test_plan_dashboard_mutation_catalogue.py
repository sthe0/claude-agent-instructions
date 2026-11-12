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
RUN_TIMEOUT_S = 1800
HUNG = -9
PILOT_MUTATIONS = sorted(name for name, (module, _, _) in mutations.TUI_MUTATIONS.items()
                         if module == mutations.TUI_APP)
STDLIB_TUI_MUTATIONS = sorted(set(mutations.TUI_MUTATIONS) - set(PILOT_MUTATIONS))
SNAPSHOT_TESTS = TESTS / "test_plan_dashboard_snapshot.py"
SNAPSHOT_NAMES = (
    "snapshot-checksum-ignored", "cache-same-size-rewrite-missed", "unread-cost-zero", "old-marking-dropped",
    "jump-uses-snapshot-window", "mark-confirmed-from-snapshot", "overlay-waits-on-order-read",
    "binding-scan-uncached", "binding-cache-never-invalidated", "source-state-cached-forever",
    "source-binding-cached-forever", "source-transcript-cached-forever", "source-ledger-cached-forever",
    "code-version-identity-ignored", "code-version-new-module-missed", "code-version-always-parsed",
    "snapshot-records-every-source", "snapshot-directory-marker-ignored", "same-named-transcript-dropped",
    "unrecorded-source-stat-skipped", "order-read-piles-up", "child-warm-piles-up", "skipped-order-read-not-made-up",
    "row-confirmed-skipped", "startup-applied-last-read-only", "startup-applied-at-check-end",
    "startup-applied-needs-old-rows")
SNAPSHOT_PILOT_NAMES = ("mark-confirmed-from-snapshot", "overlay-waits-on-order-read", "order-read-piles-up",
                        "child-warm-piles-up", "skipped-order-read-not-made-up", "row-confirmed-skipped",
                        "startup-applied-last-read-only", "startup-applied-at-check-end",
                        "startup-applied-needs-old-rows")
SNAPSHOT_STDLIB_NAMES = tuple(name for name in SNAPSHOT_NAMES if name not in SNAPSHOT_PILOT_NAMES)
HARNESS_TESTS = TESTS / "test_plan_dashboard_harness.py"
SCRIPT_MODULE_TESTS = {"harness-save-records-app-sources": HARNESS_TESTS,
                       "launcher-imports-every-subcommand": TUI_TESTS}
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
        return subprocess.CompletedProcess(command, HUNG, f"{out}\nTIMEOUT after {RUN_TIMEOUT_S}s, mutation: {mutation or 'none'}", "")


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


@pytest.fixture(scope="module")
def snapshot_results():
    return run_all(SNAPSHOT_TESTS, list(SNAPSHOT_STDLIB_NAMES))


@pytest.fixture(scope="module")
def snapshot_pilot_results():
    if not (REQUIRED or pilot_interpreter_has_textual()):
        pytest.skip("textual is not importable here; run this file under the uv command of docs/components/plan-dashboard.md")
    return run_all(PILOT_TESTS, list(SNAPSHOT_PILOT_NAMES))


def assert_not_hung(result):
    """A run that hit the hang guard says nothing about the tests: it is neither the red nor the green the
    catalogue looks for, and fails as its own outcome."""
    if result.returncode == HUNG:
        pytest.fail(f"no verdict, the run hung (hang guard {RUN_TIMEOUT_S}s):\n{result.stdout[-1000:]}", pytrace=False)


def assert_green(result):
    assert_not_hung(result)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "plan-dashboard mutation: none" in result.stdout


def assert_red(name, result, what):
    assert_not_hung(result)
    assert f"plan-dashboard mutation: {name} applied" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, f"{name} left the {what} green:\n{result.stdout[-2000:]}"
    assert "FAILED" in result.stdout


@pytest.mark.parametrize("judge", [lambda r: assert_green(r), lambda r: assert_red("m", r, "tests")], ids=["green", "red"])
def test_a_run_that_hit_the_hang_guard_is_its_own_outcome_neither_red_nor_green(judge):
    hung = subprocess.CompletedProcess([], HUNG, "plan-dashboard mutation: m applied\nFAILED x\nTIMEOUT", "")
    with pytest.raises(pytest.fail.Exception, match="the run hung"):
        judge(hung)


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


def test_the_snapshot_family_is_the_registered_one():
    assert set(SNAPSHOT_NAMES) == set(mutations.SNAPSHOT_MUTATIONS) and len(set(SNAPSHOT_NAMES)) == len(SNAPSHOT_NAMES)
    assert set(SNAPSHOT_PILOT_NAMES) == {name for name, (module, _, _) in mutations.SNAPSHOT_MUTATIONS.items()
                                         if module == mutations.TUI_APP}


def test_the_snapshot_tests_are_green_with_no_mutation(snapshot_results):
    assert_green(snapshot_results[""])


@pytest.mark.parametrize("name", SNAPSHOT_STDLIB_NAMES)
def test_a_snapshot_mutation_turns_a_snapshot_test_red(name, snapshot_results):
    assert_red(name, snapshot_results[name], "snapshot tests")


@pytest.mark.parametrize("name", SNAPSHOT_PILOT_NAMES)
def test_a_snapshot_mutation_turns_a_pilot_test_red(name, snapshot_pilot_results):
    assert_red(name, snapshot_pilot_results[name], "pilot tests")


@pytest.fixture(scope="module")
def script_module_results():
    runs = {"": HARNESS_TESTS, **SCRIPT_MODULE_TESTS}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(runs, pool.map(lambda name: run_tests(runs[name], name), runs)))


def test_the_script_module_family_is_the_registered_one():
    assert set(SCRIPT_MODULE_TESTS) == set(mutations.SCRIPT_MODULE_MUTATIONS)


def test_the_harness_tests_are_green_with_no_mutation(script_module_results):
    assert_green(script_module_results[""])


@pytest.mark.parametrize("name", sorted(SCRIPT_MODULE_TESTS))
def test_a_script_module_mutation_turns_its_test_red(name, script_module_results):
    assert_red(name, script_module_results[name], SCRIPT_MODULE_TESTS[name].name)
