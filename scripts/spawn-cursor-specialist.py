#!/usr/bin/env python3
"""Wrap `agent -p` spawn of a specialization skill for Cursor.

Cursor analogue of spawn-specialist.py (which uses `claude -p`). The wrapper
handles process concerns so the manager only supplies cognitive inputs:

  1. Validate args; refuse to spawn an unknown specialization.
  2. Read config.md constants (max-recursion-depth).
  3. Enforce the hard recursion cap before spawning.
  4. Resolve budget tier to a wall-clock timeout (no --max-budget-usd on agent).
  5. Inline-embed SKILL.md (and optional cursor/agents/<kind>-spawn.md) in the prompt.
  6. Auto-embed the permissions digest into the prompt.
  7. Spawn `agent -p` with --trust --force --approve-mcps.
  8. Validate return markers; for planner PLAN-READY:, verify the plan file.
  9. Append a JSONL row to ~/.local/log/cursor-spawn-costs.jsonl.

Use --dry-run to print the assembled prompt and command without spawning.
Use --smoke for a minimal COMPLETED: ping installation check.
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

import proc_tree  # sibling module in scripts/; supervised launch + recursive teardown
from lib import argv_text  # one place decides how an argv value names its text
from lib import marker_extract  # kept for Extraction typing / telemetry; Cursor path does not invoke it (see _build_extraction)
from lib.config_root import skills_dir  # config-root resolver (isolated system root)
from lib.runtime_models import CURSOR_COMPLEXITY_MODEL
from lib.planner_plan_check import (  # single shared home for return-marker + plan checks
    MARKER_RE,
    PLAN_PATH_RE,
    RETURN_MARKERS,
    check_planner_return,
    extract_marker,
    validate_marker,
    validate_planner_plan,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = skills_dir()
CURSOR_AGENTS_DIR = REPO_ROOT / "cursor" / "agents"
CONFIG_MD = REPO_ROOT / "config.md"
PERMISSIONS_CLI = REPO_ROOT / "scripts" / "permissions-cli.py"
DEFAULT_API_KEY_FILE = Path.home() / ".cursor_api_key"
COST_LOG = Path.home() / ".local" / "log" / "cursor-spawn-costs.jsonl"

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")

BUDGET_TIMEOUT_SEC = {"small": 300, "medium": 600, "large": 900}


def parse_config_md() -> dict[str, str]:
    """Extract `key` -> `value` from the markdown table in config.md."""
    constants: dict[str, str] = {}
    for line in CONFIG_MD.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


def recursion_max(constants: dict[str, str]) -> int:
    key = "max-recursion-depth"
    if key not in constants:
        raise SystemExit(f"error: {key} not defined in config.md")
    return int(constants[key])


def budget_timeout_sec(tier: str) -> int:
    if tier not in BUDGET_TIMEOUT_SEC:
        raise SystemExit(f"error: unknown budget tier {tier!r}")
    return BUDGET_TIMEOUT_SEC[tier]


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip()
    return text


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


def skill_path(kind: str) -> Path:
    return SKILLS_DIR / kind / "SKILL.md"


def cursor_agent_path(kind: str) -> Path | None:
    path = CURSOR_AGENTS_DIR / f"{kind}-spawn.md"
    return path if path.is_file() else None


def assemble_prompt(
    args: argparse.Namespace,
    depth: int,
    permissions: str,
    skill_body: str,
    cursor_bootstrap: str | None,
) -> str:
    # --plan is optional here (--smoke runs without one); an absent plan stays "".
    plan = argv_text.read_required_file(args.plan, "--plan") if args.plan else ""
    constraints = (argv_text.read_arg_text(args.constraints) or "").rstrip()
    done_criterion = argv_text.read_arg_text(args.done_criterion)
    dossier = (
        argv_text.read_required_file(args.context_dossier, "--context-dossier")
        if args.context_dossier
        else ""
    )

    sections = [f"AGENT_RECURSION_DEPTH={depth}", ""]
    if cursor_bootstrap:
        sections += ["## Cursor specialist bootstrap", "", cursor_bootstrap, ""]
    sections += ["## Specialization instructions", "", skill_body, ""]
    continue_worktree = getattr(args, "continue_worktree", None)
    if continue_worktree:
        sections += [
            "## Continue the prior stage — do NOT fork fresh",
            "",
            f"This stage depends on a prior stage whose committed-but-un-landed work "
            f"lives in the linked worktree `{continue_worktree}` (build on its branch). "
            f"`cd` into that worktree and continue on its branch, creating the worktree "
            f"only if it is absent — never `git worktree add` a fresh branch off "
            f"origin/main for this stage, or you will fork away from and lose the prior "
            f"stage's work.",
            "",
        ]
    sections += ["## Working plan", "", plan, ""]
    sections += [
        "## Done criterion for this step",
        "",
        f"{done_criterion}  *({args.criterion_type})*",
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


def log_cost_entry(entry: dict) -> None:
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_refused(reason: str, extra: dict) -> None:
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "refused",
        "reason": reason,
        **extra,
    }
    log_cost_entry(entry)


def find_agent_binary() -> str | None:
    return shutil.which("agent") or shutil.which("cursor-agent")


def resolve_api_key(api_key_file: Path) -> str | None:
    env_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if env_key:
        return env_key
    if api_key_file.is_file():
        return api_key_file.read_text(encoding="utf-8").strip()
    return None


def build_agent_cmd(
    agent_bin: str,
    workspace: Path,
    model: str,
    timeout_sec: int,
) -> list[str]:
    # The prompt is NOT an argv element: with the plan inlined it exceeds Linux
    # MAX_ARG_STRLEN (32 pages = 131072 bytes for a single argv string), which execve
    # rejects with E2BIG before the child starts. It travels via stdin instead (see
    # the launch below); `agent -p` reads its prompt from stdin when no positional
    # prompt is given (`-p`/`--print` is a boolean switch, the prompt a separate
    # positional).
    cmd = [
        agent_bin,
        "-p",
        "--trust",
        "--force",
        "--approve-mcps",
        "--workspace",
        str(workspace.resolve()),
        "--output-format",
        "text",
        "--model",
        model,
    ]
    timeout_bin = shutil.which("timeout")
    if timeout_bin and timeout_sec > 0:
        cmd = [timeout_bin, str(timeout_sec)] + cmd
    return cmd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--kind",
        default="developer",
        help="specialization name (must exist at ~/.claude-agent/skills/<kind>/SKILL.md; default: developer)",
    )
    p.add_argument(
        "--plan",
        type=Path,
        help="path to the markdown plan; mark the step the specialist owns with **<<this step>>**",
    )
    p.add_argument("--done-criterion",
                   help="concrete done criterion for the step; '@<path>' reads it from a file")
    p.add_argument(
        "--criterion-type",
        choices=("measurable", "acceptance-review"),
        help="how the criterion will be verified",
    )
    p.add_argument("--constraints", default="",
                   help="scope / do-not-touch / deadlines; '@<path>' reads them from a file")
    p.add_argument(
        "--context-dossier",
        type=Path,
        help="path to a file with the conversation-context digest",
    )
    p.add_argument(
        "--budget",
        choices=tuple(BUDGET_TIMEOUT_SEC),
        default="medium",
        help="budget tier → timeout default (small=300s, medium=600s, large=900s)",
    )
    p.add_argument(
        "--project-permissions",
        type=Path,
        help="project-scope permissions.json to also include in the digest",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace directory for the agent (default: cwd)",
    )
    p.add_argument(
        "--complexity",
        choices=tuple(CURSOR_COMPLEXITY_MODEL),
        help="task difficulty -> sub-agent model: "
        f"low={CURSOR_COMPLEXITY_MODEL['low']}, medium={CURSOR_COMPLEXITY_MODEL['medium']}, "
        f"high={CURSOR_COMPLEXITY_MODEL['high']}. Same rubric as spawn-specialist.py's "
        "--complexity. Overrides the default model; --model overrides this.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="explicit agent model id. Wins over --complexity and the "
        f"default ({CURSOR_COMPLEXITY_MODEL['medium']}).",
    )
    p.add_argument(
        "--stage-index", type=int, default=None,
        help="index of the plan stage this spawn serves (optional; enables per-stage cost attribution)",
    )
    p.add_argument(
        "--continue-worktree",
        default=None,
        help="path of a prior dependent stage's linked worktree/branch to continue "
        "(threaded by `agentctl dispatch` for a stage that depends on a prior spawn "
        "stage); when set, the assembled prompt instructs the specialist to build on "
        "that worktree/branch instead of forking fresh",
    )
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="wall-clock timeout when `timeout` is on PATH (default: from --budget tier)",
    )
    p.add_argument(
        "--api-key-file",
        type=Path,
        default=DEFAULT_API_KEY_FILE,
        help=f"path to API key file (default: {DEFAULT_API_KEY_FILE})",
    )
    p.add_argument("--dry-run", action="store_true", help="print the prompt and the command that would run, then exit")
    p.add_argument(
        "--smoke",
        action="store_true",
        help="installation smoke: minimal prompt expecting COMPLETED: ping",
    )
    return p


def resolve_model(args: argparse.Namespace) -> str:
    """Agent model id for `agent -p --model`, by precedence: explicit --model >
    --complexity map > the medium-tier default — mirrors spawn-specialist.py's
    resolve_model, substituting CURSOR_COMPLEXITY_MODEL for COMPLEXITY_MODEL."""
    if args.model:
        return args.model
    if args.complexity:
        return CURSOR_COMPLEXITY_MODEL[args.complexity]
    return CURSOR_COMPLEXITY_MODEL["medium"]


def _build_extraction(result_text: str, kind: str) -> "marker_extract.Extraction | None":
    """Cursor hard gate: never invoke ``claude``. ``marker_extract`` shells out
    to ``claude -p``, so this wrapper stays on the legacy any-line scan
    (same outcome as ``AGENTCTL_MARKER_EXTRACTOR=0``). Return ``None`` so the
    caller takes that path. A future ``agent``-backed extractor can replace
    this stub without changing the call site.

    ``result_text`` / ``kind`` are unused on purpose — kept so the signature
    matches the Claude-side wrappers and existing tests can drive the call site.
    """
    del result_text, kind
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1

    timeout_sec = args.timeout_sec if args.timeout_sec is not None else budget_timeout_sec(args.budget)

    if args.smoke:
        prompt = "Reply with exactly one line: COMPLETED: ping"
        timeout_sec = min(timeout_sec, 90)
    else:
        missing = [
            name
            for name, val in (
                ("plan", args.plan),
                ("done-criterion", args.done_criterion),
                ("criterion-type", args.criterion_type),
            )
            if not val
        ]
        if missing:
            print(f"error: --{missing[0]} required unless --smoke", file=sys.stderr)
            return 1

    skill = skill_path(args.kind)
    if not skill.exists():
        print(f"error: unknown specialization (no SKILL.md at {skill})", file=sys.stderr)
        log_refused("unknown-kind", {"kind": args.kind})
        return 2

    if not args.smoke and args.plan is not None and not argv_text.is_readable_file(args.plan):
        print(argv_text.file_arg_error("--plan", args.plan), file=sys.stderr)
        log_refused("plan-not-found", {"kind": args.kind, "plan": argv_text.abbreviate(args.plan)})
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

    if not args.smoke:
        skill_body = strip_frontmatter(skill.read_text(encoding="utf-8")).rstrip()
        cursor_agent = cursor_agent_path(args.kind)
        cursor_bootstrap = None
        if cursor_agent is not None:
            cursor_bootstrap = strip_frontmatter(cursor_agent.read_text(encoding="utf-8")).rstrip()
        perms = permissions_digest(args.project_permissions)
        prompt = assemble_prompt(args, depth_next, perms, skill_body, cursor_bootstrap)

    model = resolve_model(args)
    agent_bin = find_agent_binary()
    cmd = build_agent_cmd(agent_bin or "agent", workspace, model, timeout_sec)

    if args.dry_run:
        print("=== assembled prompt (delivered via stdin) ===")
        print(prompt)
        print("\n=== command (not executed) ===")
        # The prompt is piped via stdin, so it is not a member of cmd; print cmd
        # verbatim and note the stdin payload size separately.
        print(" ".join(repr(c) if " " in c else c for c in cmd))
        print(f"# stdin: <prompt {len(prompt)} chars>")
        api_key = resolve_api_key(args.api_key_file)
        print(f"\nCURSOR_API_KEY={'set' if api_key else 'missing'}")
        print(f"timeout_sec={timeout_sec} (budget_tier={args.budget})")
        return 0

    if agent_bin is None:
        print(
            "error: neither `agent` nor `cursor-agent` on PATH; cannot spawn. "
            "Re-run with --dry-run to inspect the prompt.",
            file=sys.stderr,
        )
        return 4

    api_key = resolve_api_key(args.api_key_file)
    if not api_key:
        print(
            "error: CURSOR_API_KEY not set and api key file missing or empty: "
            f"{args.api_key_file}",
            file=sys.stderr,
        )
        return 1

    env = {**os.environ, "AGENT_RECURSION_DEPTH": str(depth_next), "CURSOR_API_KEY": api_key}
    started = time.monotonic()
    # launch_supervised makes the `agent` child a session/process-group leader;
    # install_teardown then reaps that whole group if this wrapper is killed (the
    # harness sends SIGTERM ~5s before SIGKILL, and a manual `kill` lands the same
    # SIGTERM), so the agent subtree is never orphaned.
    proc = proc_tree.launch_supervised(
        cmd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    proc_tree.install_teardown(proc)
    try:
        # communicate feeds stdin and drains stdout/stderr concurrently, which is
        # what avoids the pipe-buffer deadlock a manual write()+read() would risk.
        stdout_str, stderr_str = proc.communicate(input=prompt)
    finally:
        proc_tree.kill_tree(proc)
    completed = subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout_str, stderr=stderr_str
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    result_text = completed.stdout
    if not result_text.strip() and completed.stderr.strip():
        result_text = completed.stderr

    extraction = _build_extraction(result_text, args.kind)
    forwarded, ok, parsed_marker = check_planner_return(
        result_text, args.kind, extraction=extraction
    )

    sys.stdout.write(forwarded)
    if not forwarded.endswith("\n"):
        sys.stdout.write("\n")

    log_cost_entry({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "spawn",
        "kind": "cursor-specialist-smoke" if args.smoke else args.kind,
        "budget_tier": args.budget,
        "timeout_sec": timeout_sec,
        "depth": depth_next,
        "model": model,
        "stage_index": args.stage_index,
        "workspace": str(workspace),
        "duration_ms": duration_ms,
        "return_marker": parsed_marker,
        "exit_code": completed.returncode,
        "malformed": not ok,
        "extractor_invoked": extraction is not None,
        "extractor_model": marker_extract.model() if extraction is not None else None,
        "extractor_degraded": extraction.degraded if extraction is not None else None,
        "extraction_reason": extraction.reason if extraction is not None else None,
    })

    summary_bits = [
        "spawn-cursor-specialist:",
        f"kind={args.kind}",
        f"budget={args.budget}",
        f"timeout_sec={timeout_sec}",
        f"depth={depth_next}",
        f"model={model}",
        f"duration_ms={duration_ms}",
    ]
    if parsed_marker:
        summary_bits.append(f"marker={parsed_marker}")
    if not ok:
        summary_bits.append("MALFORMED")
    print(" ".join(summary_bits), file=sys.stderr)

    if not ok:
        return 1
    return 0 if completed.returncode == 0 else completed.returncode


if __name__ == "__main__":
    sys.exit(main())
