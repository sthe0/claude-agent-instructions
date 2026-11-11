"""Pin the machine-wide worker-slot limiter (scripts/lib/test_slots.py) and its use by _xdist_auto.

Every test uses a private slot directory; none touches the machine's, and the only processes a
test signals are the holders it started itself.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

import _xdist_auto
from lib import test_slots

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = SCRIPTS_DIR / "tests"
CAP_RUN = SCRIPTS_DIR / "cap-run.sh"

HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from lib import test_slots
    limits = test_slots.Limits(8, 4, test_slots.Path(sys.argv[2]))
    holder = test_slots.acquire(int(sys.argv[3]), limits=limits, poll_s=0.05,
                                on_wait=lambda held: print("waiting", flush=True))
    print(f"acquired {holder.count} {holder.wait_s:.3f}", flush=True)
    time.sleep(float(sys.argv[4]))
""")


@pytest.fixture
def slot_dir(tmp_path, monkeypatch):
    path = tmp_path / "slots"
    monkeypatch.setenv(test_slots.DIR_ENV, str(path))
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    return path


def limits(path, total=8, per_run=4):
    return test_slots.Limits(total, per_run, path)


def _readline(proc, seconds=30):
    """The next stdout line of ``proc``, failing the test instead of hanging when none comes."""
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
    try:
        return lines.get(timeout=seconds)
    except queue.Empty:
        pytest.fail(f"pid {proc.pid} printed no line within {seconds} s")


def _wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


class Holders:
    """Holder processes this test starts; only they are ever killed."""

    def __init__(self, directory):
        self.directory = directory
        self.procs = []

    def start(self, want, hold_s=60):
        env = {k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV}
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(SCRIPTS_DIR), str(self.directory), str(want), str(hold_s)],
            stdout=subprocess.PIPE, text=True, env=env)
        self.procs.append(proc)
        return proc

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.fixture
def holders(slot_dir):
    started = Holders(slot_dir)
    yield started
    started.close()


def _pids(directory):
    return {pid for _, pid in test_slots.held(directory)}


# ── who holds what ────────────────────────────────────────────────────────

def test_held_reads_granted_flocks_on_slot_files_only(tmp_path):
    directory = tmp_path / "slots"
    directory.mkdir()
    for name in ("slot-0", "slot-1", "queue"):
        (directory / name).write_text("", encoding="utf-8")
    key = {name: test_slots._file_key(os.stat(directory / name)) for name in ("slot-0", "slot-1", "queue")}

    def row(n, kind, pid, name, waiter=False):
        major, minor, ino = key[name]
        arrow = "-> " if waiter else ""
        return f"{n}: {arrow}{kind}  ADVISORY  WRITE {pid} {major:02x}:{minor:02x}:{ino} 0 EOF"

    locks = tmp_path / "locks"
    locks.write_text("\n".join([
        row(1, "FLOCK", 111, "slot-1"),
        row(1, "FLOCK", 222, "slot-1", waiter=True),
        row(2, "POSIX", 333, "slot-0"),
        row(3, "FLOCK", 444, "queue"),
        row(4, "FLOCK", 555, "slot-0"),
    ]) + "\n", encoding="utf-8")
    assert test_slots.held(directory, str(locks)) == [("slot-0", 555), ("slot-1", 111)]


def test_a_slot_seen_twice_in_one_torn_read_counts_once(tmp_path):
    directory = tmp_path / "slots"
    directory.mkdir()
    (directory / "slot-0").write_text("", encoding="utf-8")
    major, minor, ino = test_slots._file_key(os.stat(directory / "slot-0"))
    dev = f"{major:02x}:{minor:02x}:{ino}"
    locks = tmp_path / "locks"
    locks.write_text(f"1: FLOCK  ADVISORY  WRITE 111 {dev} 0 EOF\n"
                     f"9: FLOCK  ADVISORY  WRITE 222 {dev} 0 EOF\n", encoding="utf-8")
    assert test_slots.held(directory, str(locks)) == [("slot-0", 222)]


def test_held_sees_this_process_holding_real_slots(slot_dir):
    with test_slots.acquire(2, limits=limits(slot_dir)) as holder:
        assert test_slots.held(slot_dir) == [("slot-0", os.getpid()), ("slot-1", os.getpid())]
        assert holder.slots == ("slot-0", "slot-1") and holder.source == test_slots.ACQUIRED
    assert test_slots.held(slot_dir) == []


def test_the_held_cli_reports_json(slot_dir):
    with test_slots.acquire(3, limits=limits(slot_dir)):
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "lib" / "test_slots.py"), "held", "--json",
                               "--dir", str(slot_dir)], capture_output=True, text=True)
    assert proc.returncode == 0 and '"held": 3' in proc.stdout


# ── acquisition ───────────────────────────────────────────────────────────

def test_a_run_takes_at_most_its_per_run_share(slot_dir):
    with test_slots.acquire(12, limits=limits(slot_dir)) as holder:
        assert holder.count == 4 and len(test_slots.held(slot_dir)) == 4


def test_the_wait_message_names_the_share_the_run_waits_for(tmp_path):
    text = test_slots.describe_wait(12, limits(tmp_path), [("slot-0", 7), ("slot-1", 7), ("slot-2", 9)])
    assert text.startswith("test slots: waiting for 4 of 8 ") and text.endswith("(3 held by pid 7, 9)")


def test_two_runs_share_eight_slots_and_a_third_waits_until_a_holder_dies(holders, slot_dir):
    first = holders.start(4)
    assert _wait_for(lambda: len(test_slots.held(slot_dir)) == 4, 15)
    second = holders.start(4)
    assert _wait_for(lambda: len(test_slots.held(slot_dir)) == 8, 15)
    third = holders.start(4)
    assert _readline(third).strip() == "waiting"
    time.sleep(1.0)
    assert len(test_slots.held(slot_dir)) == 8 and third.pid not in _pids(slot_dir)
    os.kill(first.pid, signal.SIGKILL)
    first.wait()
    line = _readline(third).split()
    assert line[:2] == ["acquired", "4"] and float(line[2]) >= 1.0
    assert _wait_for(lambda: _pids(slot_dir) == {second.pid, third.pid}, 5)
    assert len(test_slots.held(slot_dir)) == 8


def test_a_killed_holder_frees_its_slots_without_any_cleanup(holders, slot_dir):
    proc = holders.start(4)
    assert _wait_for(lambda: len(test_slots.held(slot_dir)) == 4, 15)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    assert test_slots.held(slot_dir) == []
    with test_slots.acquire(4, limits=limits(slot_dir), poll_s=0.01) as holder:
        assert holder.wait_s < 1.0


def test_a_run_under_the_held_marker_takes_nothing(slot_dir, monkeypatch):
    monkeypatch.setenv(test_slots.HELD_ENV, "3")
    holder = test_slots.acquire(4, limits=limits(slot_dir))
    assert (holder.count, holder.source, holder.wait_s) == (3, test_slots.INHERITED, 0.0)
    assert not slot_dir.exists() or test_slots.held(slot_dir) == []


def test_a_child_of_a_holder_inherits_without_the_marker(slot_dir):
    with test_slots.acquire(2, limits=limits(slot_dir)):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from lib import test_slots; "
             "print(test_slots.inherited(sys.argv[2], env={}))", str(SCRIPTS_DIR), str(slot_dir)],
            capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV})
    assert proc.stdout.strip() == "2", proc.stderr
    assert test_slots.inherited(slot_dir, env={}) is None, "the holder itself is not its own ancestor"


def test_without_proc_locks_only_the_marker_says_a_run_holds_slots(slot_dir):
    with test_slots.acquire(2, limits=limits(slot_dir)):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from lib import test_slots; "
             "print(test_slots.inherited(sys.argv[2], env={}, locks_path=sys.argv[3]), "
             "test_slots.inherited(sys.argv[2], env={test_slots.HELD_ENV: '2'}, locks_path=sys.argv[3]))",
             str(SCRIPTS_DIR), str(slot_dir), str(slot_dir / "no-proc-locks")],
            capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV})
    assert proc.stdout.split() == ["None", "2"], proc.stderr


def test_a_wait_past_the_limit_raises_naming_the_holders_and_reports_while_it_waits(slot_dir):
    reports = []
    whole = limits(slot_dir)
    with test_slots.acquire(4, limits=whole), test_slots.acquire(4, limits=whole):
        bounded = test_slots.Limits(8, 4, slot_dir, max_wait_s=1)
        started = time.monotonic()
        with pytest.raises(test_slots.SlotWaitExpired) as caught:
            test_slots.acquire(4, limits=bounded, poll_s=0.05, on_wait=reports.append, report_s=0.3)
    assert 1.0 <= time.monotonic() - started < 10
    assert {pid for _, pid in caught.value.holders} == {os.getpid()}
    assert f"held by pid {os.getpid()}" in str(caught.value)
    assert len(reports) >= 2 and all(len(r) == 8 for r in reports), "the report repeats while the run waits"
    assert test_slots.held(slot_dir) == [], "an expired wait keeps no slot"


def test_the_wait_limit_and_report_interval_defaults():
    assert test_slots.DEFAULT_MAX_WAIT_S == test_slots.configured().max_wait_s == 14400
    assert test_slots.WAIT_REPORT_S == 60


GATE = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from lib import suite_gate, test_slots
    holder = test_slots.acquire(4, limits=test_slots.Limits(8, 4, test_slots.Path(sys.argv[2])))
    child = ("import os, sys, time\\n"
             "open(sys.argv[2], 'w').write(str(os.getpid()))\\n"
             "deadline = time.time() + 60\\n"
             "while not os.path.exists(sys.argv[1]) and time.time() < deadline:\\n"
             "    time.sleep(0.05)\\n")
    suite_gate.run_group([sys.executable, "-c", child, sys.argv[3], sys.argv[4]], cwd=None,
                         env=dict(os.environ), timeout_s=120, pass_fds=holder.fds)
""")


def test_slots_stay_held_by_a_running_pytest_after_its_gate_is_killed(slot_dir, tmp_path):
    release, pid_file = tmp_path / "release", tmp_path / "child.pid"
    env = {k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV}
    gate = subprocess.Popen([sys.executable, "-c", GATE, str(SCRIPTS_DIR), str(slot_dir), str(release), str(pid_file)],
                            env=env)
    child_pid = None
    try:
        assert _wait_for(lambda: pid_file.is_file() and pid_file.read_text() and len(test_slots.held(slot_dir)) == 4, 30)
        child_pid = int(pid_file.read_text())
        os.kill(gate.pid, signal.SIGKILL)
        gate.wait()
        time.sleep(0.5)
        assert len(test_slots.held(slot_dir)) == 4, "the fake pytest still holds the slots"
        release.write_text("", encoding="utf-8")
        assert _wait_for(lambda: test_slots.held(slot_dir) == [], 30), "the slots are freed when the child exits"
    finally:
        if gate.poll() is None:
            gate.kill()
            gate.wait()
        if child_pid is not None and not release.exists():
            os.kill(child_pid, signal.SIGKILL)


def test_the_slot_dir_must_be_a_directory_of_this_user(tmp_path, monkeypatch):
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        test_slots.acquire(1, limits=limits(link))


def test_the_config_rows_equal_the_code_defaults(monkeypatch):
    monkeypatch.delenv(test_slots.DIR_ENV, raising=False)
    configured = test_slots.configured()
    assert (configured.total, configured.per_run) == (test_slots.DEFAULT_TOTAL, test_slots.DEFAULT_PER_RUN) == (8, 4)
    assert configured.directory == Path(f"/tmp/core-test-slots-{os.getuid()}")


def test_the_dir_override_and_bad_rows(tmp_path):
    config = tmp_path / "config.md"
    config.write_text("| `test-slots-total` | `x` |\n| `test-slots-per-run` | `2` |\n"
                      "| `test-slots-dir` | `/tmp/s-<uid>` |\n", encoding="utf-8")
    assert test_slots.configured(config, env={}) == test_slots.Limits(8, 2, Path(f"/tmp/s-{os.getuid()}"))
    assert test_slots.configured(config, env={test_slots.DIR_ENV: str(tmp_path)}).directory == tmp_path


# ── _xdist_auto takes slots ───────────────────────────────────────────────

class ExplicitConfig:
    """A pytest Config of an explicit `-n N` run of one file, enough for _xdist_auto.pytest_configure."""

    def __init__(self, n):
        self.args = ["test_x.py"]
        self.invocation_params = types.SimpleNamespace(args=("-n", str(n)))
        self.option = types.SimpleNamespace(numprocesses=n, dist="load", tx=["popen"] * n, maxprocesses=None)
        self.pluginmanager = types.SimpleNamespace(hasplugin=lambda name: name == "xdist")
        self.cleanups = []

    def getoption(self, name, default=None):
        return getattr(self.option, name, default)

    def add_cleanup(self, fn):
        self.cleanups.append(fn)


def test_an_explicit_n_12_runs_with_the_per_run_share(slot_dir, monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = ExplicitConfig(12)
    _xdist_auto.pytest_configure(config)
    assert config.option.numprocesses == 4 and config.option.tx == ["popen"] * 4
    assert len(test_slots.held(slot_dir)) == 4 and os.environ[test_slots.HELD_ENV] == "4"
    for cleanup in config.cleanups:
        cleanup()
    assert test_slots.held(slot_dir) == [] and test_slots.HELD_ENV not in os.environ


def test_a_parallel_run_that_waits_past_the_limit_stops_with_a_usage_error(slot_dir, monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    def expired(*args, **kwargs):
        raise test_slots.SlotWaitExpired(14400, [("slot-0", 4242)])

    monkeypatch.setattr(test_slots, "acquire", expired)
    with pytest.raises(pytest.UsageError, match="held by pid 4242"):
        _xdist_auto.pytest_configure(ExplicitConfig(12))


def test_a_serial_run_takes_no_slot(slot_dir, monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = ExplicitConfig(0)
    _xdist_auto.pytest_configure(config)
    assert config.cleanups == [] and not slot_dir.exists()


def test_a_nested_parallel_pytest_inside_a_holding_run_does_not_acquire(slot_dir, tmp_path):
    # This process holds every slot, so a nested run that tried to acquire would wait until the
    # cap's timeout; one that sees its holding ancestor runs at once with its 2 workers.
    project = tmp_path / "proj"
    project.mkdir()
    (project / "conftest.py").write_text(
        "from _xdist_auto import pytest_configure  # noqa: F401\n", encoding="utf-8")
    (project / "test_a.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k != test_slots.HELD_ENV and not k.startswith("PYTEST_") and k != "PYTHONPATH"}
    env.update(PYTHONPATH=str(TESTS_DIR), CORE_TEST_SLOTS_DIR=str(slot_dir))
    whole = limits(slot_dir)
    with test_slots.acquire(4, limits=whole), test_slots.acquire(4, limits=whole):
        assert len(test_slots.held(slot_dir)) == 8
        proc = subprocess.run(
            ["bash", str(CAP_RUN), "--timeout", "60", "--",
             sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", str(project)],
            capture_output=True, text=True, env=env, cwd=project, timeout=90)
    assert proc.returncode == 0 and "2 passed" in proc.stdout, proc.stdout + proc.stderr
    assert "waiting" not in proc.stderr
