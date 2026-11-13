"""Pin ``--core-shuffle-seed`` and the isolation check built on it (``shuffle-pair`` / ``compare``).

The option runs in real pytest processes over a tiny temporary project; the verdicts run on fixture
reports and on faked whole-suite runs, so nothing here starts the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _core_shuffle
import _test_report
import gate_speedup_control as control
from lib import suite_gate as gate
from lib import test_slots

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = SCRIPTS_DIR / "tests"
FIXTURES = TESTS_DIR / "fixtures"
REAL_EXCEPTIONS = TESTS_DIR / "isolation_exceptions.toml"

SAMPLE = "".join(f"def test_{n:02d}():\n    pass\n\n\n" for n in range(40))


def collected_order(project: Path, *args: str) -> list[str]:
    env = {**os.environ, "PYTHONPATH": str(TESTS_DIR)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(project), "--collect-only", "-q", "-p", "no:xdist",
         "-p", "no:cacheprovider", "-p", "_core_shuffle", "--rootdir", str(project), *args],
        capture_output=True, text=True, env=env, cwd=project, timeout=120)
    assert proc.returncode == 0, proc.stdout[-400:] + proc.stderr[-400:]
    return [line for line in proc.stdout.splitlines() if "::" in line]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


def test_two_seeds_give_two_orders_and_one_seed_the_same_order(project):
    first, again, second = (collected_order(project, f"--core-shuffle-seed={n}") for n in (1, 1, 2))
    assert first == again
    assert first != second
    assert sorted(first) == sorted(second) and len(first) == 40


def test_no_option_leaves_the_collection_order_unchanged(project):
    plain = collected_order(project)
    assert plain == sorted(plain)
    assert collected_order(project, "--core-shuffle-seed=1") != plain


def test_the_repository_conftest_accepts_the_option():
    env = {k: v for k, v in os.environ.items() if k != "CORE_TEST_REPORT"}

    def ids(*args):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(Path(__file__)), "--collect-only", "-q", "-p", "no:xdist",
             "-p", "no:cacheprovider", *args],
            capture_output=True, text=True, env=env, cwd=SCRIPTS_DIR.parent, timeout=300)
        found = [line for line in proc.stdout.splitlines() if "::" in line]
        assert proc.returncode == 0 and found, proc.stdout[-400:] + proc.stderr[-400:]
        return found

    plain = ids()
    shuffled = ids("--core-shuffle-seed=3")
    assert shuffled != plain and sorted(shuffled) == sorted(plain)


def test_shuffled_does_not_change_its_input():
    items = list(range(20))
    assert _core_shuffle.shuffled(items, 7) != items
    assert items == list(range(20))


# ── per-test outcomes ─────────────────────────────────────────────────────

def line(kind, nodeid, when=None, outcome=None):
    return {"kind": kind, "nodeid": nodeid, **({"when": when, "outcome": outcome} if when else {})}


def test_outcomes_name_failed_skipped_passed_and_unfinished():
    lines = [line("collected", i) for i in ("a", "b", "c", "d", "e")] + [
        line("report", "a", "setup", "passed"), line("report", "a", "call", "passed"),
        line("report", "b", "setup", "passed"), line("report", "b", "call", "passed"),
        line("report", "b", "teardown", "failed"),
        line("report", "c", "setup", "skipped"),
        line("report", "d", "setup", "passed"),
    ]
    assert _test_report.outcomes(lines) == {
        "a": "passed", "b": "failed", "c": "skipped", "d": "unfinished", "e": "unfinished"}


# ── the verdict ───────────────────────────────────────────────────────────

def run_record(name, seed, outcomes, *, hermetic=True, start=1000.0, end=1100.0):
    return {"name": name, "seed": seed, "hermetic": hermetic, "running_from": start, "running_to": end,
            "outcomes": outcomes}


def pair_report(*, hermetic=True, second_start=1000.0, second_outcome="passed"):
    same = {"t::a": "passed", "t::b": "passed"}
    return {"runs": [
        run_record(control.UNSHUFFLED, None, same, start=0.0, end=100.0),
        run_record("seed1", 1, same, hermetic=hermetic),
        run_record("seed2", 2, {**same, "t::b": second_outcome}, start=second_start, end=second_start + 100)]}


def write(tmp_path, report, name="report.json"):
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def exceptions(tmp_path, *entries):
    body = "".join(f'[[exception]]\nid = "{i}"\nreason = "{reason}"\n\n' for i, reason in entries)
    path = tmp_path / "exceptions.toml"
    path.write_text(body, encoding="utf-8")
    return path


def compare_exit(report_path, exceptions_path):
    return control.main(["compare", "--report", str(report_path), "--exceptions", str(exceptions_path)])


def test_compare_passes_equal_runs(tmp_path):
    assert compare_exit(write(tmp_path, pair_report()), exceptions(tmp_path)) == 0


def test_compare_refuses_an_unlisted_difference(tmp_path, capsys):
    report = write(tmp_path, pair_report(second_outcome="failed"))
    assert compare_exit(report, exceptions(tmp_path)) == 1
    assert "DIFFERS: t::b" in capsys.readouterr().out


def test_compare_accepts_a_listed_difference(tmp_path):
    report = write(tmp_path, pair_report(second_outcome="failed"))
    assert compare_exit(report, exceptions(tmp_path, ("t::b", "needs a live port"))) == 0


def test_the_committed_bad_fixture_is_refused_by_the_committed_exceptions():
    assert compare_exit(FIXTURES / "r8_bad_pair.json", REAL_EXCEPTIONS) == 1


def test_an_exception_without_a_reason_is_refused_with_exit_2():
    assert compare_exit(FIXTURES / "r8_bad_pair.json", FIXTURES / "r8_reasonless_exceptions.toml") == 2


def test_a_blank_reason_is_no_reason(tmp_path):
    report = write(tmp_path, pair_report(second_outcome="failed"))
    assert compare_exit(report, exceptions(tmp_path, ("t::b", "   "))) == 2


@pytest.mark.parametrize("content", ["not json", "{}", '{"runs": [{"name": "x"}]}'])
def test_compare_exits_3_on_an_unusable_report(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    assert compare_exit(path, exceptions(tmp_path)) == 3


def test_compare_exits_3_on_a_missing_or_malformed_exceptions_file(tmp_path):
    report = write(tmp_path, pair_report())
    assert compare_exit(report, tmp_path / "nope.toml") == 3
    broken = tmp_path / "broken.toml"
    broken.write_text("[[exception]\nid =", encoding="utf-8")
    assert compare_exit(report, broken) == 3


def test_an_id_absent_from_one_run_is_a_difference(tmp_path):
    report = pair_report()
    del report["runs"][2]["outcomes"]["t::b"]
    assert compare_exit(write(tmp_path, report), exceptions(tmp_path)) == 1


def test_the_real_exceptions_file_parses_and_every_entry_has_a_reason():
    listed, reasonless = control.load_exceptions(REAL_EXCEPTIONS)
    assert reasonless == [] and all(listed.values())


# ── shuffle-pair, with the whole-suite runs faked ─────────────────────────

def fake_run_tree(tmp_path, *, hermetic=True, staggered=False, bad_outcome=False, seen=None,
                  collected=True, pytest_exit=0):
    """A stand-in for gate.run_tree: writes a per-test report and returns the SuiteRun it describes."""
    def run_tree(repo, sha, extra_pytest_args, junit_path, slots=None, *, timeout_s):
        if seen is not None:
            seen.append((list(extra_pytest_args), slots))
        seed = next((a.split("=")[1] for a in extra_pytest_args if a.startswith("--core-shuffle-seed=")), None)
        report = tmp_path / f"{seed or 'plain'}.tests.jsonl"
        ok = "failed" if (bad_outcome and seed == "2") else "passed"
        lines = [line("collected", "t::a"), line("collected", "t::b"),
                 line("report", "t::a", "call", "passed"), line("report", "t::b", "call", ok)] if collected else []
        report.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        start = {None: 1_000_000.0, "1": 2_000_000.0, "2": 2_100_000.0 if staggered else 2_000_000.0}[seed]
        return gate.SuiteRun(
            pytest_exit, build_ok=True, hermetic=hermetic, hermetic_reason="" if hermetic else "no namespaces",
            slots_held=4, slots_source=test_slots.ACQUIRED, duration_s=3000.0,
            slots_acquired_at=gate.iso_instant(start), timeout_started_at=gate.iso_instant(start),
            report_path=str(report), junit_path=str(junit_path))
    return run_tree


@pytest.fixture
def pair(tmp_path, monkeypatch):
    # shuffle_pair pops the held-slots variable from os.environ; delenv restores it for later tests.
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    monkeypatch.setattr(control, "_git", lambda repo, *args: "" if args[0] == "status" else "a" * 40)

    def run(**fake):
        monkeypatch.setattr(gate, "run_tree", fake_run_tree(tmp_path, **fake))
        return control.main(["shuffle-pair", "--seeds", "1,2", "--out", str(tmp_path / "pair.json"),
                             "--exceptions", str(REAL_EXCEPTIONS), "-C", str(tmp_path)])
    return run


def test_shuffle_pair_passes_equal_overlapping_hermetic_runs(pair, tmp_path):
    seen = []
    assert pair(seen=seen) == 0
    assert sorted(a for a, _ in seen) == [[], ["--core-shuffle-seed=1"], ["--core-shuffle-seed=2"]]
    assert {slots for _, slots in seen} == {test_slots.configured().per_run}
    assert [r["name"] for r in json.loads((tmp_path / "pair.json").read_text())["runs"]] == [
        "unshuffled", "seed1", "seed2"]


def test_a_run_record_saying_hermetic_false_makes_shuffle_pair_exit_6(pair):
    assert pair(hermetic=False) == 6


def test_shuffled_runs_that_did_not_overlap_are_inconclusive_exit_5(pair):
    assert pair(staggered=True) == 5


def test_shuffle_pair_exits_1_on_an_unlisted_difference(pair):
    assert pair(bad_outcome=True) == 1


def test_a_difference_is_exit_1_even_when_the_shuffled_runs_did_not_overlap(pair):
    assert pair(staggered=True, bad_outcome=True) == 1


def test_runs_that_collected_nothing_are_exit_3_not_a_pass(pair, capsys):
    assert pair(collected=False) == 3
    assert "UNUSABLE" in capsys.readouterr().out


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_a_pytest_exit_other_than_0_or_1_is_exit_3(pair, code):
    assert pair(pytest_exit=code) == 3


def test_a_git_failure_is_exit_3_not_1(tmp_path, monkeypatch):
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)

    def broken_git(repo, *args):
        raise SystemExit("git rev-parse HEAD failed: not a repository")
    monkeypatch.setattr(control, "_git", broken_git)
    assert control.main(["shuffle-pair", "--seeds", "1,2", "--out", str(tmp_path / "p.json"),
                         "-C", str(tmp_path)]) == 3


@pytest.mark.parametrize("argv", [["shuffle-pair", "--seeds", "1,2"], ["compare", "--no-such-flag"]])
def test_a_usage_error_is_exit_3_not_2(argv):
    assert control.main(argv) == 3


def launch(tmp_path, monkeypatch, runs, *, sha="a" * 40, seen=None):
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    monkeypatch.setattr(control, "_git", lambda repo, *args: "" if args[0] == "status" else sha)
    monkeypatch.setattr(gate, "run_tree", fake_run_tree(tmp_path, seen=seen))
    return control.main(["shuffle-pair", "--seeds", "1,2", "--out", str(tmp_path / "pair.json"), "--runs", runs,
                         "--exceptions", str(REAL_EXCEPTIONS), "-C", str(tmp_path)])


def test_the_runs_split_across_two_launches_are_judged_by_the_second(tmp_path, monkeypatch):
    first, second = [], []
    assert launch(tmp_path, monkeypatch, "unshuffled", seen=first) == 4
    assert first == [([], test_slots.configured().per_run)]
    assert not (tmp_path / "pair.json").exists()
    assert launch(tmp_path, monkeypatch, "shuffled", seen=second) == 0
    assert sorted(a for a, _ in second) == [["--core-shuffle-seed=1"], ["--core-shuffle-seed=2"]]
    assert [r["name"] for r in json.loads((tmp_path / "pair.json").read_text())["runs"]] == [
        "unshuffled", "seed1", "seed2"]


def test_a_launch_finding_no_record_of_the_other_runs_is_partial_exit_4(tmp_path, monkeypatch):
    assert launch(tmp_path, monkeypatch, "shuffled") == 4


def test_records_of_another_commit_are_not_judged_together(tmp_path, monkeypatch, capsys):
    assert launch(tmp_path, monkeypatch, "unshuffled", sha="a" * 40) == 4
    assert launch(tmp_path, monkeypatch, "shuffled", sha="b" * 40) == 4
    assert "is of commit aaaaaaaaaaaa" in capsys.readouterr().out


def test_a_full_launch_ignores_earlier_records(tmp_path, monkeypatch):
    assert launch(tmp_path, monkeypatch, "unshuffled") == 4
    seen = []
    assert launch(tmp_path, monkeypatch, "all", seen=seen) == 0
    assert len(seen) == 3


def test_a_run_that_did_not_complete_is_exit_3(tmp_path, monkeypatch):
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    monkeypatch.setattr(control, "_git", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
    monkeypatch.setattr(gate, "run_tree", lambda *a, **k: gate.SuiteRun(None, timed_out=True))
    assert control.main(["shuffle-pair", "--seeds", "1,2", "--out", str(tmp_path / "p.json"),
                         "-C", str(tmp_path)]) == 3


def test_overlap_counts_only_the_time_both_runs_ran():
    first, second = run_record("a", 1, {}, start=0.0, end=100.0), run_record("b", 2, {}, start=60.0, end=200.0)
    assert control.overlap(first, second) == (40.0, 0.4)


# ── run_tree and what it passes down ──────────────────────────────────────

def test_run_tree_passes_the_arguments_and_slots_down_and_keeps_the_copies(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    seen = {}

    def default_runner(repo, sha, root, **kwargs):
        seen.update(kwargs)
        (root / "junit.xml").write_text("<testsuite/>", encoding="utf-8")
        (root / "tests.jsonl").write_text("", encoding="utf-8")
        return gate.SuiteRun(0, junit_path=str(root / "junit.xml"), report_path=str(root / "tests.jsonl"))

    monkeypatch.setattr(gate, "default_runner", default_runner)
    run = gate.run_tree(tmp_path, "a" * 40, ["--core-shuffle-seed=5"], tmp_path / "kept" / "run.junit.xml", 3,
                        sandbox_parent=sandbox)
    assert seen["extra_pytest_args"] == ["--core-shuffle-seed=5"] and seen["slots"] == 3
    assert Path(run.junit_path).read_text(encoding="utf-8") == "<testsuite/>"
    assert Path(run.report_path).name == "run.junit.tests.jsonl" and Path(run.report_path).is_file()
    assert list(sandbox.iterdir()) == []


def test_pytest_argv_puts_the_extra_arguments_last_and_the_gate_passes_none(tmp_path):
    plain = gate.pytest_argv(tmp_path, (), 100)
    extra = gate.pytest_argv(tmp_path, (), 100, extra_args=["--core-shuffle-seed=1"])
    assert extra[:-1] == plain and extra[-1] == "--core-shuffle-seed=1"
    assert not any("shuffle" in part for part in plain)


def test_a_full_run_asks_the_limiter_for_the_slots_it_was_given(monkeypatch):
    asked = []

    def acquire(want, **kwargs):
        asked.append(want)
        return test_slots.Holder(want, test_slots.ACQUIRED, 0.0, 0.0)

    monkeypatch.setattr(test_slots, "acquire", acquire)
    gate._hold_slots(True, (), 4)
    gate._hold_slots(True, (), None)
    assert asked == [4, os.cpu_count() or 1]
