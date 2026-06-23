#!/usr/bin/env python3
"""Drive the coordination cycle (Need -> Plan -> Approval -> Implement) in code.

This is the CLAUDE.md § Coordination cycle expressed as two subcommands that
shell out to the existing building blocks (`spawn-specialist.py`,
`verify-plan-file.py`) rather than reimplementing spawning or plan validation.

The two-phase split is deliberate: the PLAN-READY approval gate must be a *real
human gate*. There is no TTY inside Claude's `!` shell and approval must never
be inferred, so `plan` and `run` are separate invocations and `run` refuses
without an explicit `--approved` flag.

  plan <task-desc> --done-criterion C --criterion-type {measurable,acceptance-review}
        Spawn the planner from a seed brief, locate + verify the produced plan
        file, and (on PLAN-READY + verify OK) print the exact `run ... --approved`
        command to execute after a human reviews the plan.

  run <plan-path> --approved --done-criterion C --criterion-type ...
        Refuse without --approved. Otherwise spawn the developer against the
        approved plan, parse its return marker, and exit with a code reflecting
        the marker (0 only on COMPLETED).

`--dry-run` on either subcommand delegates to `spawn-specialist.py --dry-run`:
it prints the assembled prompt + the command that would run, and executes
nothing.

Exit codes (run): 0 COMPLETED, 1 INCOMPLETE, 2 CLARIFY, 3 REPLAN,
4 PERMISSION-REQUEST, 5 ESCALATE, 6 MALFORMED/unknown. The `plan` subcommand
exits 0 on PLAN-READY + verified plan, non-zero otherwise.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPAWN = REPO_ROOT / "scripts" / "spawn-specialist.py"
VERIFY_PLAN = REPO_ROOT / "scripts" / "verify-plan-file.py"

RETURN_MARKERS = (
    "COMPLETED",
    "PLAN-READY",
    "INCOMPLETE",
    "CLARIFY",
    "REPLAN",
    "PERMISSION-REQUEST",
    "ESCALATE",
)
MARKER_RE = re.compile(rf"^({'|'.join(RETURN_MARKERS)}):")
PLAN_PATH_RE = re.compile(r"^\s*Plan\s*:\s*(.+?)\s*$", re.MULTILINE)

# Developer return marker -> coordinate-task exit code. 0 only on COMPLETED.
RUN_EXIT_CODE = {
    "COMPLETED": 0,
    "INCOMPLETE": 1,
    "CLARIFY": 2,
    "REPLAN": 3,
    "PERMISSION-REQUEST": 4,
    "ESCALATE": 5,
}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50] or "task").strip("-") or "task"


def first_marker(text: str) -> str | None:
    """Return the marker keyword on the first non-empty line, or None."""
    for line in text.splitlines():
        if line.strip():
            m = MARKER_RE.match(line.strip())
            return m.group(1) if m else None
    return None


def spawn_argv(
    kind: str,
    plan_path: Path,
    done_criterion: str,
    criterion_type: str,
    budget: str,
    complexity: str | None,
    context_dossier: Path | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        str(SPAWN),
        "--kind",
        kind,
        "--plan",
        str(plan_path),
        "--done-criterion",
        done_criterion,
        "--criterion-type",
        criterion_type,
        "--budget",
        budget,
    ]
    if complexity:
        argv += ["--complexity", complexity]
    if context_dossier:
        argv += ["--context-dossier", str(context_dossier)]
    return argv


def run_verify_plan(plan_path: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(VERIFY_PLAN), plan_path],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def cmd_plan(args: argparse.Namespace) -> int:
    slug = slugify(args.task)
    seed_fd, seed_name = tempfile.mkstemp(
        prefix=f"cc-coordinate-seed-{slug}-", suffix=".md"
    )
    seed = Path(seed_name)
    seed.write_text(
        f"# Planning brief\n\n{args.task}\n\n"
        f"Produce a full plan per planner SKILL.md § Plan format, write it to "
        f"`~/.claude/plans/{slug}.md`, and return PLAN-READY: with a `Plan: <path>` line.\n",
        encoding="utf-8",
    )

    argv = spawn_argv(
        "planner",
        seed,
        args.done_criterion,
        args.criterion_type,
        args.budget,
        args.complexity,
        args.context_dossier,
    )

    if args.dry_run:
        print("# coordinate-task plan --dry-run: delegating to spawn-specialist --dry-run\n")
        return subprocess.run(argv + ["--dry-run"], check=False).returncode

    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)

    marker = first_marker(proc.stdout)
    if marker != "PLAN-READY":
        print(
            f"\ncoordinate-task: planner did not return PLAN-READY "
            f"(got {marker or 'no marker'}). Not approved — fix the plan and re-run.",
            file=sys.stderr,
        )
        return 1

    m = PLAN_PATH_RE.search(proc.stdout)
    if not m:
        print(
            "\ncoordinate-task: PLAN-READY but no `Plan: <path>` line found in planner output.",
            file=sys.stderr,
        )
        return 1
    plan_path = m.group(1).strip().strip("`'\"")

    ok, verify_out = run_verify_plan(plan_path)
    print(f"\n{verify_out}")
    if not ok:
        print(
            "\ncoordinate-task: plan file failed verify-plan-file.py — not approvable.",
            file=sys.stderr,
        )
        return 1

    self_path = Path(__file__).resolve()
    print(
        "\n=== PLAN READY (human approval gate) ===\n"
        f"Plan written and verified: {plan_path}\n\n"
        "Review the plan. Approval is NOT inferred — once you approve, run:\n\n"
        f"  python3 {self_path} run {plan_path} --approved \\\n"
        f"      --done-criterion {args.done_criterion!r} --criterion-type {args.criterion_type}\n"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.approved:
        print(
            "coordinate-task: refusing to implement — the PLAN-READY approval gate "
            "is not satisfied.\n"
            "Approval must be explicit and human; it is never inferred.\n"
            f"  Re-run with --approved once the plan at {args.plan} is approved:\n"
            f"    coordinate-task.py run {args.plan} --approved --done-criterion ... --criterion-type ...",
            file=sys.stderr,
        )
        return 2

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"coordinate-task: plan file not found: {plan_path}", file=sys.stderr)
        return 2

    argv = spawn_argv(
        "developer",
        plan_path,
        args.done_criterion,
        args.criterion_type,
        args.budget,
        args.complexity,
        args.context_dossier,
    )

    if args.dry_run:
        print("# coordinate-task run --dry-run: delegating to spawn-specialist --dry-run\n")
        return subprocess.run(argv + ["--dry-run"], check=False).returncode

    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)

    marker = first_marker(proc.stdout)
    print(
        f"\n=== developer outcome: {marker or 'NO MARKER (malformed)'} ===",
        file=sys.stderr,
    )
    return RUN_EXIT_CODE.get(marker, 6)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="subcommand", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--done-criterion", required=True, help="concrete done criterion for the step")
        sp.add_argument(
            "--criterion-type",
            choices=("measurable", "acceptance-review"),
            required=True,
            help="how the criterion will be verified",
        )
        sp.add_argument("--budget", choices=("small", "medium", "large"), default="medium", help="budget tier (config.md)")
        sp.add_argument("--complexity", choices=("low", "medium", "high"), help="task difficulty -> sub-agent model")
        sp.add_argument("--context-dossier", type=Path, help="path to a context-digest file passed through to spawn-specialist")
        sp.add_argument("--dry-run", action="store_true", help="delegate to spawn-specialist --dry-run; print commands, execute nothing")

    pp = sub.add_parser("plan", help="spawn the planner from a task brief, verify the produced plan, print the approval-gated run command")
    pp.add_argument("task", help="task description / brief the planner plans from")
    add_common(pp)
    pp.set_defaults(func=cmd_plan)

    pr = sub.add_parser("run", help="spawn the developer against an APPROVED plan (refuses without --approved)")
    pr.add_argument("plan", help="path to the approved plan markdown file")
    pr.add_argument("--approved", action="store_true", help="explicit human approval of the plan (required; never inferred)")
    add_common(pr)
    pr.set_defaults(func=cmd_run)

    return p


DEPRECATION_NOTICE = (
    "coordinate-task.py is a thin two-phase (plan/run) wrapper; the deterministic "
    "coordination engine now lives in `python3 -m agentctl` (full classify -> route "
    "-> plan-approval gate -> per-stage dispatch -> verify -> resolution state "
    "machine). This shim is kept until the prose cutover wires agentctl in.\n"
)


def main(argv: list[str] | None = None) -> int:
    sys.stderr.write(DEPRECATION_NOTICE)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
