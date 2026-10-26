"""scripts/bench-verify-final.py: the verify-final benchmark gates wall ratios and decisions.

The end-to-end case runs the benchmark with sub-second check costs and ratio limits wide
enough that interpreter and git start-up cannot flake it, while --assert-decisions checks
the candidate's logged per-check decisions exactly. The gate cases feed gate_failures
synthetic numbers.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench-verify-final.py"
REPO = BENCH.parent.parent
PINNED_BASE = "00267dd6"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_verify_final", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*argv):
    done = subprocess.run(["git", "-C", str(REPO), *argv], capture_output=True, check=False)
    return done.returncode, done.stdout


def _base_ref():
    code, _ = _git("rev-parse", "--verify", f"{PINNED_BASE}^{{commit}}")
    return PINNED_BASE if code == 0 else "HEAD"


def _repo_state():
    index = Path(_git("rev-parse", "--path-format=absolute", "--git-path", "index")[1].decode().strip())
    return (
        _git("for-each-ref")[1],
        _git("worktree", "list", "--porcelain")[1],
        index.read_bytes() if index.exists() else b"",
    )


def _args(**kw):
    base = dict(max_first_ratio=0.7, max_repeat_ratio=0.2, assert_decisions=False)
    return Namespace(**{**base, **kw})


def _ratios(first=0.4, repeat=0.01):
    return {"first": first, "repeat": repeat}


def _decisions(bench):
    return {run: dict(expected) for run, expected in bench.EXPECTED_DECISIONS.items()}


@pytest.fixture(scope="module")
def bench():
    return _load_bench()


@pytest.fixture(scope="module")
def report():
    before = _repo_state()
    done = subprocess.run(
        [sys.executable, str(BENCH), "--base-ref", _base_ref(), "--assert-decisions",
         "--cost-a", "0.3", "--cost-b", "0.15", "--cost-c", "0.15",
         "--max-first-ratio", "5", "--max-repeat-ratio", "5"],
        capture_output=True, text=True, check=False, timeout=170,
    )
    return done, before, _repo_state()


def test_bench_exits_zero_with_the_expected_decisions(report):
    done, _, _ = report
    assert done.returncode == 0, done.stdout + done.stderr
    data = json.loads(done.stdout)
    assert data["failures"] == []
    serial = data["candidate"]["serial"]["decisions"]
    assert serial["first"] == {
        "stage:1": "miss", "stage:2": "narrowed", "stage:3": "miss",
        "final_check:1": "dedup", "final_check:2": "dedup",
    }
    assert serial["second"] == {
        "stage:1": "hit", "stage:2": "hit", "stage:3": "hit",
        "final_check:1": "dedup", "final_check:2": "dedup",
    }


def test_bench_report_records_the_fixture_commit_order(report):
    data = json.loads(report[0].stdout)
    assert [c["subject"] for c in data["fixture"]["commits"]] == [
        "bench: code and docs", "bench: docs-only note",
    ]
    assert data["fixture"]["docs_only_change"] == "docs/bench/note.md"
    assert set(data["ratios"]) == {"first", "repeat", "parallel_first", "parallel_repeat"}


def test_bench_leaves_the_real_repository_untouched(report):
    _, before, after = report
    assert after == before


def test_gate_passes_when_ratios_and_decisions_hold(bench):
    assert bench.gate_failures(_args(assert_decisions=True), _ratios(), _decisions(bench)) == []


def test_gate_fails_a_first_run_ratio_over_the_limit(bench):
    failures = bench.gate_failures(_args(), _ratios(first=0.71), _decisions(bench))
    assert [f.split()[0] for f in failures] == ["first-run"]


def test_gate_fails_a_repeated_run_ratio_over_the_limit(bench):
    failures = bench.gate_failures(_args(), _ratios(repeat=0.21), _decisions(bench))
    assert [f.split()[0] for f in failures] == ["repeated-run"]


def test_gate_fails_an_unmeasurable_ratio(bench):
    failures = bench.gate_failures(_args(), _ratios(first=None, repeat=None), _decisions(bench))
    assert len(failures) == 2


def test_gate_decisions_are_asserted_only_on_request(bench):
    decisions = _decisions(bench)
    decisions["first"]["stage:2"] = "miss"
    assert bench.gate_failures(_args(), _ratios(), decisions) == []
    failures = bench.gate_failures(_args(assert_decisions=True), _ratios(), decisions)
    assert [f.split()[0] for f in failures] == ["first-run"]


def test_gate_fails_a_second_run_that_reruns_a_stage(bench):
    decisions = _decisions(bench)
    decisions["second"]["stage:1"] = "miss"
    failures = bench.gate_failures(_args(assert_decisions=True), _ratios(), decisions)
    assert [f.split()[0] for f in failures] == ["second-run"]


def test_bench_requires_a_base_ref():
    done = subprocess.run([sys.executable, str(BENCH)], capture_output=True, text=True, check=False)
    assert done.returncode == 2
    assert "--base-ref" in done.stderr
