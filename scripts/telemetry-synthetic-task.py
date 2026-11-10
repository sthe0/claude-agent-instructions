#!/usr/bin/env python3
"""Run synthetic tasks so the live record has tasks to compare and benchmark against.

  telemetry-synthetic-task.py --count N [--trace]

Each task is one headless child (`claude -p --model haiku --max-budget-usd 0.50`) with
AGENTCTL_TELEMETRY_SYNTHETIC=1 in its environment, so the engine's start handler opens its task
row with synthetic 1 and the report leaves it out by default. After each child the spools are
replayed into the record.

--trace runs each child under `strace -f -e trace=execve,openat` (or, without strace, a Python
audit hook loaded through sitecustomize) and classifies every traced process as hook path (a
hook-*.py process, the harness running a hook) or command path (anything else the child ran),
printing the per-class counts.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.telemetry import live, report, sync  # noqa: E402

MAX_BUDGET_USD = "0.50"
MODEL = "haiku"
HOOK_PATH, COMMAND_PATH = "hook", "command"
PROMPT = ("Run `python3 ~/claude-agent-instructions/scripts/agentctl-cli.py classify --title synthetic` "
          "and then `agentctl-cli.py status`, and stop.")
AUDIT_HOOK = ("import os, sys\n"
              "def _audit(event, args):\n"
              "    if event in ('os.exec', 'subprocess.Popen', 'os.posix_spawn'):\n"
              "        with open(os.environ['TELEMETRY_TRACE_LOG'], 'a') as log:\n"
              "            log.write(f\"{os.getpid()} execve {args[0]!r} {list(args[1]) if len(args) > 1 else []!r}\\n\")\n"
              "sys.addaudithook(_audit)\n")


def child_env(base: "dict | None" = None) -> dict:
    env = dict(os.environ if base is None else base)
    env[live.SYNTHETIC_ENV] = "1"
    return env


def run_task(prompt: str, env: dict, trace_log: "Path | None" = None) -> int:
    """One headless child; the seam tests replace."""
    command = ["claude", "-p", "--model", MODEL, "--max-budget-usd", MAX_BUDGET_USD, prompt]
    if trace_log is not None:
        if shutil.which("strace"):
            command = ["strace", "-f", "-e", "trace=execve,openat", "-o", str(trace_log), *command]
        else:
            site = Path(tempfile.mkdtemp(prefix="telemetry-trace-"))
            (site / "sitecustomize.py").write_text(AUDIT_HOOK, encoding="utf-8")
            env = {**env, "PYTHONPATH": os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH")])),
                   "TELEMETRY_TRACE_LOG": str(trace_log)}
    return subprocess.run(command, env=env, check=False).returncode


def classify_process(argv: list) -> str:
    """Hook path when any argument names a hook-*.py script, command path otherwise."""
    return HOOK_PATH if any(PurePosixPath(str(a)).name.startswith("hook-") and str(a).endswith(".py")
                            for a in argv) else COMMAND_PATH


def traced_processes(trace_log: Path) -> dict:
    """pid -> argv of the last execve each traced process made (strace or audit-hook lines)."""
    processes = {}
    for line in trace_log.read_text(encoding="utf-8", errors="replace").splitlines():
        pid, _, rest = line.partition(" ")
        if "execve(" in rest:
            inside = rest[rest.find("[") + 1:rest.find("]")]
            processes[pid] = [part.strip().strip('"') for part in inside.split(",")]
        elif rest.startswith("execve "):
            argv = rest.split(" ", 2)[2] if rest.count(" ") >= 2 else "[]"
            processes[pid] = [part.strip().strip("'\"") for part in argv.strip("[]").split(",")]
    return processes


def trace_summary(trace_log: Path) -> dict:
    counts = {HOOK_PATH: 0, COMMAND_PATH: 0}
    for argv in traced_processes(trace_log).values():
        counts[classify_process(argv)] += 1
    return counts


def run(count: int, trace: bool = False) -> list:
    """Run `count` synthetic tasks; one {returncode, trace} dict per task."""
    results = []
    for _ in range(count):
        trace_log = Path(tempfile.mkstemp(prefix="telemetry-trace-", suffix=".log")[1]) if trace else None
        code = run_task(PROMPT, child_env(), trace_log)
        sync.run()
        results.append({"returncode": code, "trace": trace_summary(trace_log) if trace_log else None})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    report.emit_usage("synthetic-task")
    results = run(args.count, args.trace)
    print(json.dumps(results, indent=2))
    return 0 if all(r["returncode"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
