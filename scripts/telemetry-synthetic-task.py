#!/usr/bin/env python3
"""Run synthetic tasks so the live record has tasks to compare and benchmark against.

  telemetry-synthetic-task.py --count N [--trace]

Each task is one headless child (`claude -p --model haiku --max-budget-usd 0.50`) that walks the
engine with its Bash tool through a one-stage in-thread plan: reset, classify, plan, submit-plan,
approve, partition, next-stage, dispatch, record-result, verify-final. The walk runs against a
fresh --state-root per task, under the child's own session id, with the review and approval gates
off as the engine's end-to-end fixtures run them. The child's environment carries
AGENTCTL_TELEMETRY_SYNTHETIC=1, so the reset opens its task row with synthetic 1 and the report
leaves it out by default. The live events land in the real record. After each child the spools are
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
import shlex
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
ENGINE_CLI = Path(__file__).resolve().parent / "agentctl-cli.py"
TASK_ID = "synthetic-task"
CHILD_SESSION = '"$CLAUDE_CODE_SESSION_ID"'
STAGE_PLAN = (
    '[meta]\n'
    'weight_class = "small_change"\n'
    f'task_id = "{TASK_ID}"\n'
    'goal = "Walk the engine through one in-thread stage"\n'
    'done_criterion = "the stage PASSED and verify-final reached RESOLUTION"\n'
    'criterion_type = "measurable"\n'
    '\n'
    '[[stage]]\n'
    'index = 1\n'
    'title = "Synthetic stage"\n'
    'executor = "in_thread"\n'
    'expected_result_image = "the engine records the stage as passed"\n'
    'criterion_type = "measurable"\n'
    'done_criterion = "record-result is accepted"\n'
)
GATES_OFF = {
    "AGENTCTL_PLAN_REVIEW": "0", "AGENTCTL_PLAN_PRESENTATION": "0", "AGENTCTL_PREMISE": "0",
    "AGENTCTL_CODE_REVIEW": "0", "AGENTCTL_STAGE_REVIEW": "0", "AGENTCTL_ACCEPTANCE": "0",
    "AGENTCTL_LANDING_GATE": "0", "AGENTCTL_WORKSPACE_CLEANUP": "0", "AGENTCTL_RESOLUTION_ASK_GATE": "0",
    "AGENTCTL_ADVISOR": "0", "AGENTCTL_TASK_TITLE": "0", "WORKTREE_ROUTE_DISABLE": "1", "CLAUDE_DIFFICULTY_CHANNEL_OFFLINE": "1",
}
AUDIT_HOOK = ("import os, sys\n"
              "def _audit(event, args):\n"
              "    if event in ('os.exec', 'subprocess.Popen', 'os.posix_spawn'):\n"
              "        with open(os.environ['TELEMETRY_TRACE_LOG'], 'a') as log:\n"
              "            log.write(f\"{os.getpid()} execve {args[0]!r} {list(args[1]) if len(args) > 1 else []!r}\\n\")\n"
              "sys.addaudithook(_audit)\n")


def child_env(base: "dict | None" = None) -> dict:
    env = dict(os.environ if base is None else base)
    env[live.SYNTHETIC_ENV] = "1"
    env.update(GATES_OFF)
    return env


def engine_sequence(state_root: Path, plan: Path) -> list:
    """The agentctl argv the child runs, one Bash call each: a one-stage in-thread plan walked from
    reset to verify-final against `state_root`, under the child's own session id."""
    session = ["--session", CHILD_SESSION]
    steps = [
        ["reset", *session, "--task", TASK_ID, "--goal", "synthetic engine walk",
         "--done-criterion", "verify-final reaches RESOLUTION", "--criterion-type", "measurable", "--force",
         "--host", "claude"],
        ["classify", *session, "--architectural"],
        ["plan", *session],
        ["submit-plan", *session, "--plan", str(plan)],
        ["approve", *session, "--by", "user"],
        ["partition", *session],
        ["next-stage", *session],
        ["dispatch", *session, "--budget", "small", "--complexity", "small"],
        ["record-result", *session, "--status", "passed", "--actual", "the engine accepted the stage",
         "--control", "reviewed: synthetic walk, nothing to review",
         "--observation", "the dispatch directive named stage 1 in-thread and record-result printed its next action"],
        ["verify-final", *session],
    ]
    return [["--state-root", str(state_root), *step] for step in steps]


def render_command(argv: list) -> str:
    """One shell line; the session id stays a shell reference the child's Bash expands. The host
    session id is unset for the engine so the order-agreement hold, armed only on a detected claude
    host, does not stop an unattended walk; reset binds the host with `--host claude` instead,
    since detection would otherwise depend on a Cursor CLI being installed."""
    words = [a if a == CHILD_SESSION else shlex.quote(a) for a in argv]
    return " ".join(["env", "-u", "CLAUDE_CODE_SESSION_ID", "python3", shlex.quote(str(ENGINE_CLI)), *words])


def task_prompt(state_root: Path, plan: Path) -> str:
    lines = [f"{n}. `{render_command(argv)}`" for n, argv in enumerate(engine_sequence(state_root, plan), 1)]
    return ("Run each of these commands with your Bash tool, one call per command, in order, exactly as "
            "written. Do not edit any file. After the last one, stop.\n\n" + "\n".join(lines))


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
        with tempfile.TemporaryDirectory(prefix="telemetry-synthetic-") as scratch:
            plan = Path(scratch) / "plan.toml"
            plan.write_text(STAGE_PLAN, encoding="utf-8")
            code = run_task(task_prompt(Path(scratch) / "state", plan), child_env(), trace_log)
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
