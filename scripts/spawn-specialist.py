#!/usr/bin/env python3
"""Wrap `claude -p` spawn of a specialization skill.

Replaces the hand-assembled shell template in CLAUDE.md § Spawning specialists.
The wrapper handles the *process* concerns around spawning so the manager only
has to provide the cognitive inputs (kind, plan, done criterion, etc.):

  1. Validate args; refuse to spawn an unknown specialization.
  2. Read config.md constants (max-recursion-depth, budget-*-usd).
  3. Enforce the hard recursion cap before spawning.
  4. Resolve the budget tier to a concrete --max-budget-usd value.
  5. Auto-embed the permissions digest into the prompt.
  6. Assemble the prompt exactly per CLAUDE.md template.
  7. Spawn `claude -p --output-format json`.
  8. Forward the specialist's text result to stdout; validate the first
     non-empty line carries one of the known return markers (else wrap in
     MALFORMED:).
  9. Append a JSONL row to ~/.local/log/claude-spawn-costs.jsonl with the
     run's kind / budget / depth / cost / duration / marker.

Use --dry-run to print the assembled prompt and command without spawning.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = Path.home() / ".claude" / "skills"
CONFIG_MD = REPO_ROOT / "config.md"
PERMISSIONS_CLI = REPO_ROOT / "scripts" / "permissions-cli.py"
COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"

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

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")


def parse_config_md() -> dict[str, str]:
    """Extract `key` -> `value` from the markdown table in config.md."""
    constants: dict[str, str] = {}
    for line in CONFIG_MD.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


def budget_value(tier: str, constants: dict[str, str]) -> str:
    key = f"budget-{tier}-usd"
    if key not in constants:
        raise SystemExit(f"error: {key} not defined in config.md")
    return constants[key]


def recursion_max(constants: dict[str, str]) -> int:
    key = "max-recursion-depth"
    if key not in constants:
        raise SystemExit(f"error: {key} not defined in config.md")
    return int(constants[key])


def read_text_or_file(text: str | None, file: Path | None) -> str:
    if file is not None:
        return file.read_text(encoding="utf-8").rstrip()
    return (text or "").rstrip()


def permissions_digest(project_file: Path | None) -> str:
    """Run permissions-cli.py digest for global + optional project file. Empty string if no grants."""
    chunks: list[str] = []
    cmd = [sys.executable, str(PERMISSIONS_CLI), "digest"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.stdout.strip():
        chunks.append(out.stdout.rstrip())
    if project_file is not None:
        out = subprocess.run(
            cmd + ["--file", str(project_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stdout.strip():
            chunks.append(out.stdout.rstrip())
    return "\n\n".join(chunks)


def assemble_prompt(args: argparse.Namespace, depth: int, permissions: str) -> str:
    plan = read_text_or_file(None, args.plan)
    constraints = read_text_or_file(args.constraints, None)
    dossier = read_text_or_file(None, args.context_dossier) if args.context_dossier else ""

    sections = [f"AGENT_RECURSION_DEPTH={depth}", "", "## Working plan", "", plan, ""]
    sections += [
        "## Done criterion for this step",
        "",
        f"{args.done_criterion}  *({args.criterion_type})*",
        "",
    ]
    if constraints:
        sections += ["## Constraints", "", constraints, ""]
    if dossier:
        sections += [
            "## Context dossier (what you may not infer from CLAUDE.md / repo / memory)",
            "",
            dossier,
            "",
        ]
    if permissions:
        sections += [
            "## Permissions previously granted (apply during your work)",
            "",
            permissions,
            "",
        ]
    sections += [
        "If your work needs an action not covered, return PERMISSION-REQUEST: with the request.",
        "If you hit a small specific question whose answer is needed to continue, return CLARIFY: (see § Return markers).",
    ]
    return "\n".join(sections)


def skill_path(kind: str) -> Path:
    return SKILLS_DIR / kind / "SKILL.md"


def validate_marker(result_text: str) -> tuple[str, bool]:
    """Return (text, ok). If marker is missing, prepend MALFORMED: and ok=False."""
    for line in result_text.splitlines():
        if line.strip():
            if MARKER_RE.match(line.strip()):
                return result_text, True
            break
    return f"MALFORMED: specialist output did not start with a known marker.\n\n{result_text}", False


def log_cost_entry(entry: dict) -> None:
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_refused(reason: str, extra: dict) -> None:
    """Log a spawn that was refused before reaching `claude -p` (recursion cap, unknown kind, etc.).
    Visible in cost-report.py as a separate category."""
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "refused",
        "reason": reason,
        **extra,
    }
    log_cost_entry(entry)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--kind", required=True, help="specialization name (must exist at ~/.claude/skills/<kind>/SKILL.md)")
    p.add_argument("--plan", type=Path, required=True, help="path to the markdown plan; mark the step the specialist owns with **<<this step>>**")
    p.add_argument("--done-criterion", required=True, help="concrete done criterion for the step")
    p.add_argument(
        "--criterion-type",
        choices=("measurable", "acceptance-review"),
        required=True,
        help="how the criterion will be verified",
    )
    p.add_argument("--constraints", default="", help="scope / do-not-touch / deadlines (inline)")
    p.add_argument("--context-dossier", type=Path, help="path to a file with the conversation-context digest")
    p.add_argument("--budget", choices=("small", "medium", "large"), default="medium", help="budget tier from config.md")
    p.add_argument("--project-permissions", type=Path, help="project-scope permissions.json to also include in the digest")
    p.add_argument(
        "--permission-mode",
        choices=("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"),
        help="claude --permission-mode for the spawned process. Default: bypassPermissions for kind=developer (trusted local writes), default otherwise.",
    )
    p.add_argument("--dry-run", action="store_true", help="print the prompt and the command that would run, then exit")
    return p


def resolve_permission_mode(args: argparse.Namespace) -> str | None:
    """Pick the permission mode passed to `claude -p`.

    Default policy: developer specialization needs unattended Read/Grep/Write in
    a trusted local mount, so use bypassPermissions; other specializations stay
    on harness defaults (interactive prompts) since they are mostly read-only.

    User-supplied `--permission-mode` always wins.
    """
    if args.permission_mode is not None:
        return args.permission_mode
    if args.kind == "developer":
        return "bypassPermissions"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.plan.exists():
        print(f"error: plan file not found: {args.plan}", file=sys.stderr)
        log_refused("plan-not-found", {"kind": args.kind, "plan": str(args.plan)})
        return 2
    skill = skill_path(args.kind)
    if not skill.exists():
        print(f"error: unknown specialization (no SKILL.md at {skill})", file=sys.stderr)
        log_refused("unknown-kind", {"kind": args.kind})
        return 2

    constants = parse_config_md()
    depth_now = int(os.environ.get("AGENT_RECURSION_DEPTH", "0"))
    depth_next = depth_now + 1
    cap = recursion_max(constants)
    if depth_next > cap:
        print(
            f"error: spawn would push AGENT_RECURSION_DEPTH to {depth_next}, "
            f"above max-recursion-depth={cap}. Stop and escalate to the user "
            f"per CLAUDE.md § Recursion cap (hard).",
            file=sys.stderr,
        )
        log_refused("recursion-cap", {"kind": args.kind, "depth_attempted": depth_next, "cap": cap})
        return 3

    budget = budget_value(args.budget, constants)
    perms = permissions_digest(args.project_permissions)
    prompt = assemble_prompt(args, depth_next, perms)

    cmd = [
        "claude",
        "-p",
        "--append-system-prompt-file",
        str(skill),
        "--max-budget-usd",
        budget,
        "--output-format",
        "json",
    ]
    permission_mode = resolve_permission_mode(args)
    if permission_mode is not None:
        cmd.extend(["--permission-mode", permission_mode])
    cmd.append(prompt)

    if args.dry_run:
        print("=== assembled prompt ===")
        print(prompt)
        print("\n=== command (not executed) ===")
        # Print command in a copy-pasteable form, but truncate the prompt arg.
        printable = cmd[:-1] + [f"<prompt {len(prompt)} chars>"]
        print(" ".join(repr(c) if " " in c else c for c in printable))
        return 0

    if shutil.which("claude") is None:
        print("error: `claude` not on PATH; cannot spawn. Re-run with --dry-run to inspect the prompt.", file=sys.stderr)
        return 4

    env = {**os.environ, "AGENT_RECURSION_DEPTH": str(depth_next)}
    started = time.monotonic()
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    duration_ms = int((time.monotonic() - started) * 1000)

    cost_usd: float | None = None
    result_text = completed.stdout
    parsed_marker: str | None = None
    try:
        payload = json.loads(completed.stdout)
        # Tolerant field lookup — schema may differ across versions.
        result_text = payload.get("result") or payload.get("output") or completed.stdout
        cost_usd = payload.get("cost_usd") or payload.get("total_cost_usd")
    except json.JSONDecodeError:
        pass

    forwarded, ok = validate_marker(result_text)
    if ok:
        first = forwarded.splitlines()[0].strip()
        parsed_marker = first.split(":", 1)[0]
    sys.stdout.write(forwarded)
    if not forwarded.endswith("\n"):
        sys.stdout.write("\n")

    log_cost_entry({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "spawn",
        "kind": args.kind,
        "budget_tier": args.budget,
        "budget_usd_cap": budget,
        "depth": depth_next,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "return_marker": parsed_marker,
        "exit_code": completed.returncode,
        "malformed": not ok,
    })

    summary_bits = [
        f"spawn-specialist: kind={args.kind}",
        f"budget={args.budget}",
        f"depth={depth_next}",
        f"duration_ms={duration_ms}",
    ]
    if cost_usd is not None:
        summary_bits.append(f"cost_usd={cost_usd}")
    if parsed_marker:
        summary_bits.append(f"marker={parsed_marker}")
    if not ok:
        summary_bits.append("MALFORMED")
    print(" ".join(summary_bits), file=sys.stderr)

    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
