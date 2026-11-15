"""Pin the per-test report plugin (_test_report.py) and the checks of gate_speedup_control.py.

The report runs are real pytest runs of a tiny temporary project (under cap-run.sh when they fan
out); the record checks run on fixture records; slots-selftest runs against its own private slot
directory.
"""
from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _test_report
import gate_speedup_control as control
from lib import suite_gate as gate
from lib import test_slots

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = SCRIPTS_DIR / "tests"
CAP_RUN = SCRIPTS_DIR / "cap-run.sh"
CONTROL = TESTS_DIR / "gate_speedup_control.py"

CONFTEST = """\
import _test_report


def pytest_addhooks(pluginmanager):
    _test_report.register(pluginmanager)
"""

TESTS = """\
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("planted fixture error")


def test_pass():
    pass


def test_fail():
    assert False


@pytest.mark.skip(reason="planted skip")
def test_skip():
    pass


def test_fixture_error(broken):
    pass


@pytest.mark.parametrize("value", [1, 2], ids=["a.b", "c[d]"])
def test_param(value):
    pass
"""


def _project(tmp_path, tests=TESTS):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (project / "test_planted.py").write_text(tests, encoding="utf-8")
    return project


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and k not in ("PYTHONPATH", test_slots.HELD_ENV, _test_report.REPORT_ENV)}
    env.update(PYTHONPATH=str(TESTS_DIR), CORE_TEST_SLOTS_DIR=str(tmp_path / "slots"), **extra)
    return env


def _collect_only(project, env):
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:xdist",
                           "-p", "no:cacheprovider", str(project)],
                          capture_output=True, text=True, env=env, cwd=project, timeout=60)
    ids = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            break
        ids.append(line)
    return ids


def test_under_xdist_every_collected_test_gets_one_terminal_line_keyed_by_its_node_id(tmp_path):
    project = _project(tmp_path)
    report = tmp_path / "tests.jsonl"
    env = _env(tmp_path, **{_test_report.REPORT_ENV: str(report)})
    proc = subprocess.run(
        ["bash", str(CAP_RUN), "--timeout", "90", "--",
         sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", str(project)],
        capture_output=True, text=True, env=env, cwd=project, timeout=120)
    assert proc.returncode == 1 and "1 failed" in proc.stdout and "bringing up nodes" in proc.stdout, proc.stdout + proc.stderr
    lines = _test_report.read_lines(report)
    collected = _test_report.collected_ids(lines)
    expected = _collect_only(project, _env(tmp_path))
    assert "test_planted.py::test_param[a.b]" in expected and "test_planted.py::test_param[c[d]]" in expected
    assert collections.Counter(collected) == collections.Counter(expected) and len(expected) == 6
    terminal = collections.Counter(line["nodeid"] for line in lines if _test_report.is_terminal(line))
    assert terminal == collections.Counter(expected), "exactly one terminal line per test"
    by_id = {line["nodeid"]: (line["when"], line["outcome"]) for line in lines if _test_report.is_terminal(line)}
    assert by_id["test_planted.py::test_pass"] == ("call", "passed")
    assert by_id["test_planted.py::test_fail"] == ("call", "failed")
    assert by_id["test_planted.py::test_skip"] == ("setup", "skipped")
    assert by_id["test_planted.py::test_fixture_error"] == ("setup", "failed")


def test_a_run_killed_mid_test_leaves_the_lines_of_the_tests_it_finished(tmp_path):
    marker = tmp_path / "started"
    tests = (f"import pathlib, time\n\ndef test_first():\n    pass\n\n"
             f"def test_second():\n    pathlib.Path({str(marker)!r}).write_text('x')\n    time.sleep(60)\n")
    project = _project(tmp_path, tests)
    report = tmp_path / "tests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider", str(project)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=project,
        env=_env(tmp_path, **{_test_report.REPORT_ENV: str(report)}))
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the second test never started"
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
    lines = _test_report.read_lines(report)
    assert _test_report.collected_ids(lines) == ["test_planted.py::test_first", "test_planted.py::test_second"]
    assert _test_report.finished_ids(lines) == {"test_planted.py::test_first"}


def test_without_the_variable_nothing_is_written_and_a_nested_run_does_not_inherit_it(tmp_path):
    project = _project(tmp_path, "def test_one():\n    pass\n")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
                           str(project)], capture_output=True, text=True, cwd=project, env=_env(tmp_path))
    assert proc.returncode == 0 and not list(tmp_path.glob("*.jsonl"))
    reporter = _test_report.Reporter()
    os.environ[_test_report.REPORT_ENV] = str(tmp_path / "r.jsonl")
    try:
        reporter.pytest_configure(type("Config", (), {})())
        assert _test_report.REPORT_ENV not in os.environ
    finally:
        os.environ.pop(_test_report.REPORT_ENV, None)
        reporter.pytest_unconfigure(None)


def test_a_torn_last_line_is_dropped(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps({"kind": "collected", "nodeid": "a::b"}) + '\n{"kind": "rep', encoding="utf-8")
    assert _test_report.collected_ids(_test_report.read_lines(path)) == ["a::b"]


# ── the record checks ─────────────────────────────────────────────────────

IDS = ["t.py::a", "t.py::b[x.y]"]


def _record(tmp_path, **over):
    junit = tmp_path / "r.junit.xml"
    junit.write_text("<testsuites><testsuite name='pytest'/></testsuites>", encoding="utf-8")
    report = tmp_path / "r.tests.jsonl"
    report.write_text("".join(
        json.dumps(line) + "\n" for line in
        [{"kind": "collected", "nodeid": i} for i in IDS]
        + [{"kind": "report", "nodeid": i, "when": "call", "outcome": "passed"} for i in IDS]), encoding="utf-8")
    record = {
        "status": gate.PASS, "unavailable_reason": "", "pytest_argv": [str(CAP_RUN), "--", "python3"],
        "slots_source": test_slots.ACQUIRED, "slots_held": 4, "slot_wait_s": 30.0,
        "queued_at": "2026-11-01T00:00:00.000Z", "slots_acquired_at": "2026-11-01T00:00:30.000Z",
        "timeout_started_at": "2026-11-01T00:00:30.100Z", "cpu_s": 100.0, "peak_disk_mb": 50,
        "junit_xml": str(junit), "tests_jsonl": str(report),
    }
    record.update(over)
    return record


def _problems(record, ids=IDS):
    return control.check_record(record, per_run=4, collect_ids=ids)


def test_a_complete_record_passes(tmp_path):
    assert _problems(_record(tmp_path)) == []


@pytest.mark.parametrize("over,needle", [
    ({"status": gate.UNAVAILABLE, "unavailable_reason": "pytest timed out"}, "no verdict"),
    ({"pytest_argv": ["python3", "-m", "pytest"]}, "cap-run.sh"),
    ({"slots_held": 0}, "no slots taken"),
    ({"slots_held": 5}, "no slots taken"),
    ({"slots_source": test_slots.INHERITED}, "no slots taken"),
    ({"timeout_started_at": "2026-11-01T00:00:10.000Z"}, "before the slots"),
    ({"slot_wait_s": 5.0}, "slot_wait_s"),
    ({"queued_at": ""}, "instant is missing"),
    ({"cpu_s": 0.0}, "cpu_s"),
    ({"peak_disk_mb": None}, "peak_disk_mb"),
])
def test_a_record_missing_a_property_is_caught(tmp_path, over, needle):
    problems = _problems(_record(tmp_path, **over))
    assert any(needle in p for p in problems), problems


def test_a_junit_that_does_not_parse_is_caught(tmp_path):
    record = _record(tmp_path)
    Path(record["junit_xml"]).write_text("<testsuites>", encoding="utf-8")
    assert any("JUnit" in p for p in _problems(record))


def test_collected_ids_must_equal_collect_only(tmp_path):
    assert any("differ" in p for p in _problems(_record(tmp_path), ids=IDS + ["t.py::c"]))
    assert any("differ" in p for p in _problems(_record(tmp_path), ids=IDS[:1]))


def test_an_id_collected_twice_is_caught(tmp_path):
    record = _record(tmp_path)
    with open(record["tests_jsonl"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "collected", "nodeid": IDS[0]}) + "\n")
    assert any("more than once" in p for p in _problems(record))


def test_a_pass_with_an_unfinished_test_is_caught_and_a_fail_is_not(tmp_path):
    record = _record(tmp_path)
    lines = _test_report.read_lines(record["tests_jsonl"])
    Path(record["tests_jsonl"]).write_text("".join(json.dumps(line) + "\n" for line in lines[:-1]), encoding="utf-8")
    assert any("no terminal line" in p for p in _problems(record))
    assert _problems({**record, "status": gate.FAIL}) == []


def test_the_runs_must_stay_within_the_limit_and_one_must_have_waited():
    assert control.check_runs([{"slot_wait_s": 0}, {"slot_wait_s": 12.5}], max_held=8, total=8) == []
    assert any("LIMIT EXCEEDED" in p for p in control.check_runs([{"slot_wait_s": 1}], max_held=9, total=8))
    assert any("no run waited" in p for p in control.check_runs([{"slot_wait_s": 0}] * 3, max_held=8, total=8))


def test_a_gate_run_gets_the_sandbox_scrub_list_and_no_enclosing_slot_marker_removed():
    scrubbed = gate.sandbox_scrub_names(SCRIPTS_DIR / gate.SANDBOX_SCRIPT)
    env = control.gate_env({scrubbed[0]: "x", test_slots.HELD_ENV: "4", "KEEP_ME": "1"})
    assert env == {"KEEP_ME": "1"}


def test_the_record_path_is_read_from_the_gate_output():
    assert control._record_path("SUITE: PASS\nRECORD: /x/y.json\n") == "/x/y.json"
    assert control._record_path("error\n") == ""


def _repo_with_tests(tmp_path):
    repo = tmp_path / "repo"
    for rel in ("scripts/tests/test_a.py", "scripts/tests/test_b.py", "scripts/tests/conftest.py",
                "scripts/tests/_helper.py", "scripts/tests/sub/test_c.py", "scripts/tool.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    return repo


def _tree_files(repo, tree):
    return subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", tree],
                          capture_output=True, text=True, check=True).stdout.split()


def test_a_reduced_tree_keeps_the_named_tests_and_every_non_test_file(tmp_path):
    repo = _repo_with_tests(tmp_path)
    tree = control.candidate_tree(repo, ["scripts/tests/test_a.py"])
    assert sorted(_tree_files(repo, tree)) == ["scripts/tests/_helper.py", "scripts/tests/conftest.py",
                                               "scripts/tests/test_a.py", "scripts/tool.py"]
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""  # the repository's own index and working tree are untouched


def test_without_tests_the_tree_is_heads_and_an_unknown_test_is_refused(tmp_path):
    repo = _repo_with_tests(tmp_path)
    head_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                               capture_output=True, text=True, check=True).stdout.strip()
    assert control.candidate_tree(repo, []) == head_tree
    with pytest.raises(SystemExit, match="does not track"):
        control.candidate_tree(repo, ["scripts/tests/test_missing.py"])


# ── slots-selftest itself ─────────────────────────────────────────────────

def _selftest(*args):
    env = {k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV}
    return subprocess.run([sys.executable, str(CONTROL), "slots-selftest", *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_the_selftest_passes_with_the_limiter():
    proc = _selftest()
    assert proc.returncode == 0 and proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr


def test_the_selftest_fails_without_the_limiter():
    proc = _selftest("--limiter", "none")
    assert proc.returncode == 1 and "LIMIT EXCEEDED: held=12 > 8" in proc.stdout, proc.stdout + proc.stderr


# ── verify-named ──────────────────────────────────────────────────────────

FIXTURES = TESTS_DIR / "fixtures"
NAMED_ID = "scripts/tests/test_r11_fixture_never_named.py::test_this_id_is_in_no_baseline_document"


def _verify(report, named, max_named=20):
    return control.verify_named(Path(report), Path(named), max_named)


def _doc(tmp_path, *ids):
    doc = tmp_path / "named.md"
    doc.write_text("".join(f"- `{i}` — reason\n" for i in ids), encoding="utf-8")
    return doc


def test_a_failing_id_that_the_document_names_passes(tmp_path):
    assert _verify(FIXTURES / "r11_bad_report.json", _doc(tmp_path, NAMED_ID)) == 0


def test_a_failing_id_that_the_document_does_not_name_is_refused(tmp_path):
    assert _verify(FIXTURES / "r11_bad_report.json", _doc(tmp_path, "scripts/tests/test_other.py::test_x")) == 1


def test_a_failing_id_that_is_a_prefix_of_a_named_id_is_refused(tmp_path):
    assert _verify(FIXTURES / "r11_bad_report.json", _doc(tmp_path, NAMED_ID + "_longer")) == 1


def test_a_failing_id_mentioned_only_in_prose_is_refused(tmp_path):
    doc = tmp_path / "named.md"
    doc.write_text(f"{NAMED_ID} fails here, reason unknown.\n", encoding="utf-8")
    assert _verify(FIXTURES / "r11_bad_report.json", doc) == 1


def test_a_parametrized_id_with_spaces_is_named_as_one_span(tmp_path):
    report = tmp_path / "report.json"
    spaced = "scripts/tests/t.py::test_a[two words]"
    report.write_text(json.dumps({"collected": 16000, "failing": [spaced], "collection_errors": []}),
                      encoding="utf-8")
    assert _verify(report, _doc(tmp_path, spaced)) == 0


def test_a_clean_run_passes_against_an_empty_document(tmp_path):
    assert _verify(FIXTURES / "r11_clean_report.json", _doc(tmp_path)) == 0


def test_a_document_naming_more_than_the_limit_is_refused():
    assert _verify(FIXTURES / "r11_clean_report.json", FIXTURES / "r11_21_named.md") == 3


def test_the_limit_is_a_parameter():
    assert _verify(FIXTURES / "r11_clean_report.json", FIXTURES / "r11_21_named.md", max_named=21) == 0


def test_a_run_that_did_not_finish_is_refused_even_with_nothing_failing(tmp_path):
    assert _verify(FIXTURES / "r11_incomplete_report.json", _doc(tmp_path)) == 4


def test_a_run_with_a_collection_error_is_refused_however_many_were_collected(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"collected": 16000, "failing": [], "collection_errors": ["scripts/tests/t.py"]}),
                      encoding="utf-8")
    assert _verify(report, _doc(tmp_path)) == 4


def test_an_unreadable_input_is_an_error(tmp_path):
    assert _verify(tmp_path / "missing.json", _doc(tmp_path)) == 2
    assert _verify(FIXTURES / "r11_clean_report.json", tmp_path / "missing.md") == 2


def test_the_command_exit_code_is_the_verdict(tmp_path):
    proc = subprocess.run([sys.executable, str(CONTROL), "verify-named", "--report",
                           str(FIXTURES / "r11_bad_report.json"), "--named", str(_doc(tmp_path))],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "UNNAMED" in proc.stdout, proc.stdout + proc.stderr


# ── reruns of the failing ids ─────────────────────────────────────────────

FLAKY_ID = "scripts/tests/t.py::test_flaky"
STABLE_ID = "scripts/tests/t.py::test_stable"


def test_only_confirmed_ids_must_be_named_and_the_ids_that_passed_on_rerun_are_printed(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"collected": 16000, "failing": [FLAKY_ID, STABLE_ID], "collection_errors": [],
                                  "confirmed": [STABLE_ID], "rerun_passed": [FLAKY_ID]}), encoding="utf-8")
    assert _verify(report, _doc(tmp_path, STABLE_ID)) == 0
    assert FLAKY_ID in capsys.readouterr().out
    assert _verify(report, _doc(tmp_path)) == 1


def _rerun(monkeypatch, failing, results, reruns=2):
    """Run ``_rerun_failing`` against a stand-in runner: each call takes the next set of failing ids
    from ``results`` (None: the rerun did not complete). Returns the verdict and each call's selection."""
    calls = []

    def run(repo, sha, root, *, select, timeout_s, hide=True):
        calls.append(list(select))
        failures = results.pop(0)
        if failures is None:
            return gate.SuiteRun(pytest_exit=None)
        return gate.SuiteRun(pytest_exit=int(bool(failures)), failures=tuple(failures))

    monkeypatch.setattr(control.gate, "default_runner", run)
    return control._rerun_failing(Path("."), "sha", Path("."), failing, reruns, 60), calls


def test_an_id_that_fails_any_rerun_is_confirmed_and_one_that_passes_both_is_not(monkeypatch):
    (confirmed, passed), calls = _rerun(monkeypatch, [FLAKY_ID, STABLE_ID], [[STABLE_ID], []])
    assert (confirmed, passed) == ([STABLE_ID], [FLAKY_ID])
    assert calls == [[FLAKY_ID, STABLE_ID], [FLAKY_ID]]


def test_a_failure_in_the_second_rerun_confirms_the_id(monkeypatch):
    (confirmed, passed), _ = _rerun(monkeypatch, [FLAKY_ID], [[], [FLAKY_ID]])
    assert (confirmed, passed) == ([FLAKY_ID], [])


def test_a_collection_error_is_confirmed_without_a_rerun(monkeypatch):
    (confirmed, passed), calls = _rerun(monkeypatch, ["scripts/tests/t.py"], [])
    assert (confirmed, passed, calls) == (["scripts/tests/t.py"], [], [])


def test_a_rerun_that_does_not_complete_confirms_every_id_under_test(monkeypatch):
    (confirmed, passed), _ = _rerun(monkeypatch, [FLAKY_ID, STABLE_ID], [None], reruns=1)
    assert (sorted(confirmed), passed) == (sorted([FLAKY_ID, STABLE_ID]), [])


def _scenario_record(case, *, status=None, first_run=None, result=None, failed_rounds=None, confirmed=None):
    scenario = control.VERDICT_SCENARIOS[case]
    planted = scenario.planted[0]
    node = control.planted_id(planted.name)
    return scenario, node, {
        "status": status or scenario.verdict,
        "new_failures": [node] if (confirmed if confirmed is not None else planted.result == "confirmed") else [],
        "reruns": {node: {"first_run": first_run or planted.first_run, "result": result or planted.result,
                          "failed_rounds": planted.failed_rounds if failed_rounds is None else failed_rounds}},
    }


@pytest.mark.parametrize("case", control.VERDICT_CASES)
def test_the_expected_record_of_every_scenario_has_no_disagreement(case):
    scenario = control.VERDICT_SCENARIOS[case]
    detail = {control.planted_id(p.name): {"first_run": p.first_run, "result": p.result,
                                           "failed_rounds": list(p.failed_rounds)} for p in scenario.planted}
    record = {"status": scenario.verdict, "reruns": detail,
              "new_failures": [n for n, d in detail.items() if d["result"] == "confirmed"]}
    journaled = [n for n, d in detail.items() if d["result"] == "unconfirmed"]
    assert control.judge_scenario(scenario, record, journaled)[1] == []


def test_a_wrong_verdict_is_a_disagreement():
    scenario, node, record = _scenario_record("confirmed", status=gate.PASS)
    assert any("verdict" in problem for problem in control.judge_scenario(scenario, record, [])[1])


def test_a_confirmed_test_that_was_called_unconfirmed_is_a_disagreement():
    scenario, node, record = _scenario_record("first-rerun-fail", result="unconfirmed", failed_rounds=[],
                                              confirmed=False)
    assert control.judge_scenario(scenario, record, [node])[1]


def test_an_unconfirmed_test_missing_from_the_journal_is_a_disagreement():
    scenario, node, record = _scenario_record("flake-list")
    assert any("journaled" in problem for problem in control.judge_scenario(scenario, record, [])[1])


def test_a_confirmed_test_in_the_journal_is_a_disagreement():
    scenario, node, record = _scenario_record("confirmed")
    assert any("journaled" in problem for problem in control.judge_scenario(scenario, record, [node])[1])


def test_the_single_rerun_mutant_counts_a_test_unconfirmed_when_one_rerun_passes():
    node = "scripts/tests/t.py::test_a"
    rounds = [frozenset({node}), frozenset()]
    assert control.sabotaged_passing_every_rerun({node}, rounds) == {node}
    assert gate.passing_every_rerun({node}, rounds) == frozenset()


def test_the_planted_source_has_one_test_per_planted_id_and_compiles():
    for scenario in control.VERDICT_SCENARIOS.values():
        source = control.planted_source(scenario)
        compile(source, "test_planted.py", "exec")
        for planted in scenario.planted:
            assert f"def test_{planted.name}()" in source


def test_only_listed_planted_tests_enter_the_flake_list_table():
    table = control.listed_table(control.VERDICT_SCENARIOS["flake-list"])
    assert list(table) == [control.planted_id("flaky_listed")]
    assert control.listed_table(control.VERDICT_SCENARIOS["additive"]) == {}


def test_a_journal_state_tells_absent_from_present_and_changed(tmp_path):
    path = tmp_path / "journal.jsonl"
    assert control.journal_state(path) == {"present": False}
    path.write_text("a\n", encoding="utf-8")
    first = control.journal_state(path)
    path.write_text("b\n", encoding="utf-8")
    assert first["present"] and first != control.journal_state(path)


# ── reuse-scenario and k3-negative: the judgements and helpers ────────────

K3_BAD = json.loads((TESTS_DIR / "fixtures" / "k3_bad_outcomes.json").read_text(encoding="utf-8"))["bad"]


def good_outcomes() -> dict:
    """The first bad outcome with its one defect (a regression that passed) put right."""
    outcomes = json.loads(json.dumps(K3_BAD[0]["outcomes"]))
    outcomes["regression"]["verdict"] = "FAIL"
    return outcomes


def test_the_judgement_accepts_a_good_outcome():
    assert control.judge_k3(good_outcomes()) == []


@pytest.mark.parametrize("bad", K3_BAD, ids=[b["expect"] for b in K3_BAD])
def test_the_judgement_refuses_each_recorded_bad_outcome_with_its_own_code(bad):
    assert control.judge_k3(bad["outcomes"]) == [bad["expect"]]


def test_there_are_six_bad_outcomes_with_six_different_codes():
    assert len({b["expect"] for b in K3_BAD}) == len(K3_BAD) == 6


def test_a_missing_case_is_refused_not_skipped():
    outcomes = good_outcomes()
    del outcomes["partial_sets"]
    assert control.judge_k3(outcomes) == ["missing-partial-sets"]


@pytest.mark.parametrize("change,code", [
    (("regression", "reused", 0), "regression-not-exercised"),
    (("regression", "failed_rounds", [1]), "regression-not-exercised"),
    (("partial_sets", "cross_candidate_join", "joined"), "partial-sets-combined"),
    (("partial_sets", "moved_tip_check_exit", 0), "partial-sets-combined"),
    (("partial_sets", "same_binding_join", "refused"), "same-binding-refused"),
    (("key_coverage", "narrow_source", "ran"), "narrow-not-reused"),
])
def test_each_positive_expectation_of_a_good_outcome_is_judged_too(change, code):
    outcomes = good_outcomes()
    section, field, value = change
    outcomes[section][field] = value
    assert control.judge_k3(outcomes) == [code]


def test_a_dist_control_that_reused_nothing_the_second_time_is_inert():
    outcomes = good_outcomes()
    outcomes["key_coverage"]["dist_runs"]["1.0-again"]["reused"] = 0
    assert control.judge_k3(outcomes) == ["dist-control-inert"]


def test_judge_only_exits_zero_for_a_good_outcome_and_one_for_a_bad_one(tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps({"outcomes": good_outcomes()}), encoding="utf-8")
    bad.write_text(json.dumps(K3_BAD[0]["outcomes"]), encoding="utf-8")
    assert control.main(["k3-negative", "--judge-only", str(good)]) == 0
    assert control.main(["k3-negative", "--judge-only", str(bad)]) == 1


def test_k3_negative_without_out_or_judge_only_is_a_usage_error():
    assert control.main(["k3-negative"]) == control.EXIT_REUSE_ERROR


def assertion_inputs(**overrides):
    ids = [f"t::{n}" for n in range(6)]
    touched, named = {ids[0], ids[1]}, ids[0]
    record = {"status": "PASS", "cpu_s": 10.0,
              "test_sources": {ids[0]: "ran", ids[1]: "ran", **{i: "reused" for i in ids[2:]}}}
    inputs = {"named": named, "touched_ids": touched, "hit_ids": set(ids), "collected": ids,
              "disk_mb": 5.0, "disk_max_mb": 10.0}
    return record, inputs | overrides


def failed(found):
    return [name for name, offenders in found.items() if offenders]


def test_reuse_assertions_hold_when_dependents_ran_and_the_rest_were_reused():
    record, inputs = assertion_inputs()
    assert failed(control.reuse_assertions(record, **inputs)) == []


def test_a_store_that_never_saw_the_script_fails_only_the_named_assertion():
    record, inputs = assertion_inputs()
    record["test_sources"]["t::0"] = "reused"
    assert failed(control.reuse_assertions(record, **inputs)) == [control.NAMED_ASSERTION]


def test_a_reused_dependent_a_rerun_bystander_and_a_bad_verdict_are_each_named():
    record, inputs = assertion_inputs()
    record["test_sources"]["t::1"] = "reused"
    record["test_sources"]["t::4"] = "ran"
    record["status"] = "FAIL"
    assert failed(control.reuse_assertions(record, **inputs)) == [
        "touched-dependents-ran", "others-reused", "verdict-pass"]


def test_the_disk_bound_and_the_optional_cpu_and_wall_bounds_are_assertions():
    record, inputs = assertion_inputs(disk_mb=11.0)
    assert failed(control.reuse_assertions(record, **inputs)) == ["disk-bound"]
    record, inputs = assertion_inputs()
    found = control.reuse_assertions(record, **inputs, cpu_max_s=5.0, wall_max_s=1.0, wall_s=2.0)
    assert failed(found) == ["cpu-bound", "wall-bound"]
    assert "cpu-bound" not in control.reuse_assertions(record, **inputs)


def test_an_id_the_record_does_not_list_fails_the_every_id_assertion():
    record, inputs = assertion_inputs()
    del record["test_sources"]["t::5"]
    assert failed(control.reuse_assertions(record, **inputs)) == ["others-reused", "record-lists-every-id"]


def test_the_warm_up_is_needed_only_for_a_missing_id_the_store_could_hold():
    state = {"ids": ["a", "b", "c"], "hits": {"a": 1}, "always_rerun": ["b"], "failed": ["c"]}
    assert not control.needs_warm_up(state)
    assert control.needs_warm_up({**state, "failed": []})


def test_a_nonce_comment_ends_the_file_after_a_newline():
    assert control.with_nonce_comment("x = 1\n", "n1") == "x = 1\n# n1\n"
    assert control.with_nonce_comment("x = 1", "n1") == "x = 1\n# n1\n"


def test_the_dummy_distribution_is_one_installed_distribution_of_the_given_version(tmp_path):
    directory = control.dummy_distribution(tmp_path / "d", "1.1")
    proc = subprocess.run([sys.executable, "-P", "-c",
                           "from importlib.metadata import version; print(version('core-control-dummy'))"],
                          env={**os.environ, "PYTHONPATH": directory}, capture_output=True, text=True)
    assert proc.stdout.strip() == "1.1"


def test_two_interpreter_versions_give_two_keys():
    first, second = control.interpreter_keys()
    assert first != second


def test_spied_joins_record_each_pair_and_restore_the_gate_function():
    original = gate.join_partial_sets
    ids = frozenset({"t::a"})
    one = gate.PartialSet("c" * 40, "a" * 40, ids, frozenset())
    other_tip = gate.PartialSet("c" * 40, "b" * 40, ids, frozenset())
    with control.joins_spied() as seen:
        with pytest.raises(gate.JoinRefused):
            gate.join_partial_sets(one, other_tip)
        gate.join_partial_sets(one, gate.PartialSet("c" * 40, "a" * 40, ids, ids))
    assert [s["result"] for s in seen] == ["refused", "joined"]
    assert gate.join_partial_sets is original
    assert control.join_result(one, other_tip) == "refused"


def test_edited_tree_keeps_the_test_files_that_reduced_tree_drops(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts" / "tests").mkdir(parents=True)
    (repo / "scripts" / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    (repo / "scripts" / "tests" / "helper.py").write_text("H = 1\n")
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"],
                   check=True)

    def names(tree):
        return control._git(repo, "ls-tree", "-r", "--name-only", tree).splitlines()

    kept = names(control.edited_tree(repo, {"a.txt": "b\n", "new.txt": "n\n"}))
    assert "scripts/tests/test_x.py" in kept and "new.txt" in kept
    reduced = control.reduced_tree(repo, {"new.txt": "n\n"})
    assert "scripts/tests/test_x.py" not in names(reduced) and "scripts/tests/helper.py" in names(reduced)
    changed = control.edited_tree(repo, {"a.txt": "b\n"})
    assert control._git(repo, "show", f"{changed}:a.txt") == "b"


def test_the_planted_k3_sources_compile():
    for source in (control.K3_REGRESSION, control.K3_WIDE_SOURCE, control.K3_NARROW_SOURCE, control.K3_ANCHOR):
        compile(source, "test_planted.py", "exec")
