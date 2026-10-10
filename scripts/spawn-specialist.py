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
import fnmatch
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
from typing import NamedTuple, Sequence

import proc_tree  # sibling module in scripts/; supervised launch + recursive teardown
from agentctl import grants  # the sole validator every materialized rule/add_dir passes through
from agentctl.plan import (  # parse the TOML plan for a single-stage brief projection
    PlanError,
    is_unit_id,
    load_plan,
    load_plan_with_digest,
    parse_pair,
    parse_unit,
)
from agentctl.render import (  # pure PlanDoc(+index) -> markdown; TopoUnitsCorrupt/materialize_topo_units do the one bit of I/O
    TopoUnitsCorrupt,
    materialize_topo_units,
    render_pair_review_bundle,
    render_stage_brief,
    render_unit_review_bundle,
    topo_pair_view,
    topo_pair_view_dirname,
)
from lib import argv_text  # one place decides how an argv value names its text
from lib import marker_extract  # unconditional second-pass marker extraction (model is the primary classifier)
from lib.config_root import agentctl_topo_units_dir, plans_dir, projects_roots, skills_dir  # config-root resolver (isolated system root)
from lib.kind_baselines import (  # re-exported below so `MOD.KIND_BASELINES` etc. keep working for importlib callers
    KIND_BASELINES,
    PLANNER_CHECK_ORDER_COVERAGE_RULE,
    PLANNER_LIST_DENIED_RULE,
    PLANNER_PLAN_GRANTS_RULE,
    SCRIPTS_DIR,
    baseline_for_workdir,
)
from lib.planner_plan_check import (  # single shared home for return-marker + plan checks
    MARKER_RE,
    PLAN_PATH_RE,
    RETURN_MARKERS,
    check_planner_return,
    extract_marker,
    markers_for_kind,
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
    evidence_dir: "str | None" = None,
    topo_bundle: "str | None" = None,
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
    resolved_plan = None if topo_bundle is not None else brief_plan_path(args)
    if topo_bundle is not None:
        plan_label = (
            "## Working plan — topological review unit or pair "
            "(projected; the full plan is never inlined for --review-topo — "
            "see § File-access scope for the per-pair view directory)"
        )
        plan_body = topo_bundle
    elif resolved_plan is not None:
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
    sections += [
        "## The engine belongs to the parent",
        "",
        "The `agentctl` session and plan driving this task are the parent's, not "
        "yours — never call a user-authority verb (AGENTCTL_USER_AUTHORITY_VERBS in "
        "scripts/lib/widening_targets.py: approve, resolve-permission, resolve, "
        "dispatch, record-result, replan, and the rest of that set). The read-only "
        "verbs this brief prescribes, and `classify`/`status` on your own "
        "session, are allowed. Keep cwd at your working directory root and run "
        "repo-relative commands without `cd`-ing first: `python3 "
        "<abs-path>/agentctl-cli.py <verb> ...` works from any cwd; the "
        "repo-relative `python3 scripts/agentctl-cli.py <verb> ...` only when your "
        "cwd already is the repo/worktree root. If your work needs a parent-engine "
        "action, return a marker "
        + (
            "(one of the markers your brief names)"
            if is_manager_kind(getattr(args, "kind", None))
            else "(see § Return markers)"
        )
        + " instead of calling it yourself.",
        "",
    ]
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
            "## Checkpoints",
            "",
            "Commit each checkpoint as soon as its own control goes green — don't "
            "batch unrelated checkpoints into one commit. Push the personal/ticket "
            "branch after each commit (pre-authorized; never a shared/trunk branch). "
            "Keep evidence (test output, command logs, intermediate artifacts) in the "
            "durable evidence directory below, not under `/tmp` or another OS-temp "
            "scratch root — a scratch root can be swept before anyone reviews it.",
            "",
            (
                f"Evidence directory for this stage: `{evidence_dir}`"
                if evidence_dir
                else "Evidence directory: none provided for this spawn (no --session/"
                "--stage-index) — keep evidence under your working directory instead."
            ),
            "",
        ]
    sections.append("If your work needs an action not covered, return PERMISSION-REQUEST: with the request.")
    if not is_manager_kind(getattr(args, "kind", None)):
        sections.append(
            "If you hit a small specific question whose answer is needed to continue, return CLARIFY: (see § Return markers)."
        )
    return "\n".join(sections)


# The empty specialization: a depth n+1 manager with no role SKILL.md and no
# appended marker protocol. Used by the overcome-difficulty escape.
MANAGER_KIND = "manager"


def is_manager_kind(kind: str | None) -> bool:
    return kind == MANAGER_KIND


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
    p.add_argument("--kind", required=True, help="specialization name (must exist at ~/.claude/skills/<kind>/SKILL.md); 'manager' is the empty specialization: a vanilla child with no role SKILL.md and no appended marker protocol, returning RESOLVED/INVESTIGATION/LOOP_DETECTED/PERMISSION-REQUEST")
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
        "--guard-exempt",
        action="append",
        type=Path,
        default=None,
        metavar="PATH",
        help="repeatable: one file whose own settings*.json guard deny is lifted (every "
        "other settings*.json stays denied; the .claude/.git guards are untouched). A "
        "relative PATH resolves against the child's working directory (--workdir, else "
        "this process's cwd); compared lexically, so spell it under the same root the "
        "repo-root/add_dir guards use. A '..' segment is refused",
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
    p.add_argument(
        "--review-topo",
        default=None,
        metavar="<pair>",
        help="materialize and dispatch ONE base-service pair review (a pair id "
        "'<base>-<service>' such as '3-1', 'plan-7' or 'base-plan') instead of "
        "the whole-plan/--plan-brief projection: the assembled prompt inlines "
        "the base's full brief, the service's declared product and the edge, "
        "and the child is granted read-only access to a per-plan-version view "
        "directory holding the service's FULL node file, reachable via one "
        "Read. Requires --kind thinker; refused together with --stage-index "
        "(it replaces that projection, not refines it) but may be combined "
        "with --plan-brief, which is a no-op here (--review-topo never "
        "requires --stage-index, so --plan-brief's own eligibility check never "
        "fires). Splits a plan too large for one whole-plan review spawn -- "
        "scripts/plan-review-topological.py is the planned whole-plan driver "
        "that walks every pair through this flag.",
    )
    p.add_argument(
        "--review-topo-history",
        default=None,
        metavar="<file>",
        help="JSON file holding the `agentctl plan-review-pair-history` data payload "
        "for the --review-topo pair: the bundle then carries the pair's prior "
        "verdicts and concerns and the parts changed since, for a re-review. "
        "Only valid with --review-topo; the spawn stays session-free.",
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
#     CORRECTION (stage 6/7, probe-hook-decision-semantics.py's
#     `add_dir:default_write_add_dir` cell): that probe run's child had an
#     effective mode of plain `default`, under which a write add_dir's
#     Edit(...) allow rule did not let the child write. The probe measured
#     that one child, not the fleet's settings. resolve_permission_mode
#     therefore forces `acceptEdits` whenever engine_grants carries a
#     mode="write" add_dir, and write_grant_cwd_deny_rules denies the cwd for
#     kinds not trusted with unattended writes, so neither depends on the
#     inherited defaultMode.
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


def parse_review_topo_pair(raw: str) -> str:
    """Return `--review-topo`'s value as the one pair id it names. Raises
    `ValueError` for a comma list — `--review-topo` names exactly one pair
    per spawn, never a batch; a whole-plan walk is a caller looping over
    spawns, not a wider value here. A comma-list is named explicitly in the
    message (as `--pairs`, the flag a future batch driver would use) rather
    than folded into the generic case, since a caller reaching for a list
    here is reaching for the wrong flag, not typing a malformed single id.
    Whether the id names a pair the plan has is `agentctl.plan.parse_pair`'s
    decision, taken once the plan is loaded."""
    if "," in raw:
        raise ValueError(
            f"--review-topo takes exactly one pair, not a comma list; a batch of pairs "
            f"is --pairs' job (not this flag), got: {raw!r}"
        )
    return raw


def review_topo_view_permission_rules(view_dir: Path) -> tuple[list[str], list[str]]:
    """(allow, deny) permission rules granting read-only access to a
    `--review-topo` pair's materialized view directory — mirrors
    `plans_permission_rules`' READ direction (`Read` allow paired with an
    `Edit` deny): `--add-dir` alone would otherwise make the directory
    writable under `acceptEdits` (see `resolve_permission_mode`).

    Raises ValueError if `view_dir` is not absolute."""
    if not view_dir.is_absolute():
        raise ValueError(f"view_dir must be absolute, got: {view_dir}")
    base = f"/{view_dir}/**"
    return [f"Read({base})"], [f"Edit({base})"]


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


def repo_root_deny_rules(kind: str, cwd: str, exempt_abs_paths: "Sequence[str]" = ()) -> list[str]:
    """Guard `Edit` DENY rules for a `developer` spawn's VCS repo root
    (finding S10): under `acceptEdits`/`auto` permission modes a `developer`
    child's own cwd is already unguarded-writable with no `Edit` allow rule
    required, and `repo_root_add_dir_args` widens that further to the whole
    repo root when cwd sits strictly below it — either way the spawn reaches
    `.claude/`, `settings*.json` and `.git/` anywhere under the repo root
    with no guard, the same surface `stage_grant_rules` already denies for a
    declared WRITE add_dir. Mirrors that function's four-glob shape exactly.
    `exempt_abs_paths` are `write_guard_deny_rules` point exemptions, as
    absolute file paths; only those lexically under the repo root apply.

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
    return write_guard_deny_rules(base, _guard_exempt_rel_paths(base, exempt_abs_paths))


def write_guard_deny_rules(base: str, exempt_rel_paths: "Sequence[str]" = ()) -> list[str]:
    """The four guard `Edit` DENY globs — `.claude/`, `settings*.json`,
    `.git/` and `.git` anywhere below `base`, a `grants.rule_file_arg`
    prefix — that pair with every writable directory this module
    materializes: a declared WRITE add_dir (`stage_grant_rules`) and a
    `developer` spawn's VCS repo root (`repo_root_deny_rules`).
    The guard globs are spelled in this one function only.

    `exempt_rel_paths` names files, relative to `base`, whose own
    `settings*.json` guard is lifted — for a stage whose legitimate task is
    editing that one file. The single `settings*.json` glob is then replaced
    by the decomposition `_settings_guard_split` computes from the real
    directory tree, which still denies every other existing match; the
    other three guards are never affected."""
    subtrees, levels, files = _settings_guard_split(base, exempt_rel_paths)
    return [
        f"Edit({base}/**/.claude/**)",
        *(f"Edit({prefix}/**/settings*.json)" for prefix in subtrees),
        *(f"Edit({prefix}/settings*.json)" for prefix in levels),
        *(f"Edit({path})" for path in files),
        f"Edit({base}/**/.git/**)",
        f"Edit({base}/**/.git)",
    ]


_SETTINGS_GUARD_NAME_GLOB = "settings*.json"


class _ExemptNode(NamedTuple):
    dirs: "dict[str, _ExemptNode]"
    files: "set[str]"


def _settings_guard_split(base: str, exempt_rel_paths: "Sequence[str]") -> "tuple[list[str], list[str], list[str]]":
    """`(subtrees, levels, files)` rule prefixes that together deny Edit on
    every `settings*.json` under `base` except the exempted files:
    `subtrees` get the recursive glob, `levels` the same name glob within
    that one directory, `files` a literal deny. With no exemption this is
    `([base], [], [])`, the plain recursive glob.

    No single glob in this module's dialect can say "all but this file", so
    the exemptions are walked as a tree from `base`, listing each real
    directory on the way: every subdirectory off the exempted paths gets a
    recursive deny, every directory on them a same-level deny — or, in a
    directory that holds an exempted file, a literal deny per existing
    match. A directory that does not exist yet contributes no listing.
    What the listing cannot see is not denied: a directory, or a match
    beside an exempted file, created after the spawn starts. A listed name
    carrying a glob metacharacter is emitted with `*` in its place — a
    wider deny, never a narrower one, and one `_edit_deny_covers_allow`
    can still decide."""
    if not exempt_rel_paths:
        return [base], [], []
    root = _ExemptNode({}, set())
    for rel in exempt_rel_paths:
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or ".." in parts:
            raise grants.GrantValidationError(
                f"guard exemption {rel!r} must be a relative path with no '..' segment — refused"
            )
        node = root
        for part in parts[:-1]:
            node = node.dirs.setdefault(part, _ExemptNode({}, set()))
        node.files.add(parts[-1])

    subtrees: list[str] = []
    levels: list[str] = []
    files: list[str] = []

    def walk(prefix: str, node: _ExemptNode) -> None:
        try:
            entries = sorted(os.scandir(grants.rule_file_path(prefix)), key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.name not in node.dirs and entry.is_dir():
                subtrees.append(f"{prefix}/{_glob_safe_segment(entry.name)}")
        if node.files:
            for entry in entries:
                if entry.name in node.files or entry.is_dir():
                    continue
                if fnmatch.fnmatchcase(entry.name, _SETTINGS_GUARD_NAME_GLOB):
                    files.append(f"{prefix}/{_glob_safe_segment(entry.name)}")
        else:
            levels.append(prefix)
        for name in sorted(node.dirs):
            walk(f"{prefix}/{name}", node.dirs[name])

    walk(base, root)
    return subtrees, levels, files


def _glob_safe_segment(name: str) -> str:
    metachars = re.escape("".join(sorted(_UNDECIDABLE_GLOB_CHARS | {"*"})))
    return re.sub(f"[{metachars}]+", "*", name)


def _guard_exempt_rel_paths(base: str, exempt_abs_paths: "Sequence[str]") -> list[str]:
    """The `exempt_abs_paths` lexically under `base` (a
    `grants.rule_file_arg` prefix), relative to it."""
    base_path = grants.rule_file_path(base).rstrip("/")
    return [
        os.path.normpath(path)[len(base_path) + 1:]
        for path in exempt_abs_paths
        if os.path.normpath(path).startswith(base_path + "/")
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


def stage_grant_rules(
    entries: list[dict],
    *,
    allow_provenance: "dict[str, str] | None" = None,
    deny_provenance: "dict[str, str] | None" = None,
    exempt_abs_paths: "Sequence[str] | None" = None,
) -> tuple[list[str], list[str]]:
    """(allow, deny) rule strings materialized from a flat stage-grants list
    (agentctl `cmd_stage_grants`'s `.data["grants"]` shape: each entry
    carries either `"rule"`, or `"path"`+`"mode"`, plus `"provenance"`).
    When `allow_provenance`/`deny_provenance` are given, every emitted allow
    / deny rule is recorded in its own side's map, in place, under the
    provenance of the entry that emitted it — the first entry wins for a
    rule two entries emit on the same side — so a refusal raised after the
    rules leave this function can still name the grant each came from. The
    sides are kept apart because one rule text can be an allow from one
    entry and a deny from another. A
    `"rule"` entry passes through
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
    is paired with the four guard DENYs of `write_guard_deny_rules`
    under the same prefix, since `--add-dir` alone would otherwise
    hand the child raw filesystem write into those without the kind's own
    baseline denies (which only ever cover this repo's and the plans dir's
    own such paths, never an arbitrary declared add_dir).

    A `read` add_dir pairs with a synthesized Edit DENY only, mirroring
    `plans_permission_rules`' own directional-pair pattern. A read deny
    that overlaps a write allow from the same list must not be emitted: the
    Claude client resolves a rule present in both lists as DENY, with no
    diagnostic, so it would silently void a write grant the plan's owner
    issued. Each add_dir is therefore canonicalized once into a
    `(base, mode)` pair — `_canonical_add_dir_base`, which refuses a base
    it cannot compare — and each read is decided by COVERAGE against the
    list's write entries (`_read_add_dir_needs_deny`):

    - a read whose base is, or is under, some write's base is covered: the
      write allow already spans it and the read grant only puts the
      directory in the child's workspace, so it emits no deny;
    - an uncovered read whose base contains some write's base raises
      `GrantShadowError`: its deny would punch a hole in the middle of that
      write allow, and neither grant subsumes the other;
    - any other read emits its deny, exactly as a lone read does.

    Containment is by path segment (`/a2` is not under `/a`), and nesting
    between two reads or between two writes is never a conflict — each
    keeps its own rule. A write's ALLOW plus its four guard denies are
    emitted unconditionally, whatever reads share the list, and
    `GrantShadowError` is raised only once every entry has validated. The
    read DENY is never passed through `grants.validate_rule` (deny rules
    are outside its scope by design; `grants.validate_grants` itself
    iterates only `allow` and `add_dirs`), so the same glob shape that
    would refuse an allow rule is fine here.

    `exempt_abs_paths` are `write_guard_deny_rules` point exemptions, as
    absolute file paths; each write's guards apply those lexically under
    its base, and an exemption under no write's base is unused."""
    validated: list[str | _AddDir] = []
    provenances: list[str] = []
    for entry in entries:
        rule = entry.get("rule")
        if rule is not None:
            grants.validate_rule(rule)
            validated.append(rule)
            provenances.append(entry.get("provenance", "unknown"))
            continue
        path = entry.get("path")
        mode = entry.get("mode")
        if path is None or mode is None:
            continue
        grants.validate_add_dir(path, mode)
        validated.append(_AddDir(_canonical_add_dir_base(path), mode, entry.get("provenance", "unknown")))
        provenances.append(entry.get("provenance", "unknown"))
    add_dirs = [item for item in validated if isinstance(item, _AddDir)]

    allow: list[str] = []
    deny: list[str] = []
    for item, entry_provenance in zip(validated, provenances):
        allow_start, deny_start = len(allow), len(deny)
        if not isinstance(item, _AddDir):
            allow.append(item)
        elif item.mode == "write":
            allow.append(f"Edit({item.base}/**)")
            deny.extend(write_guard_deny_rules(item.base, _guard_exempt_rel_paths(item.base, exempt_abs_paths or ())))
        elif _read_add_dir_needs_deny(item, add_dirs):
            deny.append(f"Edit({item.base}/**)")
        if allow_provenance is not None:
            for rule in allow[allow_start:]:
                allow_provenance.setdefault(rule, entry_provenance)
        if deny_provenance is not None:
            for rule in deny[deny_start:]:
                deny_provenance.setdefault(rule, entry_provenance)
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
    """An Edit allow the child was granted would lie under an Edit deny
    that voids it, detected in one of three places. Across sources, early:
    another source (the plans-dir deny, a target project's own deny) has
    already denied Edit onto a directory a write add_dir lands in — the two
    grants only meet inside `build_child_settings`, since each source
    validates independently and neither knows about the other's rules
    (`_check_write_add_dirs_not_shadowed`). Within one `stage_grant_rules`
    entry list: a read add_dir's base strictly contains the write's and no
    write covers the read (`_read_add_dir_needs_deny`). Across the finished
    payload, last: any Edit deny covers an Edit allow entirely, whatever
    either one's source (`_check_no_allow_fully_denied`)."""


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


def _is_strictly_under(child_base: str, parent_base: str) -> bool:
    """Path-segment containment on two `grants.rule_file_arg`-encoded bases:
    `//a/b` is under `//a`, `//a2` is not."""
    return child_base.startswith(parent_base.rstrip("/") + "/")


class _AddDir(NamedTuple):
    base: str
    mode: str
    provenance: str


def _canonical_add_dir_base(path: str) -> str:
    """`path` as a `grants.rule_file_arg` base, with `.` segments and
    repeated or trailing slashes collapsed by `Path` — the normalization
    `_check_write_add_dirs_not_shadowed` also compares with. A `..` segment
    is refused rather than collapsed: lexical collapse names a different
    directory once any segment is a symlink, and this module never resolves
    the filesystem, so such a base cannot be compared with another."""
    normalized = Path(path)
    if ".." in normalized.parts:
        raise grants.GrantValidationError(
            f"add_dir {path!r} has a '..' segment, so its base cannot be compared "
            f"with other add_dir bases without resolving the filesystem — refused"
        )
    return grants.rule_file_arg(str(normalized))


def _read_add_dir_needs_deny(read: _AddDir, add_dirs: list[_AddDir]) -> bool:
    """Whether `read` still needs its own Edit deny beside the write entries
    of `add_dirs` — the coverage rule `stage_grant_rules` documents: False
    when a write covers it, `GrantShadowError` when it strictly contains a
    write instead, True otherwise."""
    writes = [d for d in add_dirs if d.mode == "write"]
    if any(read.base == w.base or _is_strictly_under(read.base, w.base) for w in writes):
        return False
    for write in writes:
        if _is_strictly_under(write.base, read.base):
            raise GrantShadowError(
                f"write add_dir {grants.rule_file_path(write.base)!r} "
                f"(provenance {write.provenance}) is nested under read add_dir "
                f"{grants.rule_file_path(read.base)!r} (provenance {read.provenance}) — "
                f"the read's Edit deny would shadow part of the write's Edit allow; refused"
            )
    return True


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


_UNDECIDABLE_GLOB_CHARS = frozenset("?[]{}!")


def _edit_rule_absolute_path(rule: str) -> "str | None":
    """The absolute path glob an `Edit(//<path>)` rule names; `None` for a
    rule whose extent this module cannot decide — another tool, a
    project-relative or `~` path, a glob metacharacter other than `*`."""
    parsed = grants.rule_program_and_arg(rule)
    if parsed is None or parsed[0] != "Edit" or not parsed[1].startswith("//"):
        return None
    path = grants.rule_file_path(parsed[1])
    if any(ch in path for ch in _UNDECIDABLE_GLOB_CHARS):
        return None
    return path


def _edit_glob_regex(glob_path: str) -> "re.Pattern[str] | None":
    """`glob_path` as a regex over absolute paths: a `**` segment matches
    zero or more whole segments, `*` matches within one segment. `None`
    for a shape with no such reading (an empty segment, `**` fused into a
    longer segment)."""
    parts = []
    for segment in glob_path[1:].split("/"):
        if segment == "**":
            parts.append("(?:/[^/]+)*")
        elif not segment or "**" in segment:
            return None
        else:
            parts.append("/" + "[^/]*".join(re.escape(piece) for piece in segment.split("*")))
    return re.compile("".join(parts))


def _edit_deny_covers_allow(deny_rule: str, allow_rule: str) -> bool:
    """Whether every path `allow_rule` grants Edit on is also matched by
    `deny_rule`. Decided only for an allow naming one exact path or one
    whole subtree (`<path>/**`); anything undecidable counts as not
    covered, so it is never refused. A deny ending in `/**` covers the
    whole subtree of every path it matches; any other deny covers only the
    exact paths it matches, so it can void a single-path allow but never a
    subtree allow."""
    deny_path = _edit_rule_absolute_path(deny_rule)
    allow_path = _edit_rule_absolute_path(allow_rule)
    if deny_path is None or allow_path is None:
        return False
    if allow_path.endswith("/**"):
        allow_path = allow_path[: -len("/**")]
        if not deny_path.endswith("/**"):
            return False
    if "*" in allow_path:
        return False
    pattern = _edit_glob_regex(deny_path)
    return pattern is not None and pattern.fullmatch(allow_path) is not None


PROJECT_SETTINGS_SOURCE = "project_settings_permission_rules"


def _check_no_allow_fully_denied(
    allow: list[str], deny: list[str], allow_source: dict[str, str], deny_source: dict[str, str]
) -> None:
    """Refuse a finished permissions payload in which some Edit deny covers
    an Edit allow entirely: the Claude client resolves deny over allow with
    no diagnostic, so the allow would be void. A deny covering only part of
    an allow is the guard denies' ordinary job and is never refused. Both
    rules are named with their source, each looked up on its own side —
    the provenance of the grant entry that produced the rule, or the name
    of the function that emitted a rule no entry produced.

    A pair whose allow AND deny both come from the target project's own
    `settings.local.json` (`PROJECT_SETTINGS_SOURCE`) is only warned about
    on stderr, not refused: that pair is not the orchestrator's to fix, and
    the parent session in that project already runs with the same dead
    allow. Refusing it would fail every developer spawn into the project. A
    pair with only one side from project settings is still refused — the
    other side is ours."""
    for allow_rule in allow:
        for deny_rule in deny:
            if not _edit_deny_covers_allow(deny_rule, allow_rule):
                continue
            allow_from = allow_source.get(allow_rule, "unknown")
            deny_from = deny_source.get(deny_rule, "unknown")
            if allow_from == PROJECT_SETTINGS_SOURCE and deny_from == PROJECT_SETTINGS_SOURCE:
                print(
                    f"spawn-specialist: warning: Edit allow {allow_rule!r} is entirely covered by "
                    f"Edit deny {deny_rule!r}, so the allow is void — both come from the target "
                    f"project's own settings.local.json, so the spawn proceeds",
                    file=sys.stderr,
                )
                continue
            raise GrantShadowError(
                f"Edit allow {allow_rule!r} "
                f"(source {allow_from}) "
                f"is entirely covered by Edit deny {deny_rule!r} "
                f"(source {deny_from}) "
                f"— the client resolves deny over allow, so the allow would be void; refused"
            )


def build_child_settings(
    kind: str,
    plans_directory: "Path | None" = None,
    project_settings_file: "Path | None" = None,
    engine_grants: "list[dict] | None" = None,
    workdir: "str | None" = None,
    evidence_dir: "str | None" = None,
    guard_exempt_paths: "list[str] | None" = None,
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
    doesn't reach `.claude/`, `settings*.json` or `.git/` unguarded.

    `guard_exempt_paths` (absolute file paths) lift the `settings*.json`
    guard off those files only, in both the engine grants' write guards and
    the repo-root guards — see `write_guard_deny_rules`. The evidence
    directory's guards never take them.

    `workdir` also feeds `write_grant_cwd_deny_rules` (round-2 should-fix
    S1): when a `mode="write"` engine grant forces `acceptEdits` on a kind
    that isn't already trusted with unattended writes, that mode alone
    would make the whole cwd writable, not just the granted directory --
    the deny it adds here narrows back to the grant, and runs BEFORE
    `_check_write_add_dirs_not_shadowed` below so a write add_dir that
    itself sits under cwd is refused rather than silently shadowed.

    Once every source has contributed, `_check_no_allow_fully_denied`
    refuses the payload if any Edit deny covers an Edit allow entirely —
    it runs last because a deny from any source, the repo-root guards
    included, can void an allow from any other. Each rule is recorded with
    its source as it is added, in a map for its own side, so the refusal
    names both even when the allow and the deny are the same rule text."""
    settings: dict = {
        "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(SPAWN_AUTOCOMPACT_WINDOW_TOKENS)},
        "autoCompactWindow": SPAWN_AUTOCOMPACT_WINDOW_TOKENS,
    }
    allow: list[str] = []
    deny: list[str] = []
    allow_source: dict[str, str] = {}
    deny_source: dict[str, str] = {}

    def add_allow(rules: list[str], source: str, per_rule: "dict[str, str] | None" = None) -> None:
        allow.extend(rules)
        for rule in rules:
            allow_source.setdefault(rule, (per_rule or {}).get(rule, source))

    def add_deny(rules: list[str], source: str, per_rule: "dict[str, str] | None" = None) -> None:
        deny.extend(rules)
        for rule in rules:
            deny_source.setdefault(rule, (per_rule or {}).get(rule, source))

    baseline = baseline_for_workdir(kind, workdir)
    for rule in baseline:
        grants.validate_rule(rule)
    add_allow(baseline, "baseline_for_workdir")
    if kind in PROJECT_SETTINGS_KINDS:
        project_allow, project_deny = project_settings_permission_rules(project_settings_file)
        add_allow(project_allow, PROJECT_SETTINGS_SOURCE)
        add_deny(project_deny, PROJECT_SETTINGS_SOURCE)
    if plans_directory is not None:
        plans_allow, plans_deny = plans_permission_rules(kind, plans_directory)
        add_allow(plans_allow, "plans_permission_rules")
        add_deny(plans_deny, "plans_permission_rules")
    add_deny(write_grant_cwd_deny_rules(kind, engine_grants, workdir), "write_grant_cwd_deny_rules")
    if engine_grants:
        _check_write_add_dirs_not_shadowed(engine_grants, deny)
        engine_allow_provenance: dict[str, str] = {}
        engine_deny_provenance: dict[str, str] = {}
        engine_allow, engine_deny = stage_grant_rules(
            engine_grants,
            allow_provenance=engine_allow_provenance,
            deny_provenance=engine_deny_provenance,
            exempt_abs_paths=guard_exempt_paths,
        )
        add_allow(engine_allow, "stage_grant_rules", engine_allow_provenance)
        add_deny(engine_deny, "stage_grant_rules", engine_deny_provenance)
    if evidence_dir:
        ev_entries = [{"path": evidence_dir, "mode": "write", "provenance": "evidence_dir"}]
        _check_write_add_dirs_not_shadowed(ev_entries, deny)
        ev_allow_provenance: dict[str, str] = {}
        ev_deny_provenance: dict[str, str] = {}
        ev_allow, ev_deny = stage_grant_rules(
            ev_entries, allow_provenance=ev_allow_provenance, deny_provenance=ev_deny_provenance
        )
        add_allow(ev_allow, "stage_grant_rules", ev_allow_provenance)
        add_deny(ev_deny, "stage_grant_rules", ev_deny_provenance)
    if workdir is not None:
        add_deny(repo_root_deny_rules(kind, workdir, guard_exempt_paths or ()), "repo_root_deny_rules")
    _check_no_allow_fully_denied(allow, deny, allow_source, deny_source)
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


def load_or_create_evidence_dir(
    session_id: str,
    stage_index: int,
    kind: str,
) -> "str | None":
    """The stage's durable evidence directory (agentctl.cli.evidence_dir_for),
    created if absent, for a `kind == "developer"` spawn only — mirrors
    load_engine_stage_grants's in-process-import pattern and its
    kind-gating, but is deliberately NOT folded into that function's own
    grant computation: the directory is spawn-time-only (an --add-dir plus
    a synthetic write grant applied straight to this child's --settings),
    never touching PlanDoc/derive_stage_grants, so it cannot move an
    already-approved plan's grants_sha256. Returns None for any other kind.
    Raises agentctl.cli.EvidenceDirError when the directory itself is
    unusable — the caller fails the spawn loudly on that, rather than
    silently proceeding without evidence. Purely path-derived from
    session_id/stage_index — no session-state lookup, hence no state_root —
    since the directory must be creatable before dispatch necessarily
    reflects the stage in progress."""
    if kind != "developer":
        return None
    from agentctl import cli as agentctl_cli

    path = agentctl_cli.evidence_dir_for(session_id, stage_index)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


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


_ACCEPT_EDITS_TRUSTED_KINDS = ("developer", "tech-writer")
"""Kinds `resolve_permission_mode` grants `acceptEdits` to unconditionally
(unattended Read/Grep/Write is their whole point). Every other kind reaching
`acceptEdits` gets there only because a `mode="write"` engine grant forced
it — `write_grant_forces_accept_edits`/`write_grant_cwd_deny_rules` both key
off this same set so the forcing condition and its cwd-wide deny counterweight
never drift apart."""


def write_grant_forces_accept_edits(kind: str, engine_grants: "list[dict] | None") -> bool:
    """True exactly when a `mode="write"` entry in `engine_grants` is the
    reason `resolve_permission_mode` returns `acceptEdits` for `kind` --
    i.e. `kind` is not already trusted (`_ACCEPT_EDITS_TRUSTED_KINDS`) but
    gets the wide mode anyway because of the grant. Shared with
    `build_child_settings` (via `write_grant_cwd_deny_rules`) so the mode
    that got forced and the deny rule narrowing it back down are computed
    from the same condition."""
    if kind in _ACCEPT_EDITS_TRUSTED_KINDS:
        return False
    return bool(engine_grants) and any(e.get("mode") == "write" for e in engine_grants)


def write_grant_cwd_deny_rules(
    kind: str, engine_grants: "list[dict] | None", cwd: "str | None"
) -> list[str]:
    """Edit DENY covering the spawn's entire `cwd` (round-2 should-fix S1):
    when a `mode="write"` engine grant forces `acceptEdits` for a kind that
    is not already trusted with unattended writes
    (`write_grant_forces_accept_edits`), `acceptEdits` alone makes that
    kind's ENTIRE cwd writable with no allow rule required -- far wider
    than the one directory the grant actually names. Denying the whole cwd
    narrows the mode back down to exactly the declared grant.

    This only makes sense when the grant's own write add_dir sits OUTSIDE
    `cwd` -- a write add_dir under `cwd` would be immediately shadowed by
    this same deny, so `build_child_settings` applies this deny before
    `_check_write_add_dirs_not_shadowed` runs, and that check refuses the
    grant outright rather than silently materializing a no-op."""
    if not write_grant_forces_accept_edits(kind, engine_grants):
        return []
    if cwd is None:
        return []
    base = grants.rule_file_arg(str(Path(cwd)).rstrip("/"))
    return [f"Edit({base}/**)"]


def resolve_permission_mode(
    args: argparse.Namespace, engine_grants: "list[dict] | None" = None
) -> str:
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

    A `mode="write"` add_dir entry in `engine_grants` also forces `acceptEdits`,
    for any kind, not only developer/tech-writer. `probe-hook-decision-semantics.py`'s
    `add_dir:default_write_add_dir` cell measured that a write add_dir's own
    `Edit(//path/**)` allow rule is NOT honored under plain `default` mode — the
    write into the granted directory silently failed even though `--add-dir` and
    the allow rule were both present (docs/components/settings-and-permissions.md
    § Hook decision semantics). Under `acceptEdits`, `--add-dir` alone already
    makes a directory writable (finding from the stage-2 probes, still holding),
    so the same allow/deny rule pair now materializes real write access instead of
    silently granting nothing — a `grant_covers_call` classification built on the
    old `default`-mode assumption would misread this gap as a `planning_miss`
    rather than the `materialization_defect` it actually was.

    User-supplied `--permission-mode` always wins -- but when it does so
    over a `mode="write"` engine grant, that grant's own allow rule was
    measured (see above) to silently fail to materialize under `default`,
    and `plan` mode blocks writes outright, so the caller is warned rather
    than left to discover a silently-dropped grant later (round-2
    should-fix S1).
    """
    if args.permission_mode is not None:
        if engine_grants and any(e.get("mode") == "write" for e in engine_grants):
            print(
                f"spawn-specialist: warning: --permission-mode {args.permission_mode!r} "
                "overrides the acceptEdits a write engine grant would otherwise force -- "
                "that grant's write access will not materialize under this mode",
                file=sys.stderr,
            )
        return args.permission_mode
    if args.kind in _ACCEPT_EDITS_TRUSTED_KINDS:
        return "acceptEdits"
    if write_grant_forces_accept_edits(args.kind, engine_grants):
        return "acceptEdits"
    return "default"


def _build_extraction(result_text: str, kind: str) -> "marker_extract.Extraction | None":
    """The call site's guard, factored out so a test can drive it directly
    without invoking main()'s subprocess plumbing. The shared implementation
    (``marker_extract.build_extraction``) runs the pass unconditionally
    whenever it can, not only after the legacy any-line regex scan failed."""
    return marker_extract.build_extraction(
        result_text, kind=kind, allowed=markers_for_kind(kind)
    )


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
    skill = None if is_manager_kind(args.kind) else skill_path(args.kind)
    if skill is not None and not skill.exists():
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
    guard_exempt_paths: list[str] = []
    for exempt in args.guard_exempt or ():
        if ".." in exempt.parts:
            print(f"error: --guard-exempt {exempt} has a '..' segment — refused", file=sys.stderr)
            log_refused("guard-exempt-path-shape", {"kind": args.kind})
            return 2
        guard_exempt_paths.append(os.path.normpath(Path(workdir) / exempt))

    plans_directory = plans_dir()

    # --review-topo replaces the whole-plan/--plan-brief projection outright:
    # topo_pair stays None for every ordinary spawn (the overwhelming majority),
    # in which case every block below that checks it is a no-op and behavior is
    # unchanged from before this flag existed.
    topo_pair: "str | None" = None
    topo_view_dir: "Path | None" = None
    topo_plan_sha256: "str | None" = None
    topo_doc = None
    topo_history: "dict | None" = None
    if args.review_topo_history is not None and args.review_topo is None:
        print("error: --review-topo-history is only valid with --review-topo.", file=sys.stderr)
        log_refused("review-topo-history-without-review-topo", {"kind": args.kind})
        return 2
    if args.review_topo is not None:
        if args.kind != "thinker":
            print(
                "error: --review-topo is only valid with --kind thinker (the "
                "rely-guarantee review protocol it materializes is a thinker "
                "return shape).",
                file=sys.stderr,
            )
            log_refused("review-topo-wrong-kind", {"kind": args.kind, "review_pair": args.review_topo})
            return 2
        if args.stage_index is not None:
            print(
                "error: --review-topo replaces the whole-plan/--stage-index "
                "projection outright -- it cannot be combined with "
                "--stage-index.",
                file=sys.stderr,
            )
            log_refused("review-topo-conflict", {"kind": args.kind, "review_pair": args.review_topo})
            return 2
        try:
            topo_pair = parse_review_topo_pair(args.review_topo)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            log_refused("review-topo-bad-pair", {"kind": args.kind, "review_pair": args.review_topo})
            return 2
        try:
            topo_doc, _topo_plan_bytes, topo_plan_sha256 = load_plan_with_digest(str(args.plan))
        except PlanError as exc:
            print(f"error: --review-topo: {exc}", file=sys.stderr)
            log_refused("review-topo-plan-error", {"kind": args.kind, "review_pair": topo_pair})
            return 2
        try:
            if is_unit_id(topo_pair):
                parse_unit(topo_doc, topo_pair)
            else:
                parse_pair(topo_doc, topo_pair)
        except ValueError as exc:
            print(f"error: --review-topo {topo_pair}: {exc}", file=sys.stderr)
            log_refused(
                "review-topo-unknown-pair",
                {"kind": args.kind, "review_pair": topo_pair, "plan_sha256": topo_plan_sha256},
            )
            return 2
        if args.review_topo_history is not None:
            try:
                topo_history = json.loads(Path(args.review_topo_history).read_text(encoding="utf-8"))
                if not (isinstance(topo_history, dict) and isinstance(topo_history.get("records"), list)
                        and isinstance(topo_history.get("changed_parts_since_last"), list)):
                    raise ValueError("expected an object with `records` and `changed_parts_since_last` lists")
            except (OSError, ValueError) as exc:
                print(f"error: --review-topo-history {args.review_topo_history}: {exc}", file=sys.stderr)
                log_refused("review-topo-history-unreadable", {"kind": args.kind, "review_pair": topo_pair})
                return 2
        topo_units_override = os.environ.get("AGENTCTL_TOPO_UNITS_DIR")
        topo_root = Path(topo_units_override) if topo_units_override else agentctl_topo_units_dir()
        # A unit bundle is self-contained: the unit has no view directory, no
        # Read grant and nothing to materialize.
        if not is_unit_id(topo_pair):
            topo_view_dir = topo_root / topo_plan_sha256 / topo_pair_view_dirname(topo_pair)
        if topo_view_dir is not None and not args.dry_run:
            # Materialization is real I/O (writes the whole plan-version topo
            # tree); --dry-run must write nothing, so it only computes the path
            # above and prints the intended TOPO-VIEW line below, never
            # reaching this call.
            try:
                materialize_topo_units(topo_doc, topo_plan_sha256, topo_root)
            except (TopoUnitsCorrupt, PlanError) as exc:
                print(f"error: --review-topo: {exc}", file=sys.stderr)
                log_refused(
                    "review-topo-materialize-failed",
                    {"kind": args.kind, "review_pair": topo_pair, "plan_sha256": topo_plan_sha256},
                )
                return 2

    # engine_grants stays None unless BOTH --session and --stage-index are given
    # AND the engine's own executor field for that stage matches --kind (checked
    # inside load_engine_stage_grants) -- a mismatch or unknown session falls
    # back to kind-baseline-only settings rather than failing the spawn.
    engine_grants: "list[dict] | None" = None
    # evidence_dir stays None for the same conditions as engine_grants, plus a
    # kind gate applied inside load_or_create_evidence_dir itself: only a
    # kind=="developer" spawn gets an evidence directory. It is never folded
    # into engine_grants -- see load_or_create_evidence_dir and
    # build_child_settings's evidence_dir parameter -- so it cannot move an
    # already-approved plan's grants_sha256 (computed purely from the
    # PlanDoc's own declared/derived grants).
    evidence_dir: "str | None" = None
    if args.session is not None and args.stage_index is not None:
        engine_grants = load_engine_stage_grants(
            args.session, args.stage_index, args.kind, state_root=args.state_root
        )
        from agentctl import cli as agentctl_cli

        try:
            evidence_dir = load_or_create_evidence_dir(
                args.session, args.stage_index, args.kind
            )
        except agentctl_cli.EvidenceDirError as exc:
            print(f"error: {exc}", file=sys.stderr)
            log_refused("evidence-dir-refused", {"kind": args.kind, "stage_index": args.stage_index})
            return 2

    add_dir_argv: list[str] = []
    if topo_pair is not None:
        # --review-topo withholds the plans-directory grant entirely -- the
        # child gets only its one-pair view directory below (none for a unit),
        # never the whole plans_dir() a --plan-brief/whole-plan thinker spawn
        # would carry.
        if topo_view_dir is not None:
            add_dir_argv.extend(["--add-dir", str(topo_view_dir)])
    else:
        add_dir_argv.extend(plans_add_dir_args(args.kind, plans_directory))
    add_dir_argv.extend(repo_root_add_dir_args(args.kind, workdir))
    if engine_grants:
        add_dir_argv.extend(stage_grant_add_dir_args(engine_grants))
    if evidence_dir:
        add_dir_argv.extend(stage_grant_add_dir_args([{"path": evidence_dir, "mode": "write"}]))
    add_dir_paths = _paths_from_add_dir_argv(add_dir_argv)

    permission_mode = resolve_permission_mode(args, engine_grants)

    perms = permissions_digest(args.project_permissions)
    topo_bundle: "str | None" = None
    if topo_pair is not None:
        if topo_view_dir is None:
            topo_bundle = render_unit_review_bundle(
                topo_doc, topo_pair, plan_sha256=topo_plan_sha256, history=topo_history,
            )
        else:
            topo_bundle = render_pair_review_bundle(
                topo_doc, topo_pair, plan_sha256=topo_plan_sha256, view_dir=topo_view_dir,
                history=topo_history,
            )
    try:
        prompt = assemble_prompt(
            args,
            depth_next,
            perms,
            workdir=workdir,
            permission_mode=permission_mode,
            add_dir_paths=add_dir_paths,
            stage_grant_entries=engine_grants,
            evidence_dir=evidence_dir,
            topo_bundle=topo_bundle,
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
            f"refusing before spawning. Shrink constraints/dossier, dispatch with "
            f"--plan-brief if not already set, or split the review itself via "
            f"--review-topo <pair> (materializes one base-service pair review "
            f"at a time; scripts/plan-review-topological.py is the planned "
            f"whole-plan driver that walks every pair through it). If none of "
            f"that brings it under the ceiling, a user-authored override -- "
            f"dispatching this child by hand, outside this refusal -- is the "
            f"remaining exit; this script never raises the ceiling or degrades "
            f"the prompt silently to force a fit.",
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
                "review_pair": topo_pair,
                "plan_sha256": topo_plan_sha256,
            },
        )
        return 5

    try:
        child_settings = build_child_settings(
            args.kind,
            None if topo_pair is not None else plans_directory,
            args.project_settings,
            engine_grants,
            workdir=workdir,
            evidence_dir=evidence_dir,
            guard_exempt_paths=guard_exempt_paths,
        )
    except GrantShadowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        log_refused("grant-shadowed", {"kind": args.kind, "stage_index": args.stage_index})
        return 2

    if topo_view_dir is not None:
        # The plans-directory grant was withheld above; grant the one-pair
        # view directory instead (Read allow + Edit deny, same directional
        # pair plans_permission_rules uses for its own read kinds).
        view_allow, view_deny = review_topo_view_permission_rules(topo_view_dir)
        child_permissions = child_settings.setdefault("permissions", {})
        child_permissions.setdefault("allow", []).extend(view_allow)
        child_permissions.setdefault("deny", []).extend(view_deny)

    cmd = [
        "claude",
        "-p",
        *(
            ["--append-system-prompt-file", str(composed_system_prompt_file(skill))]
            if skill is not None
            else []
        ),
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
        if topo_view_dir is not None:
            view_files = topo_pair_view(topo_doc, topo_pair)
            print(f"TOPO-VIEW: {topo_view_dir} files={','.join(view_files)}")
        elif topo_pair is not None:
            print("TOPO-VIEW: none files=")
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
        if transcript_path is None and child_session_id:
            for candidate in _iter_workdir_transcripts(workdir):
                if candidate.stem == child_session_id:
                    transcript_path = candidate
                    break
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
        "review_pair": topo_pair,
        "plan_sha256": topo_plan_sha256,
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
