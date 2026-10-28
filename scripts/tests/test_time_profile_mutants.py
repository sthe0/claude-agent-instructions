"""The negative control for test_time_profile_extract.py, run from pytest.

Each mutant in time_profile_mutants.MUTANTS must make the extractor suite FAIL (exit 1),
and a mutant that does not exist must end the run with exit 4 rather than pass or fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from time_profile_mutants import ENV, MUTANTS

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SUITE = HERE / "test_time_profile_extract.py"


def run_suite(mutant: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if mutant is not None:
        env[ENV] = mutant
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-p", "no:cacheprovider", "-p", "no:xdist"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_the_extractor_suite_fails_under_the_mutant(mutant):
    assert run_suite(mutant).returncode == 1


def test_an_unknown_mutant_name_ends_the_run_with_exit_4():
    result = run_suite("no_such_mutant")
    assert result.returncode == 4, result.stdout[-1500:] + result.stderr[-1500:]
