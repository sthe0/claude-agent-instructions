"""Every plan-dashboard mutation turns the model tests red; with none installed they stay green.

Each case runs the model tests in a subprocess with one mutation from
``plan_dashboard_mutations.MUTATIONS`` installed, so a mutation that no test notices fails here.
"""
from __future__ import annotations

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
WORKERS = 4


def run_model_tests(mutation: str) -> subprocess.CompletedProcess:
    env = {**os.environ, mutations.ENV: mutation, "PYTHONPATH": f"{TESTS}{os.pathsep}{SCRIPTS}"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", "-p", "plan_dashboard_mutations",
         str(MODEL_TESTS)],
        cwd=SCRIPTS.parent, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture(scope="module")
def results():
    names = ["", *sorted(mutations.MUTATIONS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(names, pool.map(run_model_tests, names)))


def test_the_model_tests_are_green_with_no_mutation(results):
    result = results[""]
    assert result.returncode == 0, result.stdout[-2000:]
    assert "plan-dashboard mutation: none" in result.stdout


@pytest.mark.parametrize("name", sorted(mutations.MUTATIONS))
def test_a_mutation_turns_a_model_test_red(name, results):
    result = results[name]
    assert f"plan-dashboard mutation: {name} applied" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, f"{name} left the model tests green:\n{result.stdout[-2000:]}"
    assert "FAILED" in result.stdout
