"""Read the author-written TOML plan into typed Stage[] and diff plans for replan.

The plan artifact is TOML (human/LLM-authored, read-only here via tomllib); the
machine-written record is JSON (state.py). Keeping the author surface separate
from the durable state means a plan edit is reviewable as a plain diff and never
silently rewrites engine state.

TOML shape (minimal):

    [meta]
    task_id = "steady-riding-dragonfly"
    goal = "..."
    done_criterion = "pytest green ..."
    criterion_type = "measurable"        # or "acceptance_review"
    repo_root = "/abs/path/to/repo"      # optional; each verify_command runs here
                                         # (cd repo_root && cmd). Unset -> inherit
                                         # invoker cwd, so verify paths must then be
                                         # absolute. Byte-identical to pre-field default.

    [[stage]]
    index = 1
    title = "Scaffold package"
    executor = "in_thread"               # or "spawn:developer"
    expected_result_image = "package imports, status runs on empty state"
    criterion_type = "measurable"
    done_criterion = "python3 -m agentctl status exits 0"
    verify_command = "python3 -m agentctl status"  # optional; executable form of done_criterion
    expected_exit = 0                     # optional (default 0); engine gates passed on this exit
    depends_on = []                       # optional
    output_artifacts = ["scripts/agentctl/"]  # optional; paths this stage produces.
                                              # Parsed onto Stage.output_artifacts and
                                              # consulted by the verify-command
                                              # reachability lint: a verify_command path
                                              # that neither exists yet nor is declared
                                              # here by some stage is unreachable-green.

For substantive plans (meta.weight_class = "substantive") the [meta] table must
also carry a plan-level external-research decision:

    external_research = "checked internal wiki + WebSearch; no prior art applies"
                                         # required for substantive; what
                                         # internet/intranet research found, or
                                         # why it is not warranted.

and every stage must also carry the 8-element activity-structure fields:

    material = "..."
    means = "..."
    method = "..."
    conditions = "..."
    invariants = "..."
    capability_required = "..."          # required for substantive

    [stage.principle]
    statement = "..."
    source = "..."
    derivation = "..."                   # how the claim follows from the source
                                         # (checkable second half of provenance;
                                         # must differ from statement and source)
    confidence = "high"                  # high | medium | low
    refutation = "..."

diff_plans classifies a replan as no_change / refinement / substantive, mirroring
CLAUDE.md § Acting without asking: structural edits (stage set, dependencies,
executors, done criteria, weight_class) are substantive and re-arm the plan-approval
gate; wording-only edits (titles, expected-result prose) are refinements.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .state import (
    Actor,
    Confidence,
    Criterion,
    CriterionType,
    FinalCheck,
    Means,
    Outcome,
    Principle,
    Stage,
    StageStatus,
    Subject,
    Supply,
)
from .text_shape import ELEMENT_NAMES as _ELEMENT_NAMES
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import normalize_string as _normalize_string


@dataclass
class PlanMeta:
    task_id: str
    goal: str = ""
    done_criterion: str = ""
    criterion_type: str = CriterionType.MEASURABLE.value
    weight_class: str | None = None
    # Plan-level external-research decision (planner SKILL.md § Research). Required
    # non-empty for substantive plans; None for legacy/non-substantive.
    external_research: str | None = None
    # Directory each stage's verify_command runs in. None (default) inherits the
    # invoker's cwd — byte-identical to pre-repo_root behaviour. Set it so a plan's
    # repo-relative verify paths resolve no matter where the engine is driven from.
    repo_root: str | None = None
    # The linked worktree a worktree-delivered change is authored in, when it
    # differs from repo_root (a Core/IaC change lands via PR; the canonical
    # checkout at repo_root stays frozen on main until landing). None (default) =
    # no worktree-venue signal, byte-identical to pre-field behaviour. Backs the
    # final_check_venue_warnings lint below.
    delivery_worktree: str | None = None
    # Optional typed end-to-end checks run by verify-final after per-stage re-runs.
    # Absent => [] (back-compat). Parsed from top-level [[final_check]] tables.
    final_check: list[FinalCheck] = field(default_factory=list)


@dataclass
class PlanDoc:
    meta: PlanMeta
    stages: list[Stage] = field(default_factory=list)


class PlanError(Exception):
    """The TOML plan is missing required structure."""


# The only two executor shapes the engine dispatches: in-thread, or a named spawn
# kind matching a spawn-specialist.py --kind. Anything else (a typo, a free-text
# description) must be rejected at submission — a silent default to in_thread
# degrades the whole plan to in-thread execution with no visible error (#7).
_EXECUTOR_RE = re.compile(r"^(in_thread|spawn:[a-z][a-z0-9_-]*)$")

# Extra stage fields required for substantive plans (8-element activity structure).
_SUBSTANTIVE_STAGE_FIELDS = ("material", "means", "method", "conditions", "invariants", "capability_required")
_PRINCIPLE_SUBFIELDS = ("statement", "source", "derivation", "confidence", "refutation")


def _validate_substantive_stage(s: dict, index: int) -> None:
    """Raise PlanError if a substantive stage is missing any activity-structure field."""
    for field_name in _SUBSTANTIVE_STAGE_FIELDS:
        if not s.get(field_name):
            raise PlanError(
                f"stage {index} missing {field_name!r} (required for substantive plans)"
            )
    crit_type = str(s.get("criterion_type", CriterionType.MEASURABLE.value))
    if crit_type == CriterionType.MEASURABLE.value and not s.get("verify_command"):
        raise PlanError(
            f"stage {index} is a substantive measurable stage but has no verify_command "
            f"(a measurable criterion you cannot execute is really acceptance_review)"
        )
    principle = s.get("principle")
    if not isinstance(principle, dict):
        raise PlanError(
            f"stage {index} missing [stage.principle] table (required for substantive plans)"
        )
    for sub in _PRINCIPLE_SUBFIELDS:
        if not principle.get(sub):
            raise PlanError(
                f"stage {index} [stage.principle] missing {sub!r} (required for substantive plans)"
            )
    conf = principle.get("confidence")
    if conf not in {c.value for c in Confidence}:
        raise PlanError(
            f"stage {index} [stage.principle] confidence {conf!r} is not one of "
            f"{sorted(c.value for c in Confidence)}"
        )
    # Element 7 is ALWAYS a норма (должное); there is no a-priori знание-vs-норма tag to
    # validate (ADR-0004 dropped it as a category error). A legacy principle block still
    # carrying the retired key parses unchanged — the extra key is simply not read, not rejected.
    # Anti-template: the cheapest degradation of a required free-text field is
    # boilerplate. Reject placeholder values and reject a principle that merely
    # echoes another field back at itself (refutation == statement, or the
    # principle collapsing into a restatement of the stage's own method).
    for sub in _PRINCIPLE_SUBFIELDS:
        if _normalize_string(str(principle.get(sub, ""))) in _PLACEHOLDER_SET:
            raise PlanError(
                f"stage {index} [stage.principle] {sub!r} is a placeholder "
                f"(must be a real value, not {principle.get(sub)!r})"
            )
    norm_statement = _normalize_string(str(principle.get("statement", "")))
    norm_refutation = _normalize_string(str(principle.get("refutation", "")))
    if norm_statement and norm_statement == norm_refutation:
        raise PlanError(
            f"stage {index} [stage.principle] refutation must differ from statement "
            f"(a refutation identical to the claim it refutes proves nothing)"
        )
    # Derivation is the second checkable half of provenance: source says the ground
    # exists, derivation says the claim follows from it. A derivation that just echoes
    # the statement (or the source) asserts the inference instead of showing it, so it
    # is no more checkable than a bare citation — reject both collapses.
    norm_derivation = _normalize_string(str(principle.get("derivation", "")))
    norm_source = _normalize_string(str(principle.get("source", "")))
    if norm_derivation and norm_derivation == norm_statement:
        raise PlanError(
            f"stage {index} [stage.principle] derivation must differ from statement "
            f"(a derivation that restates the claim shows no inference from the source)"
        )
    if norm_derivation and norm_derivation == norm_source:
        raise PlanError(
            f"stage {index} [stage.principle] derivation must differ from source "
            f"(a derivation that restates the source shows no inference to the claim)"
        )
    norm_method = _normalize_string(str(s.get("method", "")))
    if norm_statement and norm_statement == norm_method:
        raise PlanError(
            f"stage {index} [stage.principle] statement must differ from the stage's "
            f"method (a principle that only restates the method is not a principle)"
        )


def _build_supplies(s: dict, index: int) -> list[Supply]:
    """Build typed Supply edges. Explicit [[stage.supplies]] wins; otherwise the
    flat `depends_on` list is lifted into element-less edges."""
    raw = s.get("supplies")
    if raw:
        supplies = []
        for edge in raw:
            if "on" not in edge:
                raise PlanError(f"stage {index} supply missing 'on'")
            supplies.append(
                Supply(
                    on=int(edge["on"]),
                    element=edge.get("element"),
                    artifact=edge.get("artifact"),
                )
            )
        return supplies
    return [Supply(on=int(d)) for d in s.get("depends_on", [])]


def _validate_graph(stages: list[Stage], *, is_substantive: bool) -> None:
    """Validate the derived provision graph: (iii) no dangling Supply.on, (iv) for
    substantive stages every named element is known, (v) the graph is acyclic."""
    known = {s.index for s in stages}
    for s in stages:
        for sup in s.supplies:
            if sup.on not in known:
                raise PlanError(
                    f"stage {s.index} supplies from stage {sup.on} which does not exist (dangling edge)"
                )
            if is_substantive and sup.element is not None and sup.element not in _ELEMENT_NAMES:
                raise PlanError(
                    f"stage {s.index} supply element {sup.element!r} is not a known "
                    f"activity element {sorted(_ELEMENT_NAMES)}"
                )
    # (v) acyclicity over the derived depends_on projection (DFS 3-colour).
    adj = {s.index: s.depends_on for s in stages}
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {i: WHITE for i in known}

    def visit(node: int, trail: list[int]) -> None:
        colour[node] = GRAY
        for dep in adj.get(node, []):
            if colour[dep] == GRAY:
                cycle = trail[trail.index(dep):] + [dep]
                raise PlanError(f"stage dependency cycle: {' -> '.join(map(str, cycle))}")
            if colour[dep] == WHITE:
                visit(dep, trail + [dep])
        colour[node] = BLACK

    for i in known:
        if colour[i] == WHITE:
            visit(i, [i])


# --- verify_command scope lint (advisory, never blocking) -------------------
# Difficulty removed: a stage's verify_command, or the plan's final_check,
# running a whole aggregate suite (verify-all.py, a bare pytest invocation)
# without scoping to the paths actually touched lets pre-existing, unrelated
# reds elsewhere in the repo false-fail the stage/resolution — a recurring
# authoring miss (experience leaf 2026-06-29, ~20 accumulated contexts,
# several of them final_check whole-suite hostages). This is the DECIDABLE
# rule part (does the command look like an unscoped aggregate run); whether a
# whole-suite run is actually justified is perception left to the plan author
# — hence advisory, never a block.
_VERIFY_ALL_MARKER = "verify-all"
_PYTEST_TOKENS = ("pytest", "py.test")


def _pytest_invocation_tail(tokens: list[str]) -> list[str] | None:
    """None if `tokens` isn't a pytest invocation; otherwise the tokens after the
    invocation itself (so the `-m` in `python -m pytest` is never mistaken for a
    `-m` marker-selection flag scoping the run)."""
    if tokens and tokens[0] in _PYTEST_TOKENS:
        return tokens[1:]
    for i in range(len(tokens) - 2):
        if tokens[i] in ("python", "python3") and tokens[i + 1] == "-m" and tokens[i + 2] == "pytest":
            return tokens[i + 3:]
    return None


def _pytest_is_scoped(tail: list[str]) -> bool:
    return any(
        t in ("-k", "-m") or "::" in t or t.endswith(".py") or ("/" in t and not t.startswith("-"))
        for t in tail
    )


def _subcommand_is_aggregate_unscoped(sub: str) -> bool:
    tokens = sub.split()
    if not tokens:
        return False
    if _VERIFY_ALL_MARKER in sub:
        return "--staged" not in tokens
    tail = _pytest_invocation_tail(tokens)
    if tail is not None:
        return not _pytest_is_scoped(tail)
    return False


def _first_unscoped_subcommand(cmd: str) -> str | None:
    """The first aggregate-unscoped subcommand in `cmd` (split on shell
    separators), or None if every subcommand is scoped or non-aggregate."""
    for sub in re.split(r"&&|;|\|", cmd):
        sub = sub.strip()
        if sub and _subcommand_is_aggregate_unscoped(sub):
            return sub
    return None


def verify_command_scope_warnings(stages, final_check=None) -> list[str]:
    """Warn (never block) when a stage's verify_command, or a plan's
    final_check, runs an aggregate test suite (verify-all.py, a bare pytest
    invocation) without narrowing it to the gate that enforces it — the miss
    recorded in experience leaf 2026-06-29 (~20 accumulated contexts, several
    of them final_check whole-suite hostages). One warning per offending
    stage or final_check entry."""
    warnings: list[str] = []
    for s in stages:
        cmd = s.criterion.verify_command
        if not cmd:
            continue
        sub = _first_unscoped_subcommand(cmd)
        if sub:
            warnings.append(
                f"stage {s.index} ({s.title!r}): verify_command runs an aggregate "
                f"suite without a scope flag ({sub!r}); scope it to the gate that "
                f"enforces it (--staged, or an explicit test path) so pre-existing "
                f"unrelated reds cannot false-fail the stage "
                f"(see experience leaf 2026-06-29)."
            )
    for fi, fc in enumerate(final_check or [], 1):
        if not fc.command:
            continue
        sub = _first_unscoped_subcommand(fc.command)
        if sub:
            label = fc.label or fc.command
            warnings.append(
                f"final_check {fi} ({label!r}): verify command runs an "
                f"aggregate suite without a scope flag ({sub!r}); scope it to "
                f"the change's own tests (an explicit path, -k/-m, or --staged) "
                f"so pre-existing unrelated reds cannot false-fail resolution "
                f"(see experience leaf 2026-06-29, instances 17/18/19)."
            )
    return warnings


# --- verify_command green-reachability lint (BLOCKING for substantive) -------
# Difficulty removed: the scope lint above stops a control from being false-RED;
# it says nothing about the other direction. A verify_command / final_check can
# name a path that no stage ever produces and that does not yet exist — the
# control can then never go GREEN honestly, so "green" would only ever mean the
# author never ran it. This is the second half of two-directional control: a
# control is trusted only when it goes RED on mutation AND its GREEN direction is
# reachable. Unlike scope (perception: is a whole-suite run justified here?),
# green-reachability has no legitimate instance — a control that cannot pass is a
# broken control, full stop — so this is DECIDABLE with no author discretion and
# is therefore a BLOCKER, not an advisory.
#
# A "path" is green-reachable iff it already exists under repo_root OR some
# stage declares it (a prefix of it) in output_artifacts (the machine-readable
# answer to "which stage produces this path").
#
# Deliberately NARROW to keep the false-positive population empty-in-practice:
#   * Only RELATIVE, literal, path-shaped tokens are considered. Absolute paths
#     (/dev/null, /tmp/scratch written at runtime) are OUT OF SCOPE — a runtime
#     temp file is exactly the false positive this narrowing avoids.
#   * Globs ("*?["), shell variables ("$..."), URLs ("://"), option values
#     ("k=v") and the program string after `-c` / module after `-m` are dropped:
#     none is a literal filesystem path.
# Residual false-positive population (documented, not eliminated): a relative
# path a stage's command *creates then reads within the same command* (so it is
# neither pre-existing nor a declared cross-stage artifact). Declare such a path
# in that stage's output_artifacts to silence the lint.
#
# LIMITS, stated so the green light is not over-read:
#   * Reachability is NOT validity: a reachable path proves the command *can*
#     run green, never that green *means the stage is done* — that is the
#     author's done_criterion, which this lint does not judge.
#   * Path-reachability is NOT green-reachability in full: a command can still
#     fail green for reasons no static path check can see (a missing binary, a
#     network dep, a wrong exit code). This closes the one decidable, recurring
#     sub-case — a path nothing produces — not the general halting question.
_PATH_EXTS = (".py", ".toml", ".json", ".md", ".txt", ".sh", ".cfg",
              ".ini", ".yaml", ".yml", ".csv", ".sql")


def _reachability_path_tokens(cmd: str) -> list[str]:
    """The relative, literal, path-shaped tokens of a shell command — the tokens
    whose green-reachability is decidable. shlex parses the WHOLE command in one
    pass so a quoted `python3 -c "..."` body — which itself contains `;` `|` `<`
    `>` between Python statements — collapses into ONE token that the `-c` drop
    then discards, instead of being shattered on shell metacharacters that also
    occur inside it. Shell operators (`&&`, `|`, `2>&1`, `>`) survive as tokens but
    are not path-shaped, so they fall out. Tolerant: unbalanced quotes fall back to
    a plain split rather than raising."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    tokens: list[str] = []
    skip_next = False
    for t in toks:
        if skip_next:
            skip_next = False
            continue
        if t in ("-c", "-m"):  # program string / module name follows, not a path
            skip_next = True
            continue
        if t.startswith("-"):
            continue
        if any(ch.isspace() for ch in t):
            continue  # a real path token has no whitespace or newline
        head = t.split("::", 1)[0]  # drop a pytest node-id suffix
        if not head or head.startswith("/"):
            continue  # empty or absolute -> out of scope
        if any(ch in head for ch in "*?[$=") or "://" in head:
            continue  # glob / variable / option-value / URL -> not a literal path
        if "/" in head or head.endswith(_PATH_EXTS):
            tokens.append(head)
    return tokens


def _path_is_reachable(token: str, declared: list[str], repo_root: str | None) -> bool:
    base = Path(repo_root) if repo_root else Path(".")
    if (base / token).exists():
        return True
    tnorm = token.rstrip("/")
    for decl in declared:
        dnorm = decl.rstrip("/")
        if tnorm == dnorm or tnorm.startswith(dnorm + "/") or dnorm.startswith(tnorm + "/"):
            return True
    return False


def verify_command_reachability_blockers(stages, final_check, repo_root) -> list[str]:
    """BLOCK a substantive plan whose verify_command / final_check names a bare
    literal relative path that is neither present under repo_root nor declared as
    some stage's output_artifacts — a control that can never go green honestly.
    One blocker per offending (surface, path). See the module comment above for
    the false-positive narrowing and the two named limits."""
    declared: list[str] = []
    for s in stages:
        declared.extend(getattr(s, "output_artifacts", []) or [])
    blockers: list[str] = []

    def _check(cmd: str | None, where: str) -> None:
        if not cmd:
            return
        seen: set[str] = set()
        for tok in _reachability_path_tokens(cmd):
            if tok in seen:
                continue
            seen.add(tok)
            if not _path_is_reachable(tok, declared, repo_root):
                blockers.append(
                    f"{where}: path {tok!r} is not green-reachable — it does not exist "
                    f"under repo_root and no stage declares it in output_artifacts, so "
                    f"this control can never pass honestly. Route out (pick one): create "
                    f"the file before this control runs, OR declare {tok!r} in the "
                    f"output_artifacts of the stage that produces it."
                )

    for s in stages:
        _check(s.criterion.verify_command, f"stage {s.index} ({s.title!r}) verify_command")
    for fi, fc in enumerate(final_check or [], 1):
        label = fc.label or fc.command
        _check(fc.command, f"final_check {fi} ({label!r})")
    return blockers


# --- final_check worktree-venue lint (advisory, never blocking) -------------
# Difficulty removed: a worktree-delivered change (Core edits land via PR; the
# canonical checkout stays frozen on main until landing) is ABSENT from
# repo_root pre-landing, so a final_check whose `cd` targets repo_root observes
# the wrong tree — a genuinely-green delivery false-fails (experience leaves
# 2026-06-24-agentctl-verify-venue-worktree-needs-substantive-replan,
# 2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan). This is
# perception, not a decidable defect — a repo_root-anchored final_check is the
# CORRECT post-landing confirmation, so the lint only warns (never blocks) and
# only fires when [meta] delivery_worktree names the pre-landing venue.
def final_check_venue_warnings(
    final_check, repo_root: str | None, delivery_worktree: str | None
) -> list[str]:
    """Warn (never block) when a final_check `cd`s into the canonical repo_root
    while [meta] delivery_worktree is set — pre-landing that observes the wrong
    tree; the repo_root run is the post-landing confirmation instead. Silent
    when delivery_worktree is unset (no signal) or repo_root is unset (nothing
    to compare against)."""
    if not delivery_worktree or not repo_root:
        return []
    repo_root_p = Path(repo_root).resolve()
    worktree_p = Path(delivery_worktree).resolve()
    warnings: list[str] = []
    for fi, fc in enumerate(final_check or [], 1):
        for sub in re.split(r"&&|;|\|", fc.command):
            sub = sub.strip()
            if not sub:
                continue
            try:
                toks = shlex.split(sub)
            except ValueError:
                continue
            if len(toks) < 2 or toks[0] != "cd":
                continue
            target = Path(toks[1])
            if not target.is_absolute():
                target = repo_root_p / target
            target = target.resolve()
            under_repo_root = target == repo_root_p or repo_root_p in target.parents
            under_worktree = target == worktree_p or worktree_p in target.parents
            if under_repo_root and not under_worktree:
                label = fc.label or fc.command
                warnings.append(
                    f"final_check {fi} ({label!r}) cd's into the canonical repo_root "
                    f"but [meta] delivery_worktree is set; pre-landing this observes "
                    f"the wrong tree — cd into {delivery_worktree} so the check runs "
                    f"where the un-landed change lives (the repo_root run is the "
                    f"post-landing confirmation)."
                )
                break
    return warnings


def parse_plan(
    data: dict, *, strict: bool = True, strict_executor: bool | None = None
) -> PlanDoc:
    """Pure: a parsed-TOML dict -> PlanDoc. No filesystem.

    strict=True (default) is the full submission-grade validation every newly
    authored or resubmitted plan goes through (cmd_submit_plan, the NEW side of
    cmd_replan): the executor vocabulary check, the substantive `external_research`
    meta requirement, and the per-stage substantive activity/principle checks.

    strict=False loads a plan purely as a read-only comparison baseline
    (cmd_replan's OLD/approved-snapshot side). It keeps the BASIC structural
    parse — [meta].task_id, at least one [[stage]], the per-stage
    title/executor/expected_result_image/done_criterion, unique indices, and
    _validate_graph — but skips every submission-grade check above, so a snapshot
    frozen before a newer trunk tightened the schema (e.g. before
    [stage.principle].derivation became required) stays diffable without
    retroactively bricking its own session's replan flow. On this path every
    principle subfield is read via .get() so a genuinely old snapshot missing a
    subfield parses to a partial Principle instead of raising KeyError.

    strict_executor is a retained back-compat alias for strict (the flag once
    only gated the executor vocabulary check); when given it overrides strict."""
    if strict_executor is not None:
        strict = strict_executor
    if "meta" not in data:
        raise PlanError("plan missing [meta] table")
    m = data["meta"]
    if not m.get("task_id"):
        raise PlanError("[meta] missing task_id")
    raw_weight = m.get("weight_class")
    raw_fcs = data.get("final_check", [])
    final_checks: list[FinalCheck] = []
    for fi, fc in enumerate(raw_fcs, 1):
        cmd = fc.get("command", "")
        if not cmd or not isinstance(cmd, str):
            raise PlanError(f"final_check {fi} missing 'command' (required, non-empty string)")
        xc = fc.get("expected_exit", 0)
        if not isinstance(xc, int):
            raise PlanError(f"final_check {fi} expected_exit must be an int")
        final_checks.append(FinalCheck(command=cmd, expected_exit=xc, label=str(fc.get("label", ""))))

    meta = PlanMeta(
        task_id=str(m["task_id"]),
        goal=str(m.get("goal", "")),
        done_criterion=str(m.get("done_criterion", "")),
        criterion_type=str(m.get("criterion_type", CriterionType.MEASURABLE.value)),
        weight_class=str(raw_weight) if raw_weight is not None else None,
        external_research=str(m["external_research"]) if m.get("external_research") else None,
        repo_root=str(m["repo_root"]) if m.get("repo_root") else None,
        delivery_worktree=str(m["delivery_worktree"]) if m.get("delivery_worktree") else None,
        final_check=final_checks,
    )

    raw_stages = data.get("stage", [])
    if not raw_stages:
        raise PlanError("plan defines no [[stage]] entries")

    is_substantive = meta.weight_class is not None and meta.weight_class.lower() == "substantive"

    if strict and is_substantive and not meta.external_research:
        raise PlanError(
            "[meta] missing 'external_research' (required for substantive plans): "
            "record whether internet/intranet research for information or ideas would "
            "improve the plan, or one line on why it is not warranted"
        )

    stages: list[Stage] = []
    for i, s in enumerate(raw_stages, start=1):
        index = int(s.get("index", i))
        for required in ("title", "executor", "expected_result_image", "done_criterion"):
            if not s.get(required):
                raise PlanError(f"stage {index} missing {required!r}")
        if strict and not _EXECUTOR_RE.match(str(s["executor"])):
            raise PlanError(
                f"stage {index} executor {s['executor']!r} is outside the vocabulary "
                "(expected 'in_thread' or 'spawn:<kind>')"
            )
        if strict and is_substantive:
            _validate_substantive_stage(s, index)
        raw_principle = s.get("principle")
        principle = None
        if isinstance(raw_principle, dict) and raw_principle:
            if strict:
                # Submission grade: _validate_substantive_stage already guaranteed
                # the required subfields, so a missing one here is a genuine bug —
                # keep direct indexing so it fails loudly rather than silently.
                principle = Principle(
                    statement=str(raw_principle["statement"]),
                    source=str(raw_principle["source"]),
                    derivation=str(raw_principle.get("derivation", "")),
                    confidence=str(raw_principle["confidence"]),
                    refutation=str(raw_principle["refutation"]),
                )
            else:
                # Read-only baseline: a snapshot frozen before a subfield became
                # required must parse to a partial Principle, not raise KeyError.
                principle = Principle(
                    statement=str(raw_principle.get("statement", "")),
                    source=str(raw_principle.get("source", "")),
                    derivation=str(raw_principle.get("derivation", "")),
                    confidence=str(raw_principle.get("confidence", "")),
                    refutation=str(raw_principle.get("refutation", "")),
                )
        stages.append(
            Stage(
                index=index,
                title=str(s["title"]),
                subject=Subject(
                    material=str(s.get("material", "")),
                    result=str(s["expected_result_image"]),
                    invariants=str(s["invariants"]) if s.get("invariants") else None,
                ),
                means=Means(
                    means=str(s.get("means", "")),
                    method=str(s.get("method", "")),
                ),
                actor=Actor(
                    executor=str(s["executor"]),
                    capability_required=(
                        str(s["capability_required"]) if s.get("capability_required") else None
                    ),
                ),
                criterion=Criterion(
                    criterion_type=str(s.get("criterion_type", CriterionType.MEASURABLE.value)),
                    done_criterion=str(s["done_criterion"]),
                    verify_command=(
                        str(s["verify_command"]) if s.get("verify_command") else None
                    ),
                    expected_exit=int(s.get("expected_exit", 0)),
                ),
                principle=principle,
                conditions=str(s["conditions"]) if s.get("conditions") else None,
                supplies=_build_supplies(s, index),
                output_artifacts=[str(p) for p in s.get("output_artifacts", [])],
                outcome=Outcome(status=StageStatus.PENDING.value),
            )
        )

    indices = [s.index for s in stages]
    if len(set(indices)) != len(indices):
        raise PlanError(f"duplicate stage indices: {indices}")
    _validate_graph(stages, is_substantive=is_substantive)
    return PlanDoc(meta=meta, stages=stages)


def load_plan(
    path: str | Path, *, strict: bool = True, strict_executor: bool | None = None
) -> PlanDoc:
    p = Path(path)
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_plan(data, strict=strict, strict_executor=strict_executor)


def _structural_signature(doc: PlanDoc) -> dict:
    """The fields whose change makes a replan substantive."""
    return {
        "done_criterion": doc.meta.done_criterion,
        "criterion_type": doc.meta.criterion_type,
        "weight_class": doc.meta.weight_class,
        "stages": {
            s.index: (
                s.actor.executor,
                tuple(sorted(s.depends_on)),
                s.criterion.done_criterion,
                s.criterion.criterion_type,
            )
            for s in doc.stages
        },
    }


def stage_carry_key(stage) -> tuple:
    """Full-fidelity per-stage identity for PASSED carry-forward across a
    substantive replan (#12): a stage keeps its PASSED status only if NOTHING about
    its definition changed.

    A superset of `_structural_signature`'s per-stage tuple (executor / deps /
    done_criterion / criterion_type) PLUS the prose fields (title / result /
    invariants / means / method / conditions / verify_command / expected_exit).
    Kept SEPARATE from `_structural_signature` (which drives diff_plans'
    refinement-vs-substantive classification) so that extending the carry-forward
    key never reclassifies a prose refinement as substantive — the two answer
    different questions and must evolve independently. Operates on a Stage, so both
    plan-doc stages and live SessionState stages key identically."""
    return (
        stage.actor.executor,
        tuple(sorted(stage.depends_on)),
        stage.criterion.done_criterion,
        stage.criterion.criterion_type,
        stage.criterion.verify_command,
        stage.criterion.expected_exit,
        stage.title,
        stage.subject.result,
        stage.subject.invariants,
        stage.means.means,
        stage.means.method,
        stage.conditions,
    )


def stage_question_key(stage) -> str:
    """Stable digest of a stage's FULL definition, used by premise.py to decide
    whether a disposed Question bound to `stage:<n>.<element>` still targets the
    same bytes it was answered against.

    A THIRD member of the key family beside `_structural_signature` (drives
    replan refinement-vs-substantive classification) and `stage_carry_key` (drives
    PASSED carry-forward): it answers a THIRD question — 'did the bytes this
    question was answered against change?' — distinct from either of the other
    two, so per the convention `stage_carry_key`'s own docstring states (the keys
    "answer different questions and must evolve independently"), it is a new
    function rather than an extension of `stage_carry_key`.

    Unlike `stage_carry_key`, this covers every field a Question.target can
    legally name (text_shape.ELEMENT_NAMES: material, result, invariants, means,
    method, executor, capability, criterion, done_criterion, principle,
    conditions) — including `principle` and `supplies`, which `stage_carry_key`
    omits because carry-forward never needed them. A question targeting
    `stage:<n>.principle` must be invalidated when that principle is rewritten;
    `stage_carry_key` would not notice, so it cannot be reused for this purpose.

    Returns a stable sha256 hex digest, not a tuple: the value is persisted in
    Question.disposed_at_key and compared across processes, so it must survive a
    JSON round-trip byte-for-byte (a tuple would not, once JSON turns it into a
    list)."""
    principle = stage.principle
    principle_tuple = (
        (principle.statement, principle.source, principle.derivation,
         principle.confidence, principle.refutation)
        if principle is not None else None
    )
    supplies_tuple = tuple((s.on, s.element, s.artifact) for s in stage.supplies)
    payload = repr((
        stage.actor.executor,
        stage.actor.capability_required,
        tuple(sorted(stage.depends_on)),
        stage.criterion.done_criterion,
        stage.criterion.criterion_type,
        stage.criterion.verify_command,
        stage.criterion.expected_exit,
        stage.title,
        stage.subject.material,
        stage.subject.result,
        stage.subject.invariants,
        stage.means.means,
        stage.means.method,
        stage.conditions,
        principle_tuple,
        supplies_tuple,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_plans(old: PlanDoc, new: PlanDoc) -> str:
    """Return 'no_change' | 'refinement' | 'substantive'."""
    if _structural_signature(old) != _structural_signature(new):
        return "substantive"
    # Structurally identical — any other change is a refinement. The means/method/
    # conditions/invariants are included so that adjusting a stage's MEANS to remove
    # a difficulty (the overcome-difficulty replan) classifies as 'refinement', not
    # 'no_change' — otherwise the corrected means would be silently dropped.
    def _prose(doc: PlanDoc):
        return [
            (s.index, s.title, s.subject.result, s.subject.invariants,
             s.means.means, s.means.method, s.conditions,
             s.criterion.verify_command, s.criterion.expected_exit)
            for s in doc.stages
        ]
    def _fc(doc: PlanDoc):
        return [(fc.command, fc.expected_exit, fc.label) for fc in doc.meta.final_check]
    if (_prose(old) != _prose(new) or old.meta.goal != new.meta.goal
            or old.meta.repo_root != new.meta.repo_root
            or _fc(old) != _fc(new)):
        return "refinement"
    return "no_change"
