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
    cost_tier = "medium"                  # optional; small|medium|large. Declares the
                                          # stage's expected size: dispatch reads it as the
                                          # spawn budget label, and the effort-divergence
                                          # estimate sums it over the plan. Absent means
                                          # "medium" at the point of use -- an inferred
                                          # default, not a norm anyone chose, so declare it
                                          # on any stage whose size is not typical.
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

a non-empty goal and done_criterion, at least one [[final_check]], and the typed
order the plan serves:

    [meta.order]
    customer_id = "user"                 # machine-comparable identifier of the
                                         # position the order came from
    customer = "the user, as the position that posed the task"
                                         # the prose that identifier names —
                                         # a PAIR, because an acceptance author
                                         # is compared against the identifier
    functional_place = "the norm that governs an act of activity here"
                                         # the place this plan's product fills,
                                         # need being that place stripped of an
                                         # adequate filling
    requirements = [                     # id/text PAIRS, not sentences: the id
      { id = "R1", text = "..." },       # is the key the coverage map and the
      { id = "R2", text = "..." },       # acceptance verdicts range over
    ]

    [meta.order.coverage]                # requirement id -> the controls that
    R1 = ["stage 2 verify_command"]      # decide it; every declared id needs an
    R2 = ["final_check 1"]               # entry (totality is machine-checked,
                                         # sufficiency is review)

Those five are SUBMISSION-seam requirements (submission.py), not loader ones: the
loader parses [meta.order] and can never refuse it, so a plan approved before the
table existed still re-reads cleanly inside its own live session.

and every stage must also carry the 8-element activity-structure fields:

    material = "..."
    means = "..."
    method = "..."                       # the REQUIREMENT on the way of acting: what
                                         # the transformation must be an instance of
    procedure = "1. ... 2. ..."          # the SEQUENCE of operations proposed for
                                         # meeting that requirement — the executor's
                                         # own, replaceable via replan --renormalize
    conditions = "..."                   # what must hold OF THE WORLD for the stage's
                                         # transformation to go through
    preconditions = "..."                # what must already be true before the stage
                                         # may START (inherited from outside it)
    knowledge = "..."                    # the знание the stage acts FROM
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

`procedure`, `preconditions` and `knowledge` are SUBMISSION-seam requirements too, for
the same reason [meta.order] is: they were added after the corpus was frozen, so the
loader parses them and can never refuse their absence.

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
    CheckKind,
    CheckVenue,
    Confidence,
    Criterion,
    CriterionType,
    FinalCheck,
    LandedSpec,
    LANDED_GIT_ERROR_EXIT,
    Means,
    Order,
    Outcome,
    Principle,
    Stage,
    StageStatus,
    Subject,
    Supply,
)
from .text_shape import ELEMENT_NAMES as _ELEMENT_NAMES
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import WHOLE_STAGE_ELEMENT
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
    # check_venue_warnings lint below.
    delivery_worktree: str | None = None
    # Optional typed end-to-end checks run by verify-final after per-stage re-runs.
    # Absent => [] (back-compat). Parsed from top-level [[final_check]] tables.
    final_check: list[FinalCheck] = field(default_factory=list)
    # The order this plan serves, typed (state.Order), parsed from [meta.order].
    # None (the default) is every plan authored before the table existed, and the
    # parse can never refuse: requiredness is a submission-seam grade
    # (submission._order_violations), so the loader stays exactly as permissive.
    order: "Order | None" = None


@dataclass
class PlanDoc:
    meta: PlanMeta
    stages: list[Stage] = field(default_factory=list)


class PlanError(Exception):
    """The TOML plan is missing required structure."""


_CHECK_VENUE_VALUES = {v.value for v in CheckVenue}


def _parse_check_venue(raw: object, context: str) -> str:
    """Validate a stage's `verify_venue` or a final_check's `venue` against the
    CheckVenue vocabulary, defaulting to "delivery" when absent so an
    un-annotated check keeps observing the same tree dispatch wrote to."""
    if raw is None:
        return CheckVenue.DELIVERY.value
    value = str(raw)
    if value not in _CHECK_VENUE_VALUES:
        raise PlanError(
            f"{context} venue {value!r} is not one of {sorted(_CHECK_VENUE_VALUES)}"
        )
    return value


def _parse_verify_venue_at_final(raw: object, context: str) -> str | None:
    """Validate a stage's optional `verify_venue_at_final` against the same
    CheckVenue vocabulary as `verify_venue` (schema 24). Unlike
    `_parse_check_venue`, absence is NOT defaulted to "delivery" — it returns
    None, distinguishing "not declared" (V4: resolves to verify_venue at read
    time via SessionState.resolve_final_check_venue) from "declared and equal"."""
    if raw is None:
        return None
    value = str(raw)
    if value not in _CHECK_VENUE_VALUES:
        raise PlanError(
            f"{context} verify_venue_at_final {value!r} is not one of {sorted(_CHECK_VENUE_VALUES)}"
        )
    return value


_CHECK_KIND_VALUES = {v.value for v in CheckKind}


def _parse_check_kind(raw: object, context: str) -> str:
    """Validate a stage's `verify_kind` or a final_check's `kind` against the
    CheckKind vocabulary, defaulting to "shell" when absent — mirrors
    _parse_check_venue exactly (schema 23). A free-text kind is rejected the
    same way an out-of-vocabulary venue is."""
    if raw is None:
        return CheckKind.SHELL.value
    value = str(raw)
    if value not in _CHECK_KIND_VALUES:
        raise PlanError(
            f"{context} kind {value!r} is not one of {sorted(_CHECK_KIND_VALUES)}"
        )
    return value


def _parse_landed_venue(raw_venue: object, context: str) -> str:
    """A landed check's venue is always "repo_root" (R3): default it there when
    absent, reject any other EXPLICIT value. Deliberately bypasses
    _parse_check_venue's "delivery" default — the two kinds disagree on it,
    since a landed assertion is about the canonical checkout's trunk, not the
    delivery worktree."""
    if raw_venue is None:
        return CheckVenue.REPO_ROOT.value
    value = str(raw_venue)
    if value != CheckVenue.REPO_ROOT.value:
        raise PlanError(
            f"{context}: a landed check's venue must be \"repo_root\" (got {value!r}); "
            f"the assertion is about the canonical checkout's trunk, so an "
            f"explicit \"delivery\" (or any other) venue is rejected rather than "
            f"silently overridden"
        )
    return value


# A landed check's target/remote are git ref NAMES, never shell content: no
# whitespace, no shell metacharacters. Structural validation of a name's SHAPE,
# not classification of free-text meaning.
_LANDED_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _parse_landed_spec(
    raw_table: object,
    *,
    kind: str,
    context: str,
    stage_indices: set[int],
    owner_index: int | None,
) -> "LandedSpec | None":
    """Validate and build the typed LandedSpec payload of a `kind = "landed"`
    check (R2/R4/R5); reject a `[*.landed]` table on a kind="shell" check (R7 —
    never silently ignored). `owner_index` is the declaring stage's own index
    for a stage criterion, or None for a [[final_check]] (which may name any
    existing stage index — it runs after every stage). Returns None for a
    shell check carrying no landed table (the common case)."""
    if kind != CheckKind.LANDED.value:
        if raw_table:
            raise PlanError(
                f"{context}: a [*.landed] table is only valid with kind = "
                f"\"landed\" (this check is kind={kind!r}); drop the table or "
                f"set the kind (R7)"
            )
        return None
    if not isinstance(raw_table, dict) or not raw_table:
        raise PlanError(f"{context}: kind = \"landed\" requires a [*.landed] table")
    target = raw_table.get("target")
    if not target or not isinstance(target, str):
        raise PlanError(f"{context}: landed.target is required (non-empty string) (R2)")
    if not _LANDED_REF_RE.match(target):
        raise PlanError(
            f"{context}: landed.target {target!r} is not a valid git ref name "
            f"(expected to match {_LANDED_REF_RE.pattern}) (R2)"
        )
    remote = str(raw_table.get("remote", "origin"))
    if not _LANDED_REF_RE.match(remote):
        raise PlanError(
            f"{context}: landed.remote {remote!r} is not a valid git ref name "
            f"(expected to match {_LANDED_REF_RE.pattern}) (R2)"
        )
    raw_stage = raw_table.get("delivered_stage")
    if raw_stage is None:
        raise PlanError(f"{context}: landed.delivered_stage is required (R4)")
    delivered_stage = int(raw_stage)
    if delivered_stage not in stage_indices:
        raise PlanError(
            f"{context}: landed.delivered_stage {delivered_stage} does not name "
            f"an existing stage (R4)"
        )
    if owner_index is not None and delivered_stage > owner_index:
        raise PlanError(
            f"{context}: landed.delivered_stage {delivered_stage} is later than "
            f"the declaring stage {owner_index} — a forward reference cannot "
            f"have been recorded yet (self-reference, delivered_stage == "
            f"{owner_index}, is fine) (R5)"
        )
    return LandedSpec(target=target, remote=remote, delivered_stage=delivered_stage)


# The only two executor shapes the engine dispatches: in-thread, or a named spawn
# kind matching a spawn-specialist.py --kind. Anything else (a typo, a free-text
# description) must be rejected at submission — a silent default to in_thread
# degrades the whole plan to in-thread execution with no visible error (#7).
_EXECUTOR_RE = re.compile(r"^(in_thread|spawn:[a-z][a-z0-9_-]*)$")
# Mirrors spawn-specialist.py's --budget choices and config.md's budget-<tier>-usd rows.
# Rejected at submission rather than defaulted silently: an unrecognized tier would
# otherwise surface as an argparse usage error three layers away in the spawn, or as a
# KeyError raised from inside cmd_approve when the effort estimate reads the config row.
_COST_TIERS = ("small", "medium", "large")

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
    verify_kind = str(s.get("verify_kind", CheckKind.SHELL.value))
    if (
        crit_type == CriterionType.MEASURABLE.value
        and not s.get("verify_command")
        and verify_kind != CheckKind.LANDED.value
    ):
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


# --- check-venue lint (advisory, never blocking) -----------------------------
# Difficulty removed: schema 22 made the check venue a DECLARED field
# (Criterion.verify_venue / FinalCheck.venue, resolved by
# SessionState.resolve_check_venue) shared by dispatch and every verify site.
# Once the venue is decidable from that field, a lint that still GUESSES the
# intended venue from a `cd` target is a second, weaker copy of the same rule —
# its disagreements with the field are unresolvable. This lint is therefore
# rebased on CONTRADICTION: it warns only when a check's first `cd` target
# disagrees with the venue it itself declares (default "delivery"), and stays
# silent when a check declares venue = "repo_root" and cd's to canon — that is
# now a deliberate, reviewable declaration, not a suspected mistake. Perception
# (inferring a `cd` target from free-text command bodies) stays a lint; the
# rule (which venue is intended) lives in the field. Still advisory-only:
# fires only when [meta] delivery_worktree names a venue distinct from repo_root.
def check_venue_warnings(
    stages, final_check, repo_root: str | None, delivery_worktree: str | None
) -> list[str]:
    """Warn (never block) when a stage verify_command or a final_check `cd`s
    into a tree that CONTRADICTS its own declared venue: a "delivery"-venue
    check cd-ing into the canonical repo_root, or a "repo_root"-venue check
    cd-ing into the delivery worktree. Silent when the declared venue and the
    `cd` target agree (including a "repo_root"-venue check cd-ing to canon —
    the intentional post-landing confirmation), or when delivery_worktree is
    unset (no second venue exists to contradict) or repo_root is unset
    (nothing to resolve relative `cd` targets against). Also carries the
    schema-24 survivability warning: a bare "delivery"-venue stage check in a
    plan that asserts landing, which will refuse at verify-final once the
    delivery venue is gone — see the check near the end of this function."""
    if not delivery_worktree or not repo_root:
        return []
    repo_root_p = Path(repo_root).resolve()
    worktree_p = Path(delivery_worktree).resolve()

    def _first_cd_target(command: str) -> Path | None:
        for sub in re.split(r"&&|;|\|", command):
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
            return target.resolve()
        return None

    warnings: list[str] = []

    def _warn_if_contradicts_venue(command: str, venue: str, where: str) -> None:
        target = _first_cd_target(command)
        if target is None:
            return
        under_repo_root = target == repo_root_p or repo_root_p in target.parents
        under_worktree = target == worktree_p or worktree_p in target.parents
        if venue == CheckVenue.REPO_ROOT.value:
            if under_worktree and not under_repo_root:
                warnings.append(
                    f"{where} declares venue = \"repo_root\" but cd's into the "
                    f"delivery worktree {delivery_worktree}; cd into {repo_root} "
                    f"to match its declared venue, or drop the venue override if "
                    f"the worktree is the intended target."
                )
        else:
            if under_repo_root and not under_worktree:
                warnings.append(
                    f"{where} cd's into the canonical repo_root but its declared "
                    f"venue is \"delivery\"; cd into {delivery_worktree} so the "
                    f"check runs where the un-landed change lives, or declare "
                    f"venue = \"repo_root\" if this check is the intentional "
                    f"post-landing confirmation."
                )

    for s in stages or []:
        if s.criterion.verify_command:
            _warn_if_contradicts_venue(
                s.criterion.verify_command,
                s.criterion.verify_venue,
                f"stage {s.index} ({s.title!r}) verify_command",
            )
    for fi, fc in enumerate(final_check or [], 1):
        label = fc.label or fc.command
        _warn_if_contradicts_venue(fc.command, fc.venue, f"final_check {fi} ({label!r})")

    # Survivability warning (schema 24): a plan that asserts landing removes its
    # own delivery venue as part of that landing, so a "delivery"-venue stage
    # check with no `verify_venue_at_final` WILL refuse the moment verify-final
    # re-runs it — the exact defect this schema-24 field exists to let a plan
    # opt out of. Fires only when the plan also asserts landing somewhere
    # (a `kind = "landed"` stage or final_check): with no landed assertion the
    # delivery venue has no declared reason to disappear, so warning would be
    # noise. Deliberately advisory, never a blocker — `--keep-branch` lets a
    # delivery venue legitimately survive landing, so the condition is a strong
    # signal, not a proof. Restricted to measurable stages because verify-final
    # re-runs a verify_command only for those (an acceptance-review stage's
    # command never re-runs at final, so it cannot refuse there).
    asserts_landing = any(
        s.criterion.verify_kind == CheckKind.LANDED.value for s in stages or []
    ) or any(fc.kind == CheckKind.LANDED.value for fc in final_check or [])
    if asserts_landing:
        for s in stages or []:
            crit = s.criterion
            if (
                crit.verify_command
                and crit.criterion_type == CriterionType.MEASURABLE.value
                and crit.verify_kind != CheckKind.LANDED.value
                and crit.verify_venue == CheckVenue.DELIVERY.value
                and crit.verify_venue_at_final is None
            ):
                warnings.append(
                    f"stage {s.index} ({s.title!r}) declares venue = \"delivery\" "
                    f"with no verify_venue_at_final, but this plan asserts landing "
                    f"elsewhere — landing removes the delivery worktree, so this "
                    f"check will REFUSE at verify-final unless the worktree happens "
                    f"to survive (e.g. `land-branch.py --keep-branch`); declare "
                    f"verify_venue_at_final = \"repo_root\" if the check should "
                    f"re-verify against the landed artifact instead."
                )
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

    raw_stages = data.get("stage", [])
    if not raw_stages:
        raise PlanError("plan defines no [[stage]] entries")
    # Pre-scanned so a [[final_check]]'s landed.delivered_stage (R4) can be
    # validated against the full stage-index domain before any Stage is built;
    # the per-stage loop below reuses the same domain for its own R4/R5 checks.
    _stage_index_domain = {int(s.get("index", i)) for i, s in enumerate(raw_stages, start=1)}

    raw_fcs = data.get("final_check", [])
    final_checks: list[FinalCheck] = []
    for fi, fc in enumerate(raw_fcs, 1):
        fc_ctx = f"final_check {fi}"
        fc_kind = _parse_check_kind(fc.get("kind"), fc_ctx)
        fc_landed = _parse_landed_spec(
            fc.get("landed"), kind=fc_kind, context=fc_ctx,
            stage_indices=_stage_index_domain, owner_index=None,
        )
        if fc_kind == CheckKind.LANDED.value:
            if fc.get("command"):
                raise PlanError(
                    f"{fc_ctx}: kind = \"landed\" must not carry 'command' (R1) "
                    f"— the engine synthesizes the check"
                )
            if "expected_exit" in fc:
                raise PlanError(
                    f"{fc_ctx}: kind = \"landed\" must not carry 'expected_exit' "
                    f"(R1) — the synthesized command's exit contract is fixed "
                    f"(0 contained / 1 not landed / {LANDED_GIT_ERROR_EXIT} git error)"
                )
            cmd = ""
            xc = 0
            venue = _parse_landed_venue(fc.get("venue"), fc_ctx)
        else:
            cmd = fc.get("command", "")
            if not cmd or not isinstance(cmd, str):
                raise PlanError(f"{fc_ctx} missing 'command' (required, non-empty string)")
            xc = fc.get("expected_exit", 0)
            if not isinstance(xc, int):
                raise PlanError(f"{fc_ctx} expected_exit must be an int")
            venue = _parse_check_venue(fc.get("venue"), fc_ctx)
        final_checks.append(
            FinalCheck(
                command=cmd, expected_exit=xc, label=str(fc.get("label", "")),
                venue=venue, kind=fc_kind, landed=fc_landed,
            )
        )

    # Additive and unconditionally lenient, like `Order.from_dict`: a refusal here would
    # be retroactive over every plan a live session re-reads, which is why refusals live
    # in submission.py instead. A present, non-dict `order` is recorded as
    # `malformed=("order",)` rather than left `None` — see `Order.malformed` for why.
    raw_order = m.get("order")
    if isinstance(raw_order, dict):
        order = Order.from_dict(raw_order)
    elif raw_order is not None:
        order = Order(malformed=("order",))
    else:
        order = None

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
        order=order,
    )

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
        if strict and s.get("cost_tier") and str(s["cost_tier"]) not in _COST_TIERS:
            raise PlanError(
                f"stage {index} cost_tier {s['cost_tier']!r} is outside the vocabulary "
                f"(expected one of {'|'.join(sorted(_COST_TIERS))})"
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
        stage_ctx = f"stage {index}"
        crit_type = str(s.get("criterion_type", CriterionType.MEASURABLE.value))
        verify_kind = _parse_check_kind(s.get("verify_kind"), stage_ctx)
        landed = _parse_landed_spec(
            s.get("landed"), kind=verify_kind, context=stage_ctx,
            stage_indices=_stage_index_domain, owner_index=index,
        )
        raw_vvaf = s.get("verify_venue_at_final")
        if verify_kind == CheckKind.LANDED.value:
            if s.get("verify_command"):
                raise PlanError(
                    f"{stage_ctx}: verify_kind = \"landed\" must not carry "
                    f"'verify_command' (R1) — the engine synthesizes the check"
                )
            if "expected_exit" in s:
                raise PlanError(
                    f"{stage_ctx}: verify_kind = \"landed\" must not carry "
                    f"'expected_exit' (R1) — the synthesized command's exit "
                    f"contract is fixed (0 contained / 1 not landed / "
                    f"{LANDED_GIT_ERROR_EXIT} git error)"
                )
            if crit_type != CriterionType.MEASURABLE.value:
                raise PlanError(
                    f"{stage_ctx}: verify_kind = \"landed\" requires "
                    f"criterion_type = \"measurable\" (a landed assertion is "
                    f"objective, never acceptance-review) (R6)"
                )
            if raw_vvaf is not None:
                raise PlanError(
                    f"{stage_ctx}: verify_venue_at_final must not be declared "
                    f"on a verify_kind = \"landed\" criterion (V2) — a landed "
                    f"check's venue is already fixed at repo_root (R3), so a "
                    f"second venue is meaningless"
                )
            verify_venue = _parse_landed_venue(s.get("verify_venue"), stage_ctx)
            verify_command = None
            expected_exit = 0
            verify_venue_at_final = None
        else:
            verify_venue = _parse_check_venue(s.get("verify_venue"), stage_ctx)
            verify_command = str(s["verify_command"]) if s.get("verify_command") else None
            expected_exit = int(s.get("expected_exit", 0))
            verify_venue_at_final = _parse_verify_venue_at_final(raw_vvaf, stage_ctx)
            if (
                verify_venue_at_final is not None
                and not meta.delivery_worktree
                and verify_venue_at_final != verify_venue
            ):
                raise PlanError(
                    f"{stage_ctx}: verify_venue_at_final {verify_venue_at_final!r} "
                    f"differs from verify_venue {verify_venue!r} but [meta] "
                    f"delivery_worktree is unset — there is no second venue for "
                    f"it to name (V3)"
                )
        stages.append(
            Stage(
                index=index,
                title=str(s["title"]),
                subject=Subject(
                    material=str(s.get("material", "")),
                    result=str(s["expected_result_image"]),
                    invariants=str(s["invariants"]) if s.get("invariants") else None,
                    material_refs=[str(r) for r in s.get("material_refs", [])],
                    knowledge_refs=[str(r) for r in s.get("knowledge_refs", [])],
                ),
                means=Means(
                    means=str(s.get("means", "")),
                    method=str(s.get("method", "")),
                    procedure=str(s.get("procedure", "")),
                ),
                actor=Actor(
                    executor=str(s["executor"]),
                    capability_required=(
                        str(s["capability_required"]) if s.get("capability_required") else None
                    ),
                    cost_tier=str(s["cost_tier"]) if s.get("cost_tier") else None,
                ),
                criterion=Criterion(
                    criterion_type=crit_type,
                    done_criterion=str(s["done_criterion"]),
                    verify_command=verify_command,
                    expected_exit=expected_exit,
                    verify_venue=verify_venue,
                    verify_kind=verify_kind,
                    landed=landed,
                    verify_venue_at_final=verify_venue_at_final,
                ),
                principle=principle,
                conditions=str(s["conditions"]) if s.get("conditions") else None,
                # Same permissive parse, same reason as `knowledge` below: the requirement
                # that a substantive stage declare its starting preconditions — and the
                # refusal of a `conditions` that only restates depends_on, which is the
                # other half of the same defect — both live at the submission seam.
                preconditions=(
                    str(s["preconditions"]) if s.get("preconditions") else None
                ),
                # Parsed permissively on BOTH load modes — the знание requirement lives at
                # the submission seam (submission.py), never in an `if strict:` branch
                # here, because load_plan is re-read in-session from seven call sites and a
                # loader-side requirement is retroactive over plans already accepted.
                knowledge=str(s["knowledge"]) if s.get("knowledge") else None,
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
    # A syntax error in the TOML is a malformed plan like any other, so it leaves here as
    # PlanError rather than as the tomllib type. Every caller that already handles a bad
    # plan handles it by catching PlanError; letting TOMLDecodeError through meant the
    # single commonest malformation was the one case none of them caught, surfacing as a
    # traceback instead of the caller's own message.
    with p.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise PlanError(f"malformed TOML in plan file {p}: {exc}") from exc
    return parse_plan(data, strict=strict, strict_executor=strict_executor)


def order_scope(meta) -> tuple:
    """The SCOPE-bearing half of the order, as a contribution to a change-decision key:
    a one-element tuple holding the requirement ids and the coverage map's keys, or the
    EMPTY tuple when the plan declares no order.

    The split between this and `order_place` below is the meta-level answer to the same
    question `_structural_signature` answers per stage — which edits need re-approval.
    ADDING or REMOVING a requirement changes what the plan is for, so it re-arms the
    plan-approval gate; re-wording an existing requirement, the customer prose or the
    functional place does not, any more than re-wording a stage's `material` does.
    Coverage KEYS ride here rather than in the prose half because a key set that no
    longer covers the requirement ids is a scope claim, not a wording one.

    Empty for an order-less plan, so every plan authored before [meta.order] existed
    classifies exactly as it did before this field — the same contribute-only-when-
    declared identity `knowledge_place` and `preconditions_place` keep."""
    order = meta.order
    if order is None:
        return ()
    return ((tuple(r.id for r in order.requirements), tuple(sorted(order.coverage))),)


def order_place(meta) -> tuple:
    """The WHOLE order as a contribution to a change-decision key, or the empty tuple
    when none is declared — the refinement-tier companion to `order_scope`.

    Deliberately a superset rather than the complement: `diff_plans` reads `order_scope`
    first and returns 'substantive' before this is consulted, so overlap costs nothing,
    while a complement would leave a newly added Order field belonging to NEITHER key if
    whoever added it forgot the split. Everything the order holds is therefore covered
    here, and the scope half is the only thing anyone has to remember to extend.
    `malformed` and `requirements_dropped` ride here for that reason and one of their
    own: between them they are the only trace a dropped `requirements = ["a sentence"]`
    leaves, so an edit that turns a readable order into an unreadable one — or that
    changes how much of it is unreadable — would otherwise move no key at all.

    The membership below is a hand-written list, not a derivation over
    `dataclasses.fields(Order)`, because each field needs its own normalization into a
    hashable, order-stable form. So "everything the order holds" is a claim a reader
    cannot check here; `test_order_place_exhausts_the_order_s_field_set` is what makes
    it true, by going red the day a field is added and not listed."""
    order = meta.order
    if order is None:
        return ()
    return ((
        order.customer_id,
        order.customer,
        order.functional_place,
        tuple((r.id, r.text) for r in order.requirements),
        tuple(sorted((k, tuple(v)) for k, v in order.coverage.items())),
        order.malformed,
        order.requirements_dropped,
    ),)


def _structural_signature(doc: PlanDoc) -> dict:
    """The fields whose change makes a replan substantive."""
    return {
        "done_criterion": doc.meta.done_criterion,
        "criterion_type": doc.meta.criterion_type,
        "weight_class": doc.meta.weight_class,
        "order_scope": order_scope(doc.meta),
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


# The absent form of the знание place: no local knowledge, no refs on either projection.
_KNOWLEDGE_PLACE_ABSENT = (None, (), ())


def knowledge_place(stage) -> tuple:
    """The stage's знание place — `knowledge` plus its two ref projections — as a
    contribution to a change-decision key: a ONE-element tuple holding the group, or the
    EMPTY tuple when the stage declares none of the three.

    Shared by all three key functions (stage_carry_key, stage_question_key, diff_plans'
    _prose) so the three cannot drift on which fields the place consists of — the coupling
    is the point, since a field entering one key and not the others is exactly how a
    correction gets silently dropped.

    Grouped rather than spliced field-by-field for a reason the single pre-existing
    conditional field (verify_venue_at_final) never had to face: three independently
    conditional splices collide — (knowledge='x', refs empty) and (knowledge=None,
    material_refs=['x']) would both flatten to ('x',). Nesting the whole place under one
    conditional keeps every combination distinct while preserving the identity that
    matters: a stage declaring NONE of the three contributes nothing at all, so its key is
    byte-identical to the schema-23 key it had before this place existed. That identity is
    load-bearing — stage_question_key is persisted in Question.disposed_at_key and compared
    across processes, so an unconditional contribution (or a `... or ""` default) would
    flip every disposed question of every live session to a spurious staleness blocker."""
    place = (
        stage.knowledge,
        tuple(stage.subject.material_refs),
        tuple(stage.subject.knowledge_refs),
    )
    return () if place == _KNOWLEDGE_PLACE_ABSENT else (place,)


def preconditions_place(stage) -> tuple:
    """The stage's preconditions — what must hold before it may START — as a contribution
    to a change-decision key: a ONE-element tuple holding the value WRAPPED in a tuple of
    its own, or the EMPTY tuple when the stage declares none.

    Shared by all three key functions for the same reason `knowledge_place` is: a field
    that enters one key and not the others is exactly how a correction gets silently
    dropped.

    Two properties carry over from `knowledge_place`, both load-bearing. Undeclared
    contributes NOTHING, so a plan predating this field keeps the exact key it had —
    stage_question_key is persisted in Question.disposed_at_key and compared across
    processes, so an unconditional contribution (or a `... or ""` default) would flip every
    disposed question of every live session to a spurious staleness blocker. And the value
    is NESTED rather than spliced flat, because it is now the second independently
    conditional splice in each key: a preconditions text reading "delivery" would otherwise
    flatten to the same key element as a verify_venue_at_final of "delivery" on a stage
    that declares the other field and not this one."""
    return () if not stage.preconditions else ((stage.preconditions,),)


def procedure_place(stage) -> tuple:
    """The stage's procedure — the SEQUENCE of operations proposed for meeting the
    method's requirement — as a contribution to a change-decision key: a ONE-element
    tuple holding the value TAGGED with this field's name inside a tuple of its own, or
    the EMPTY tuple when the stage declares none.

    Shared by all three key functions for the reason its two siblings are: a field that
    enters one key and not the others is how a correction gets silently dropped. Here the
    consequence is sharper than "dropped", because a whole branch depends on it —
    `diff_plans` classifies an edit that touches only the procedure as `no_change` unless
    this place is in `_prose`, and a `no_change` replan never reaches the renormalization
    the field exists to admit.

    Declared-only, for the reason `preconditions_place` documents: a plan predating the
    field keeps the exact key it had (stage_question_key is persisted in
    Question.disposed_at_key and compared across processes, so `... or ""` would flip
    every disposed question of every live session to a spurious staleness blocker).

    TAGGED, which its two siblings are not, and the tag is what nesting alone turned out
    not to buy. Nesting stops a value flattening into the splices beside it; it does not
    stop two INDEPENDENTLY-conditional splices from producing the same element. This is
    the third conditional splice of `stage_question_key` and `stage_carry_key`, so
    `preconditions = "delivery"` and `procedure = "delivery"` both reduced to
    `(("delivery",),)` and a stage that MOVED one sentence from the first place to the
    second carried its PASSED outcome forward as though nothing had changed. Tagging
    only the new place fixes that without touching either older encoding — the keys of
    every already-disposed question stay byte-identical, which a retrofit of all three
    would not.

    `diff_plans._prose` splices five conditional components rather than three, and its
    two extra ones (`verify_venue_at_final` and `cost_tier`) are BOTH bare strings, so
    the same collision was reachable there between two fields neither of which is this
    one. It is closed at that site instead of here, by tagging them in `_prose` only:
    `_prose` is computed live between two documents and never persisted, so a tag costs
    nothing there, while retagging `verify_venue_at_final` in the two KEYS would flip
    every already-disposed question of every live session."""
    return () if not stage.means.procedure else (("procedure", stage.means.procedure),)


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
        _normalize_string(stage.criterion.verify_venue),
        _normalize_string(stage.criterion.verify_kind),
        stage.criterion.landed,
        # Contribute the field ONLY when declared, so a plan without it hashes
        # byte-identically to a schema-23 plan (the V4 identity), uniform with
        # stage_question_key where this identity is load-bearing across processes.
        *((_normalize_string(stage.criterion.verify_venue_at_final),)
          if stage.criterion.verify_venue_at_final else ()),
        *knowledge_place(stage),
        *preconditions_place(stage),
        *procedure_place(stage),
    )


_WHOLE_STAGE_DEFINITION: tuple[str, ...] | None = None

# Which stage fields constitute each name of the question-target vocabulary, as dotted
# leaf paths of `Stage`. TOTAL over text_shape.ELEMENT_NAMES by construction — a name
# absent here raises KeyError rather than degrading to the whole-stage digest, and
# `test_question_key_scope.py` goes red the moment the vocabulary gains one. Its
# companion test also pins the COMPLEMENT: every leaf of `Stage` is either claimed by a
# name here or recorded there as deliberately unclaimed, so a field added to `Stage`
# cannot end up invalidating nothing by default.
_ELEMENT_FIELDS: dict[str, tuple[str, ...] | None] = {
    "material": ("subject.material", "subject.material_refs",
                 "supplies.on", "supplies.element", "supplies.artifact"),
    "result": ("subject.result",),
    "invariants": ("subject.invariants",),
    "knowledge": ("knowledge", "subject.knowledge_refs"),
    "means": ("means.means",),
    "method": ("means.method",),
    "procedure": ("means.procedure",),
    "executor": ("actor.executor",),
    "capability": ("actor.capability_required",),
    "criterion": ("criterion.criterion_type", "criterion.done_criterion",
                  "criterion.verify_command", "criterion.expected_exit",
                  "criterion.verify_venue", "criterion.verify_kind",
                  "criterion.landed.target", "criterion.landed.delivered_stage",
                  "criterion.landed.remote", "criterion.verify_venue_at_final"),
    "done_criterion": ("criterion.done_criterion",),
    "principle": ("principle.statement", "principle.source", "principle.derivation",
                  "principle.confidence", "principle.refutation"),
    "conditions": ("conditions",),
    "preconditions": ("preconditions",),
    "control": _WHOLE_STAGE_DEFINITION,
    "order": _WHOLE_STAGE_DEFINITION,
    "requirements": _WHOLE_STAGE_DEFINITION,
}


def _leaf_values(stage, path: str) -> tuple:
    """Values reached by a dotted leaf path from a Stage, always as a tuple.

    A list-valued segment PROJECTS rather than terminating: `supplies.on` yields every
    supply's `on`, in declaration order, so the tuple is sensitive to a reordering as
    well as to a rewrite. A None owner short-circuits to `(None,)`, which is why the
    optional structs (`principle`, `criterion.landed`) can be addressed leaf-by-leaf
    without a presence test at every call site — and is unambiguous only because neither
    struct is constructible with all of its own leaves None (`LandedSpec.target` and
    `Principle.statement` are required)."""
    owners: tuple = (stage,)
    for name in path.split("."):
        reached: list = []
        for owner in owners:
            value = None if owner is None else getattr(owner, name)
            if isinstance(value, list):
                reached.extend(value)
            else:
                reached.append(value)
        owners = tuple(reached)
    return owners


def stage_element_keys(stage) -> dict[str, str]:
    """Every change-decision key a question bound to this stage can be checked against:
    one per name of the question-target vocabulary, plus the reserved WHOLE_STAGE_ELEMENT
    entry holding the whole-stage digest.

    Which of the two a given stamp is allowed to match is premise.py's decision, not this
    module's — see `premise._accepted_keys`."""
    keys = {WHOLE_STAGE_ELEMENT: stage_question_key(stage)}
    for name in sorted(_ELEMENT_NAMES):
        keys[name] = stage_question_key(stage, name)
    return keys


def stage_question_key(stage, element: str | None = None) -> str:
    """Stable digest of a stage's FULL definition — or, given an `element`, of just that
    element's contribution — used by premise.py to decide whether a disposed Question
    bound to `stage:<n>.<element>` still targets the same bytes it was answered against.

    With no `element` (and for the three names `_ELEMENT_FIELDS` maps to
    `_WHOLE_STAGE_DEFINITION`) the digest covers the whole stage, byte-for-byte as it did
    before element scoping existed — the identity the back-compatibility of every already
    persisted `disposed_at_key` rests on. With an `element` it hashes that element's
    fields TAGGED with the element's own name, so two elements whose text happens to
    coincide cannot produce one digest (the collision this key family has already been
    bitten by twice — see `procedure_place`).

    The element form is deliberately the STRICTER of the two on the venue fields: it
    hashes `verify_venue` / `verify_kind` / `verify_venue_at_final` raw where the
    whole-stage payload normalizes them, so a whitespace-only edit invalidates a
    `criterion` question that the whole-stage digest would have let stand. Erring toward
    re-confirmation is the safe direction here (the reachable route out is
    `question-rebind --confirm-still-valid`), and matching the normalization would mean
    threading it per path through a walker that has no business knowing which fields are
    prose.

    A THIRD member of the key family beside `_structural_signature` (drives
    replan refinement-vs-substantive classification) and `stage_carry_key` (drives
    PASSED carry-forward): it answers a THIRD question — 'did the bytes this
    question was answered against change?' — distinct from either of the other
    two, so per the convention `stage_carry_key`'s own docstring states (the keys
    "answer different questions and must evolve independently"), it is a new
    function rather than an extension of `stage_carry_key`.

    Unlike `stage_carry_key`, this covers every STAGE FIELD a Question.target can
    legally name — including `principle` and `supplies`, which `stage_carry_key`
    omits because carry-forward never needed them. The vocabulary is not restated
    here (it is text_shape.ELEMENT_NAMES, and a copy of a list rots): read it there.
    Three of its names have no stage field for this key to cover, so a question
    targeting one of those binds to the rest of the stage's definition: `order` and
    `requirements` (both on `[meta.order]`) and `control` (written only by
    `record-result --control`, never parsed from plan TOML). `procedure` was the
    fourth and is one no longer: `Means.procedure` exists, so it is covered here like
    any other field, through `procedure_place`. They are named rather than described
    as a class, because a class with no extension is a standing licence not to cover
    the next member. A question targeting
    `stage:<n>.principle` must be invalidated when that principle is rewritten;
    `stage_carry_key` would not notice, so it cannot be reused for this purpose.

    Returns a stable sha256 hex digest, not a tuple: the value is persisted in
    Question.disposed_at_key and compared across processes, so it must survive a
    JSON round-trip byte-for-byte (a tuple would not, once JSON turns it into a
    list)."""
    if element is not None:
        paths = _ELEMENT_FIELDS[element]
        if paths is not _WHOLE_STAGE_DEFINITION:
            payload = repr((element, tuple(_leaf_values(stage, p) for p in paths)))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
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
        _normalize_string(stage.criterion.verify_venue),
        _normalize_string(stage.criterion.verify_kind),
        stage.criterion.landed,
        # Persisted in Question.disposed_at_key and compared at the plan_approval
        # gate across processes, so an absent field MUST reproduce the schema-23
        # digest exactly (the V4 identity) — contribute it only when declared,
        # never as `... or ""` (which would flip every disposed question of every
        # live session to a spurious "stage definition changed" blocker).
        *((_normalize_string(stage.criterion.verify_venue_at_final),)
          if stage.criterion.verify_venue_at_final else ()),
        # `knowledge` is a legal Question.target (it is in ELEMENT_NAMES), and
        # material_refs is material's structural projection — a question answered
        # against the old material must be invalidated when the refs are redrawn.
        *knowledge_place(stage),
        # `preconditions` is the other half of the `conditions` place, which IS a legal
        # Question.target: a question answered against conditions that carried the
        # starting requirements must be invalidated when they move to their own field.
        *preconditions_place(stage),
        # `procedure` is a legal Question.target too, and the reason it must be covered
        # is the sharper one: it is the field an executor may replace WITHOUT
        # re-approval, so an answer given against the old sequence is exactly the kind
        # that goes stale without anyone being asked.
        *procedure_place(stage),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


META_PART = "meta"


def stage_part(index: int) -> str:
    return f"s{index}"


def plan_meta_digest(doc: PlanDoc) -> str:
    """Digest of everything the plan states about itself outside its stages — the goal,
    the done criterion and the order. Its own function rather than a slice of the
    composite below, because a question raised against the goal goes stale on exactly
    these bytes and on no stage's."""
    payload = repr((
        doc.meta.goal,
        doc.meta.done_criterion,
        doc.meta.criterion_type,
        doc.meta.weight_class,
        doc.meta.repo_root,
    ) + order_place(doc.meta))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_stage_digests(doc: PlanDoc) -> dict[int, str]:
    return {s.index: stage_element_keys(s)[WHOLE_STAGE_ELEMENT] for s in doc.stages}


def plan_content_digest(doc: PlanDoc) -> str:
    """The whole-plan digest, recomposed from the same per-stage values
    `plan_stage_digests` reports.

    The payload is byte-for-byte the one this function produced before the per-part
    split, and must stay so: escapes, launch windows and every already-persisted
    `enumerated_at` bind to this value, so a changed payload would void a live
    session's escape and re-arm a discharged cross-check. `test_enumeration_keying`
    pins the value for a fixture plan. The order stays SPLICED (`+ order_place(...)`)
    rather than taking a slot in the tuple: `order_place` is empty for an order-less
    plan, which is what keeps such a plan's payload the one this produced before the
    order field existed."""
    payload = repr((
        doc.meta.goal,
        doc.meta.done_criterion,
        doc.meta.criterion_type,
        doc.meta.weight_class,
        doc.meta.repo_root,
        tuple(sorted(
            (s.index, stage_element_keys(s)[WHOLE_STAGE_ELEMENT]) for s in doc.stages)),
    ) + order_place(doc.meta))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def changed_parts(doc: PlanDoc, baseline_digests: dict) -> tuple[bool, set[int]]:
    """Which parts of `doc` have moved since `baseline_digests` — `(meta_moved,
    {stage indices})`, given `{'meta': <digest>, 'stages': {index: <digest>}}`.

    The baseline is a PARAMETER rather than something read out of a particular
    record, so the same comparison serves a premise bag's enumeration record and a
    plan review's own recorded keys. Stage indices are compared as strings: the
    baseline typically arrives from JSON, which has no integer keys."""
    recorded = {str(k): v for k, v in (baseline_digests.get("stages") or {}).items()}
    moved = {
        index for index, digest in plan_stage_digests(doc).items()
        if recorded.get(str(index)) != digest
    }
    return (baseline_digests.get("meta") or "") != plan_meta_digest(doc), moved


def diff_plans(old: PlanDoc, new: PlanDoc) -> str:
    """Return 'no_change' | 'refinement' | 'substantive'."""
    if _structural_signature(old) != _structural_signature(new):
        return "substantive"
    # Structurally identical — any other change is a refinement. The means/method/
    # conditions/invariants are included so that adjusting a stage's MEANS to remove
    # a difficulty (the overcome-difficulty replan) classifies as 'refinement', not
    # 'no_change' — otherwise the corrected means would be silently dropped.
    #
    # cost_tier (schema 25) is here for the same reason and joins conditionally, so a
    # plan omitting it still hashes byte-identically: it is engine-consumed (dispatch
    # budget + the effort estimate) but sits in neither _structural_signature nor
    # stage_carry_key, so without this a tier-only edit would diff as 'no_change' —
    # applied by _apply_refined_stage_fields, yet leaving state.plan_path naming the OLD
    # file (only the refinement branch rewrites it) and the directive reporting a no-op.
    #
    # verify_venue/verify_kind/landed (and fc.venue/fc.kind/fc.landed below) close
    # two latent omissions found while adding the landed kind (schema 23): venue
    # was engine-executed (SessionState.resolve_check_venue) but absent from both
    # _prose and _fc, so a venue-only correction diffed as 'no_change' and was
    # silently dropped — the exact failure this key family exists to prevent
    # (experience leaf 2026-06-29 instances 6/9). verify_venue/verify_kind pass
    # through _normalize_string for whitespace/case robustness, matching
    # _operative_surface's convention; landed's target/remote are compared RAW
    # (git ref names are case-sensitive — casefolding "Main" and "main" together
    # would silently drop a real correction, the very bug this key closes).
    def _prose(doc: PlanDoc):
        return [
            (s.index, s.title, s.subject.result, s.subject.invariants,
             s.means.means, s.means.method, s.conditions,
             s.criterion.verify_command, s.criterion.expected_exit,
             _normalize_string(s.criterion.verify_venue),
             _normalize_string(s.criterion.verify_kind),
             s.criterion.landed,
             # Both TAGGED, for the reason `procedure_place`'s docstring gives: they are
             # two independently-conditional splices of the same type, so untagged a
             # `cost_tier` and a `verify_venue_at_final` carrying the same word reduce to
             # the same element and an edit MOVING between them diffs as `no_change`.
             # Tagged here and not in the two persisted keys, where `cost_tier` does not
             # appear at all and a retag would flip every disposed question's key.
             *((("verify_venue_at_final",
                 _normalize_string(s.criterion.verify_venue_at_final)),)
               if s.criterion.verify_venue_at_final else ()),
             *((("cost_tier", s.actor.cost_tier),) if s.actor.cost_tier else ()),
             # Without this a knowledge-only correction — the exact edit an
             # overcome-difficulty replan makes when the fault addressed знание —
             # diffs to 'no_change' and is silently dropped.
             *knowledge_place(s),
             # Same argument one field over: moving a stage's starting requirements out of
             # `conditions` and into `preconditions` is a real correction, and without this
             # the two edits cancel in the diff and the replan reads as 'no_change'.
             *preconditions_place(s),
             # And one field further, where the omission would be self-defeating rather
             # than merely lossy: replacing ONLY the sequence of operations is the edit
             # the renormalization branch exists to admit, so without this place here
             # that edit diffs as 'no_change' and the branch is unreachable by
             # construction.
             *procedure_place(s))
            for s in doc.stages
        ]
    def _fc(doc: PlanDoc):
        return [
            (fc.command, fc.expected_exit, fc.label,
             _normalize_string(fc.venue), _normalize_string(fc.kind), fc.landed)
            for fc in doc.meta.final_check
        ]
    # `order_place` is the meta-level sibling of the `knowledge_place`/`preconditions_place`
    # splices above, and it is here for the identical reason: without it a re-worded
    # requirement, a corrected functional place or a coverage entry pointed at a different
    # control would diff as 'no_change' and be silently dropped. Its scope half is already
    # in `_structural_signature`, so what reaches this line is only the wording.
    if (_prose(old) != _prose(new) or old.meta.goal != new.meta.goal
            or old.meta.repo_root != new.meta.repo_root
            or _fc(old) != _fc(new)
            or order_place(old.meta) != order_place(new.meta)):
        return "refinement"
    return "no_change"
