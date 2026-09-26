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
import tempfile
import threading
import time
from pathlib import Path

import proc_tree  # sibling module in scripts/; supervised launch + recursive teardown
from agentctl import grants  # the sole validator every materialized rule/add_dir passes through
from agentctl.plan import load_plan  # parse the TOML plan for a single-stage brief projection
from agentctl.render import render_stage_brief  # pure PlanDoc+index -> markdown brief
from lib import argv_text  # one place decides how an argv value names its text
from lib import marker_extract  # unconditional second-pass marker extraction (model is the primary classifier)
from lib.config_root import plans_dir, projects_roots, skills_dir  # config-root resolver (isolated system root)
from lib.kind_baselines import (  # re-exported below so `MOD.KIND_BASELINES` etc. keep working for importlib callers
    KIND_BASELINES,
    PLANNER_CHECK_ORDER_COVERAGE_RULE,
    PLANNER_LIST_DENIED_RULE,
    PLANNER_PLAN_GRANTS_RULE,
    SCRIPTS_DIR,
)
from lib.planner_plan_check import (  # single shared home for return-marker + plan checks
    MARKER_RE,
    PLAN_PATH_RE,
    RETURN_MARKERS,
    check_planner_return,
    extract_marker,
    validate_marker,
    validate_planner_plan,
)
from session_scope import registry  # scope registry: deregister the child's scope on exit

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = skills_dir()
CONFIG_MD = REPO_ROOT / "config.md"
PERMISSIONS_CLI = REPO_ROOT / "scripts" / "permissions-cli.py"
COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def _spawn_tags() -> dict:
    """Best-effort session/ticket tags for the cost log (enables per-ticket attribution).

    ticket: $CLAUDE_TICKET, else a TICKET-123 pattern in cwd (dedicated mounts encode it).
    session_id: $CLAUDE_CODE_SESSION_ID if the harness exposes it, else None.
    """
    ticket = os.environ.get("CLAUDE_TICKET")
    if not ticket:
        m = re.search(r"[A-Z][A-Z0-9]+-\d+", os.getcwd())
        ticket = m.group(0) if m else None
    return {"session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"), "ticket": ticket}


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


# Multiple of a spawn's tier LABEL above which realized cost triggers one stderr
# soft-warn line (no kill, no non-zero exit) — a cheap calibration signal.
SOFT_WARN_MULT = 2.0


def runaway_ceiling(constants: dict[str, str]) -> str:
    """The single global runaway backstop passed as --max-budget-usd to every
    spawn (spawn-runaway-ceiling-usd in config.md). Under flat billing the
    per-tier budget-*-usd values are expected-size LABELS, not kill-caps; the
    kill is this one high ceiling. Fail-safe: if the key is absent, fall back to
    the large tier so a partial rollout can never remove the backstop entirely
    (never unbounded)."""
    key = "spawn-runaway-ceiling-usd"
    if key in constants:
        return constants[key]
    return budget_value("large", constants)


def recursion_max(constants: dict[str, str]) -> int:
    key = "max-recursion-depth"
    if key not in constants:
        raise SystemExit(f"error: {key} not defined in config.md")
    return int(constants[key])


def build_child_lineage(inherited: "str | None", own_id: "str | None") -> str:
    """Child's AGENT_LINEAGE_IDS: the inherited lineage (this spawn's own
    ancestors) plus the spawning session's own id, ordered and deduped, so a
    parent and its synchronously-spawned descendants form one write-lineage
    (see session_scope.detector.detect_conflicts)."""
    ids = registry.parse_lineage(inherited)
    if own_id and own_id not in ids:
        ids.append(own_id)
    return registry.format_lineage(ids)


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


def brief_plan_path(args: argparse.Namespace) -> Path | None:
    """The resolved path of `args.plan` when assemble_prompt should project it
    to a single-stage brief rather than inlining the whole plan text; None
    when it should not (any condition failing falls back to whole-plan
    behavior, never raises).

    Returns the resolved path rather than `args.plan` as given because a
    path given outside `plans_dir()` whose target resolves inside it is still
    eligible, and the child's --add-dir only covers `plans_dir()` — a pointer
    spelled as given could name a file the child has no grant to open.
    """
    kind_can_read_plans = getattr(args, "kind", None) in PLANS_READ_KINDS
    opted_in = getattr(args, "plan_brief", False)
    has_stage_index = getattr(args, "stage_index", None) is not None
    plan_path = getattr(args, "plan", None)
    if not (kind_can_read_plans and opted_in and has_stage_index and plan_path):
        return None
    try:
        resolved_plan = Path(plan_path).resolve()
        resolved_plan.relative_to(plans_dir().resolve())
    except (OSError, ValueError):
        return None
    return resolved_plan


def brief_eligible(args: argparse.Namespace) -> bool:
    """Whether `args` earns a projected stage brief — `brief_plan_path`'s
    predicate face, for callers that need the decision and not the path."""
    return brief_plan_path(args) is not None


def assemble_prompt(
    args: argparse.Namespace,
    depth: int,
    permissions: str,
    *,
    workdir: "str | None" = None,
    permission_mode: "str | None" = None,
    add_dir_paths: "list[str] | None" = None,
    stage_grant_entries: "list[dict] | None" = None,
) -> str:
    plan = argv_text.read_required_file(args.plan, "--plan")
    constraints = (argv_text.read_arg_text(args.constraints) or "").rstrip()
    done_criterion = argv_text.read_arg_text(args.done_criterion)
    dossier = (
        argv_text.read_required_file(args.context_dossier, "--context-dossier")
        if args.context_dossier
        else ""
    )

    sections = [f"AGENT_RECURSION_DEPTH={depth}", ""]
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
    resolved_plan = brief_plan_path(args)
    if resolved_plan is not None:
        doc = load_plan(str(args.plan))
        plan_label = (
            f"## Working plan — stage {args.stage_index} brief "
            f"(projected; the full plan lives at `{resolved_plan}`, not inlined here)"
        )
        plan_body = render_stage_brief(doc, args.stage_index)
    else:
        plan_label = "## Working plan"
        plan_body = plan
    sections += [plan_label, "", plan_body, ""]
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
    if workdir is not None or permission_mode is not None or add_dir_paths or stage_grant_entries:
        scope_lines = ["## File-access scope", ""]
        if workdir is not None:
            scope_lines.append(f"- Working directory: `{workdir}`")
        if permission_mode is not None:
            scope_lines.append(f"- Permission mode: `{permission_mode}`")
        if add_dir_paths:
            for path in add_dir_paths:
                scope_lines.append(f"- Additional directory: `{path}`")
        if stage_grant_entries:
            scope_lines.append("- Stage grants (provenance):")
            scope_lines.extend(stage_grant_provenance_lines(stage_grant_entries))
        scope_lines.append("")
        sections += scope_lines
    if getattr(args, "kind", None) == "planner":
        sections += [
            "## Prescribed research commands",
            "",
            "Before drafting, run these against the ENGINE'S own state rather than "
            "assuming a stage's grants will materialize as you expect, or that a "
            "past denial means what its transcript text alone suggests:",
            "",
            f"- `python3 {SCRIPTS_DIR}/agentctl-cli.py plan-grants --plan <plan-path> "
            "--format compact` — what each stage's declared/derived grants will "
            "actually materialize to in a spawned child's --settings.",
            f"- `python3 {SCRIPTS_DIR}/check-spawn-tool-run.py --list-denied "
            "--transcript <transcript-path>` — every tool call a past transcript was "
            "actually refused, so a stage's grants can be sized against real denials.",
            "",
        ]
    if getattr(args, "kind", None) == "developer":
        sections += [
            "## Verification command hygiene",
            "",
            "Run verification commands (tests, linters) as a single BARE command — "
            "no `&&`, `;`, or pipes (classified as multiple operations), no `$VAR` "
            "shell expansion (classified as simple_expansion), and no `python3 -c` "
            "(requires approval). Reference paths as absolute arguments inside your "
            "working directory rather than `cd`-ing first.",
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


def composed_system_prompt_file(skill: Path) -> Path:
    """SKILL.md with the shared marker-protocol appended, as a temp file.

    Specialization SKILL.md files reference skills/specializations/_shared/
    marker-protocol.md instead of each repeating the invocation contract and
    the CLARIFY:/PERMISSION-REQUEST: format blocks. A spawned specialist gets
    its SKILL.md as the system prompt and has no guarantee of reading linked
    files, so the spawn is the composition point that must inline the shared
    text (invariant: no information loss at spawn time). Falls back to the
    bare SKILL.md when no shared file exists (project-local skills that carry
    their own contract).
    """
    shared = skill.resolve().parent.parent / "_shared" / "marker-protocol.md"
    if not shared.exists():
        shared = REPO_ROOT / "skills" / "specializations" / "_shared" / "marker-protocol.md"
    if not shared.exists():
        return skill
    combined = (
        skill.read_text(encoding="utf-8")
        + "\n\n---\n\n"
        + shared.read_text(encoding="utf-8")
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="skill-composed-", delete=False, encoding="utf-8"
    )
    with tmp:
        tmp.write(combined)
    return Path(tmp.name)


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
    p.add_argument("--done-criterion", required=True,
                   help="concrete done criterion for the step; '@<path>' reads it from a file")
    p.add_argument(
        "--criterion-type",
        choices=("measurable", "acceptance-review"),
        required=True,
        help="how the criterion will be verified",
    )
    p.add_argument("--constraints", default="",
                   help="scope / do-not-touch / deadlines; '@<path>' reads them from a file")
    p.add_argument("--context-dossier", type=Path, help="path to a file with the conversation-context digest")
    p.add_argument("--budget", choices=("small", "medium", "large"), default="medium", help="budget tier from config.md (kind=developer floors small->medium: the static prefix alone ~$1)")
    p.add_argument("--project-permissions", type=Path, help="project-scope permissions.json to also include in the digest")
    p.add_argument(
        "--project-settings",
        type=Path,
        help="kind=developer only: a target project's own .claude/settings.local.json, whose "
             "permissions.allow/deny entries are merged into this child's --settings grant "
             "(distinct from --project-permissions, which only affects the prose digest)",
    )
    p.add_argument(
        "--permission-mode",
        choices=("default", "plan"),
        help="claude --permission-mode for the spawned process. The wider modes "
        "(acceptEdits/auto/bypassPermissions/dontAsk) are no longer user-settable "
        "here -- they are internal decisions resolve_permission_mode makes per "
        "kind (acceptEdits for developer/tech-writer), never a flag value a "
        "caller can widen to. Default: harness default (per-kind via "
        "resolve_permission_mode) when unset.",
    )
    model_group = p.add_mutually_exclusive_group(required=True)
    model_group.add_argument(
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
        "Required unless --model is given: there is no inherit-the-parent-model "
        "fallback, the manager always classifies. Clamped by --max-complexity if set.",
    )
    model_group.add_argument(
        "--model",
        help="explicit model alias (e.g. sonnet, haiku, opus). An intentional exact "
        "override, NOT subject to --max-complexity (which only clamps the "
        "--complexity classification path). Mutually exclusive with --complexity; "
        "prefer --complexity unless you need an exact model.",
    )
    p.add_argument(
        "--max-complexity",
        choices=("low", "medium", "high"),
        default=None,
        help="ceiling on the model tier a --complexity classification may resolve "
        "to (e.g. --max-complexity medium caps a 'high' classification down to "
        "sonnet). Default: unset, no ceiling -- --complexity high still reaches "
        "opus. Does not affect an explicit --model.",
    )
    p.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        required=True,
        help="claude -p --effort reasoning-effort level for the spawned child. "
        "Required, with no inherit-the-parent fallback, on the same rationale as "
        "--complexity/--model (see resolve_model): an optional flag with a "
        "default degrades into an unconsidered default under time pressure. "
        "Rubric: low = cheap dispatch/retrieval/polling; medium = standard "
        "implementation or analysis (pick when unsure); high/xhigh = subtle "
        "reasoning, architecture, adversarial verification where correctness is "
        "load-bearing; max = rare, only for the most contested judgment calls.",
    )
    p.add_argument("--stage-index", type=int, default=None, help="index of the plan stage this spawn serves (optional; enables per-stage cost attribution)")
    p.add_argument(
        "--session",
        default=None,
        help="engine session id owning this spawn; combined with --stage-index and "
        "a matching --kind, materializes the stage's engine-recorded grants "
        "(declared/derived/runtime) into --settings/--add-dir and into the "
        "prompt's File-access scope section. Read-only introspection "
        "(stage-grants) -- never grants the spawned child agentctl user-authority "
        "verbs itself.",
    )
    p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="override the engine's state-store root used to resolve --session "
        "(defaults to the store's own default root; for tests/fixtures)",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="child process working directory (must already exist); defaults to "
        "this process's own cwd when unset",
    )
    p.add_argument(
        "--plan-brief",
        action="store_true",
        help="project only --stage-index's stage into the prompt (render_stage_brief) "
        "instead of inlining the whole plan; requires --kind in PLANS_READ_KINDS, "
        "--stage-index set, and --plan to resolve inside plans_dir() — falls back "
        "to today's whole-plan behavior otherwise (see brief_eligible)",
    )
    p.add_argument(
        "--continue-worktree",
        default=None,
        help="path of a prior dependent stage's linked worktree/branch to continue "
        "(threaded by `agentctl dispatch` for a stage that depends on a prior spawn "
        "stage); when set, the assembled prompt instructs the specialist to build on "
        "that worktree/branch instead of forking fresh",
    )
    p.add_argument("--dry-run", action="store_true", help="print the prompt and the command that would run, then exit")
    return p


# Task difficulty -> model. The manager judges difficulty per spawn; this is the
# primary lever (see --complexity). Aliases resolve to the latest of each family.
# The ladder deliberately terminates at opus: fable is a tier above it at 2x the
# per-token price, so routing any complexity band there is a spend decision for the
# user to make explicitly via `--model fable`, not a default.
COMPLEXITY_MODEL = {"low": "haiku", "medium": "sonnet", "high": "opus"}
COMPLEXITY_ORDER = ("low", "medium", "high")


def _clamp_complexity(complexity: str, ceiling: "str | None") -> str:
    """`complexity`, capped to `ceiling` on COMPLEXITY_ORDER (no-op if
    `ceiling` is None or complexity is already at/below it)."""
    if ceiling is None:
        return complexity
    if COMPLEXITY_ORDER.index(complexity) > COMPLEXITY_ORDER.index(ceiling):
        return ceiling
    return complexity


def resolve_model(args: argparse.Namespace) -> str:
    """Model alias for `claude -p --model`, by precedence:
    explicit --model (exact override, ignores --max-complexity) > --complexity,
    clamped by --max-complexity if set, mapped through COMPLEXITY_MODEL.

    --model and --complexity are a required mutually-exclusive pair (see
    build_parser) — there is no third "neither given" case, so this never
    inherits the parent's model."""
    if args.model:
        return args.model
    complexity = _clamp_complexity(args.complexity, args.max_complexity)
    return COMPLEXITY_MODEL[complexity]


# Absolute context ceiling before auto-compaction (tokens) — our own intent, not a
# copy of anything Anthropic owns. It reaches the child via `claude --settings`
# (highest in the settings precedence ladder) rather than the child's process env,
# because an env entry in ~/.claude/settings.json is applied after process start and
# would win over process env. See memory-global leaves autocompact-threshold-policy.md
# and claude-code-settings-env-precedence.md.
AUTOCOMPACT_CEILING_TOKENS = 150_000

# The client resolves the effective window as min(model max, configured), then fires
# auto-compaction at min(round((window - 20000) * (1 - frac)), (window - 20000) - 13000).
# Pinning the WINDOW rather than a percentage lets that min() do the per-model work,
# so no per-model window table is needed here: a Haiku child clamps itself to its own
# 200k maximum with nothing about Haiku recorded in this file.
# Residual exposure, stated rather than hidden: `frac` is server-driven (flags
# tengu_amber_moleskin / tengu_amber_rokovoko in client 2.1.220), so a fraction change
# moves the trigger. The previous percentage mechanism carried the same exposure —
# the fraction term is an outer min() in the client's trigger — so nothing is lost.
# The next two are CLIENT-side constants, read out of the installed bundle at
# /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe,
# client 2.1.220 — not values this repository owns. They move on a client
# release without our involvement, so re-READ them from that bundle rather than
# re-deriving them, and locate them by the surrounding literal strings rather
# than by symbol name (the minifier renames symbols between builds). This is
# the discipline config.md's claude-md-max-chars row already records for a
# borrowed constant; the version named above is the only thing that later tells
# a reader whether the numbers here still match the installed client.
OUTPUT_RESERVE_TOKENS = 20_000        # min(maxOutputTokens, 20000) in the client
CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS = 13_000   # the floor term of the trigger's min()
PRECOMPUTE_BUFFER_FRACTION = 0.2      # client default; server-tunable (see above)
SPAWN_AUTOCOMPACT_WINDOW_TOKENS = (
    round(AUTOCOMPACT_CEILING_TOKENS / (1 - PRECOMPUTE_BUFFER_FRACTION))
    + OUTPUT_RESERVE_TOKENS
)

# The `model max` term of the client's min(model max, configured window) step.
# Deliberately ONE number rather than a per-model table, on the same rationale
# as SPAWN_AUTOCOMPACT_WINDOW_TOKENS: every COMPLEXITY_MODEL entry (haiku,
# sonnet, opus) shares this 200k maximum, so applying it to every model is
# exact for the roster as it stands and conservative for anything larger. A
# smaller-window model joining the roster would make it too generous: a
# residual, not a guarantee.
MODEL_FLOOR_WINDOW_TOKENS = 200_000

# Conservative chars-per-token divisor for estimating an assembled prompt's
# token count from its char count without invoking a tokenizer. Fixed below
# the 1.744 chars/token actually measured on the failed dispatch below
# (375,759 chars / 215,416 tokens), so this estimate errs toward refusing
# early rather than discovering the failure only after the child is spawned.
# The distance from 1.744 down to 1.5 is picked headroom, not a derivation:
# only the ratio is measured, and nothing in it fixes how far below to sit.
PROMPT_CHARS_PER_TOKEN = 1.5


def dispatch_prompt_ceiling_tokens(model: str | None) -> int:
    """Largest assembled prompt (in tokens) this parent will spawn a child with."""
    window = min(MODEL_FLOOR_WINDOW_TOKENS, SPAWN_AUTOCOMPACT_WINDOW_TOKENS)
    usable = window - OUTPUT_RESERVE_TOKENS
    fraction_term = round(usable * (1 - PRECOMPUTE_BUFFER_FRACTION))
    floor_margin_term = usable - CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS
    return min(fraction_term, floor_margin_term)


def dispatch_prompt_ceiling_chars(model: str | None) -> int:
    """The token ceiling above, converted into the unit the assembled prompt is
    already measured in. The CEILING is multiplied by a divisor set below the
    measured ratio, so the conversion shrinks the char budget; the prompt is
    never converted the other way, which would invert that safety margin."""
    return int(dispatch_prompt_ceiling_tokens(model) * PROMPT_CHARS_PER_TOKEN)


def prompt_exceeds_ceiling(prompt: str, model: str | None = None) -> bool:
    """Whether the assembled prompt (stdin payload) is too large to safely
    spawn. Applies uniformly regardless of whether the size came from the
    brief or whole-plan path."""
    return len(prompt) > dispatch_prompt_ceiling_chars(model)


# KIND_BASELINES / SCRIPTS_DIR / the PLANNER_* rule constants now live in
# lib/kind_baselines.py (imported at top of file) so agentctl/cli.py can read
# them without loading this launcher module; re-exported here at module
# scope purely by virtue of that top-level import, so every existing
# caller/test that reads them off spawn-specialist.py (`MOD.KIND_BASELINES`
# via importlib) keeps working unchanged.

# The plan-artifact directory (lib.config_root.plans_dir()) is where a
# planner's SKILL.md tells it to write its deliverable and where a reviewer's
# SKILL.md tells it to read the plan under review — a contract naming a
# directory neither kind was ever granted. planner writes; thinker and
# code-reviewer only read, plus the one Bash prefix that lets a reviewer
# compute the plan's own sha256 to report in its REVIEW message (as a
# `Plan digest: <sha256>` line) — the reviewer never runs `agentctl
# plan-review --plan-digest` itself (that call needs --session, which a
# review spawn is never given; see plugins_review_dispatch.py's directive
# text), only the ROOT does, once the reviewer's digest is in hand. Rides
# the same --settings seam KIND_BASELINES uses, never settings/base.json.
#
# Three facts measured live against the CLI on 2026-08-05 (probes A-D in the
# stage-2 continuation), none of them documented anywhere the plan's authors
# could find beforehand:
#   - An ABSOLUTE path in a permission rule needs a DOUBLE leading slash. A
#     single leading "/" is read as relative to the project root, so
#     "Edit(/Users/.../plans/**)" matches nothing; "Edit(//Users/.../plans/**)"
#     matches. `plans_directory` already starts with "/", so the rule string
#     below prepends exactly one more.
#   - `Write(path)` rules are not matched by file permission checks at all —
#     the CLI says so itself: only `Edit(path)` rules are, and Edit rules
#     cover every file-editing tool. So the write grant is Edit-only.
#   - Every non-developer kind gets no --permission-mode flag
#     (resolve_permission_mode returns None), so it inherits whatever
#     defaultMode the harness settings declare — acceptEdits on this fleet —
#     under which --add-dir alone already makes a directory writable. An
#     allow-only payload therefore cannot express "read but not write"; the
#     read kinds need an explicit `Edit(...)` DENY alongside their `Read`
#     allow, or the grant is directional in name only.
#
# `developer` was excluded when the read kinds were first named, on the
# reasoning that an executor never needs to open the plan: assemble_prompt
# reads the file and embeds its full text, so the norm arrives with the brief.
# That holds only while the brief is still in context. Observed 2026-08-10: a
# stage-8 developer ran 11 minutes, was auto-compacted, lost the embedded plan,
# and had no way back to it — `Read` and `Bash cat` on the plans directory both
# refused, so it returned PERMISSION-REQUEST having written nothing ($6.50).
# Delivery once is not availability: the longer a spawn runs, the likelier it
# is to need its norm again and the likelier compaction has already taken it.
# So an executor reads for the same reason a reviewer does, and gets the same
# directional pair — the Edit DENY matters MORE here than for a reviewer, since
# an executor that could rewrite the plan it is executing would be editing the
# norm it is measured against.
PLANS_WRITE_KINDS = ("planner",)
PLANS_READ_KINDS = ("thinker", "code-reviewer", "developer")

# The only kind whose spawn inherits the target project's own
# `.claude/settings.local.json` permissions (see build_child_settings) —
# named the same way as the plans-access tuples above, rather than a
# hardcoded `kind == "developer"` check, so the grant is discoverable
# alongside its siblings and not a one-off literal buried in a function body.
PROJECT_SETTINGS_KINDS = ("developer",)


def plans_permission_rules(kind: str, plans_directory: Path) -> tuple[list[str], list[str]]:
    """(allow, deny) permission rules granting `kind` access to
    `plans_directory`, in the direction that kind needs. Empty pair for a
    kind granted neither. A read kind's allow additionally carries the
    `Bash(shasum -a 256:*)` prefix, which is NOT confined to
    `plans_directory` — it hashes any file the child can already reach.

    Raises ValueError if `plans_directory` is not absolute."""
    if not plans_directory.is_absolute():
        raise ValueError(f"plans_directory must be absolute, got: {plans_directory}")
    base = f"/{plans_directory}/**"
    if kind in PLANS_WRITE_KINDS:
        return [f"Edit({base})"], []
    if kind in PLANS_READ_KINDS:
        return [f"Read({base})", "Bash(shasum -a 256:*)"], [f"Edit({base})"]
    return [], []


def plans_add_dir_args(kind: str, plans_directory: Path) -> list[str]:
    """`--add-dir` argv for `kind`, when it is granted access to `plans_directory`.

    A permissions.allow rule alone does not put a directory outside the
    child's cwd into its workspace — --add-dir is required too, or the child
    still prompts for a decision it cannot answer headlessly (see the plan's
    stage-2 material: instance 17's empty-output-file symptom)."""
    if kind in PLANS_WRITE_KINDS or kind in PLANS_READ_KINDS:
        return ["--add-dir", str(plans_directory)]
    return []


def _vcs_root(cwd: str) -> "str | None":
    """VCS root of `cwd`: git first, then arc (mirrors
    hook-scope-track.py::resolve_repo_root_vcs; duplicated rather than
    imported since that module is a standalone hook script, not a library)."""
    for probe in (["git", "rev-parse", "--show-toplevel"], ["arc", "root"]):
        try:
            out = subprocess.run(probe, cwd=cwd, capture_output=True, text=True, timeout=4)
        except Exception:
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def repo_root_add_dir_args(kind: str, cwd: str) -> list[str]:
    """`--add-dir` argv granting a `developer` spawn the same VCS-repo-root
    scope its parent session already has, when the spawn's cwd (the
    resolved `--workdir`, or the parent process's own cwd when unset — see
    `main`) sits strictly below that root.

    Difficulty removed: a monorepo mount can hold several product subtrees
    under one repo_root (e.g. `team-a/service` and a sibling
    `team-b/tool/...`). The parent session's own trust boundary
    (session_scope, repo_root-granular) already covers the whole mount, but a
    spawned developer's workspace defaults to just its cwd, so a stage whose
    declared deliverable legitimately lives in a sibling subtree hits a
    permission wall the parent was never actually going to hit. Granting
    repo_root here does not widen trust past what the parent already holds —
    it only propagates the parent's own already-established boundary down to
    the child. Scoped to `developer` only: read-only kinds (thinker,
    code-reviewer) don't write outside their brief, and `planner` writes only
    its own plan file (see PLANS_WRITE_KINDS)."""
    if kind != "developer":
        return []
    root = _vcs_root(cwd)
    if not root or os.path.normpath(root) == os.path.normpath(cwd):
        return []
    return ["--add-dir", root]


def repo_root_deny_rules(kind: str, cwd: str) -> list[str]:
    """Guard `Edit` DENY rules for a `developer` spawn's VCS repo root
    (finding S10): under `acceptEdits`/`auto` permission modes a `developer`
    child's own cwd is already unguarded-writable with no `Edit` allow rule
    required, and `repo_root_add_dir_args` widens that further to the whole
    repo root when cwd sits strictly below it — either way the spawn reaches
    `.claude/`, `settings*.json` and `.git/` anywhere under the repo root
    with no guard, the same surface `stage_grant_rules` already denies for a
    declared WRITE add_dir. Mirrors that function's four-glob shape exactly.

    Round-2 should-fix (S10 deny gap): this used to return `[]` whenever
    `repo_root_add_dir_args` did, which included the `cwd == root` case —
    but that case needs the SAME denies, not none: no `--add-dir` grant is
    needed to reach a directory the child already starts in, so the deny
    pairing must not be conditioned on that grant having fired. Fires
    whenever `cwd` sits inside a VCS repo (root found at all), independent
    of whether root strictly exceeds cwd."""
    if kind != "developer":
        return []
    root = _vcs_root(cwd)
    if not root:
        return []
    base = grants.rule_file_arg(root.rstrip("/"))
    return [
        f"Edit({base}/**/.claude/**)",
        f"Edit({base}/**/settings*.json)",
        f"Edit({base}/**/.git/**)",
        f"Edit({base}/**/.git)",
    ]


def project_settings_permission_rules(project_settings_file: "Path | str | None") -> tuple[list[str], list[str]]:
    """(allow, deny) lifted from a target project's own `.claude/settings.local.json`
    `permissions.allow`/`permissions.deny` arrays — the same shape the harness
    itself reads for an ordinary (unspawned) session in that project.

    This is deliberately NOT `permissions_digest`/`--project-permissions`: that
    mechanism reads a `permissions/*.json` AUDIT-LOG file (pattern/granted_at/
    context records) and embeds a prose digest into the PROMPT — it never
    reaches the child's actual `--settings` grant, so a developer spawned with
    only that flag remains exactly as sandboxed as one spawned without it. A
    project that has already, deliberately, allow-listed its own build/test
    commands in `.claude/settings.local.json` gets no benefit from that
    grant unless those entries reach the `--settings` JSON directly, which is
    what this function is for (see dispatch-project-permissions leaf).

    Fails open to `([], [])` — a missing file, a directory, or unparseable /
    unexpected-shape JSON changes nothing rather than crashing the spawn; the
    project simply gets no extra grant, same as before this function existed.
    """
    if project_settings_file is None:
        return [], []
    # Accept either a Path or a bare str (the real call site always passes a
    # Path; a caller that already has the string form -- e.g. a test -- should
    # not have to know that distinction).
    try:
        data = json.loads(Path(project_settings_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return [], []
    allow = permissions.get("allow", [])
    deny = permissions.get("deny", [])
    allow = [r for r in allow if isinstance(r, str)] if isinstance(allow, list) else []
    deny = [r for r in deny if isinstance(r, str)] if isinstance(deny, list) else []
    # Finding S10: these are literal Bash/Edit/etc rule strings straight from
    # a target project's own settings.local.json, never routed through the
    # validator before reaching the child's --settings -- a project's own
    # allow-list is not more trustworthy than a declared/derived/runtime
    # grant, so a rule validate_rule refuses (a wildcarded interpreter/wrapper,
    # `.claude`/`.git`/settings-file coverage, etc) must not pass through here
    # either. Only `allow` is filtered: a `deny` entry only narrows, so an
    # unparseable one is dropped harmlessly rather than refusing the whole
    # rule (deny's directory-glob shapes are exactly what validate_rule
    # categorically refuses -- see stage_grant_rules).
    allow = [r for r in allow if _validates(r)]
    return allow, deny


def _validates(rule: str) -> bool:
    try:
        grants.validate_rule(rule)
    except grants.GrantValidationError:
        return False
    return True


def stage_grant_rules(entries: list[dict]) -> tuple[list[str], list[str]]:
    """(allow, deny) rule strings materialized from a flat stage-grants list
    (agentctl `cmd_stage_grants`'s `.data["grants"]` shape: each entry
    carries either `"rule"`, or `"path"`+`"mode"`, plus `"provenance"` —
    provenance is not consumed here, only by `stage_grant_provenance_lines`
    for the prompt header). A `"rule"` entry passes through
    `grants.validate_rule` here too — an engine grant is never trusted more
    than a declared one materialized straight from the plan TOML, per this
    module's own validate-at-every-entry-point invariant.

    A `path`+`mode` entry (an add_dir grant) is validated via
    `grants.validate_add_dir`, never `grants.validate_rule`: a directory-glob
    Edit/Read rule (`Edit(//<path>/**)`) is categorically refused by
    `grants.validate_rule` (unbounded/unpredictable path expansion — see its
    `_GLOB_METACHARS` check), so a `write` add_dir's ALLOW rule is
    synthesized directly here, from the plain path string
    `grants.validate_add_dir` already accepted, bypassing `validate_rule`
    entirely — `validate_add_dir`'s own write-mode checks (glob/absolute/
    `.git`/launch-surface) are what stand in for it. The synthesized allow
    is paired with four guard DENYs (`.claude/`, `settings*.json`, `.git/`,
    `.git`) under the same prefix, since `--add-dir` alone would otherwise
    hand the child raw filesystem write into those without the kind's own
    baseline denies (which only ever cover this repo's and the plans dir's
    own such paths, never an arbitrary declared add_dir). A `read` add_dir
    DOES pair with a synthesized Edit DENY only, mirroring
    `plans_permission_rules`' own directional-pair pattern — but that DENY
    is never passed through `grants.validate_rule` (deny rules are outside
    its scope by design; `grants.validate_grants` itself iterates only
    `allow` and `add_dirs`), so the same glob shape that would refuse an
    allow rule is fine here."""
    allow: list[str] = []
    deny: list[str] = []
    for entry in entries:
        rule = entry.get("rule")
        if rule is not None:
            grants.validate_rule(rule)
            allow.append(rule)
            continue
        path = entry.get("path")
        mode = entry.get("mode")
        if path is None or mode is None:
            continue
        grants.validate_add_dir(path, mode)
        base = grants.rule_file_arg(path.rstrip("/"))
        if mode == "read":
            deny.append(f"Edit({base}/**)")
        elif mode == "write":
            allow.append(f"Edit({base}/**)")
            deny.extend(
                [
                    f"Edit({base}/**/.claude/**)",
                    f"Edit({base}/**/settings*.json)",
                    f"Edit({base}/**/.git/**)",
                    f"Edit({base}/**/.git)",
                ]
            )
    return allow, deny


def stage_grant_add_dir_args(entries: list[dict]) -> list[str]:
    """`--add-dir` argv for every `path`+`mode` entry in a flat stage-grants
    list — a permissions.allow rule alone does not put a directory outside
    the child's cwd into its workspace (same reasoning as
    `plans_add_dir_args`)."""
    args: list[str] = []
    for entry in entries:
        path = entry.get("path")
        mode = entry.get("mode")
        if path is None or mode is None:
            continue
        grants.validate_add_dir(path, mode)
        args.extend(["--add-dir", path])
    return args


def _paths_from_add_dir_argv(argv: list[str]) -> list[str]:
    """Recover the bare directory paths from a flat `["--add-dir", path, ...]`
    argv list (the shape every `*_add_dir_args` helper returns), for the
    prompt's File-access scope section, which lists paths, not argv."""
    return argv[1::2]


def stage_grant_provenance_lines(entries: list[dict]) -> list[str]:
    """One `<destination> — <provenance>` markdown bullet per stage-grant
    entry, for `assemble_prompt`'s File-access scope section — the brief
    arrives with not just WHAT it may access but WHY (declared by the plan
    author, derived from its own verify_command/output_artifacts, or
    granted at runtime for this one stage)."""
    lines = []
    for entry in entries:
        dest = entry.get("rule") or f"{entry.get('path')} ({entry.get('mode')})"
        lines.append(f"- `{dest}` — {entry.get('provenance', 'unknown')}")
    return lines


class GrantShadowError(ValueError):
    """A write add_dir grant lands inside a directory another source (the
    plans-dir deny, a target project's own deny) has already denied Edit
    onto — the two grants only meet inside `build_child_settings`, since
    each source validates independently and neither knows about the
    other's rules."""


def _deny_rule_directory_prefix(deny_rule: str) -> "str | None":
    """The absolute directory `deny_rule` denies Edit onto, if `deny_rule`
    is an `Edit(//<prefix>/**)` glob deny; `None` for any other shape
    (a single-file deny, a non-Edit rule, ...) — those can never shadow an
    entire add_dir."""
    parsed = grants.rule_program_and_arg(deny_rule)
    if parsed is None or parsed[0] != "Edit":
        return None
    tool_arg = parsed[1]
    if not tool_arg.startswith("//") or not tool_arg.endswith("/**"):
        return None
    return grants.rule_file_path(tool_arg[: -len("/**")])


def _check_write_add_dirs_not_shadowed(entries: list[dict], existing_deny: list[str]) -> None:
    """Refuse a `write` add_dir entry whose path is-or-is-under a directory
    an already-accumulated deny rule covers — materializing the ALLOW
    anyway would silently lose to a later-applied deny at best, and at
    worst the two-rule ordering the harness resolves is not something this
    module controls, so the shadow is refused outright rather than
    trusted to resolve safely."""
    for entry in entries:
        if entry.get("mode") != "write":
            continue
        path = entry.get("path")
        if path is None:
            continue
        norm_path = str(Path(path))
        for deny_rule in existing_deny:
            prefix = _deny_rule_directory_prefix(deny_rule)
            if prefix is None:
                continue
            if norm_path == prefix or norm_path.startswith(prefix.rstrip("/") + "/"):
                raise GrantShadowError(
                    f"write add_dir {path!r} is shadowed by existing deny rule "
                    f"{deny_rule!r} — refused"
                )


def build_child_settings(
    kind: str,
    plans_directory: "Path | None" = None,
    project_settings_file: "Path | None" = None,
    engine_grants: "list[dict] | None" = None,
    workdir: "str | None" = None,
) -> dict:
    """Child `--settings` payload: the auto-compaction window pin for every kind
    (both forms, mirroring settings/base.json — the env key wins in the client's
    window resolution, the top-level key is the settings-path fallback), plus
    every kind's `KIND_BASELINES` grant (uniform for all kinds — no
    kind-gate here; `KIND_BASELINES.get(kind, KIND_BASELINES["default"])`
    covers an unknown kind too), plus the plans-directory grant for the
    kinds that need it, plus — for kind=="developer" only — the target
    project's own `.claude/settings.local.json` permissions.allow/deny (see
    project_settings_permission_rules): a spawned developer inherits the
    same project-scope grants an interactive session in that project
    already has, instead of being limited to the fleet-wide developer
    baseline, which is scoped to this repo's own verifiers and knows
    nothing about a target project's build/test commands. Plus, when
    `engine_grants` is given (the stage's own declared/derived/runtime
    grant set — see `load_engine_stage_grants`), those too.

    `grants.validate_rule` gates the baseline and `engine_grants` sources
    directly here — the four categories the stage-grants model actually
    names (baseline/declared/derived/runtime) — plus `project_allow`, which
    `project_settings_permission_rules` now validates entry-by-entry itself
    before returning (a lifted project rule is not more trustworthy than a
    declared/derived/runtime one). `plans_allow` is the one remaining
    exemption: its directory-glob rule shape (`Read(//<dir>/**)`)
    `grants.validate_rule` categorically refuses — see `stage_grant_rules` —
    so it keeps its own, separate acceptance path rather than being routed
    through the same validator.

    `workdir`, when given, feeds `repo_root_deny_rules` (finding S10): for a
    `kind=="developer"` spawn whose cwd sits below its VCS root,
    `repo_root_add_dir_args` already grants that whole root via `--add-dir`
    (see `main`'s call site) — this pairs that grant with the same guard
    DENYs a declared WRITE add_dir gets, so the widened filesystem surface
    doesn't reach `.claude/`, `settings*.json` or `.git/` unguarded."""
    settings: dict = {
        "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(SPAWN_AUTOCOMPACT_WINDOW_TOKENS)},
        "autoCompactWindow": SPAWN_AUTOCOMPACT_WINDOW_TOKENS,
    }
    allow: list[str] = []
    deny: list[str] = []
    baseline = KIND_BASELINES.get(kind, KIND_BASELINES["default"])
    for rule in baseline:
        grants.validate_rule(rule)
    allow.extend(baseline)
    if kind in PROJECT_SETTINGS_KINDS:
        project_allow, project_deny = project_settings_permission_rules(project_settings_file)
        allow.extend(project_allow)
        deny.extend(project_deny)
    if plans_directory is not None:
        plans_allow, plans_deny = plans_permission_rules(kind, plans_directory)
        allow.extend(plans_allow)
        deny.extend(plans_deny)
    if engine_grants:
        _check_write_add_dirs_not_shadowed(engine_grants, deny)
        engine_allow, engine_deny = stage_grant_rules(engine_grants)
        allow.extend(engine_allow)
        deny.extend(engine_deny)
    if workdir is not None:
        deny.extend(repo_root_deny_rules(kind, workdir))
    permissions: dict = {}
    if allow:
        permissions["allow"] = allow
    if deny:
        permissions["deny"] = deny
    if permissions:
        settings["permissions"] = permissions
    return settings


def load_engine_stage_grants(
    session_id: str,
    stage_index: int,
    kind: str,
    state_root: "Path | None" = None,
) -> "list[dict] | None":
    """The stage's effective grant set (declared + derived + unconsumed
    runtime) from the agentctl engine, or `None` when `kind` disagrees with
    the stage's own declared executor (a spawn whose --kind is not this
    stage's authorized actor gets no engine grants — its KIND_BASELINES-only
    settings are all it receives) or the session/stage cannot be resolved.

    Direct in-process import of `agentctl.cli`/`agentctl.store`, mirroring
    this file's existing `agentctl.plan`/`agentctl.render` import
    precedent, rather than a subprocess call to the `agentctl` CLI
    executable — spawn-specialist.py already trusts the engine's Python
    surface directly for the plan-brief projection, so the grant read uses
    the same seam instead of introducing a second, shell-mediated one."""
    from agentctl import cli as agentctl_cli
    from agentctl.store import FileStateStore

    store = FileStateStore(state_root) if state_root is not None else FileStateStore()
    try:
        directive = agentctl_cli.cmd_stage_grants(
            argparse.Namespace(session=session_id, stage=stage_index, json=True),
            store=store,
        )
    except KeyError:
        return None
    if not directive.ok:
        return None
    if directive.data.get("executor") != f"spawn:{kind}":
        return None
    return directive.data.get("grants", [])


def _project_dir_name(cwd: str) -> str:
    """Directory name the harness uses under `<config root>/projects/` for a
    given cwd — every byte outside `[A-Za-z0-9]` (path separators, dots,
    tildes) becomes `-`, mirroring the harness's own encoding."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _iter_workdir_transcripts(workdir: str) -> list[Path]:
    """Every `*.jsonl` transcript (including per-session `subagents/` ones)
    under the `--workdir`-scoped project directory, across all config roots.

    Scoping by workdir — rather than scanning every project directory and
    picking the globally freshest file — is what makes discovery correct when
    an unrelated session is writing its own transcript concurrently: that
    session's project directory is a different one (unless it shares this
    exact workdir), so its transcript is never a candidate here regardless of
    its mtime.
    """
    name = _project_dir_name(workdir)
    out: list[Path] = []
    for root in projects_roots():
        out.extend((root / name).glob("**/*.jsonl"))
    return sorted(out)


def _snapshot_transcripts(workdir: str) -> set[Path]:
    """Set of the `--workdir`-scoped project's `*.jsonl` transcripts that exist
    right now, across both config roots — a child spawned by a bare-`claude`
    manager writes under the HARNESS root, so reading only the agent root made
    the diff below always empty."""
    return set(_iter_workdir_transcripts(workdir))


def _discover_transcript_path(workdir: str, known_before: set[Path], timeout: float = 10.0) -> Path | None:
    """Find a new transcript under the `--workdir`-scoped project directory that
    didn't exist before the spawn. Polls every 0.5s up to `timeout` seconds.

    Filtering by "not in known_before" avoids picking up a transcript this same
    workdir's project directory already held from an earlier spawn. Returns the
    freshest new jsonl, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates: list[tuple[float, Path]] = []
        for p in _iter_workdir_transcripts(workdir):
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


def _load_json_payload(text: str) -> "dict | None":
    """Best-effort JSON parse of the child's stdout. None on any parse
    failure (empty stdout from a communicate() exception, truncated output,
    plain-text output) — every caller already has a degrade path for "no
    payload"."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def resolve_child_session_id(stdout_text: str, transcript_path: "Path | None") -> "str | None":
    """Child spawn's session id, for scope deregistration on exit.

    The result JSON's session_id field wins — it's authoritative and
    available even when transcript discovery timed out. The transcript
    filename stem is the fallback (the harness names each transcript
    <session-id>.jsonl), recovering the id when stdout never parsed as JSON,
    e.g. the child was killed before writing a result.
    """
    payload = _load_json_payload(stdout_text)
    if payload:
        sid = payload.get("session_id")
        if sid:
            return str(sid)
    return transcript_path.stem if transcript_path is not None else None


def deregister_child_scope(
    session_id: "str | None", scopes_dir: "Path" = registry.DEFAULT_SCOPES_DIR
) -> None:
    """Remove the child spawn's scope registration on exit, so a dead
    session never blocks a writer for the rest of its heartbeat TTL (stage
    1's pid probe narrows that window; this closes it outright for the
    common supervised-exit case, leaving TTL/probe expiry as the fallback
    for a supervisor that itself dies mid-spawn).

    Best-effort: an unresolved session_id or any deregistration failure is
    swallowed and logged to stderr — it must never affect the spawn's own
    exit code.
    """
    if not session_id:
        return
    try:
        registry.delete(scopes_dir, session_id)
    except Exception as exc:  # noqa: BLE001 - must never affect the spawn's own exit code
        print(
            f"spawn-specialist: scope deregistration failed for session {session_id}: {exc}",
            file=sys.stderr,
        )


def resolve_permission_mode(args: argparse.Namespace) -> str:
    """Pick the permission mode passed to `claude -p`.

    Default policy: the developer and tech-writer specializations need
    unattended Read/Grep/Write in a trusted local mount, so use `acceptEdits`
    — the narrowest mode granting exactly that. Every other kind — including
    thinker/planner/code-reviewer and any kind outside KIND_BASELINES — gets
    `default` (interactive-prompt semantics), since they are mostly read-only
    and any write they need goes through an explicit, reviewable grant
    instead.

    NOT bypassPermissions, for two independent reasons. It is far wider than the
    need: it waives EVERY permission class, not only file writes. And on a fleet
    whose managed layer sets `permissions.disableBypassPermissionsMode`, asking for
    it is silently ignored and the child falls back to the settings `defaultMode` —
    so a spawn that LOOKED unattended in fact ran under prompts nobody could answer.
    That pair cost a ~40-minute six-spawn deadlock on 2026-08-04, and the flag being
    inert is precisely why it misdirected the diagnosis. `acceptEdits` is narrower
    AND actually takes effect.

    Any capability beyond file writes belongs in an explicit, reviewable grant
    (`KIND_BASELINES`), never in a blanket waiver. `--permission-mode` itself is
    narrowed at the CLI layer to `default`/`plan` only — the wider modes below
    are resolved here, never accepted as a caller-supplied flag value.

    User-supplied `--permission-mode` always wins.
    """
    if args.permission_mode is not None:
        return args.permission_mode
    if args.kind in ("developer", "tech-writer"):
        return "acceptEdits"
    return "default"


def _build_extraction(result_text: str, kind: str) -> "marker_extract.Extraction | None":
    """The call site's guard, factored out so a test can drive it directly
    without invoking main()'s subprocess plumbing. The shared implementation
    (``marker_extract.build_extraction``) runs the pass unconditionally
    whenever it can, not only after the legacy any-line regex scan failed."""
    return marker_extract.build_extraction(result_text, kind=kind)


# The CHILD's own terminal condition, distinct from every marker/extraction
# outcome above: these two classes mean the child never got far enough to
# answer at all, so asking the marker question about its output is asking the
# wrong question (issue #78, #80 — see CHILD_OUTCOME_NOTE below). CHILD_ANSWERED
# is not a signature match; it is what a genuine terminal marker forces
# regardless of any match (see _resolve_child_outcome's precedence rule).
CHILD_ANSWERED = "CHILD_ANSWERED"
CHILD_INFRA_FAILURE = "CHILD_INFRA_FAILURE"
CHILD_EXHAUSTED = "CHILD_EXHAUSTED"

# Signatures the `claude` CLI itself emits on its own stdout/stderr when a run
# never reaches (or loses) the API, or is refused outright for size before it
# can answer. These are STRUCTURAL strings a host CLI emits about its own
# process, not free text a model authored, so matching them here is parsing a
# machine's own output rather than classifying meaning (the exception this
# repo's regex-not-for-semantic-classification rule carves out) — and the
# match only ever shapes a RECOMMENDATION (see _resolve_child_outcome), never
# suppresses a result the child produced. One entry per family; each cites the
# observed instance that put it here. Additive: an unmatched failure falls
# back to today's NO_MARKER/EXTRACTOR_* handling, never to a guess.
_CHILD_OUTCOME_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Issue #78, 2026-08-13 dispatch: died on
    # "API Error: Unable to connect to API (ENOTFOUND)" after 24.7 minutes and
    # $0.68 — the child never reached a model turn.
    (CHILD_INFRA_FAILURE, ("API Error", "Unable to connect to API", "ENOTFOUND")),
    # Issue #80, 2026-08-16 dispatch: ran 28.4 minutes and $5.52 and was
    # refused outright with "Prompt is too long", nothing committed.
    (CHILD_EXHAUSTED, ("Prompt is too long",)),
)

CHILD_OUTCOME_NOTE: dict[str, str] = {
    CHILD_INFRA_FAILURE: (
        "the child spawn never reached, or lost, the API before exiting — a "
        "transient infrastructure condition, not a judgement about the "
        "specialist's output. Recommended move: retry the same dispatch."
    ),
    CHILD_EXHAUSTED: (
        "the child spawn was refused for size (a context/prompt-size limit) "
        "before it could answer — a resource condition, not a judgement about "
        "the specialist's output. Recommended move: a reduced brief, or the "
        "re-attest path for a stage the child may have partly completed."
    ),
}


def classify_child_outcome(stdout: str, stderr: str, returncode: int) -> tuple[str, str | None]:
    """The high-recall signature scan alone, with no knowledge of whether a
    marker was found — ``returncode`` is accepted for a future signature
    keyed on exit code but unused today; every current signature is a
    substring of the CLI's own stdout/stderr. Returns
    ``(CHILD_INFRA_FAILURE | CHILD_EXHAUSTED, matched_substring)`` or
    ``(CHILD_ANSWERED, None)`` when nothing matches. Callers that need the
    marker-precedence rule applied use ``_resolve_child_outcome`` instead —
    this function alone does NOT know a marker exists and must never be
    treated as the final verdict."""
    haystack = f"{stdout}\n{stderr}"
    for outcome, signatures in _CHILD_OUTCOME_SIGNATURES:
        for signature in signatures:
            if signature in haystack:
                return outcome, signature
    return CHILD_ANSWERED, None


def _resolve_child_outcome(
    stdout: str, stderr: str, returncode: int, marker: str | None
) -> tuple[str, str | None]:
    """Apply the precedence rule the prefilter's legitimacy rests on: a
    genuine terminal marker ALWAYS outranks a signature match. ``marker`` is
    ``check_planner_return``'s parsed marker (``None`` when no line carried
    one) — the same signal that already decided whether the specialist's
    output was routable. A signature can therefore only relabel a run that
    was already going to MALFORMED/NO_MARKER; it can never discard or
    override a result the child produced."""
    if marker is not None:
        return CHILD_ANSWERED, None
    return classify_child_outcome(stdout, stderr, returncode)


def _child_outcome_envelope(outcome: str, matched: str, result_text: str, stderr: str) -> str:
    """The envelope for a CHILD_INFRA_FAILURE / CHILD_EXHAUSTED run: states
    what happened to the RUN, never that the output was malformed. Falls back
    to stderr for the preserved body when the child's stdout never carried
    anything (the common shape for both documented instances)."""
    body = result_text if result_text.strip() else stderr
    return f"{outcome}: {CHILD_OUTCOME_NOTE[outcome]} (matched {matched!r}).\n\n{body}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    bound_host = os.environ.get("AGENTCTL_RUNTIME_HOST")
    if bound_host == "cursor":
        print(
            "error: AGENTCTL_RUNTIME_HOST=cursor; refusing to spawn a Claude "
            "`claude -p` specialist from a Cursor-bound session. Use "
            "spawn-cursor-specialist.py instead.",
            file=sys.stderr,
        )
        log_refused("cross-host", {"kind": args.kind, "bound_host": bound_host, "this_host": "claude"})
        return 5

    if not argv_text.is_readable_file(args.plan):
        print(argv_text.file_arg_error("--plan", args.plan), file=sys.stderr)
        log_refused("plan-not-found", {"kind": args.kind, "plan": argv_text.abbreviate(args.plan)})
        return 2
    skill = skill_path(args.kind)
    if not skill.exists():
        print(f"error: unknown specialization (no SKILL.md at {skill})", file=sys.stderr)
        log_refused("unknown-kind", {"kind": args.kind})
        return 2

    if args.project_settings is not None and tuple(args.project_settings.parts[-2:]) != (".claude", "settings.local.json"):
        print(
            f"error: --project-settings must be a path ending in .claude/settings.local.json, "
            f"got {args.project_settings} — refusing to merge an arbitrary file's permissions "
            f"into this child's --settings grant",
            file=sys.stderr,
        )
        log_refused("project-settings-path-shape", {"kind": args.kind})
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

    # The tier value is now a telemetry LABEL (recorded on the spawn-costs row,
    # used for the soft-warn), NOT the applied kill-cap. The cap passed to
    # `claude -p --max-budget-usd` is the single global runaway ceiling, so the
    # kill fires only on a true runaway, not on legitimate large work.
    tier_label_usd = budget_value(args.budget, constants)
    cap = runaway_ceiling(constants)

    if args.workdir is not None and not args.workdir.is_dir():
        print(f"error: --workdir does not exist or is not a directory: {args.workdir}", file=sys.stderr)
        log_refused("workdir-not-found", {"kind": args.kind, "workdir": str(args.workdir)})
        return 2
    workdir = str(args.workdir) if args.workdir is not None else os.getcwd()

    plans_directory = plans_dir()
    # engine_grants stays None unless BOTH --session and --stage-index are given
    # AND the engine's own executor field for that stage matches --kind (checked
    # inside load_engine_stage_grants) -- a mismatch or unknown session falls
    # back to kind-baseline-only settings rather than failing the spawn.
    engine_grants: "list[dict] | None" = None
    if args.session is not None and args.stage_index is not None:
        engine_grants = load_engine_stage_grants(
            args.session, args.stage_index, args.kind, state_root=args.state_root
        )

    add_dir_argv: list[str] = []
    add_dir_argv.extend(plans_add_dir_args(args.kind, plans_directory))
    add_dir_argv.extend(repo_root_add_dir_args(args.kind, workdir))
    if engine_grants:
        add_dir_argv.extend(stage_grant_add_dir_args(engine_grants))
    add_dir_paths = _paths_from_add_dir_argv(add_dir_argv)

    permission_mode = resolve_permission_mode(args)

    perms = permissions_digest(args.project_permissions)
    try:
        prompt = assemble_prompt(
            args,
            depth_next,
            perms,
            workdir=workdir,
            permission_mode=permission_mode,
            add_dir_paths=add_dir_paths,
            stage_grant_entries=engine_grants,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        log_refused(
            "stage-brief-error",
            {"kind": args.kind, "plan": argv_text.abbreviate(args.plan), "stage_index": args.stage_index},
        )
        return 2
    model = resolve_model(args)

    ceiling_chars = dispatch_prompt_ceiling_chars(model)
    if prompt_exceeds_ceiling(prompt, model):
        measured = len(prompt)
        print(
            f"error: assembled prompt is {measured} chars, exceeding the "
            f"{ceiling_chars}-char ({dispatch_prompt_ceiling_tokens(model)}-token) "
            f"pre-spawn refusal ceiling for this child (resolved to --model {model}); "
            f"refusing before spawning. Shrink constraints/dossier, or dispatch with "
            f"--plan-brief if not already set.",
            file=sys.stderr,
        )
        log_refused(
            "prompt-too-large",
            {
                "kind": args.kind,
                "chars": measured,
                "ceiling_chars": ceiling_chars,
                "model": model,
                "plan_brief": getattr(args, "plan_brief", False),
                "stage_index": args.stage_index,
            },
        )
        return 5

    try:
        child_settings = build_child_settings(
            args.kind, plans_directory, args.project_settings, engine_grants, workdir=workdir
        )
    except GrantShadowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        log_refused("grant-shadowed", {"kind": args.kind, "stage_index": args.stage_index})
        return 2

    cmd = [
        "claude",
        "-p",
        "--append-system-prompt-file",
        str(composed_system_prompt_file(skill)),
        "--max-budget-usd",
        cap,
        "--output-format",
        "json",
        # Pass the child's autocompact window pin via --settings (highest in the
        # settings precedence ladder) so it beats the same key in
        # ~/.claude/settings.json. Setting it in the child's process env does NOT
        # work: settings.json env is applied after process start and wins (see
        # memory-global leaf claude-code-settings-env-precedence.md).
        "--settings",
        json.dumps(child_settings),
    ]
    cmd.extend(add_dir_argv)
    if permission_mode is not None:
        cmd.extend(["--permission-mode", permission_mode])
    cmd.extend(["--model", model])
    cmd.extend(["--effort", args.effort])
    # The prompt is NOT appended to argv: with the plan inlined it exceeds Linux
    # MAX_ARG_STRLEN (32 pages = 131072 bytes for a single argv string), which execve
    # rejects with E2BIG before the child starts. It travels via stdin instead (see
    # the launch below); `claude -p` reads its prompt from stdin.

    if args.dry_run:
        print("=== assembled prompt (delivered via stdin) ===")
        print(prompt)
        print("\n=== command (not executed) ===")
        # The prompt is piped via stdin, so it is not a member of cmd; print cmd
        # verbatim and note the stdin payload size separately.
        print(" ".join(repr(c) if " " in c else c for c in cmd))
        print(f"# stdin: <prompt {len(prompt)} chars>")
        return 0

    if shutil.which("claude") is None:
        print("error: `claude` not on PATH; cannot spawn. Re-run with --dry-run to inspect the prompt.", file=sys.stderr)
        return 4

    env = {
        **os.environ,
        "AGENT_RECURSION_DEPTH": str(depth_next),
        "AGENT_LINEAGE_IDS": build_child_lineage(
            os.environ.get("AGENT_LINEAGE_IDS", ""),
            os.environ.get("CLAUDE_CODE_SESSION_ID"),
        ),
    }

    # Snapshot existing transcripts BEFORE spawning so we can identify the
    # child's new jsonl (the parent manager's own live transcript would
    # otherwise win on mtime).
    transcripts_before = _snapshot_transcripts(workdir)
    started = time.monotonic()

    # Use Popen so we can print the child's transcript path to stderr early —
    # the parent (manager) can then tail it for monitoring while we block on
    # the child's final JSON output. launch_supervised makes the child a
    # session/process-group leader; install_teardown then reaps that whole group
    # if this wrapper is killed (the harness sends SIGTERM ~5s before SIGKILL, and
    # a manual `kill` of the wrapper lands the same SIGTERM), so the claude -p
    # subtree is never orphaned.
    proc = proc_tree.launch_supervised(
        cmd, env=env, cwd=workdir, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    proc_tree.install_teardown(proc)
    transcript_path: Path | None = None
    stdout_str, stderr_str = "", ""

    # Transcript discovery must run OFF the main thread: the child does not create
    # its transcript until it has read the prompt, and the prompt is only written
    # when communicate() runs below — so a discover-then-communicate ordering (the
    # old argv path could afford, because the prompt was already on the command
    # line) would always miss. The daemon thread polls for the transcript while
    # communicate() feeds stdin and drains stdout/stderr concurrently; communicate
    # is what avoids the pipe-buffer deadlock a manual write()+read() would risk.
    def _announce_transcript() -> None:
        nonlocal transcript_path
        transcript_path = _discover_transcript_path(workdir, transcripts_before, timeout=10.0)
        if transcript_path is not None:
            print(f"spawn-specialist: transcript={transcript_path}", file=sys.stderr, flush=True)
        else:
            print("spawn-specialist: transcript=<not-found-within-10s>", file=sys.stderr, flush=True)

    announcer = threading.Thread(target=_announce_transcript, daemon=True)
    child_session_id: "str | None" = None
    try:
        announcer.start()
        stdout_str, stderr_str = proc.communicate(input=prompt)
        announcer.join(timeout=11.0)
    finally:
        # Normal completion already reaped the child (no-op here); any abnormal
        # exit (exception, KeyboardInterrupt, timeout) still tears down the whole
        # subtree instead of leaking the claude -p children. Scope deregistration
        # lives in this same finally (not after it) so it fires on every exit
        # path — including one that raises before the result JSON is parsed
        # below — using whatever of {stdout, transcript_path} it managed to get.
        # child_session_id is captured to the outer variable here (rather than
        # recomputed later) so the spawn-costs ledger row below can carry it
        # without a second resolve_child_session_id call.
        proc_tree.kill_tree(proc)
        child_session_id = resolve_child_session_id(stdout_str, transcript_path)
        deregister_child_scope(child_session_id)
    completed = subprocess.CompletedProcess(args=cmd, returncode=proc.returncode, stdout=stdout_str, stderr=stderr_str)
    duration_ms = int((time.monotonic() - started) * 1000)

    cost_usd: float | None = None
    result_text = completed.stdout
    payload = _load_json_payload(completed.stdout)
    if payload:
        # Tolerant field lookup — schema may differ across versions.
        result_text = payload.get("result") or payload.get("output") or completed.stdout
        cost_usd = payload.get("cost_usd") or payload.get("total_cost_usd")

    extraction = _build_extraction(result_text, args.kind)
    forwarded, ok, parsed_marker = check_planner_return(
        result_text, args.kind, extraction=extraction
    )

    # Classify the CHILD's own terminal condition BEFORE trusting the marker
    # question's answer: a run that never reached the API or was refused for
    # size answers "no marker" for a reason that has nothing to do with the
    # specialist's output being unparseable. Precedence is enforced inside
    # _resolve_child_outcome — a found marker always wins, so this can only
    # relabel a run already headed to MALFORMED/NO_MARKER, never suppress one.
    child_outcome, child_matched = _resolve_child_outcome(
        completed.stdout, completed.stderr, completed.returncode, parsed_marker
    )
    if child_outcome != CHILD_ANSWERED:
        forwarded = _child_outcome_envelope(child_outcome, child_matched, result_text, completed.stderr)

    sys.stdout.write(forwarded)
    if not forwarded.endswith("\n"):
        sys.stdout.write("\n")

    # outcome_class defaults to the extraction's own verdict; a matched CHILD_*
    # signature overrides it — this stage's whole point is that "the extractor
    # judged NO_MARKER" is the wrong report when the child never ran at all.
    outcome_class = extraction.outcome if extraction is not None else None
    if child_outcome != CHILD_ANSWERED:
        outcome_class = child_outcome

    log_cost_entry({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "spawn",
        "kind": args.kind,
        "budget_tier": args.budget,
        "budget_usd_cap": cap,
        "effort": args.effort,
        "depth": depth_next,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "return_marker": parsed_marker,
        "exit_code": completed.returncode,
        "malformed": not ok,
        "stage_index": args.stage_index,
        "plan_path": str(args.plan),
        "extractor_invoked": extraction is not None,
        "extractor_model": marker_extract.model() if extraction is not None else None,
        "extractor_degraded": extraction.degraded if extraction is not None else None,
        "extraction_reason": extraction.reason if extraction is not None else None,
        "outcome_class": outcome_class,
        "child_session_id": child_session_id,
        "transcript_path": str(transcript_path) if transcript_path is not None else None,
        **_spawn_tags(),
    })

    # Soft-warn (no kill, no exit-code change): realized cost exceeding a
    # multiple of the tier LABEL is a cheap calibration signal that the tier's
    # expected size is set too low for this kind of work. It never truncates the
    # spawn — the only kill is the runaway ceiling applied above.
    if cost_usd is not None:
        try:
            label_usd = float(tier_label_usd)
        except ValueError:
            label_usd = 0.0
        if label_usd > 0 and cost_usd > SOFT_WARN_MULT * label_usd:
            print(
                f"spawn-specialist: soft-warn: realized cost_usd={cost_usd} exceeds "
                f"{SOFT_WARN_MULT}x the '{args.budget}' tier label ({tier_label_usd}); "
                f"tier expected-size may be under-set (not a kill — cap is the runaway ceiling {cap}).",
                file=sys.stderr,
            )

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
        summary_bits.append(child_outcome if child_outcome != CHILD_ANSWERED else "MALFORMED")
    print(" ".join(summary_bits), file=sys.stderr)

    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
