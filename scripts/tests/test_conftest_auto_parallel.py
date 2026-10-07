"""Pin the auto-parallel hook (`_xdist_auto.py`) and the cap wrapper that makes it safe to trial.

A fan-out hook without an "I am the worker" exit is a fork bomb (issue #304): every worker
re-fires it. The guard is pinned three ways — a unit test per veto clause, a mutation
catalogue (M1-M7) showing a named control goes RED when each clause or parameter is
removed, and one real run that counts the workers and their parent. Every pytest process
this file starts goes through `scripts/cap-run.sh`.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

import _xdist_auto
from _xdist_auto import CLAUSES, pytest_configure, pytest_terminal_summary, wants_auto_parallel

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent.parent
CAP_RUN = REPO / "scripts" / "cap-run.sh"

XDIST_IMPORTABLE = importlib.util.find_spec("xdist") is not None
needs_xdist = pytest.mark.skipif(not XDIST_IMPORTABLE, reason="pytest-xdist is not importable")
needs_nonroot = pytest.mark.skipif(
    os.geteuid() == 0, reason="RLIMIT_NPROC is not enforced for root"
)


def _clean_env(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PYTEST_XDIST_") and k != "PYTEST_ADDOPTS"
    }
    env.update(extra)
    return env


def _cap_run(extra_tasks, cap_timeout, *command, wait=90, **run_kwargs):
    argv = [
        "bash", str(CAP_RUN),
        "--extra-tasks", str(extra_tasks), "--timeout", str(cap_timeout), "--", *command,
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=wait, **run_kwargs)


def _describe(proc):
    return f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


_FORK_UNTIL_EAGAIN = """
import subprocess
kids = []
hit = None
try:
    for i in range(500):
        try:
            kids.append(subprocess.Popen(["sleep", "5"]))
        except OSError:
            hit = i
            break
finally:
    for k in kids:
        k.kill()
    for k in kids:
        k.wait()
print("none" if hit is None else hit)
"""


@needs_nonroot
def test_cap_enforces_the_task_limit():
    # Only this child keeps a tight budget: it must hit EAGAIN well before the ceiling of
    # 500 forks. The per-user thread count swings by tens within seconds on a shared host,
    # so a budget under ~20 can fail the child's own start; retried for the same reason.
    for _ in range(3):
        proc = _cap_run(40, 60, sys.executable, "-c", _FORK_UNTIL_EAGAIN)
        if proc.stdout.strip():
            break
    assert proc.stdout.strip() not in ("", "none"), (
        "no EAGAIN within 500 forks under --extra-tasks 40: the cap is not enforced\n"
        + _describe(proc)
    )
    assert int(proc.stdout.strip()) < 500


def test_cap_lets_a_command_with_headroom_run():
    proc = _cap_run(200, 60, sys.executable, "-c", "print(1)")
    assert proc.returncode == 0 and proc.stdout.strip() == "1", _describe(proc)


def test_nested_cap_clamps_instead_of_failing():
    inner = [
        "bash", str(CAP_RUN), "--extra-tasks", "200", "--timeout", "30",
        "--", sys.executable, "-c", "print(1)",
    ]
    proc = _cap_run(150, 60, *inner)
    assert proc.returncode == 0 and proc.stdout.strip() == "1", _describe(proc)


def _launch_in_own_group(script, marker):
    # The outer `bash -c` is the session and group leader; `; exit $?` defeats bash's
    # exec-the-last-command optimisation, so the cap-run bash is a child of the leader.
    # Launching cap-run directly would make it the session leader, and its own setpgid
    # (EPERM) would hide whether --foreground works.
    inner = f'python3 -c "import time; time.sleep(60)  # {marker}"'
    cmd = f"bash {script} --extra-tasks 100 --timeout 60 -- {inner}; exit $?"
    return subprocess.Popen(
        ["bash", "-c", cmd],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sleeper_pids(marker):
    # Anchored: the ancestors' command lines (bash -c, cap-run, timeout) carry the marker too.
    out = subprocess.run(
        ["pgrep", "-f", f"^python3 -c .*{marker}"], capture_output=True, text=True
    )
    return [int(p) for p in out.stdout.split()]


def _wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def test_group_kill_reaches_the_command_through_cap_run():
    marker = uuid.uuid4().hex
    proc = _launch_in_own_group(CAP_RUN, marker)
    try:
        assert _wait_for(lambda: _sleeper_pids(marker), 15), "sleeper never started"
        os.killpg(proc.pid, signal.SIGKILL)
        assert _wait_for(lambda: not _sleeper_pids(marker), 5), (
            "the sleeper survived a group kill: cap-run.sh orphans its command"
        )
    finally:
        for pid in _sleeper_pids(marker):
            os.kill(pid, signal.SIGKILL)
        proc.wait()


def test_mutant_without_foreground_orphans_the_command(tmp_path):
    original = CAP_RUN.read_text()
    mutant_text = original.replace("timeout --foreground -k", "timeout -k")
    assert mutant_text != original, "the mutation did not apply"
    mutant = tmp_path / "cap-run-no-foreground.sh"
    mutant.write_text(mutant_text)
    marker = uuid.uuid4().hex
    proc = _launch_in_own_group(mutant, marker)
    try:
        assert _wait_for(lambda: _sleeper_pids(marker), 15), "sleeper never started"
        os.killpg(proc.pid, signal.SIGKILL)
        time.sleep(5)
        assert _sleeper_pids(marker), (
            "the group-kill control did not discriminate: the sleeper died without --foreground"
        )
    finally:
        for pid in _sleeper_pids(marker):
            os.kill(pid, signal.SIGKILL)
        proc.wait()


@pytest.fixture(autouse=True)
def _no_xdist_environment(monkeypatch):
    # This file also runs inside xdist workers, where PYTEST_XDIST_WORKER is set and would
    # veto every positive case below.
    for key in list(os.environ):
        if key.startswith("PYTEST_XDIST_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)


_UNSET = object()


class FakeConfig:
    """Just enough of pytest's Config for the hooks: a suite-shaped, xdist-loaded run."""

    def __init__(self, args=(str(TESTS_DIR),), argv=(), xdist=True, plugins=("xdist",),
                 auto_workers=4, workerinput=_UNSET, **options):
        self.args = list(args)
        self.invocation_params = types.SimpleNamespace(args=tuple(argv))
        self.option = types.SimpleNamespace(
            collectonly=False, usepdb=False, trace=False, stepwise=False,
            stepwise_skip=False, showfixtures=False, show_fixtures_per_test=False,
            setupplan=False, setuponly=False, markers=False, capture="fd",
        )
        if xdist:
            self.option.numprocesses = None
            self.option.dist = "no"
            self.option.tx = []
            self.option.maxprocesses = None
            self.option.looponfail = False
        vars(self.option).update(options)
        self._plugins = set(plugins)
        self._auto_workers = auto_workers
        if workerinput is not _UNSET:
            self.workerinput = workerinput
        self.pluginmanager = types.SimpleNamespace(hasplugin=lambda name: name in self._plugins)
        self.hook = types.SimpleNamespace(
            pytest_xdist_auto_num_workers=lambda config: self._auto_workers
        )

    def getoption(self, name, default=_UNSET, skip=False):
        options = vars(self.option)
        if name in options:
            return options[name]
        if default is _UNSET:
            raise ValueError(f"no option named {name!r}")
        return default


class FakeReporter:
    def __init__(self):
        self.lines = []

    def write_line(self, line, **markup):
        self.lines.append(line)


def _summary(config, **kwargs):
    reporter = FakeReporter()
    pytest_terminal_summary(reporter, 0, config, **kwargs)
    return reporter.lines


# --- one positive and one negative test per clause -------------------------------------


def test_a_plain_suite_run_fans_out():
    assert wants_auto_parallel(FakeConfig()) is True


def test_unknown_disabled_clause_is_rejected():
    with pytest.raises(ValueError):
        wants_auto_parallel(FakeConfig(), disabled={"no-such-clause"})


def test_worker_clause_vetoes_on_workerinput():
    assert wants_auto_parallel(FakeConfig(workerinput={})) is False


def test_worker_clause_vetoes_on_env_marker(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert wants_auto_parallel(FakeConfig()) is False


def test_worker_clause_ignores_an_unrelated_environment(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "x")
    assert wants_auto_parallel(FakeConfig()) is True


def test_xdist_absent_clause_vetoes_without_the_plugin():
    assert wants_auto_parallel(FakeConfig(xdist=False, plugins=())) is False


def test_xdist_absent_clause_passes_with_the_plugin():
    assert wants_auto_parallel(FakeConfig(plugins=("xdist",))) is True


@pytest.mark.parametrize("options", [
    {"numprocesses": 0},
    {"numprocesses": 4},
    {"numprocesses": "auto"},
    {"dist": "load"},
    {"tx": ["popen"]},
])
def test_explicit_clause_defers_to_an_xdist_option(options):
    assert wants_auto_parallel(FakeConfig(**options)) is False


@pytest.mark.parametrize("argv", [("--dist", "no"), ("--dist=no",), ("-d",), ("--dist", "load")])
def test_explicit_clause_defers_to_a_dist_flag_on_the_command_line(argv):
    assert wants_auto_parallel(FakeConfig(argv=argv)) is False


@pytest.mark.parametrize("addopts", ["--dist no", "-q --dist=no", "-d"])
def test_explicit_clause_defers_to_a_dist_flag_in_addopts(monkeypatch, addopts):
    monkeypatch.setenv("PYTEST_ADDOPTS", addopts)
    assert wants_auto_parallel(FakeConfig()) is False


def test_explicit_clause_ignores_unrelated_arguments():
    assert wants_auto_parallel(FakeConfig(argv=("-q", "-p", "no:cacheprovider"))) is True


@pytest.mark.parametrize("flag", [
    "collectonly", "usepdb", "trace", "stepwise", "stepwise_skip", "looponfail",
    "showfixtures", "show_fixtures_per_test", "setupplan", "setuponly", "markers",
])
def test_mode_clause_vetoes_each_mode_flag(flag):
    assert wants_auto_parallel(FakeConfig(**{flag: True})) is False


def test_mode_clause_vetoes_unbuffered_output():
    assert wants_auto_parallel(FakeConfig(capture="no")) is False


def test_mode_clause_passes_default_capture():
    assert wants_auto_parallel(FakeConfig(capture="sys")) is True


@pytest.mark.parametrize("args", [
    [str(Path(__file__))],
    [f"{Path(__file__)}::test_a_plain_suite_run_fans_out"],
    ["no/such/path"],
    [],
])
def test_shape_clause_keeps_non_suite_runs_serial(args):
    assert wants_auto_parallel(FakeConfig(args=args)) is False


def test_shape_clause_accepts_a_directory_among_arguments():
    assert wants_auto_parallel(FakeConfig(args=[str(Path(__file__)), str(TESTS_DIR)])) is True


# --- hooks ------------------------------------------------------------------------------


def test_configure_sets_the_options_xdist_reads():
    config = FakeConfig(auto_workers=5)
    pytest_configure(config)
    assert config.option.numprocesses == 5
    assert config.option.dist == "load"
    assert config.option.tx == ["popen"] * 5


def test_configure_clips_to_maxprocesses():
    config = FakeConfig(auto_workers=8, maxprocesses=3)
    pytest_configure(config)
    assert config.option.numprocesses == 3
    assert config.option.tx == ["popen"] * 3


def test_configure_leaves_a_vetoed_run_untouched():
    config = FakeConfig(workerinput={})
    pytest_configure(config)
    assert config.option.numprocesses is None and config.option.dist == "no"


def test_configure_stays_serial_for_a_single_worker():
    config = FakeConfig(auto_workers=1)
    pytest_configure(config)
    assert config.option.numprocesses is None


def test_summary_reports_workers_when_the_dispatcher_runs():
    config = FakeConfig(plugins=("xdist", "dsession"), auto_workers=3)
    pytest_configure(config)
    (line,) = _summary(config)
    assert line.startswith("auto-parallel: 3 workers")


def test_summary_is_silent_in_a_worker():
    config = FakeConfig(plugins=("xdist", "dsession"), auto_workers=3)
    pytest_configure(config)
    config.workerinput = {}
    assert _summary(config) == []


def test_summary_writes_the_install_hint_when_xdist_is_not_importable(monkeypatch):
    monkeypatch.setattr(_xdist_auto, "_xdist_importable", lambda: False)
    config = FakeConfig(xdist=False, plugins=())
    (line,) = _summary(config)
    assert "pip install pytest-xdist" in line


def test_summary_has_no_install_hint_when_xdist_is_importable_but_unloaded(monkeypatch):
    # `-p no:xdist` on a machine that has the package: the hint would be false.
    monkeypatch.setattr(_xdist_auto, "_xdist_importable", lambda: True)
    config = FakeConfig(xdist=False, plugins=())
    assert _summary(config) == []


def test_summary_has_no_install_hint_for_a_non_suite_run(monkeypatch):
    monkeypatch.setattr(_xdist_auto, "_xdist_importable", lambda: False)
    config = FakeConfig(xdist=False, plugins=(), args=[str(Path(__file__))])
    assert _summary(config) == []


def test_importability_lookup_matches_find_spec():
    assert _xdist_auto._xdist_importable() is XDIST_IMPORTABLE


def test_wiring_identity():
    spec = importlib.util.spec_from_file_location(
        "_repo_tests_conftest", TESTS_DIR / "conftest.py"
    )
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    assert conftest.pytest_configure is _xdist_auto.pytest_configure
    assert conftest.pytest_terminal_summary is _xdist_auto.pytest_terminal_summary


# --- mutation catalogue ------------------------------------------------------------------

_VETOED_BY = {
    "worker": lambda: FakeConfig(workerinput={}),
    "xdist_absent": lambda: FakeConfig(xdist=False, plugins=()),
    "explicit": lambda: FakeConfig(numprocesses=0),
    "mode": lambda: FakeConfig(collectonly=True),
    "shape": lambda: FakeConfig(args=[str(Path(__file__))]),
}


def _clause_control(clause, disabled=frozenset()):
    assert wants_auto_parallel(_VETOED_BY[clause](), disabled=disabled) is False, (
        f"the {clause!r} clause no longer vetoes a run it must veto"
    )


def test_clause_catalogue_matches_the_controls():
    assert {name for name, _ in CLAUSES} == set(_VETOED_BY)


@pytest.mark.parametrize("clause", sorted(_VETOED_BY))
def test_clause_controls_are_green_on_the_real_predicate(clause):
    _clause_control(clause)


def _clause_mutant(clause):
    with pytest.raises(AssertionError):
        _clause_control(clause, disabled={clause})


def test_mutant_m1():
    _clause_mutant("worker")


def test_mutant_m2():
    _clause_mutant("xdist_absent")


def test_mutant_m3():
    _clause_mutant("explicit")


def test_mutant_m4():
    _clause_mutant("mode")


def test_mutant_m5():
    _clause_mutant("shape")


def _single_worker_control(**kwargs):
    config = FakeConfig(auto_workers=1)
    pytest_configure(config, **kwargs)
    assert config.option.numprocesses is None, "a single worker must stay serial"


def test_mutant_m6():
    _single_worker_control()
    with pytest.raises(AssertionError):
        _single_worker_control(min_workers=1)


def _no_dispatcher_control(**kwargs):
    config = FakeConfig(plugins=("xdist",), auto_workers=3)
    pytest_configure(config)
    assert _summary(config, **kwargs) == [], "no workers line without a dispatcher"


def test_mutant_m7():
    _no_dispatcher_control()
    with pytest.raises(AssertionError):
        _no_dispatcher_control(require_dsession=False)


# --- one real, capped run that counts the workers and their parent -----------------------

_MINI_CONFTEST = """\
import os
import sys

sys.path.insert(0, {tests_dir!r})
from _xdist_auto import pytest_configure, pytest_terminal_summary  # noqa: F401


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):
        with open(os.path.join({results!r}, "controller.txt"), "w") as f:
            f.write(str(os.getpid()))
"""

_MINI_TESTS = """\
import os

import pytest


@pytest.mark.parametrize("i", range(12))
def test_record(i):
    with open(os.path.join({results!r}, f"{{i}}.txt"), "w") as f:
        f.write(f"{{os.getpid()}} {{os.getppid()}} {{os.environ.get('PYTEST_XDIST_WORKER', '')}}")
"""


def _run_mini_suite(root, extra_env=None, extra_args=()):
    suite = root / "suite"
    results = root / "results"
    suite.mkdir()
    results.mkdir()
    (suite / "conftest.py").write_text(
        _MINI_CONFTEST.format(tests_dir=str(TESTS_DIR), results=str(results))
    )
    (suite / "test_mini.py").write_text(_MINI_TESTS.format(results=str(results)))
    env = _clean_env(PYTEST_XDIST_AUTO_NUM_WORKERS="3", **(extra_env or {}))
    proc = _cap_run(
        150, 120, sys.executable, "-m", "pytest", str(suite), "-q", "-p", "no:cacheprovider",
        *extra_args, cwd=str(root), env=env, wait=150,
    )
    controller = int((results / "controller.txt").read_text())
    rows = [
        (results / f"{i}.txt").read_text().split(" ")
        for i in range(12)
        if (results / f"{i}.txt").exists()
    ]
    return proc, controller, rows


@needs_nonroot
@needs_xdist
def test_real_run_fans_out_to_exactly_the_requested_workers_under_the_controller(tmp_path):
    proc, controller, rows = _run_mini_suite(tmp_path)
    assert proc.returncode == 0, _describe(proc)
    assert len(rows) == 12, _describe(proc)
    assert len({r[0] for r in rows}) == 3, _describe(proc)
    assert {int(r[1]) for r in rows} == {controller}, _describe(proc)
    assert {r[2].strip() for r in rows} == {"gw0", "gw1", "gw2"}, _describe(proc)
    assert "auto-parallel: 3 workers" in proc.stdout, _describe(proc)


@needs_nonroot
@needs_xdist
def test_real_run_with_a_worker_marker_in_the_environment_stays_serial(tmp_path):
    proc, controller, rows = _run_mini_suite(
        tmp_path, extra_env={"PYTEST_XDIST_WORKER": "gw9"}
    )
    assert proc.returncode == 0, _describe(proc)
    assert {int(r[0]) for r in rows} == {controller}, _describe(proc)
    assert {r[2].strip() for r in rows} == {"gw9"}, _describe(proc)
    assert "auto-parallel:" not in proc.stdout, _describe(proc)


@needs_nonroot
@needs_xdist
def test_real_run_with_xdist_disabled_stays_serial(tmp_path):
    proc, controller, rows = _run_mini_suite(tmp_path, extra_args=("-p", "no:xdist"))
    assert proc.returncode == 0, _describe(proc)
    assert {int(r[0]) for r in rows} == {controller}, _describe(proc)
    assert {r[2].strip() for r in rows} == {""}, _describe(proc)
    assert "auto-parallel:" not in proc.stdout, _describe(proc)
    assert "tests ran serially" not in proc.stdout, _describe(proc)
