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
        # One probe reads the live count and the cap at the same moment, so the window
        # between cap-run's own count and the check is milliseconds, not a second ps run.
        probe = (
            "import os, resource, subprocess; "
            "live = sum(int(x) for x in subprocess.run("
            "['ps', '-u', str(os.getuid()), '-o', 'nlwp='], capture_output=True, text=True"
            ").stdout.split()); "
            "print(resource.getrlimit(resource.RLIMIT_NPROC)[0], os.environ['CAP_RUN_NPROC'], live)")
        done = subprocess.run(
            [str(CAP_RUN), "--extra-tasks", "512", "--",
             sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60, check=True)
        soft_seen, cap_run_nproc, live = (int(v) for v in done.stdout.split())
        assert soft_seen == cap_run_nproc, "CAP_RUN_NPROC and the child's soft limit disagree"
        # The 64-task slack is other workers' churn in the milliseconds between
        # cap-run's count and the probe's own.
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


def test_own_tasks_mode_exports_its_own_cap_marker():
    """--own-tasks mode exports CAP_RUN_OWN_TASKS so a consumer keying on "my soft limit
    equals CAP_RUN_NPROC" (the count-mode contract) still knows it runs capped once the
    supervisor has moved the soft limit past that snapshot."""
    probe = ("import os, resource; print(os.environ.get('CAP_RUN_OWN_TASKS', ''), "
             "os.environ.get('CAP_RUN_NPROC', ''), "
             "resource.getrlimit(resource.RLIMIT_NPROC)[0])")
    done = subprocess.run(
        [str(CAP_RUN), "--own-tasks", "512", "--timeout", "60", "--",
         sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60, check=True)
    own_var, nproc_var, soft = done.stdout.split()
    assert own_var == "512", "the own-tasks budget is not exported as CAP_RUN_OWN_TASKS"
    # The snapshot is the starting soft limit, but the supervisor re-sets the soft from
    # its first tick on, so by probe time machine churn may have moved it a little;
    # that movement is exactly why this mode exports its own marker.
    assert abs(int(nproc_var) - int(soft)) <= 64, (
        f"CAP_RUN_NPROC {nproc_var} is not around the soft limit {soft} the child sees")

    plain = subprocess.run(
        [str(CAP_RUN), "--extra-tasks", "512", "--",
         sys.executable, "-c", "import os; print(os.environ.get('CAP_RUN_OWN_TASKS', ''))"],
        capture_output=True, text=True, timeout=60, check=True)
    assert plain.stdout.strip() == "", "count mode must not export CAP_RUN_OWN_TASKS"


def test_nested_own_tasks_bound_is_not_raised_from_above(tmp_path):
    """An outer --own-tasks supervisor must not re-set the soft limit of a nested
    --own-tasks cap's subtree: between the outer's raise and the inner's next re-set,
    the inner bound would not be in force. The inner subtree's soft limit moves only
    as the inner cap moves it."""
    with _process_tests_lock():
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import resource, time\n"
            "def soft():\n"
            "    return resource.getrlimit(resource.RLIMIT_NPROC)[0]\n"
            "before = soft()\n"
            "time.sleep(1.5)  # three outer supervisor ticks\n"
            "print(before, soft())\n", encoding="utf-8")
        argv = [str(CAP_RUN), "--own-tasks", "4096", "--timeout", "120", "--",
                str(CAP_RUN), "--own-tasks", str(BUDGET), "--timeout", "120", "--",
                sys.executable, str(probe)]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        assert done.returncode == 0, f"nested cap-run failed: rc={done.returncode}\n{done.stderr[-500:]}"
        before, after = (int(v) for v in done.stdout.split())
        # The inner supervisor may itself re-tighten or track machine churn; what must
        # never happen is a jump toward the outer's bound (others + 4096 + 128, some
        # 4096 above the inner's). The allowance is churn, not the outer's budget.
        assert after <= before + 256, (
            f"the outer raised the nested bound from {before} to {after}")


def test_delegated_pids_covers_the_nested_supervisors_subtree(monkeypatch):
    """delegated_pids finds the whole subtree of every nested own-tasks supervisor in
    the tree: those pids' bounds belong to the inner supervisor, not this one."""
    from lib import cap_own_tasks
    procs = {100: (1, 1), 200: (100, 1), 300: (200, 1), 400: (100, 1)}
    monkeypatch.setattr(cap_own_tasks, "_is_own_tasks_supervisor", lambda pid: pid == 200)
    tree = cap_own_tasks.tree_pids(100, procs)
    assert cap_own_tasks.delegated_pids(tree, procs) == {200, 300}


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
