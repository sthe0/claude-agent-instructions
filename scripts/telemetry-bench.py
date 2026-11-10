#!/usr/bin/env python3
"""Benchmark what recording costs: hook emission and the command-path excess.

  telemetry-bench.py [--hook-runs N] [--pairs R] [--hook-bound-ms 10] [--command-bound-ms 250]

Hook emission: N fresh interpreters each import the emitter and emit one event; the p95 of that
time must stay within --hook-bound-ms. Commands: for each command of the synthetic sequence, R paired runs, one
with recording on and one with AGENTCTL_TELEMETRY=off; the p95 (nearest rank) of the per-pair
excess must stay within --command-bound-ms. R below 20 is refused: with fewer pairs the nearest-rank
p95 is the maximum, not a percentile.

Exit 0 when every bound holds, 1 when one is exceeded, 2 on a refused argument.
"""
from __future__ import annotations

import argparse
import json
import math
import os
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
EMIT_SNIPPET = ("import sys, time; sys.path.insert(0, %r); t = time.perf_counter(); "
                "from lib.telemetry import live; "
                "live.emit_event('telemetry:bench', 'decision', 'telemetry:recorder', {'bench': True}); "
                "print((time.perf_counter() - t) * 1000)")
SEQUENCE = (("status",), ("classify", "--title", "bench"))


def p95(values: list) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def _timed(command: list, env: dict) -> float:
    started = time.perf_counter()
    subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return (time.perf_counter() - started) * 1000


def hook_run(env: dict) -> float:
    """The emission's own ms in a fresh interpreter (the import of the emitter included, the
    interpreter's start-up not)."""
    out = subprocess.run([sys.executable, "-c", EMIT_SNIPPET % str(SCRIPTS)], env=env, capture_output=True,
                         text=True, check=False).stdout.strip().splitlines()
    return float(out[-1]) if out else math.inf


def command_run(verb: tuple, env: dict, state_root: str) -> float:
    return _timed([sys.executable, str(SCRIPTS / "agentctl-cli.py"), "--state-root", state_root, *verb], env)


def bench(hook_runs: int, pairs: int, hook_bound_ms: float, command_bound_ms: float) -> dict:
    if pairs < MIN_PAIRS:
        raise ValueError(f"--pairs {pairs} is below {MIN_PAIRS}")
    env = dict(os.environ)
    off = {**env, OFF_ENV: "off"}
    hook = p95([hook_run(env) for _ in range(hook_runs)])
    commands = {}
    with tempfile.TemporaryDirectory(prefix="telemetry-bench-") as state_root:
        for verb in SEQUENCE:
            excess = [command_run(verb, env, state_root) - command_run(verb, off, state_root) for _ in range(pairs)]
            commands[" ".join(verb)] = p95(excess)
    failures = ([f"hook emission p95 {hook:.1f} ms > {hook_bound_ms} ms"] if hook > hook_bound_ms else [])
    failures += [f"{name} excess p95 {ms:.1f} ms > {command_bound_ms} ms"
                 for name, ms in commands.items() if ms > command_bound_ms]
    return {"ok": not failures, "hook_p95_ms": hook, "command_excess_p95_ms": commands, "pairs": pairs,
            "hook_runs": hook_runs, "failures": failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hook-runs", type=int, default=1000)
    parser.add_argument("--pairs", type=int, default=MIN_PAIRS)
    parser.add_argument("--hook-bound-ms", type=float, default=10.0)
    parser.add_argument("--command-bound-ms", type=float, default=report.RECORDER_BOUND_MS)
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
