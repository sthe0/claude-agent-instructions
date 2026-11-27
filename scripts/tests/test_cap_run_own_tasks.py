"""cap-run.sh --own-tasks: the run's own process tree is the cap, still bounded, and a
cap-caused thread failure is UNAVAILABLE naming the cap.

The two process tests run small budgets (64) so a red or green run takes seconds, and
both the spawner and the runaway probe are self-limited and killed at the end of each
test. The classification tests are pure: synthetic SuiteRuns through the gate's own
classify/derive_status.
"""
import fcntl
import os
import pathlib
import resource
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from lib import suite_gate as gate

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
CAP_RUN = SCRIPTS_DIR / "cap-run.sh"
BUDGET = 64
SLACK = 128  # cap_own_tasks.SLACK: the documented overshoot
SELF_LIMIT = 2000  # the runaway template gives up on its own here


@contextmanager
def _process_tests_lock():
    """Serialize the process tests against each other, also under xdist: a test's
    spawner or count measurement running while another caps a tree would make the
    other's bound depend on this suite's own timing, not on the cap."""
    lock = pathlib.Path(tempfile.gettempdir()) / "cgmi-cap-run-own-tasks.lock"
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

WAIT_GO = """\
import os, sys, time
go = sys.argv[1]
deadline = time.time() + 300
while not os.path.exists(go):
    if time.time() > deadline:
        sys.exit(3)
    time.sleep(0.05)
os.execv(sys.argv[2], sys.argv[2:])
"""

SPAWNER = """\
import sys, threading, time
want, out = int(sys.argv[1]), sys.argv[2]
def hold():
    time.sleep(3600)
for _ in range(want):
    threading.Thread(target=hold, daemon=True).start()
while True:
    with open(out, "w") as fh:
        fh.write(str(threading.active_count()))
    time.sleep(0.2)
"""

# Threads that all start: passes only while the run keeps its own headroom.
START_THREADS = """\
import threading
def test_threads_start():
    started = [threading.Thread(target=lambda: None) for _ in range(40)]
    for t in started:
        t.start()
    for t in started:
        t.join()
"""

# A runaway that appends its live count every 16 threads, self-limited well past the
# bound, so the peak survives whatever stops it.
RUNAWAY = """\
import os, sys, threading, time
counts = sys.argv[1]
threading.stack_size(64 * 1024)
def hold():
    time.sleep(3600)
n = 0
def append():
    with open(counts, "a") as fh:
        fh.write(f"{n}\\n")
try:
    while n < 2000:
        threading.Thread(target=hold, daemon=True).start()
        n += 1
        if n % 16 == 0:
            append()
    append()
except RuntimeError:
    append()
    sys.exit(0)
"""


def _user_tasks():
    out = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "nlwp="],
                          capture_output=True, text=True, check=True).stdout
    return sum(int(line) for line in out.split())


def test_other_tasks_growth_keeps_headroom(tmp_path):
    """Other same-user tasks appearing AFTER the cap was computed must not stop the
    run's own threads: the own-tree cap keeps the run's headroom while others grow."""
    with _process_tests_lock():
        go = tmp_path / "go"
        waiter = tmp_path / "wait_go.py"
        waiter.write_text(WAIT_GO, encoding="utf-8")
        test = tmp_path / "test_start.py"
        test.write_text(START_THREADS, encoding="utf-8")
        spawner = tmp_path / "spawner.py"
        spawner.write_text(SPAWNER, encoding="utf-8")
        hb = tmp_path / "spawner.txt"
        argv = [str(CAP_RUN), "--extra-tasks", "4096", "--own-tasks", str(BUDGET),
                "--timeout", "180", "--",
                sys.executable, str(waiter), str(go),
                sys.executable, "-m", "pytest", str(test), "-q", "-p", "no:cacheprovider"]
        capped = subprocess.Popen(argv, cwd=str(tmp_path), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        proc = None
        try:
            # The capped run is up and waiting: its cap was computed before any load.
            load = 2 * BUDGET
            proc = subprocess.Popen([sys.executable, str(spawner), str(load), str(hb)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            alive, deadline = 0, time.time() + 60
            while alive < load and time.time() < deadline:
                try:
                    alive = int(hb.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    alive = 0
                if alive < load:
                    time.sleep(0.2)
            assert alive >= load, f"spawner never held {load} threads ({alive})"
            time.sleep(1.0)  # two supervisor periods: the cap has seen the load
            go.write_text("go", encoding="utf-8")
            out, err = capped.communicate(timeout=180)
            assert capped.returncode == 0, f"pytest failed under load: rc={capped.returncode}\n{out}\n{err}"
            assert "can't start new thread" not in out + err
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()
            if capped.poll() is None:
                capped.kill()


def test_runaway_stopped_within_overshoot(tmp_path):
    """A runaway inside the own-tree cap is stopped at at most budget + SLACK tasks, by
    the kernel at fork time or by a whole-tree kill — never past the overshoot."""
    with _process_tests_lock():
        runaway = tmp_path / "runaway.py"
        runaway.write_text(RUNAWAY, encoding="utf-8")
        counts = tmp_path / "counts"
        argv = [str(CAP_RUN), "--extra-tasks", "4096", "--own-tasks", str(BUDGET),
                "--timeout", "180", "--", sys.executable, str(runaway), str(counts)]
        done = subprocess.run(argv, cwd=str(tmp_path), capture_output=True, text=True, timeout=240)
        values = [int(v) for v in counts.read_text(encoding="utf-8").splitlines()]
        peak = max(values) if values else 0
        assert peak > 0, "the runaway never reported a thread count"
        assert peak <= BUDGET + SLACK, (
            f"runaway ran to {peak} tasks, past budget+slack {BUDGET + SLACK}")
        stopped_by_kill = done.returncode in (-9, 137) and "own-tasks" in done.stderr
        stopped_by_refusal = done.returncode == 0 and peak < SELF_LIMIT
        assert stopped_by_kill or stopped_by_refusal, (
            f"the runaway was stopped some other way: rc={done.returncode}\n{done.stderr[-500:]}")


def test_default_contract_unchanged():
    """Without --own-tasks the contract is exactly what it was: the machine-wide cap is
    set to the user's task count plus the headroom and exported as CAP_RUN_NPROC, the
    child sees it as its own soft RLIMIT_NPROC, and the hard limit is not touched."""
    with _process_tests_lock():
        hard_before = resource.getrlimit(resource.RLIMIT_NPROC)[1]
        live = _user_tasks()
        probe = (
            "import os, resource; "
            "print(resource.getrlimit(resource.RLIMIT_NPROC)[0], os.environ['CAP_RUN_NPROC'])")
        done = subprocess.run(
            [str(CAP_RUN), "--extra-tasks", "512", "--",
             sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60, check=True)
        soft_seen, cap_run_nproc = (int(v) for v in done.stdout.split())
        assert soft_seen == cap_run_nproc, "CAP_RUN_NPROC and the child's soft limit disagree"
        assert live <= cap_run_nproc <= live + 512 + 64, (
            f"cap {cap_run_nproc} is not the live task count {live} plus the 512 headroom")
        assert resource.getrlimit(resource.RLIMIT_NPROC)[1] == hard_before, "hard limit moved"

        bad = subprocess.run(
            [str(CAP_RUN), "--own-tasks", "not-a-number", "--", "true"],
            capture_output=True, text=True, timeout=60)
        assert bad.returncode == 2, "an invalid --own-tasks value must be a usage error"


def test_cap_thread_errors_unavailable_naming_cap():
    """A run whose own threads the cap refused classifies UNAVAILABLE with a
    'process-cap' reason and no counted failures, whatever its exit code; without the
    markers the same run is still FAIL with counted ids."""
    log = "RuntimeError: can't start new thread\nuncaught exception:\n" \
          'what() -> "thread constructor failed: Resource temporarily unavailable"'
    for exit_code in (1, -6):
        run = gate.SuiteRun(pytest_exit=exit_code,
                            failures=("tests/test_x.py::test_x",),
                            sandbox_core_sha="sha", log_tail=log)
        ids, reason = gate.classify(run, "sha")
        assert reason.startswith("process-cap"), reason
        assert ids == (), "a cap-caused thread error must not count failed tests"
        assert gate.derive_status(ids, reason) == gate.UNAVAILABLE
        observed = gate.observe(run, "sha")
        assert observed.reason.startswith("process-cap")
        assert not observed.failed and not observed.unfinished

    clean = gate.SuiteRun(pytest_exit=1, failures=("tests/test_x.py::test_x",),
                         sandbox_core_sha="sha", log_tail="assert 0 == 1")
    ids, reason = gate.classify(clean, "sha")
    assert ids and not reason
    assert gate.derive_status(ids, reason) == gate.FAIL

    exit_zero = gate.SuiteRun(pytest_exit=0, log_tail=log, sandbox_core_sha="sha")
    assert gate.classify(exit_zero, "sha") == ((), "")
    assert gate.derive_status(*gate.classify(exit_zero, "sha")) == gate.PASS


def test_pytest_argv_emits_own_tasks_headroom(tmp_path):
    """pytest_argv keeps its call shape and spends extra_tasks twice: as the machine-wide
    headroom and as the run's own-tree budget; without extra_tasks neither flag appears."""
    argv = gate.pytest_argv(tmp_path, [], 100, 512)
    assert argv[argv.index("--extra-tasks") + 1] == "512"
    assert argv[argv.index("--own-tasks") + 1] == "512"
    plain = gate.pytest_argv(tmp_path, [], 100)
    assert not any(a in ("--extra-tasks", "--own-tasks") for a in plain)
    assert plain == [argv[0], "--timeout", argv[argv.index("--timeout") + 1],
                     "--"] + argv[argv.index("--") + 1:]
