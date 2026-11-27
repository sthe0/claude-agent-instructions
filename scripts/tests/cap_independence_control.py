#!/usr/bin/env python3
"""R2 control: the suite gate's process cap is decided on the run's own process tree.

Exit 0: every leg passed. Exit 1: a leg failed (red). Exit 3: this machine cannot run
the control (an environment verdict, never a pass).

Legs, with budget B and overshoot O = 128 (the slack cap-run.sh --own-tasks documents;
s2-verify.sh holds the result to these numbers, not this control):

  independence - a capped pytest run is released (a go-file) only after a bounded
      same-user spawner holds 2*B threads and two supervisor periods (2 x 0.5 s) passed,
      so the cap was computed before the load appeared; the run must still finish clean.
  bound - a self-limited thread runaway (64 KiB stacks, 4000-thread self-limit,
      appending its live count every 16 threads) runs inside the capped pytest run and
      must be stopped at at most B + O tasks, by the kernel at fork time or by a
      whole-tree kill.
  bound_sanity - the same probe with no own-tree cap (only the outer cap-run) must
      pass B + O, or the bound leg could not have gone red (exit 3, never a pass).
  verdict - a synthetic test that hits the cap (starting 2*B threads) inside the capped
      pytest run classifies through this repo's own gate classification as UNAVAILABLE
      with a 'process-cap' reason and no counted failures.

Every capped run imports the repo's own suite_gate.pytest_argv with extra_tasks = B —
the same call shape on old and new code — and so uses the repo's own cap-run.sh: old code
caps the machine-wide count at start (the spawner then eats the headroom), new code
bounds the run's own tree. Old code is expected to fail the independence leg; that red
is the point of the control.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

OVERSHOOT = 128        # cap-run.sh --own-tasks documents this slack over the budget
PERIOD_S = 0.5         # the --own-tasks supervisor's tick, allowed to by the norm
# OVERSHOOT and PERIOD_S mirror cap_own_tasks.SLACK and PERIOD_S; the templates below
# mirror test_cap_run_own_tasks.py's. Drift in either changes the legs' numbers: keep
# them in step when the supervisor's contract changes.
PYTEST_TIMEOUT_S = 120
SPAWN_TIMEOUT_S = 120
PROBE_TARGET = 4000
MARKER = "can't start new thread"  # the thread-creation failure the cap causes

WAIT_GO = """\
import os, sys, time
go = sys.argv[1]
deadline = time.time() + 600
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

# Standalone runaway (the bound_sanity leg): thread stack 64 KiB, self-limited at
# PROBE_TARGET, appending its live count every 16 threads so the peak survives a kill.
PROBE = f"""\
import sys, threading, time
counts_path = sys.argv[1]
threading.stack_size(64 * 1024)
def hold():
    time.sleep(3600)
n = 0
def append():
    with open(counts_path, "a") as fh:
        fh.write(f"{{n}}\\n")
try:
    while n < {PROBE_TARGET}:
        threading.Thread(target=hold, daemon=True).start()
        n += 1
        if n % 16 == 0:
            append()
    append()
except RuntimeError:
    append()
    sys.exit(0)
"""

# The bound leg: the same runaway inside a pytest test, so it runs inside the capped
# argv; the cap's refusal re-raises, which fails the test and the run.
BOUND_TEST = f"""\
import os, threading, time
def test_runaway():
    counts = os.environ["R2_PROBE_COUNTS"]
    threading.stack_size(64 * 1024)
    def hold():
        time.sleep(3600)
    n = 0
    def append():
        with open(counts, "a") as fh:
            fh.write(f"{{n}}\\n")
    while n < {PROBE_TARGET}:
        try:
            threading.Thread(target=hold, daemon=True).start()
        except RuntimeError:
            append()
            raise
        n += 1
        if n % 16 == 0:
            append()
    append()
"""

# The independence leg: a few dozen threads must all start while the spawner holds 2*B.
INDEPENDENCE_TEST = """\
import threading
def test_threads_start():
    started = [threading.Thread(target=lambda: None) for _ in range(40)]
    for t in started:
        t.start()
    for t in started:
        t.join()
"""

# The verdict leg: a synthetic test that demands twice the budget in HELD threads (a
# thread that returns immediately never shows up in the tree's task count), so the cap
# itself stops it and the gate's classification must call that UNAVAILABLE, not FAIL.
VERDICT_TEST = """\
import os, threading
def test_hits_the_cap():
    want = int(os.environ["R2_VERDICT_THREADS"])
    release = threading.Event()
    started = []
    for _ in range(want):
        t = threading.Thread(target=release.wait, daemon=True)
        t.start()
        started.append(t)
    release.set()
    for t in started:
        t.join()
"""


def load_gate(repo: pathlib.Path):
    """The repo's own suite_gate module (scripts/lib/suite_gate.py)."""
    sys.path.insert(0, str(repo / "scripts"))
    from lib import suite_gate
    return suite_gate


def log_tail(text: str) -> str:
    return "\n".join(text.splitlines()[-400:])


class Leg:
    def __init__(self, repo: pathlib.Path, budget: int, tmp: pathlib.Path):
        self.repo, self.budget, self.tmp = repo, budget, tmp
        self.gate = load_gate(repo)
        self.env = self.capped_env()

    def capped_env(self) -> dict:
        """The environment of a capped run: the gate's own plugin materialized beside it.

        pytest_argv loads ``-p suite_gate_plugin``, which the real gate writes into its
        sandbox root at run time; it is not in the tree, so the control writes the repo's
        own PLUGIN_SOURCE into its own plugin dir and puts that on PYTHONPATH.
        """
        env = dict(os.environ)
        plugdir = self.tmp / "plugin"
        plugdir.mkdir(exist_ok=True)
        (plugdir / f"{self.gate.PLUGIN_MODULE}.py").write_text(self.gate.PLUGIN_SOURCE,
                                                                encoding="utf-8")
        env["PYTHONPATH"] = str(plugdir)
        env["SUITE_GATE_FAILURES"] = str(self.tmp / "failures.txt")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def root(self) -> pathlib.Path:
        root = self.tmp / f"root-{time.monotonic_ns()}"
        (root / "core").mkdir(parents=True)
        return root

    def capped_run(self, select, *, wrapper=(), timeout_s=PYTEST_TIMEOUT_S):
        argv = self.gate.pytest_argv(
            self.root(), select, timeout_s, self.budget, wrapper)
        return subprocess.run(
            argv, cwd=str(self.tmp), env=self.env,
            capture_output=True, text=True, timeout=timeout_s + 240)

    def independence(self) -> dict:
        """The capped run starts BEFORE the load and waits on a go-file, so the cap is
        computed without it; only then does the spawner grow other same-user tasks, and
        the go-file is written once the spawner holds 2*B threads and two supervisor
        periods (2 x 0.5 s) have passed."""
        spawner = self.write("spawner.py", SPAWNER)
        hb = self.tmp / "spawner.txt"
        go = self.write("go", "")
        go.unlink()
        waiter = self.write("wait_go.py", WAIT_GO)
        test = self.write("independence_test.py", INDEPENDENCE_TEST)
        argv = self.gate.pytest_argv(
            self.root(), [str(test)], PYTEST_TIMEOUT_S, self.budget,
            [sys.executable, str(waiter), str(go)])
        capped = subprocess.Popen(argv, cwd=str(self.tmp), env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc = None
        try:
            proc = subprocess.Popen([sys.executable, str(spawner), str(2 * self.budget), str(hb)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            alive = 0
            deadline = time.time() + SPAWN_TIMEOUT_S
            while alive < 2 * self.budget and time.time() < deadline:
                try:
                    alive = int(hb.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    alive = 0
                if alive < 2 * self.budget:
                    time.sleep(0.2)
            time.sleep(2 * PERIOD_S)  # two supervisor periods: the cap sees the load
            go.write_text("go", encoding="utf-8")
            out, err = capped.communicate(timeout=PYTEST_TIMEOUT_S + 240)
            errors = (out + err).count(MARKER)
            return {"passed": capped.returncode == 0 and errors == 0 and alive >= 2 * self.budget,
                    "pytest_exit": capped.returncode, "thread_errors": errors,
                    "spawner_threads": alive,
                    "output_tail": log_tail(out + "\n" + err)[-2000:]}
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()
            if capped.poll() is None:
                capped.kill()

    def bound(self) -> dict:
        test = self.write("bound_test.py", BOUND_TEST)
        counts = self.tmp / f"bound-counts-{time.monotonic_ns()}"
        self.env["R2_PROBE_COUNTS"] = str(counts)
        try:
            counts.unlink()
        except OSError:
            pass
        done = self.capped_run([str(test)])
        peak = self.peak_of(counts)
        stopped = ("tree-kill" if done.returncode in (-9, 137) and "own-tasks" in done.stderr
                   else "kernel-refusal")
        return {"passed": peak is not None and peak <= self.budget + OVERSHOOT and peak < PROBE_TARGET,
                "peak_threads": peak, "stopped_by": stopped, "pytest_exit": done.returncode}

    def bound_sanity(self) -> dict:
        probe = self.write("probe.py", PROBE)
        counts = self.tmp / f"sanity-counts-{time.monotonic_ns()}"
        done = subprocess.run([sys.executable, str(probe), str(counts)],
                              capture_output=True, text=True, timeout=SPAWN_TIMEOUT_S + 240)
        return {"peak_threads": self.peak_of(counts), "pytest_exit": done.returncode}

    def verdict(self) -> dict:
        test = self.write("verdict_test.py", VERDICT_TEST)
        self.env["R2_VERDICT_THREADS"] = str(2 * self.budget)
        done = self.capped_run([str(test)])
        failures = self.read_failures(self.env["SUITE_GATE_FAILURES"])
        run = self.gate.SuiteRun(
            pytest_exit=done.returncode, failures=failures,
            sandbox_core_sha="r2-verdict",
            log_tail=log_tail(done.stdout + "\n" + done.stderr))
        ids, reason = self.gate.classify(run, "r2-verdict")
        status = self.gate.derive_status(ids, reason)
        return {"status": status, "reason": reason, "counted_failures": list(ids),
                "pytest_exit": done.returncode, "log_tail": run.log_tail[-2000:]}

    @staticmethod
    def read_failures(path) -> tuple:
        try:
            return tuple(sorted({line for line in pathlib.Path(path).read_text(
                encoding="utf-8").splitlines() if line}))
        except OSError:
            return ()

    @staticmethod
    def peak_of(counts: pathlib.Path) -> int | None:
        try:
            values = [int(v) for v in counts.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            return None
        return max(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-C", "--repo", default=".", help="the repo whose gate is under test")
    parser.add_argument("--budget", type=int, default=256, help="the run's own-task budget B")
    parser.add_argument("--out", required=True, help="path of the JSON result")
    args = parser.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    tmp = pathlib.Path(os.environ.get("TMPDIR") or "/tmp") / f"cgmi-r2-{time.monotonic_ns()}"
    tmp.mkdir(parents=True)
    result = {
        "repo_sha": subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip(),
        "budget": args.budget,
    }
    try:
        leg = Leg(repo, args.budget, tmp)
        result["independence"] = leg.independence()
        result["bound"] = leg.bound()
        result["bound_sanity"] = leg.bound_sanity()
        result["verdict"] = leg.verdict()

        sanity = result["bound_sanity"]["peak_threads"]
        if not (sanity and sanity > args.budget + OVERSHOOT):
            # The probe cannot outrun the bound, so the bound leg proves nothing here.
            print(f"bound_sanity peak {sanity} never passed budget+overshoot "
                  f"{args.budget + OVERSHOOT}: this machine cannot run the control", file=sys.stderr)
            return 3
        ok = all(result[name]["passed"] for name in ("independence", "bound"))
        ok = ok and result["verdict"]["status"] == "UNAVAILABLE" \
            and result["verdict"]["reason"].startswith("process-cap") \
            and not result["verdict"]["counted_failures"]
        result["passed"] = ok
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("repo_sha", "budget", "independence", "bound",
                                                "bound_sanity", "verdict")}, indent=2))
        return 0 if ok else 1
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
