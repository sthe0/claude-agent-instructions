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
    output_artifacts = ["scripts/agentctl/"]  # optional

For substantive plans (meta.weight_class = "substantive") the [meta] table must
also carry a plan-level external-research decision:

    external_research = "checked internal wiki + WebSearch; no prior art applies"
                                         # required for substantive; what
                                         # internet/intranet research found, or
                                         # why it is not warranted. Mirrors the
                                         # markdown `External research:` line
                                         # checked by verify-plan-file.py.

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
    confidence = "high"                  # high | medium | low
    refutation = "..."

diff_plans classifies a replan as no_change / refinement / substantive, mirroring
CLAUDE.md § Acting without asking: structural edits (stage set, dependencies,
executors, done criteria, weight_class) are substantive and re-arm the plan-approval
gate; wording-only edits (titles, expected-result prose) are refinements.
"""
from __future__ import annotations

import re
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
    # non-empty for substantive plans; None for legacy/non-substantive. Mirrors the
    # markdown `External research:` line checked by verify-plan-file.py.
    external_research: str | None = None
    # Directory each stage's verify_command runs in. None (default) inherits the
    # invoker's cwd — byte-identical to pre-repo_root behaviour. Set it so a plan's
    # repo-relative verify paths resolve no matter where the engine is driven from.
    repo_root: str | None = None
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
_PRINCIPLE_SUBFIELDS = ("statement", "source", "confidence", "refutation")

# The activity-ontology elements a stage may supply to a dependent stage. A
# substantive stage's Supply.element must name one of these (or be absent).
_ELEMENT_NAMES = frozenset(
    {
        "material", "result", "invariants",   # subject cluster
        "means", "method",                    # means cluster
        "executor", "capability",             # actor cluster
        "criterion", "done_criterion",        # criterion cluster
        "principle", "conditions",
    }
)


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
# Difficulty removed: a stage's verify_command running a whole aggregate suite
# (verify-all.py, a bare pytest invocation) without scoping to the paths the
# stage actually touches lets pre-existing, unrelated reds elsewhere in the
# repo false-fail the stage — a recurring authoring miss (experience leaf
# 2026-06-29, ~20 accumulated contexts). This is the DECIDABLE rule part
# (does the command look like an unscoped aggregate run); whether a
# whole-suite run is actually justified for a given stage is perception left
# to the plan author — hence advisory, never a block.
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


def verify_command_scope_warnings(stages) -> list[str]:
    """Warn (never block) when a stage's verify_command runs an aggregate test
    suite (verify-all.py, a bare pytest invocation) without narrowing it to the
    gate that enforces the stage — the miss recorded in experience leaf
    2026-06-29 (~20 accumulated contexts). One warning per offending stage."""
    warnings: list[str] = []
    for s in stages:
        cmd = s.criterion.verify_command
        if not cmd:
            continue
        for sub in re.split(r"&&|;|\|", cmd):
            sub = sub.strip()
            if sub and _subcommand_is_aggregate_unscoped(sub):
                warnings.append(
                    f"stage {s.index} ({s.title!r}): verify_command runs an aggregate "
                    f"suite without a scope flag ({sub!r}); scope it to the gate that "
                    f"enforces it (--staged, or an explicit test path) so pre-existing "
                    f"unrelated reds cannot false-fail the stage "
                    f"(see experience leaf 2026-06-29)."
                )
                break
    return warnings


def parse_plan(data: dict, *, strict_executor: bool = True) -> PlanDoc:
    """Pure: a parsed-TOML dict -> PlanDoc. No filesystem.

    strict_executor=True (default) rejects any stage executor outside the
    {in_thread, spawn:<kind>} vocabulary — the path every newly authored or
    resubmitted plan goes through (cmd_submit_plan, the new side of cmd_replan).
    strict_executor=False tolerates legacy free-text executors when loading a
    plan purely for read-only comparison (cmd_replan's OLD/approved-snapshot
    side) — plans approved before this vocabulary existed must stay diffable
    without retroactively bricking their session's replan flow."""
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
        final_check=final_checks,
    )

    raw_stages = data.get("stage", [])
    if not raw_stages:
        raise PlanError("plan defines no [[stage]] entries")

    is_substantive = meta.weight_class is not None and meta.weight_class.lower() == "substantive"

    if is_substantive and not meta.external_research:
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
        if strict_executor and not _EXECUTOR_RE.match(str(s["executor"])):
            raise PlanError(
                f"stage {index} executor {s['executor']!r} is outside the vocabulary "
                "(expected 'in_thread' or 'spawn:<kind>')"
            )
        if is_substantive:
            _validate_substantive_stage(s, index)
        raw_principle = s.get("principle")
        principle = (
            Principle(
                statement=str(raw_principle["statement"]),
                source=str(raw_principle["source"]),
                confidence=str(raw_principle["confidence"]),
                refutation=str(raw_principle["refutation"]),
            )
            if isinstance(raw_principle, dict) and raw_principle
            else None
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
                outcome=Outcome(status=StageStatus.PENDING.value),
            )
        )

    indices = [s.index for s in stages]
    if len(set(indices)) != len(indices):
        raise PlanError(f"duplicate stage indices: {indices}")
    _validate_graph(stages, is_substantive=is_substantive)
    return PlanDoc(meta=meta, stages=stages)


def load_plan(path: str | Path, *, strict_executor: bool = True) -> PlanDoc:
    p = Path(path)
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_plan(data, strict_executor=strict_executor)


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
