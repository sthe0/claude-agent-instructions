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
