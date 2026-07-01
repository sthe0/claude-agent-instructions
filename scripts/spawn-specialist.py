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
  8. Forward the specialist's text result to stdout; validate that some line
     carries one of the known return markers (else wrap in MALFORMED:).
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

import proc_tree  # sibling module in scripts/; supervised launch + recursive teardown

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = Path.home() / ".claude" / "skills"
CONFIG_MD = REPO_ROOT / "config.md"
PERMISSIONS_CLI = REPO_ROOT / "scripts" / "permissions-cli.py"
COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def _spawn_tags() -> dict:
    """Best-effort session/ticket tags for the cost log (enables per-ticket attribution).

    ticket: $CLAUDE_TICKET, else a TICKET-123 pattern in cwd (dedicated mounts encode it).
    session_id: $CLAUDE_SESSION_ID if the harness exposes it, else None.
    """
    ticket = os.environ.get("CLAUDE_TICKET")
    if not ticket:
        m = re.search(r"[A-Z][A-Z0-9]+-\d+", os.getcwd())
        ticket = m.group(0) if m else None
    return {"session_id": os.environ.get("CLAUDE_SESSION_ID"), "ticket": ticket}


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
    """Resolve a specialization's SKILL.md. Global (~/.claude/skills/<kind>/) wins;
    project-local (<cwd>/.claude/skills/specializations/<kind>/) is the documented
    fallback (CLAUDE.md dispatch table) so a project ships domain experts spawnable
    with the same claude -p isolation, without polluting the global catalog. Returns
    the global path when neither exists, so the caller's not-found error names it."""
    global_path = SKILLS_DIR / kind / "SKILL.md"
    if global_path.exists():
        return global_path
    project_path = Path.cwd() / ".claude" / "skills" / "specializations" / kind / "SKILL.md"
    if project_path.exists():
        return project_path
    return global_path


def validate_marker(result_text: str) -> tuple[str, bool]:
    """Return (text, ok). A return marker is the label of the message; accept it on
    ANY line (the ^MARKER: anchor keeps prose from matching by accident), not only the
    first non-empty one — specialists routinely write a short summary before the
    marker, and rejecting that as MALFORMED false-BLOCKs an otherwise-passing stage.
    If no line carries a known marker, prepend MALFORMED: and ok=False."""
    for line in result_text.splitlines():
        if MARKER_RE.match(line.strip()):
            return result_text, True
    return f"MALFORMED: specialist output contained no known return marker line.\n\n{result_text}", False


PLAN_PATH_RE = re.compile(r"^\s*Plan\s*:\s*(.+?)\s*$", re.MULTILINE)


def validate_planner_plan(result_text: str) -> tuple[str, bool]:
    """For planner PLAN-READY: outputs, extract the `Plan: <path>` line and
    run verify-plan-file.py against it. Return (forwarded_text, ok)."""
    m = PLAN_PATH_RE.search(result_text)
    if not m:
        return (
            "MALFORMED: planner PLAN-READY: output is missing a "
            "`Plan: <absolute-path>` line. The plan must be written to a file "
            "(convention: ~/.claude/plans/<slug>.md) and the path declared "
            "on its own line right after PLAN-READY:.\n\n" + result_text,
            False,
        )
    plan_path = m.group(1).strip().strip("`'\"")
    verifier = Path(__file__).resolve().parent / "verify-plan-file.py"
    if not verifier.exists():
        return result_text, True  # graceful degrade if verifier missing
    proc = subprocess.run(
        ["python3", str(verifier), plan_path],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode == 0:
        return result_text, True
    return (
        f"MALFORMED: planner PLAN-READY: declared plan at `{plan_path}` "
        f"but verify-plan-file.py rejected it:\n{proc.stderr}\n\n"
        + result_text,
        False,
    )


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
    p.add_argument("--budget", choices=("small", "medium", "large"), default="medium", help="budget tier from config.md (kind=developer floors small->medium: the static prefix alone ~$1)")
    p.add_argument("--project-permissions", type=Path, help="project-scope permissions.json to also include in the digest")
    p.add_argument(
        "--permission-mode",
        choices=("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"),
        help="claude --permission-mode for the spawned process. Default: bypassPermissions for kind=developer (trusted local writes), default otherwise.",
    )
    p.add_argument(
        "--complexity",
        choices=("low", "medium", "high"),
        help="task difficulty -> sub-agent model: low=haiku, medium=sonnet, high=opus. "
        "The manager sets this per spawn from how hard the ASSIGNED task is, not the "
        "specialization. Rubric: "
        "low  = mechanical / narrow / fully specified (single-file edit by an example, "
        "rename, format/lint fix, fetch-and-summarize); "
        "medium = standard implementation or analysis (multi-file change with tests, "
        "scoped refactor, standard plan, routine debugging) -- pick this when unsure; "
        "high = subtle reasoning, architecture, tricky debugging, cross-cutting change, "
        "or adversarial verification where correctness is load-bearing. "
        "Overrides the per-kind default; --model overrides this.",
    )
    p.add_argument(
        "--model",
        help="explicit model alias (e.g. sonnet, haiku, opus). Wins over --complexity "
        "and the per-kind default. Prefer --complexity unless you need an exact model.",
    )
    p.add_argument("--stage-index", type=int, default=None, help="index of the plan stage this spawn serves (optional; enables per-stage cost attribution)")
    p.add_argument("--dry-run", action="store_true", help="print the prompt and the command that would run, then exit")
    return p


# Task difficulty -> model. The manager judges difficulty per spawn; this is the
# primary lever (see --complexity). Aliases resolve to the latest of each family.
COMPLEXITY_MODEL = {"low": "haiku", "medium": "sonnet", "high": "opus"}

# Fallback model per specialization, used only when neither --model nor --complexity
# is given. Cheap-but-capable Sonnet for the high-volume implementation/analysis roles;
# planner is omitted so it inherits the parent (stronger) model.
MODEL_BY_KIND = {
    "developer": "sonnet",
    "thinker": "sonnet",
    "tech-writer": "sonnet",
    "yandex-cloud-expert": "sonnet",
}


def resolve_model(args: argparse.Namespace) -> str | None:
    """Model alias for `claude -p --model`, by precedence:
    explicit --model > --complexity map > per-kind default > None (inherit parent)."""
    if args.model:
        return args.model
    if args.complexity:
        return COMPLEXITY_MODEL[args.complexity]
    return MODEL_BY_KIND.get(args.kind)


# Absolute context ceiling before auto-compaction (tokens). The harness knob is a
# percent of the window, so we convert per-model: pct = ceiling / window. This is
# passed to the child via `claude --settings` (see cmd construction) rather than
# process env, because env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE in ~/.claude/settings.json
# would otherwise win over process env (settings.json env is applied after process
# start). See memory-global leaves autocompact-threshold-policy.md and
# claude-code-settings-env-precedence.md.
AUTOCOMPACT_CEILING_TOKENS = 150_000

# Context window per model family (tokens). With the 1M context tier disabled
# (env.CLAUDE_CODE_DISABLE_1M_CONTEXT=1 in ~/.claude/settings.json) every model is
# 200k -> 75% -> 150k. If 1M is re-enabled for a model, bump its window here (and
# DEFAULT) so the derived percent tracks it.
MODEL_WINDOW_TOKENS = {"opus": 200_000, "sonnet": 200_000, "haiku": 200_000, "fable": 200_000}
DEFAULT_WINDOW_TOKENS = 200_000


def autocompact_pct_for_model(model: str | None) -> str:
    """Percent of the window at which the child should auto-compact, so the
    absolute context ceiling stays AUTOCOMPACT_CEILING_TOKENS regardless of which
    model (window) the child runs. Matches a family by substring so both aliases
    (`sonnet`) and full ids (`claude-sonnet-4-6`) resolve."""
    window = DEFAULT_WINDOW_TOKENS
    if model:
        for family, w in MODEL_WINDOW_TOKENS.items():
            if family in model:
                window = w
                break
    pct = round(AUTOCOMPACT_CEILING_TOKENS / window * 100)
    return str(max(1, min(95, pct)))


def _snapshot_transcripts() -> set[Path]:
    """Set of `~/.claude/projects/**/*.jsonl` that exist right now."""
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return set()
    return set(root.rglob("*.jsonl"))


def _discover_transcript_path(known_before: set[Path], timeout: float = 10.0) -> Path | None:
    """Find a new `~/.claude/projects/**/*.jsonl` that didn't exist before the
    spawn. Polls every 0.5s up to `timeout` seconds.

    Filtering by "not in known_before" avoids picking the parent manager's own
    live transcript (which is being touched concurrently and would otherwise
    win on mtime). Returns the freshest new jsonl, or None on timeout.
    """
    root = Path.home() / ".claude" / "projects"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            candidates: list[tuple[float, Path]] = []
            for p in root.rglob("*.jsonl"):
                if p in known_before:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                candidates.append((mtime, p))
            if candidates:
                candidates.sort(reverse=True)
                return candidates[0][1]
        time.sleep(0.5)
    return None


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

    # A developer spawn's static prefix (skill body + context dossier) alone burns
    # ~$1 in cache reads before the first edit, so budget-small ($1) is exhausted
    # mid-flight (error_max_budget_usd) even on a trivial fix. Floor developer at
    # medium. See experience leaf 2026-06-24-developer-marker-not-on-line-1-false-block.
    if args.kind == "developer" and args.budget == "small":
        print(
            "notice: kind=developer with --budget small is structurally insufficient "
            "(static prefix alone ~$1); bumping to medium.",
            file=sys.stderr,
        )
        args.budget = "medium"

    budget = budget_value(args.budget, constants)
    perms = permissions_digest(args.project_permissions)
    prompt = assemble_prompt(args, depth_next, perms)
    model = resolve_model(args)

    cmd = [
        "claude",
        "-p",
        "--append-system-prompt-file",
        str(skill),
        "--max-budget-usd",
        budget,
        "--output-format",
        "json",
        # Pass the per-model autocompact threshold via --settings (highest in the
        # settings precedence ladder) so it beats env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
        # in ~/.claude/settings.json. Setting it in the child's process env does NOT
        # work: settings.json env is applied after process start and wins (see
        # memory-global leaf claude-code-settings-env-precedence.md).
        "--settings",
        json.dumps({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": autocompact_pct_for_model(model)}}),
    ]
    permission_mode = resolve_permission_mode(args)
    if permission_mode is not None:
        cmd.extend(["--permission-mode", permission_mode])
    if model is not None:
        cmd.extend(["--model", model])
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

    env = {
        **os.environ,
        "AGENT_RECURSION_DEPTH": str(depth_next),
    }

    # Snapshot existing transcripts BEFORE spawning so we can identify the
    # child's new jsonl (the parent manager's own live transcript would
    # otherwise win on mtime).
    transcripts_before = _snapshot_transcripts()
    started = time.monotonic()

    # Use Popen so we can print the child's transcript path to stderr early —
    # the parent (manager) can then tail it for monitoring while we block on
    # the child's final JSON output. launch_supervised makes the child a
    # session/process-group leader; install_teardown then reaps that whole group
    # if this wrapper is killed (the harness sends SIGTERM ~5s before SIGKILL, and
    # a manual `kill` of the wrapper lands the same SIGTERM), so the claude -p
    # subtree is never orphaned.
    proc = proc_tree.launch_supervised(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    proc_tree.install_teardown(proc)
    try:
        transcript_path = _discover_transcript_path(transcripts_before, timeout=10.0)
        if transcript_path is not None:
            print(f"spawn-specialist: transcript={transcript_path}", file=sys.stderr, flush=True)
        else:
            print("spawn-specialist: transcript=<not-found-within-10s>", file=sys.stderr, flush=True)

        stdout_str, stderr_str = proc.communicate()
    finally:
        # Normal completion already reaped the child (no-op here); any abnormal
        # exit (exception, KeyboardInterrupt, timeout) still tears down the whole
        # subtree instead of leaking the claude -p children.
        proc_tree.kill_tree(proc)
    completed = subprocess.CompletedProcess(args=cmd, returncode=proc.returncode, stdout=stdout_str, stderr=stderr_str)
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
        if args.kind == "planner" and parsed_marker == "PLAN-READY":
            forwarded, ok = validate_planner_plan(forwarded)
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
        "stage_index": args.stage_index,
        "plan_path": str(args.plan),
        **_spawn_tags(),
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
