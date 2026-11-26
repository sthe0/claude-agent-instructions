#!/usr/bin/env python3
"""Benchmark what recording costs: hook emission and the command-path excess, in CPU time.

  telemetry-bench.py [--hook-runs N] [--pairs R] [--hook-bound-ms 20] [--command-bound-ms 250]

The clock is CPU time, not wall-clock time: on a loaded machine the wall-clock time of a fresh
interpreter measures the queue for a CPU, not the code (on 2026-11-21/22 the wall-clock hook p95
read 43-97 ms at load 11-16 on 12 CPUs while the CPU time of the same code stayed about 15 ms).
Each wall-clock p95 is printed beside its CPU figure and judges nothing; the output carries
"clock": "cpu". The price: CPU time does not count blocking waits (the spool flock, the store's
busy_timeout, fsync), so a regression that makes the hook wait passes the CPU bound. Waits are
not judged here; read them in hook_wall_p95_ms, and judge lock contention with
`telemetry.py check-latency --lock-contention-from-landing/--lock-contention-until-landing`.

Hook emission: N fresh interpreters each import the emitter and emit one event; the p95 of that
child's own CPU time (time.process_time around the import and the emit) must stay within
--hook-bound-ms. Commands: for each command of the synthetic sequence, R paired runs, one with
recording on and one with AGENTCTL_TELEMETRY=off, each timed by the user+system CPU time of the
child (resource.getrusage(RUSAGE_CHILDREN) before and after); the p95 (nearest rank) of the
per-pair excess must stay within --command-bound-ms. R below 20 is refused: with fewer pairs the
nearest-rank p95 is the maximum, not a percentile.

Exit 0 when every bound holds, 1 when one is exceeded, 2 on a refused argument.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.telemetry import report  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
MIN_PAIRS = 20
OFF_ENV = "AGENTCTL_TELEMETRY"
CLOCK = "cpu"
EMIT_SNIPPET = ("import sys, time; sys.path.insert(0, %r); c = time.process_time(); t = time.perf_counter(); "
                "from lib.telemetry import live; "
                "live.emit_event('telemetry:bench', 'decision', 'telemetry:recorder', {'bench': True}); "
                "print((time.process_time() - c) * 1000, (time.perf_counter() - t) * 1000)")
SEQUENCE = (("status",), ("classify", "--title", "bench"))


def p95(values: list) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def _children_cpu_ms() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (usage.ru_utime + usage.ru_stime) * 1000


def _timed(command: list, env: dict) -> "tuple[float, float]":
    """(CPU ms, wall ms) of one child run: its user+system time and its elapsed time."""
    cpu, started = _children_cpu_ms(), time.perf_counter()
    subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return _children_cpu_ms() - cpu, (time.perf_counter() - started) * 1000


def hook_run(env: dict) -> "tuple[float, float]":
    """(CPU ms, wall ms) of the emission in a fresh interpreter (the import of the emitter
    included, the interpreter's start-up not)."""
    out = subprocess.run([sys.executable, "-c", EMIT_SNIPPET % str(SCRIPTS)], env=env, capture_output=True,
                         text=True, check=False).stdout.strip().splitlines()
    try:
        cpu, wall = (float(v) for v in out[-1].split())
    except (IndexError, ValueError):
        return math.inf, math.inf
    return cpu, wall


def command_run(verb: tuple, env: dict, state_root: str) -> "tuple[float, float]":
    return _timed([sys.executable, str(SCRIPTS / "agentctl-cli.py"), "--state-root", state_root, *verb], env)


def bench(hook_runs: int, pairs: int, hook_bound_ms: float, command_bound_ms: float) -> dict:
    if pairs < MIN_PAIRS:
        raise ValueError(f"--pairs {pairs} is below {MIN_PAIRS}")
    env = dict(os.environ)
    off = {**env, OFF_ENV: "off"}
    hooks = [hook_run(env) for _ in range(hook_runs)]
    hook = p95([cpu for cpu, _ in hooks])
    hook_wall = p95([wall for _, wall in hooks])
    commands, commands_wall = {}, {}
    with tempfile.TemporaryDirectory(prefix="telemetry-bench-") as state_root:
        for verb in SEQUENCE:
            runs = [(command_run(verb, env, state_root), command_run(verb, off, state_root)) for _ in range(pairs)]
            commands[" ".join(verb)] = p95([on[0] - off[0] for on, off in runs])
            commands_wall[" ".join(verb)] = p95([on[1] - off[1] for on, off in runs])
    failures = ([f"hook emission p95 {hook:.1f} ms > {hook_bound_ms} ms"] if hook > hook_bound_ms else [])
    failures += [f"{name} excess p95 {ms:.1f} ms > {command_bound_ms} ms"
                 for name, ms in commands.items() if ms > command_bound_ms]
    return {"ok": not failures, "clock": CLOCK, "hook_p95_ms": hook, "hook_wall_p95_ms": hook_wall,
            "command_excess_p95_ms": commands, "command_excess_wall_p95_ms": commands_wall, "pairs": pairs,
            "hook_runs": hook_runs, "failures": failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hook-runs", type=int, default=1000)
    parser.add_argument("--pairs", type=int, default=MIN_PAIRS)
    parser.add_argument("--hook-bound-ms", type=float, default=20.0,
                        help="bound on the hook emission's CPU-time p95 (default 20)")
    parser.add_argument("--command-bound-ms", type=float, default=report.RECORDER_BOUND_MS,
                        help="bound on each command's CPU-time excess p95")
    args = parser.parse_args(argv)
    report.emit_usage("bench")
    try:
        result = bench(args.hook_runs, args.pairs, args.hook_bound_ms, args.command_bound_ms)
    except ValueError as error:
        print(f"telemetry-bench.py: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
