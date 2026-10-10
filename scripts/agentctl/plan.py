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
    negative_control = "python3 -m agentctl status --session does-not-exist"
                                          # required for a SUBSTANTIVE plan's measurable
                                          # shell-kind stage that carries verify_command
                                          # (submission.py refuses to submit without it or
                                          # negative_control_waiver below): a command, run
                                          # in the same venue, that must NOT exit
                                          # expected_exit -- proof the check can go red.
    negative_control_waiver = "check probes a live external service; no safe known-bad input"
                                          # alternative to negative_control: a non-empty
                                          # reason the check cannot be shown to fail
    cost_tier = "medium"                  # optional; small|medium|large. Declares the
                                          # stage's expected size: dispatch reads it as the
                                          # spawn budget label, and the effort-divergence
                                          # estimate sums it over the plan. Absent means
                                          # "medium" at the point of use -- an inferred
                                          # default, not a norm anyone chose, so declare it
                                          # on any stage whose size is not typical.
    guard_exempt_paths = ["a/settings.json"]  # optional; repo-relative to the dispatch
                                          # --workdir. Dispatch forwards each as
                                          # `--guard-exempt` to spawn-specialist.py,
                                          # lifting that file's settings*.json guard deny.
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

from . import grants as _grants
from .grants import AddDirGrant, RuleGrant, StageGrants
from .landed_providers import PROVIDER_NAME_RE
from .script_effects import StageEffectDeclaration
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
    PAIR_BINDING_KEYS,
    PAIR_CONTENT_KEYS,
    Principle,
    Stage,
    StageStatus,
    Subject,
    Supply,
    SUPPLY_DELIVERIES,
    asserts_landing,
)
from .text_shape import ELEMENT_NAMES as _ELEMENT_NAMES
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import CARRY_ELEMENT, INTERFACE_ELEMENT, WHOLE_STAGE_ELEMENT
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
    # The plan-time, reviewed reason a plan declares no `kind = "landed"` check. A plan
    # that sets delivery_worktree must declare a landed check OR this waiver (never both),
    # so "this change lands nothing" is a decision the approved plan carries, not one the
    # resolving actor makes after the outcome is known. None = no waiver.
    landing_waiver: str | None = None
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
    # The RAW `depends_on` TOML list for every stage, keyed by 1-based stage
    # index. `_build_supplies` merges it with `[[stage.supplies]]`, so
    # `Stage.depends_on` already carries every declared edge; this field keeps the
    # list as written and is filled by `parse_plan` for EVERY stage index (empty
    # tuple when the stage declares no `depends_on`).
    raw_depends_on: dict[int, tuple[int, ...]] = field(default_factory=dict)


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


def _parse_stage_grants(raw: object, context: str, *, strict: bool) -> "StageGrants | None":
    """Parse a stage's optional `[stage.grants]` table into a validated
    `StageGrants`, or None when the stage declares no grants block at all —
    byte-identical to every plan authored before this field existed. Every
    entry is validated through `grants.validate_grants` (the sole authority
    grants.py's module docstring names) when `strict` — the same load-time
    refusal every other required-shape field in this loader gets — so an
    invalid declared grant can never reach a spawned child's --settings.

    Read-only baseline loads (strict=False) skip validation, matching every
    other field parsed here under that flag: a plan snapshot frozen before a
    grant became invalid (validator tightened after approval) must still
    load for diffing, not raise."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise PlanError(f"{context}: grants must be a table, got {type(raw).__name__}")
    allow = []
    for r in raw.get("allow", []):
        if not isinstance(r, str):
            raise PlanError(f"{context}: grants.allow entries must be strings, got {r!r}")
        allow.append(RuleGrant(rule=r, provenance="declared"))
    add_dirs = []
    for a in raw.get("add_dirs", []):
        if not isinstance(a, dict) or not a.get("path"):
            raise PlanError(f"{context}: grants.add_dirs entries need a 'path', got {a!r}")
        add_dirs.append(
            AddDirGrant(path=str(a["path"]), mode=str(a.get("mode", "read")), provenance="declared")
        )
    if raw.get("permission_mode"):
        # Finding S7: `permission_mode` is not a field of the grant model at
        # all — every value the plan-authoring surface could set was refused
        # anyway, so the field carried no real information; a plan naming it
        # is refused outright instead of being silently ignored.
        raise PlanError(
            f"{context}: grants.permission_mode is not a supported grant field — refused"
        )
    stage_grants = StageGrants(allow=allow, add_dirs=add_dirs)
    if strict:
        try:
            _grants.validate_grants(stage_grants)
        except _grants.GrantValidationError as exc:
            raise PlanError(f"{context}: grants: {exc}") from exc
    return stage_grants if not stage_grants.is_empty() else None


def _parse_stage_effects(raw: object, context: str) -> list[StageEffectDeclaration]:
    """Parse a stage's optional `[[stage.effects]]` array into validated
    `StageEffectDeclaration`s, or `[]` when the stage declares none at all —
    byte-identical to every plan authored before this field existed.

    Load-time validation is limited to SHAPE (every field present) and the
    R1/C1 refusal of a declared `op="land"` — mirroring the same defensive
    check `tool_contracts.load_contract_table` and `script_effects.
    load_script_effects_table` apply to their own tables (C1: no contract,
    registry or [[stage.effects]] entry may ever declare op="land"). Whether
    the declared digest still matches the live script, and whether this plan
    is still the last user-approved version, are RESOLVE-time questions
    (cli.py, via `script_effects.resolve_script`) — not decidable from the
    TOML alone."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise PlanError(f"{context}: effects must be an array of tables, got {type(raw).__name__}")
    out: list[StageEffectDeclaration] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PlanError(f"{context}: effects[{i}] must be a table, got {item!r}")
        if item.get("op") == "land":
            raise PlanError(
                f"{context}: effects[{i}] declares op=\"land\", which is refused -- "
                f"no [[stage.effects]] entry may ever produce an op=\"land\" resource (R1/C1)"
            )
        path = item.get("path")
        sha256 = item.get("sha256")
        resolver = item.get("resolver")
        if not path or not sha256 or not resolver:
            raise PlanError(f"{context}: effects[{i}] needs 'path', 'sha256' and 'resolver'")
        out.append(StageEffectDeclaration(path=str(path), sha256=str(sha256), resolver=str(resolver)))
    return out


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
    provider = raw_table.get("provider", "git")
    if not isinstance(provider, str) or not PROVIDER_NAME_RE.fullmatch(provider):
        raise PlanError(
            f"{context}: landed.provider {provider!r} is not a plain identifier "
            f"(expected to match {PROVIDER_NAME_RE.pattern}) (R12)"
        )
    target = raw_table.get("target")
    if not target or not isinstance(target, str):
        raise PlanError(f"{context}: landed.target is required (non-empty string) (R2)")
    if provider == "git" and not _LANDED_REF_RE.match(target):
        raise PlanError(
            f"{context}: landed.target {target!r} is not a valid git ref name "
            f"(expected to match {_LANDED_REF_RE.pattern}) (R2)"
        )
    remote = str(raw_table.get("remote", "origin"))
    if provider == "git" and not _LANDED_REF_RE.match(remote):
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
    return LandedSpec(target=target, remote=remote, delivered_stage=delivered_stage,
                      provider=provider)


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
    """Build typed Supply edges: the explicit [[stage.supplies]] plus an
    element-less edge for each flat `depends_on` index no supply already names."""
    raw = s.get("supplies")
    if raw:
        supplies = []
        for edge in raw:
            if "on" not in edge:
                raise PlanError(f"stage {index} supply missing 'on'")
            delivery = edge.get("delivery")
            if delivery is not None and delivery not in SUPPLY_DELIVERIES:
                raise PlanError(
                    f"stage {index} supply on stage {edge['on']} has unknown delivery "
                    f"{delivery!r}; allowed: {list(SUPPLY_DELIVERIES)}"
                )
            supplies.append(
                Supply(
                    on=int(edge["on"]),
                    element=edge.get("element"),
                    artifact=edge.get("artifact"),
                    delivery=delivery,
                )
            )
    else:
        supplies = []
    named = {sup.on for sup in supplies}
    for d in s.get("depends_on", []):
        if int(d) not in named:
            supplies.append(Supply(on=int(d)))
    return supplies


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
# Deliberately NARROW, to keep the false-positive population as small as the
# checker can make it — NOT empty: five exemptions are needed to reach the
# population actually observed, and each is recorded here, with its why and
# its accepted residual, so the next reader auditing a widening finds every
# one in this one place rather than scattered across commit messages.
#   * Only RELATIVE, literal, path-shaped tokens are considered. Absolute paths
#     (/dev/null, /tmp/scratch written at runtime) are OUT OF SCOPE — a runtime
#     temp file is exactly the false positive this narrowing avoids.
#   * Globs ("*?["), shell variables ("$..."), URLs ("://"), option values
#     ("k=v") and the program string after `-c` / module after `-m` are dropped:
#     none is a literal filesystem path.
#   * (A) Here-document bodies (`_strip_heredoc_bodies_for_reachability`) — a
#     `python3 - <<'TAG'` body is source text, not shell argv.
#   * (B) A path inside a negated `! test -f P` / `! [ -f P ]` clause (either
#     spelling of `!`'s position) — the author is asserting ABSENCE.
#   * (C) A candidate token's trailing `;`, `&`, `|`, `)` — shell clause
#     syntax shlex glues onto the word, e.g. the `P;` in `for F in ... P; do`.
#   * (D) The pattern operand of a grep-family command (`grep -qE
#     '(review|pull)/15149870' file`) is not a path. ACCEPTED HOLE: `grep -f
#     patterns.txt target.py` has no literal pattern operand, so the
#     positional rule mistakes `patterns.txt` for it and silently exempts it
#     too (the `--file=patterns.txt` spelling is worse — see the full account
#     at (D) in `_reachability_path_tokens`'s own docstring).
#   * (E) A path with a conventional build/test OUTPUT directory name as one
#     of its segments (`_BUILD_OUTPUT_SEGMENTS`) — a byproduct the checked
#     command's own build/test step writes mid-run, not a precondition. The
#     WEAKEST of the five: convention-based, not a syntactic fact, and wide
#     enough that a real precondition file stored under e.g. `build/` loses
#     the check silently — see that constant's own module comment.
# Full per-exemption detail (narrowing conditions, interaction with the other
# exemptions, each accepted limit) lives on `_reachability_path_tokens`'s own
# docstring, next to the code it describes; this block is the index.
#
# Why not reuse lib/shell_tokens.strip_heredoc_bodies for (A): that module is
# fail-CLOSED for two SECURITY consumers (the canon guard,
# git_cwd.effective_git_cwd) and its clause (ii) disqualifies any command
# containing `;`, `&&`, `{`, `}` or `$(` — which every real multi-clause
# verify_command has (measured: it strips neither offending v23 command this
# exemption targets). This lint's polarity is the opposite of a security
# gate's — it BLOCKS on an unreachable path, so over-stripping only means
# fewer tokens get checked (the safe direction) — so merging the two would
# erode the security module's non-widening argument for consumers that need
# the opposite doubt polarity. `_strip_first_heredoc_body` above is therefore
# lint-local by design, not an oversight.
#
# Why not an inline `# path-check: skip <token>` annotation instead of any of
# the five: it was considered and rejected because it requires editing the
# command it annotates, which is unavailable for a plan whose bytes must stay
# frozen (e.g. an already-submitted, hash-pinned reference plan) — the
# annotation route only works going forward, never on an existing plan this
# lint must also judge correctly.
#
# Residual false-positive population (documented, not eliminated): a relative
# path a stage's command *creates then reads within the same command*, where
# neither pre-existing on disk nor a declared cross-stage artifact NOR caught
# by (A)-(E) above. Declare such a path in that stage's output_artifacts to
# silence the lint.
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

# Characters that reject a candidate token outright: globs, shell variables,
# option-values and URLs (original set), plus `(` and `|` — regex
# metacharacters a grep-family pattern operand commonly carries even after (D)
# below has failed to recognize it positionally, e.g. the outer
# `(review|pull)` in `(review|pull)/15149870` — defence in depth, not the
# primary mechanism.
# ACCEPTED LIMIT, named rather than left implicit: a real, existing path that
# contains `(` or `|` (mid-token — a trailing `)` is already stripped by
# `_GLUE_CHARS` before this set is consulted) is silently excluded from
# checking too, e.g. `test -f "report (1).csv"`. `)` was considered for this
# set and dropped: real regex patterns pair it with `(`, so `(` alone already
# catches every pattern this defence targets, while keeping `)` bought no
# additional catch and doubled the false-negative surface for a filename that
# contains `)` but not `(`, e.g. `scripts/gh)ost.py`. Accepted, like the
# `grep -f` limit in (D) below, because it fails in the same non-blocking
# direction as every exemption here: a real orphan path containing `(` or
# `|` goes unchecked rather than wrongly blocked.
_REJECT_CHARS = "*?[$=(|"

# Trailing characters that are shell clause syntax glued onto a word by shlex
# (which never splits on them), never a legal trailing character of an
# unquoted filename — see false-positive (C) below.
_GLUE_CHARS = ";&|)"

# The one clause-terminator token (B)'s exemption below matches DIRECTLY.
# `&&`, `||`, `;`, `|` also end the clause but are NOT members here: a token
# made entirely of `_GLUE_CHARS` (`;`, `&`, `|`, `)`) collapses to the empty
# string under (C)'s rstrip before this set is ever consulted, so it is the
# trailing-glue reset (`if glued: ...`) that actually ends the clause for
# those four spellings, not a lookup here. This set exists only for `]`, the
# one terminator `_GLUE_CHARS` does not touch.
_CLOSING_BRACKET = frozenset({"]"})

# grep-family commands whose pattern operand is not a path — see (D) below.
_GREP_FAMILY = frozenset({"grep", "egrep", "fgrep", "rg"})

# Conventional build/test OUTPUT directory names — see (E) below. A relative
# path token with one of these as a path SEGMENT (any component, not just the
# last) is a byproduct the checked command's own build/test step produces as
# it runs, not a precondition the command requires up front.
# ACCEPTED LIMIT, named rather than left implicit: `build`, `target`, `dist`
# and `coverage` are ordinary SOURCE directory names too, so this set is
# genuinely wide — a project keeping a real precondition file under a
# directory with one of these names loses the reachability check on it
# entirely, silently. This is a convention-based judgement, not a syntactic
# fact like (A)-(D), which makes it the WEAKEST of the five exemptions. Kept
# wide rather than narrowed to `test-results` alone, because a narrow set
# would be tuned to one plan and misfire on the next repo; the width is
# compensated by (E) being self-contained (revertable in one commit without
# touching (A)-(D)).
_BUILD_OUTPUT_SEGMENTS = frozenset({
    "test-results", "build", "dist", "target", "node_modules",
    "__pycache__", ".pytest_cache", "htmlcov", "coverage",
})

# A here-document/here-string introducer: `<<`/`<<-` or `<<<` followed by an
# optionally quoted identifier delimiter.
_HEREDOC_START = re.compile(
    r"<<-?\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\"|"
    r"([A-Za-z_][A-Za-z0-9_]*))"
)


def _strip_first_heredoc_body(cmd: str) -> str:
    """Remove the body of the first here-document operator found outside quotes,
    or return `cmd` unchanged on any doubt (no body line, no terminator line).
    Quote-aware only — see `_strip_heredoc_bodies_for_reachability` for why a
    fuller recognition shape is not needed here."""
    i, n, quote = 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = _HEREDOC_START.match(cmd, i)
            if m:
                tag = m.group(1) or m.group(2) or m.group(3)
                line_end = cmd.find("\n", m.end())
                if line_end == -1:
                    return cmd  # doubt: no body line follows -> leave untouched
                lines = cmd[line_end + 1:].split("\n")
                terminator = next(
                    (idx for idx, line in enumerate(lines) if line.strip() == tag),
                    None,
                )
                if terminator is None:
                    return cmd  # doubt: no terminator line -> leave untouched
                head = cmd[:line_end]
                tail = "\n".join(lines[terminator + 1:])
                return head + ("\n" + tail if tail else "")
        i += 1
    return cmd


def _strip_heredoc_bodies_for_reachability(cmd: str) -> str:
    """`cmd` with every here-document body removed, so a Python/shell script
    living inside `python3 - <<'TAG'` is never shlex-tokenized as shell argv —
    e.g. `print("ci/tests:", ...)` would otherwise collapse into the bogus
    path-shaped token `print(ci/tests:,`.

    Deliberately LINT-LOCAL rather than a reuse of lib.shell_tokens.strip_heredoc_
    bodies: that module is fail-CLOSED for two SECURITY consumers (the canon
    guard and git_cwd.effective_git_cwd) and its clause (ii) disqualifies any
    command containing `;`, `&&`, `{`, `}` or `$(` — which every real
    verify_command with more than one clause has (measured: it strips nothing
    from either offending v23 reference-plan command this exemption targets, nor
    does the unlanded issue-#108 span-API follow-up, for the same clause (ii)
    reason). Merging the two would erode the security module's non-widening
    argument for consumers that need the opposite doubt polarity. This lint's
    polarity is the opposite: it BLOCKS on an unreachable path, so
    over-stripping a body only means fewer tokens get checked — the safe,
    non-blocking direction — while under-stripping produces exactly the false
    positive this function exists to remove. A quote-aware scan for the
    positive `<<TAG ... TAG` shape is therefore enough; it need not carry
    shell_tokens' full seven-clause security-grade recognition."""
    prev = None
    while cmd != prev:
        prev = cmd
        cmd = _strip_first_heredoc_body(cmd)
    return cmd


def _reachability_path_tokens(cmd: str) -> list[str]:
    """The relative, literal, path-shaped tokens of a shell command — the tokens
    whose green-reachability is decidable. shlex parses the WHOLE command in one
    pass so a quoted `python3 -c "..."` body — which itself contains `;` `|` `<`
    `>` between Python statements — collapses into ONE token that the `-c` drop
    then discards, instead of being shattered on shell metacharacters that also
    occur inside it. Shell operators (`&&`, `|`, `2>&1`, `>`) survive as tokens but
    are not path-shaped, so they fall out. Tolerant: unbalanced quotes fall back to
    a plain split rather than raising.

    Four exemptions layered on top of that base tokenizer, each narrowed to the
    shape that actually misfires and paired with a regression case in
    test_verify_reachability.py pinning what must still block:

    (A) Here-document bodies are stripped from `cmd` before shlex ever sees them
        — see `_strip_heredoc_bodies_for_reachability`.
    (B) A path token inside a `! test <flag> P` / `! [ <flag> P ]` clause, or
        its POSIX operand-position spelling `test ! <flag> P` / `[ ! <flag> P
        ]`, is exempt: the author is asserting ABSENCE, so demanding P be
        producible is exactly backwards. Narrowed to arm only when `!` is
        either the token immediately preceding `test`/`[` (command-position)
        or the FIRST operand after a `test`/`[` command word (operand-
        position) — `! grep ... P`, `! <anything else> P`, and a `!` anywhere
        but the very first operand of `test`/`[` are NOT exempt. The clause
        ends at a literal `]`, or — via the trailing-glue reset in (C), not a
        lookup against `_CLOSING_BRACKET` — at any token made entirely of
        `_GLUE_CHARS` (`&&`, `||`, `;`, `|`); see that set's module comment.
        ACCEPTED LIMIT: a negated clause with NEITHER a literal `]` NOR a
        `_GLUE_CHARS`-only token before the next clause starts is never
        closed — e.g. `test ! -f a.txt\ntest -f scripts/ghost.py` (a bare
        newline carries no clause-boundary information once shlex has
        collapsed it to ordinary whitespace) silently exempts ghost.py too.
        Not closed: doing so needs recognizing a fresh `test`/`[` command
        word as an implicit clause end even while `command_start` is still
        False, which risks re-arming (B) on state this lint does not track
        elsewhere. Accepted for the same reason as the other three limits
        named in this docstring: it fails in the non-blocking direction.
    (C) A candidate token's TRAILING `;`, `&`, `|`, `)` characters (including
        runs such as `;;`) are shell clause syntax glued onto the word by shlex
        (which never splits on them) — e.g. the `P;` in
        `for F in ... P; do ...` — and are stripped before the word is judged.
        (C) runs before (B) reads the stream: `! test -f foo.txt;` must both end
        (B)'s clause at the `;` AND, were it ever path-checked, be judged on the
        bare `foo.txt` — running (B) first on the raw `foo.txt;` would leave the
        trailing `;` unstripped, the trailing-glue reset would never fire, and
        (B)'s exemption would run past its own clause.
    (D) The pattern operand of a grep-family invocation (`grep`, `egrep`,
        `fgrep`, `rg`) is not a path, even when it is `/`-shaped
        (`grep -qE '(review|pull)/15149870'`). Recognized positionally: after
        the command word, the operand following a flag cluster ending in
        `e`/`E`/`P`, otherwise the first non-flag operand. FILE operands after
        the pattern remain checked normally. `_REJECT_CHARS` gained `(` and `|`
        as defence in depth for a pattern this positional rule fails to catch
        — see that constant's module comment for the accepted cost and why
        `)` was considered and dropped.
        ACCEPTED LIMIT: `grep -f patterns.txt target.py` has no literal pattern
        operand (it comes from a file), so the positional rule mistakes
        `patterns.txt` for the pattern and silently exempts it, while
        `target.py` is still checked. The `--file=patterns.txt` spelling is
        WORSE than that: it doesn't end in `e`/`E`/`P` so the positional rule
        never consumes it as a two-token flag+operand pair, and instead
        mistakes the FOLLOWING token — `target.py`, the real file operand —
        for the pattern, so neither `patterns.txt` nor `target.py` is
        checked. Not closed: closing it needs the full grep flag grammar
        (`-f`, repeated `-e`, `--file=`, bundled short flags) — the parser
        this lint deliberately declines to write — and both cases fail in
        the same non-blocking direction as every exemption here.
    (E) A path token with a conventional build/test OUTPUT directory name as
        one of its path segments — `test-results`, `build`, `dist`, `target`,
        `node_modules`, `__pycache__`, `.pytest_cache`, `htmlcov`, `coverage`
        (`_BUILD_OUTPUT_SEGMENTS`) — is exempt, e.g.
        `library/svc/data_science/tests/test-results/py3test/ytest.report.trace`,
        which `scripts/ya_test_textlog.py` reads only AFTER the `ya make` run
        it itself launches has written it: a byproduct of the checked
        command's own execution, not a precondition. See that constant's
        module comment for the accepted width and why (E) is the WEAKEST of
        the five exemptions.
    """
    cmd = _strip_heredoc_bodies_for_reachability(cmd)
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    tokens: list[str] = []
    i = 0
    n = len(toks)
    negated_clause = False  # (B): currently inside an exempt `! test`/`! [` clause
    command_start = True    # (D): is the next token a command word?
    prev_bang = False       # (B): previous token was exactly `!` (command-position)
    awaiting_test_operand = False  # (B): previous token was `test`/`[` as a command
                                    # word, waiting to see if its FIRST operand is `!`
    while i < n:
        raw = toks[i]
        i += 1
        stripped = raw.rstrip(_GLUE_CHARS)  # (C)
        glued = stripped != raw

        if prev_bang:
            prev_bang = False
            if stripped in ("test", "["):
                negated_clause = True
                command_start = False
                if glued:
                    negated_clause = False
                    command_start = True
                continue  # `test` / `[` itself is never path-shaped

        if awaiting_test_operand:
            awaiting_test_operand = False
            if stripped == "!":
                negated_clause = True
                continue  # `!` itself is never path-shaped
            # first operand wasn't `!` -- not a negated clause; this token
            # (a flag or a path) falls through to the ordinary checks below

        if stripped == "!":
            prev_bang = True
            continue

        if stripped in _CLOSING_BRACKET:
            negated_clause = False
            command_start = True
            continue

        if stripped in ("-c", "-m"):  # program string / module name follows
            i += 1
            command_start = False
            if glued:
                negated_clause = False
                command_start = True
            continue

        if command_start and not negated_clause:
            # Neither branch below re-checks `glued` on the command word itself
            # the way `prev_bang` and `-c`/`-m` above do -- e.g. a literal
            # `test;` or `grep;` token (glue on the command word, not its
            # operand) is not a realistic unquoted shell shape, so the
            # asymmetry is harmless on any input seen so far, but it is an
            # asymmetry: those two branches assume `glued` cannot fire here.
            base = stripped.rsplit("/", 1)[-1]
            if base in _GREP_FAMILY:  # (D)
                command_start = False
                found_pattern = False
                while i < n and not found_pattern:
                    nxt = toks[i].rstrip(_GLUE_CHARS)
                    if nxt.startswith("-"):
                        i += 1
                        if nxt[-1:] in ("e", "E", "P") and i < n:
                            i += 1  # this flag's operand is the pattern
                            found_pattern = True
                        continue
                    i += 1  # first non-flag operand is the pattern
                    found_pattern = True
                continue
            if stripped in ("test", "["):  # (B) operand-position negation
                command_start = False
                awaiting_test_operand = True
                continue

        command_start = False
        if not negated_clause:
            if not stripped.startswith("-") and not any(ch.isspace() for ch in stripped):
                head = stripped.split("::", 1)[0]  # drop a pytest node-id suffix
                if head and not head.startswith("/"):
                    if not any(ch in head for ch in _REJECT_CHARS) and "://" not in head:
                        if "/" in head or head.endswith(_PATH_EXTS):
                            if not any(part in _BUILD_OUTPUT_SEGMENTS
                                       for part in Path(head).parts):  # (E)
                                tokens.append(head)
        if glued:
            negated_clause = False
            command_start = True
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
    if asserts_landing(stages, final_check):
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
        landing_waiver=str(m["landing_waiver"]) if m.get("landing_waiver") else None,
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
    raw_depends_on: dict[int, tuple[int, ...]] = {}
    for i, s in enumerate(raw_stages, start=1):
        index = int(s.get("index", i))
        # The raw list as written, kept beside the derived Stage.depends_on
        # (see PlanDoc.raw_depends_on).
        raw_depends_on[index] = tuple(int(d) for d in s.get("depends_on", []))
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
        # Permissive, unconditional of strict (same treatment as `knowledge` /
        # `preconditions` below): submission.py's non-raising seam is where the
        # requirement lives, not the loader, so a legacy plan or an in-session
        # re-read of an already-accepted plan never breaks over a missing field.
        negative_control = (
            str(s["negative_control"]) if s.get("negative_control") else None
        )
        negative_control_waiver = (
            str(s["negative_control_waiver"]).strip()
            if str(s.get("negative_control_waiver") or "").strip()
            else None
        )
        # Same permissive treatment as negative_control_waiver above: the requirement
        # (an output_artifacts entry under exempt_paths.scratch_roots() needs either no
        # such entry or this waiver) lives at the submission seam, not here.
        ephemeral_artifacts_waiver = (
            str(s["ephemeral_artifacts_waiver"]).strip()
            if str(s.get("ephemeral_artifacts_waiver") or "").strip()
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
                    guard_exempt_paths=[str(p) for p in s.get("guard_exempt_paths", [])],
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
                    negative_control=negative_control,
                    negative_control_waiver=negative_control_waiver,
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
                ephemeral_artifacts_waiver=ephemeral_artifacts_waiver,
                outcome=Outcome(status=StageStatus.PENDING.value),
                grants=_parse_stage_grants(s.get("grants"), stage_ctx, strict=strict),
                effects=_parse_stage_effects(s.get("effects"), stage_ctx),
            )
        )

    indices = [s.index for s in stages]
    if len(set(indices)) != len(indices):
        raise PlanError(f"duplicate stage indices: {indices}")
    _validate_graph(stages, is_substantive=is_substantive)
    return PlanDoc(meta=meta, stages=stages, raw_depends_on=raw_depends_on)


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


def load_plan_with_digest(
    path: str | Path, *, strict: bool = True, strict_executor: bool | None = None,
) -> tuple[PlanDoc, bytes, str]:
    """Read the plan file's bytes exactly once and derive the parsed doc, the raw
    bytes and their sha256 digest all from that single buffer.

    `load_plan` re-reads the file for every caller, so a command that needs the
    doc AND a digest AND a snapshot of the same bytes (cmd_approve, cmd_replan)
    previously read the file up to three separate times. Between any two of those
    reads a concurrent edit (another session, an editor save) can slip in, binding
    a digest or a snapshot to bytes that were never the ones parsed and diffed.
    Reading once and deriving everything from that buffer removes the window."""
    p = Path(path)
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    data = p.read_bytes()
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"malformed TOML in plan file {p}: {exc}") from exc
    doc = parse_plan(raw, strict=strict, strict_executor=strict_executor)
    digest = hashlib.sha256(data).hexdigest()
    return doc, data, digest


# --- Topological-review protocol constants ------------------------------
# Sourced by render.render_pair_review_bundle so the checklist/protocol text
# rendered into a --review-topo starting prompt and the markers a reviewer's
# own REVIEW: reply is parsed against never drift apart: render.py must
# import these, never duplicate them as string literals.
REVIEW_MARKER = "REVIEW:"
VERDICT_MARKER = "Verdict:"
PLAN_DIGEST_MARKER = "Plan digest:"
CONDITION_MARKERS = ("C1:", "C2:", "C3:", "C4:")

# A reviewer's concern is `<severity>: [re:<concern-id>] <body>`; the engine stores the
# body in `concerns` and the severity / restated id beside it, so the body keeps the
# leading `cut:`/`add:` remedy tag, part token and `C1:`..`C4:` marker its readers key on.
SEVERITY_BLOCKING = "blocking"
SEVERITY_NOTE = "note"
CONCERN_SEVERITIES = (SEVERITY_BLOCKING, SEVERITY_NOTE)
RESTATES_PREFIX = "re:"
CONCERN_FORM = (
    f"`{SEVERITY_BLOCKING}: [{RESTATES_PREFIX}<concern-id>] <concern>` or "
    f"`{SEVERITY_NOTE}: [{RESTATES_PREFIX}<concern-id>] <concern>`"
)
_CONCERN_RE = re.compile(
    rf"^\s*({'|'.join(CONCERN_SEVERITIES)}):\s*(?:{re.escape(RESTATES_PREFIX)}(\S+)\s+)?(\S.*)$",
    re.DOTALL,
)
_RESTATES_ID_STRIP = "<>[]()`\"'*,;:."


class ConcernFormatError(ValueError):
    """A `--concern` / REVIEW line that is not in the `CONCERN_FORM` grammar."""


@dataclass(frozen=True)
class ParsedConcern:
    severity: str
    restates: str
    body: str


def parse_concern(text: str) -> ParsedConcern:
    """Split a tagged concern into severity, the restated concern id ('' when it is not a
    restatement) and body. An untagged concern is an error, never defaulted: a severity
    the reviewer did not write would decide what blocks."""
    found = _CONCERN_RE.match(text or "")
    if found is None:
        raise ConcernFormatError(
            f"concern {(text or '')[:80]!r} is untagged: write it as {CONCERN_FORM}"
        )
    restates = (found.group(2) or "").strip(_RESTATES_ID_STRIP)
    return ParsedConcern(found.group(1), restates, found.group(3).strip())


def format_concern(severity: str, restates: str, body: str) -> str:
    re_part = f"{RESTATES_PREFIX}{restates} " if restates else ""
    return f"{severity}: {re_part}{body}"


def _stage_by_index(doc: PlanDoc, n: int) -> Stage:
    """`doc`'s stage at 1-based index `n`, or PlanError if there is none."""
    for stage in doc.stages:
        if stage.index == n:
            return stage
    raise PlanError(
        f"stage {n} not found in plan (valid indices: {sorted(s.index for s in doc.stages)})"
    )


def reliance_set(doc: PlanDoc, n: int) -> frozenset[int]:
    """Stage `n`'s RAW reliance edges: the union of its raw TOML
    `depends_on` (`doc.raw_depends_on`) and its typed `supplies[].on`
    edges. `_build_supplies` merges the two, so for a parsed plan this equals
    `Stage.depends_on`; the explicit union is kept for a PlanDoc whose
    `raw_depends_on` was edited after parsing. Raises PlanError when `n` is unknown, when `n`
    has no entry in `doc.raw_depends_on` (parse_plan populates one for
    every stage; a missing key means the caller handed us a foreign
    PlanDoc, not "no edges"), or when an edge points at a stage index the
    plan does not have."""
    stage = _stage_by_index(doc, n)
    if n not in doc.raw_depends_on:
        raise PlanError(f"stage {n} has no raw_depends_on entry (foreign or stale PlanDoc)")
    valid = {s.index for s in doc.stages}
    edges = set(doc.raw_depends_on[n]) | {s.on for s in stage.supplies}
    dangling = edges - valid
    if dangling:
        raise PlanError(f"stage {n} relies on unknown stage(s) {sorted(dangling)}")
    return frozenset(edges)


def consumers(doc: PlanDoc, n: int) -> frozenset[int]:
    """Every stage index that relies on `n` -- the reverse of
    `reliance_set`. Raises PlanError when `n` is unknown."""
    _stage_by_index(doc, n)
    return frozenset(s.index for s in doc.stages if n in reliance_set(doc, s.index))


def first_hop(doc: PlanDoc, n: int) -> frozenset[int]:
    """`n`'s direct neighbours in EITHER direction: what it relies on,
    union what relies on it."""
    return reliance_set(doc, n) | consumers(doc, n)


def reliance_closure(doc: PlanDoc, n: int) -> frozenset[int]:
    """The transitive closure of `reliance_set` upstream from `n` (n's
    reliances, their reliances, ...), excluding `n` itself.

    Raises PlanError on a reliance cycle. The raw union graph
    `reliance_set` reads is not guaranteed acyclic even when the derived
    (supplies-only) graph `_validate_graph` already checked is: a stage
    pair whose raw `depends_on` cycles but whose `supplies`-derived edges
    do not is exactly the collapse this module exists to catch, so this
    closure does its own cycle detection rather than trusting the
    already-validated derived graph."""
    _raise_on_reliance_cycle(doc, n)
    closure: set[int] = set()
    stack = [n]
    while stack:
        for dep in reliance_set(doc, stack.pop()):
            if dep not in closure:
                closure.add(dep)
                stack.append(dep)
    return frozenset(closure)


def _raise_on_reliance_cycle(doc: PlanDoc, start: int) -> None:
    """Three-colour DFS over the raw reliance graph reachable from `start`.
    A node still on the DFS stack (grey) reached again is a back edge, and
    therefore a cycle, whichever path first reached it; a finished (black)
    node is never re-entered, so a cycle reached only through an
    already-visited node is still found on the path that first enters it."""
    grey, black = set(), set()
    path: list[int] = [start]
    grey.add(start)
    iterators = [iter(sorted(reliance_set(doc, start)))]
    while iterators:
        dep = next(iterators[-1], None)
        if dep is None:
            iterators.pop()
            done = path.pop()
            grey.discard(done)
            black.add(done)
            continue
        if dep in grey:
            cycle = path[path.index(dep):] + [dep]
            raise PlanError(f"reliance cycle detected: {' -> '.join(str(p) for p in cycle)}")
        if dep not in black:
            grey.add(dep)
            path.append(dep)
            iterators.append(iter(sorted(reliance_set(doc, dep))))


def interface_empty(stage: Stage) -> bool:
    """True when `render_stage_interface`'s projection would carry no
    concrete signal for a consumer beyond prose -- i.e. the stage's
    result image or done criterion is blank. `output_artifacts` legitimately
    defaults to empty for stages with no file deliverable, so it cannot be
    the signal; `subject.result` and `criterion.done_criterion` are what a
    consumer actually relies on, and TOML can still hand parse_plan a
    whitespace-only string for either. `render_stage_interface(doc, n,
    contract=True)` falls back to the full brief when this is True, rather
    than handing a consumer nothing concrete to rely on."""
    return not stage.subject.result.strip() or not stage.criterion.done_criterion.strip()


def stage_interface_digest(doc: PlanDoc, stage: Stage) -> str:
    """Digest over a stage's INTERFACE only -- title, expected result image,
    criterion type, done criterion, and output_artifacts -- never
    method/means/procedure, so a method-only edit never moves it (stage 2's
    interface_keys binding).

    When `interface_empty(stage)`, digests the bytes of
    `render_stage_interface(doc, stage.index, contract=True)` instead -- the
    SAME full-brief fallback a --review-topo bundle actually renders for such
    a stage -- rather than the blank interface fields, so a method-only edit
    to an interface_empty transitive member (which a reviewer read in full
    via that fallback) still stales every record whose interface_keys names
    it. A projection of `StageNorm.interface_digest`; imports it locally:
    stage_norm.py imports from this module, so a module-level import would cycle."""
    from .stage_norm import StageNorm
    return StageNorm.from_stage(stage, doc=doc).interface_digest()


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
        tuple((r.id, r.text, r.derivation) for r in order.requirements),
        tuple(sorted((k, tuple(v)) for k, v in order.coverage.items())),
        order.malformed,
        order.requirements_dropped,
        order.requires_traceability,
    ),)


def order_extra_digest(meta: PlanMeta) -> str:
    """Digest of the PlanMeta fields `plan_meta_digest` does not cover — `final_check`,
    `external_research`, `task_id`, `delivery_worktree` — so the `plan` node's identity
    key (`_pair_node_key`) moves when any of them is edited."""
    payload = repr((
        tuple(
            (fc.command, fc.expected_exit, fc.label, fc.venue, fc.kind)
            for fc in meta.final_check
        ),
        meta.external_research,
        meta.task_id,
        meta.delivery_worktree,
        *((("landing_waiver", meta.landing_waiver),) if meta.landing_waiver else ()),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
                *grants_place(s),
                *effects_place(s),
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


def grants_place(stage) -> tuple:
    """The stage's DECLARED `[stage.grants]` — never the derived half, which
    depends on a venue this function does not take — as a contribution to a
    change-decision key: a ONE-element tuple holding `StageGrants.effective_tuple()`,
    or the EMPTY tuple when the stage declares no grants block at all.

    Unlike `knowledge_place`/`preconditions_place`/`procedure_place`, this is not
    shared with `stage_carry_key`/`stage_question_key`: no name in the question-target
    vocabulary (`text_shape.ELEMENT_NAMES`) names a stage's permission surface, so a
    disposed Question can never target it, and `grants` is listed in
    `test_question_key_scope.py`'s `_UNCLAIMED_STAGE_LEAVES` for that reason. It IS
    spliced into `_structural_signature` (below) and into `gates._renorm_stage_residual`
    (imported from here), on the same footing as `actor.cost_tier` and
    `output_artifacts` there: declaring or widening what a stage may touch is a norm an
    executor may not silently move under the light (`--renormalize`) path, and a plan
    edit that adds or widens a declared grant needs the same re-approval a new resource
    or external action does.

    Declared-only and one-element-tuple-wrapped for the reason `preconditions_place`
    documents: a plan predating this field (every plan before schema 36) keeps the
    exact structural signature it had — `_structural_signature` is compared by
    `diff_plans` on every replan, and an unconditional contribution would reclassify
    every existing plan's next refinement as substantive for no edit anyone made."""
    g = getattr(stage, "grants", None)
    if g is None or g.is_empty():
        return ()
    return (g.effective_tuple(),)


def effects_place(stage) -> tuple:
    """The stage's declared `[[stage.effects]]` script-effect claims as a
    contribution to a change-decision key: a ONE-element tuple holding a
    hashable, order-independent `frozenset` of (path, sha256, resolver)
    triples, or the EMPTY tuple when the stage declares no effects at all.
    Mirrors `grants_place` in shape and in rationale: like a declared grant,
    a declared script-effect trust claim is a permission surface (which
    scripts resolve this stage's calls, pinned to which bytes) that an
    executor may not silently move under the light (`--renormalize`) path,
    so this is spliced into `_structural_signature` (below) and into
    `gates._renorm_stage_residual` (imported from here) on the same footing
    as `grants`. Also unclaimed by `stage_carry_key`/`stage_question_key`
    for the same reason: no name in the question-target vocabulary names a
    stage's trusted-script surface."""
    effects = getattr(stage, "effects", None)
    if not effects:
        return ()
    return (frozenset((e.path, e.sha256, e.resolver) for e in effects),)


_NEGATIVE_CONTROL_PLACE_ABSENT = (None, None)


def negative_control_place(stage) -> tuple:
    """The stage's negative-control place — `criterion.negative_control` plus its
    waiver — as a contribution to a change-decision key: a ONE-element tuple holding
    the pair, or the EMPTY tuple when the stage declares neither.

    Grouped rather than spliced field-by-field for the same collision `knowledge_place`
    guards against: (negative_control='x', waiver=None) and (negative_control=None,
    waiver='x') would otherwise both flatten to the same lone element.

    Declared-only, for the reason `preconditions_place` documents: a plan predating
    this place keeps the exact key it had — stage_question_key is persisted in
    Question.disposed_at_key and compared across processes, and stage_carry_key gates
    PASSED carry-forward, so an unconditional contribution (or a `... or ""` default)
    would flip every disposed question, and every already-PASSED stage, of every live
    session that predates the field."""
    place = (stage.criterion.negative_control, stage.criterion.negative_control_waiver)
    return () if place == _NEGATIVE_CONTROL_PLACE_ABSENT else (place,)


def delivery_place(stage) -> tuple:
    """The edge deliveries as a contribution to a change-decision key: a ONE-element tuple
    holding the per-edge deliveries TAGGED with this field's name, or the EMPTY tuple when
    no edge declares one.

    Declared-only, for the reason `preconditions_place` documents: a plan that predates
    `Supply.delivery` keeps the exact key it had, so every persisted
    `Question.disposed_at_key` stays valid. Tagged, as `procedure_place` is, so the splice
    cannot collide with another independently conditional one. Unlike its siblings it
    joins `stage_question_key` only; `stage_carry_key` already sees the deliveries as part
    of the typed edges."""
    deliveries = tuple(s.delivery for s in stage.supplies)
    return () if all(d is None for d in deliveries) else (("delivery", deliveries),)


def stage_carry_key(stage) -> tuple:
    """Full-fidelity per-stage identity for PASSED carry-forward across a
    substantive replan (#12): a stage keeps its PASSED status only if NOTHING about
    its definition changed.

    A superset of `_structural_signature`'s per-stage tuple (executor / deps /
    done_criterion / criterion_type) PLUS the prose fields (title / result /
    invariants / means / method / conditions / verify_command / expected_exit), with
    the deps as typed edges (`StageNorm.carry_key`).
    Kept SEPARATE from `_structural_signature` (which drives diff_plans'
    refinement-vs-substantive classification) so that extending the carry-forward
    key never reclassifies a prose refinement as substantive — the two answer
    different questions and must evolve independently. Operates on a Stage, so both
    plan-doc stages and live SessionState stages key identically."""
    from .stage_norm import StageNorm
    return StageNorm.from_stage(stage).carry_key()


def stage_reattest_key(stage) -> tuple:
    """Narrow "operative surface" identity for the stage-6 re-attest route: a prior
    PASSED control attestation is still trustworthy without re-running the actor
    that produced it only if this key is unchanged — method, control criterion,
    expected result image, executor, and done criterion.

    Deliberately NARROWER than `stage_carry_key` (which gates the FREE, no-cost
    Outcome carry-forward on a stage's WHOLE definition — title/conditions/
    invariants included) and differently scoped than `gates._operative_surface`
    (a whole-PLAN function answering the refinement-vs-substantive question,
    which for that reason excludes done_criterion/expected_result_image as
    prose). Re-attest still pays for a fresh control re-run (the third of its
    three conditions), so this key only needs to rule out a replan that moved
    WHAT the stage is being held to — a title/conditions/knowledge edit elsewhere
    in the stage is safe to re-attest through.
    """
    return (
        stage.means.method,
        stage.actor.executor,
        stage.subject.result,
        stage.criterion.criterion_type,
        stage.criterion.done_criterion,
        stage.criterion.verify_command,
        stage.criterion.expected_exit,
        _normalize_string(stage.criterion.verify_venue),
        _normalize_string(stage.criterion.verify_kind),
        stage.criterion.landed,
        *((_normalize_string(stage.criterion.verify_venue_at_final),)
          if stage.criterion.verify_venue_at_final else ()),
    )


def stage_reattest_digest(stage) -> str:
    """Stable sha256 hex digest of `stage_reattest_key(stage)` — persisted on a
    `ReattestStash` at replan time and recomputed against the LIVE stage at
    dispatch time, so a further plan edit made during the PLAN_READY window
    (after the stash was built but before `dispatch --re-attest` runs) is
    caught rather than trusted stale. A digest, not the raw tuple, for the same
    reason `stage_question_key` returns one: it is compared across processes."""
    return hashlib.sha256(repr(stage_reattest_key(stage)).encode("utf-8")).hexdigest()


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
                  "criterion.landed.remote", "criterion.landed.provider",
                  "criterion.verify_venue_at_final",
                  "criterion.negative_control", "criterion.negative_control_waiver"),
    "done_criterion": ("criterion.done_criterion",),
    "principle": ("principle.statement", "principle.source", "principle.derivation",
                  "principle.confidence", "principle.refutation"),
    "conditions": ("conditions",),
    "preconditions": ("preconditions",),
    "control": _WHOLE_STAGE_DEFINITION,
    "order": _WHOLE_STAGE_DEFINITION,
    "requirements": _WHOLE_STAGE_DEFINITION,
}


# Leaf values that contribute nothing to an element's key, so a stage that never
# declared the field (or declared its default) keeps the digest it had before the field
# existed — the declared-only rule the whole-stage payload follows.
_ELIDED_WHEN_DEFAULT: dict[str, tuple[tuple, ...]] = {
    "criterion.landed.provider": ((None,), ("git",)),
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
            values = tuple(
                v for p in paths
                if (v := _leaf_values(stage, p)) not in _ELIDED_WHEN_DEFAULT.get(p, ())
            )
            # A declared edge delivery is part of what `material` hands over, so it joins
            # this element's payload only when declared (legacy keys stay byte-identical).
            extra = delivery_place(stage) if element == "material" else ()
            payload = repr((element, values, *extra))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    from .stage_norm import StageNorm
    return StageNorm.from_stage(stage).review_digest()


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


def order_digest(doc: PlanDoc) -> str:
    """The order-approvals ledger key (`order_approvals.py`) — narrower than
    `plan_meta_digest`/`order_place` on purpose: it excludes every DERIVED
    order field (`coverage`, each requirement's `derivation`, `malformed`,
    `requirements_dropped`) because those describe how the order is
    currently BEING SERVED, not what the customer actually ordered. Two
    plans serving the identical order through a different coverage map (a
    stage renamed, a control moved) must key to the SAME ledger entry, or a
    customer's earlier approval would silently stop covering a later,
    functionally-identical replan (K5's failure mode one level up: not a
    reused task_id, but a reused order re-derived into a new digest).

    Requirements ride in as an (id, text) SET (`frozenset`, not the
    `order_place` tuple's insertion-ordered list) — reordering the
    `[[meta.order.requirements]]` table in the TOML changes nothing a
    customer approved, so it must not change this digest either.

    `doc.meta.goal` is the one field this shares with `plan_meta_digest`
    that is NOT part of `order_place` — included directly because the goal
    is part of what was ordered, same as `done_criterion`.

    `repo_root` is deliberately ABSENT (unlike `plan_meta_digest`): a plan
    reused against a different checkout of the same order is still the same
    order, and a self-grant must not re-ask merely because `repo_root`
    moved."""
    order = doc.meta.order
    if order is None:
        customer_id = customer = functional_place = ""
        requires_traceability = False
        requirement_set: frozenset = frozenset()
    else:
        customer_id = order.customer_id
        customer = order.customer
        functional_place = order.functional_place
        requires_traceability = order.requires_traceability
        requirement_set = frozenset((r.id, r.text) for r in order.requirements)
    payload = repr((
        doc.meta.goal,
        doc.meta.done_criterion,
        doc.meta.criterion_type,
        doc.meta.weight_class,
        customer_id,
        customer,
        functional_place,
        requires_traceability,
        tuple(sorted(requirement_set)),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_meta_element_key(doc: PlanDoc, element: str) -> str:
    """Digest of a single plan-level field a Question can target — 'goal' or
    'done_criterion' — narrower than `plan_meta_digest`, which bundles goal,
    done_criterion, criterion_type, weight_class, repo_root and the order into
    ONE hash and would invalidate a goal-bound question on an unrelated
    done_criterion edit (or vice versa). Tagged with the element name, mirroring
    `stage_question_key`'s element form, so a goal and a done_criterion that
    happen to hold identical text cannot collide.

    Returns a stable sha256 hex digest for the same reason `stage_question_key`
    does: the value is persisted in Question.disposed_at_key and compared across
    processes."""
    value = doc.meta.goal if element == "goal" else doc.meta.done_criterion
    payload = repr((element, value))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_meta_element_keys(doc: PlanDoc) -> dict[str, str]:
    """Both plan-level element keys, in the {element: key} shape premise.py
    already uses per stage (`stage_element_keys`)."""
    return {
        "goal": plan_meta_element_key(doc, "goal"),
        "done_criterion": plan_meta_element_key(doc, "done_criterion"),
    }


def plan_stage_digests(doc: PlanDoc) -> dict[int, str]:
    return {s.index: stage_element_keys(s)[WHOLE_STAGE_ELEMENT] for s in doc.stages}


PAIR_PLAN_NODE = "plan"
PAIR_BASE_NODE = "base"

META_TOKEN = "meta:"
ORDER_TOKEN = "order:"


def stage_token(index: int) -> str:
    return f"stage:{index}"


def part_digest_map(doc: PlanDoc) -> dict[str, str]:
    """`{part token: digest}` for every part a review concern can name — `meta:` (the
    goal and done criterion, without the order table), `order:` and each `stage:<n>` —
    the baseline a later record compares against to say which parts changed."""
    meta_payload = repr((
        doc.meta.goal, doc.meta.done_criterion, doc.meta.criterion_type,
        doc.meta.weight_class, doc.meta.repo_root,
    ))
    digests = {
        META_TOKEN: hashlib.sha256(meta_payload.encode("utf-8")).hexdigest(),
        ORDER_TOKEN: hashlib.sha256(repr(order_place(doc.meta)).encode("utf-8")).hexdigest(),
    }
    digests.update({stage_token(i): d for i, d in plan_stage_digests(doc).items()})
    return digests


UNIT_ID_PREFIX = "unit:"


def unit_id(node: "int | str") -> str:
    """The review id of `node`'s unit: `unit:base` or `unit:<n>`."""
    return f"{UNIT_ID_PREFIX}{node}"


def is_unit_id(review_id: str) -> bool:
    return review_id.startswith(UNIT_ID_PREFIX)


def pair_part_tokens(pair_id: str) -> tuple[str, ...]:
    """The parts a pair or unit review judges: its nodes' parts, the `base` node
    standing for the meta and the order (the legacy `plan` node likewise)."""
    nodes = (unit_node(pair_id),) if is_unit_id(pair_id) else split_pair_id(pair_id)
    tokens: list[str] = []
    for node in nodes:
        own = (META_TOKEN, ORDER_TOKEN) if node in (PAIR_PLAN_NODE, PAIR_BASE_NODE) else (
            stage_token(node),)
        tokens.extend(t for t in own if t not in tokens)
    return tuple(tokens)


def plan_coverage_entries(doc: PlanDoc) -> dict[int, tuple[tuple[str, str], ...]]:
    """`{stage index: ((requirement id, control text), ...)}` for every stage a
    `[meta.order.coverage]` control names through one of the stage-addressed
    coverage grammars (`controls.COVERAGE_GRAMMARS`), entries in coverage order.
    Each entry is an ordinary typed edge of `unit:base` on that stage: the
    requirement is the element of base it supplies, the control the part of the
    stage the entry names. Empty when the plan declares no order. Imports
    `controls` locally: it imports this module."""
    from .controls import STAGE_LANDED_ASSERTION, STAGE_VERIFY_COMMAND
    order = doc.meta.order
    if order is None:
        return {}
    grammars = (STAGE_VERIFY_COMMAND, STAGE_LANDED_ASSERTION)
    entries: dict[int, list[tuple[str, str]]] = {}
    for req_id, controls in order.coverage.items():
        for control in controls:
            for grammar in grammars:
                match = grammar.pattern.match(control)
                if match is None:
                    continue
                entries.setdefault(int(match.group(1)), []).append((req_id, control))
                break
    return {n: tuple(rows) for n, rows in entries.items()}


def plan_coverage_refs(doc: PlanDoc) -> dict[int, tuple[str, ...]]:
    """`{stage index: requirement ids}` for every stage a coverage control names
    (`plan_coverage_entries`), requirement ids in coverage order, each once."""
    return {n: tuple(dict.fromkeys(req for req, _ in rows))
            for n, rows in plan_coverage_entries(doc).items()}


def plan_reliance_set(doc: PlanDoc) -> frozenset[int]:
    """The stages the plan as a whole relies on to discharge the order: every
    sink (a stage no other stage relies on) plus every stage a coverage
    control names (`plan_coverage_refs`)."""
    sinks = {s.index for s in doc.stages if not consumers(doc, s.index)}
    valid = {s.index for s in doc.stages}
    return frozenset(sinks | (set(plan_coverage_refs(doc)) & valid))


def review_pairs(doc: PlanDoc) -> tuple[str, ...]:
    """Every review pair of `doc` as an id `<b>-<s>` — `b` relies on `s` — in
    review order: `base-<s>` by `s` for each stage a `[meta.order.coverage]`
    entry names (`plan_coverage_refs`; the coverage entry is an ordinary edge of
    `unit:base`), then `<n>-<s>` by `n` and then `s` for every reliance edge
    between stages. There is no `plan-<s>` pair and no `base-plan`: a stage
    nothing relies on and no requirement names is caught by its own unit review
    (`review_units`).

    Raises PlanError for a dangling or cyclic raw reliance graph: the raw
    union `reliance_set` reads can cycle while the supplies-derived graph
    `_validate_graph` checked does not, so every stage is walked here and
    each pair-level entry point (`parse_pair`, the bundle, `pair_binding`)
    inherits the check."""
    for stage in doc.stages:
        reliance_closure(doc, stage.index)
    valid = {s.index for s in doc.stages}
    pairs = [f"{PAIR_BASE_NODE}-{s}" for s in sorted(set(plan_coverage_refs(doc)) & valid)]
    for stage in sorted(doc.stages, key=lambda st: st.index):
        pairs.extend(f"{stage.index}-{s}" for s in sorted(reliance_set(doc, stage.index)))
    return tuple(pairs)


def review_units(doc: PlanDoc) -> tuple[str, ...]:
    """Every unit of `doc` in review order: `unit:base` — always, an order-less
    plan included — then `unit:<n>` by stage index. A unit is reviewed alone
    (`render.render_unit_review_bundle`): `unit:base` holds the order (customer,
    functional place, requirements, coverage) and the meta's goal, done criterion
    and final checks; `unit:<n>` every field of stage `n` and its full edge set."""
    return (unit_id(PAIR_BASE_NODE),) + tuple(
        unit_id(s.index) for s in sorted(doc.stages, key=lambda st: st.index)
    )


def review_ids(doc: PlanDoc) -> tuple[str, ...]:
    """Every id the walk demands: the units, then the pairs."""
    return review_units(doc) + review_pairs(doc)


def is_legacy_review_id(review_id: str) -> bool:
    """A review id a stored record may still carry but no plan has any more:
    `base-plan`, `plan-<n>` and `unit:plan`. Such a record loads and validates and
    is stale, never current; nothing demands it."""
    if review_id in (f"{PAIR_BASE_NODE}-{PAIR_PLAN_NODE}", unit_id(PAIR_PLAN_NODE)):
        return True
    head, _, tail = review_id.partition("-")
    return head == PAIR_PLAN_NODE and tail.isdigit()


def parse_pair(doc: PlanDoc, pair_id: str) -> tuple["int | str", "int | str"]:
    """`(b, s)` for a pair id `doc` has — stage nodes as ints, the order node as
    `PAIR_BASE_NODE`. Splits on the first `-`. Raises ValueError for any id
    outside `review_pairs(doc)`, including unit ids and legacy `plan-<s>` /
    `base-plan`."""
    if pair_id not in review_pairs(doc):
        raise ValueError(
            f"no review pair {pair_id!r} in plan {doc.meta.task_id!r} "
            f"(valid pairs: {', '.join(review_pairs(doc)) or 'none'})"
        )
    return split_pair_id(pair_id)


def parse_unit(doc: PlanDoc, review_id: str) -> "int | str":
    """The node (`PAIR_BASE_NODE` or a stage index) of a unit id `doc` has.
    Raises ValueError for any other id."""
    if review_id not in review_units(doc):
        raise ValueError(
            f"no review unit {review_id!r} in plan {doc.meta.task_id!r} "
            f"(valid units: {', '.join(review_units(doc))})"
        )
    return unit_node(review_id)


def unit_node(review_id: str) -> "int | str":
    """The node of a unit id already known to be in `review_units`, with no
    membership check (`parse_unit` re-enumerates every unit per call)."""
    node = review_id[len(UNIT_ID_PREFIX):]
    return int(node) if node.isdigit() else node


def split_pair_id(pair_id: str) -> tuple["int | str", "int | str"]:
    """`(b, s)` of a pair id already known to be in `review_pairs`, with no
    membership check (`parse_pair` re-enumerates every pair per call)."""
    b, s = pair_id.split("-", 1)
    return (int(b) if b.isdigit() else b, int(s) if s.isdigit() else s)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pair_node_key(doc: PlanDoc, node: "int | str") -> str:
    from .render import node_file_text
    if node == PAIR_BASE_NODE:
        return _sha256_hex(node_file_text(doc, node))
    stage = _stage_by_index(doc, node)
    return _sha256_hex(repr((
        stage_element_keys(stage)[WHOLE_STAGE_ELEMENT],
        stage_interface_digest(doc, stage),
    )))


def pair_binding(doc: PlanDoc, pair_id: str) -> dict:
    """The seven digests a CURRENT review of `pair_id` must match against
    `doc`, recomputed fresh on every call. Together they cover what the
    reviewer was shown: the order context (`context_digest`, the base file's
    sha256), both nodes' identity keys, both nodes' file bytes, the exact
    service-interface and edge sections of the bundle. For a `base-<s>` pair
    the context, base key and base file digests are the same value by
    construction. Raises ValueError for an unknown pair, PlanError for a
    dangling or cyclic reliance graph."""
    from .render import node_file_text, pair_edge_text, pair_service_text
    b, s = parse_pair(doc, pair_id)
    base_text = node_file_text(doc, PAIR_BASE_NODE)
    return {
        "context_digest": _sha256_hex(base_text),
        "base_key": _pair_node_key(doc, b),
        "service_key": _pair_node_key(doc, s),
        "base_file_digest": _sha256_hex(node_file_text(doc, b)),
        "service_file_digest": _sha256_hex(node_file_text(doc, s)),
        "service_interface_digest": _sha256_hex(pair_service_text(doc, s, b)),
        "edge_digest": _sha256_hex(pair_edge_text(doc, b, s)),
    }


PAIR_SERVICE_CONSTRUCTION_KEYS = ("service_key", "service_file_digest")


def pair_shows_declared_product_only(doc: PlanDoc, pair_id: str) -> bool:
    """Whether the bundle of `pair_id` shows its service only as the declared product
    (`render.pair_service_text`): a stage service that relies on something and has a
    concrete interface. A source stage and an `interface_empty` stage are shown in
    full."""
    _, s = parse_pair(doc, pair_id)
    return _service_shown_as_declared_product(doc, s)


def _service_shown_as_declared_product(doc: PlanDoc, s: "int | str") -> bool:
    return bool(reliance_set(doc, int(s))) and not interface_empty(_stage_by_index(doc, int(s)))


def pair_currency_keys(doc: PlanDoc, pair_id: str) -> tuple[str, ...]:
    """The `PAIR_BINDING_KEYS` a LEGACY record of `pair_id` (one written before the
    content digests, `PlanPairReview.is_content_keyed`) is judged current by -- the
    content-derived pair of its seven digests, so an upgrade does not stale a record
    whose plan content is unchanged: the base's `base_key` and the service's
    `service_key`, or its `service_interface_digest` when the reviewer was shown
    only its declared product (the construction digests are then not evidence).
    The rendered-text digests (`context_digest`, `edge_digest`, the file digests)
    are not consulted: a render-code change must not stale a record."""
    if pair_shows_declared_product_only(doc, pair_id):
        return ("base_key", "service_interface_digest")
    return ("base_key", "service_key")


def _element_digest(stage: Stage, keys: dict[str, str], element: "str | None") -> str:
    """The digest of one element of `stage`: the element's own key, or the
    whole-stage key for an element-less edge (the whole stage is supplied) and for a
    name the key family does not know (the wider answer)."""
    if element is None or element not in keys:
        return keys[WHOLE_STAGE_ELEMENT]
    return keys[element]


def _reviewed_stage_keys(stage: Stage) -> dict[str, str]:
    """`stage_element_keys` with every whole-stage entry widened to what a
    reviewer is shown of `stage`: the declared fields its file renders that no
    question-target name covers (output artifacts, cost tier, ephemeral-artifacts
    waiver, grants, script effects) join the whole-stage digest. Unchanged for a
    stage declaring none of them. `stage_question_key` is left alone: a premise
    stamp binds to the question vocabulary, which names none of these."""
    keys = stage_element_keys(stage)
    whole = keys[WHOLE_STAGE_ELEMENT]
    grants = grants_place(stage)
    rules, add_dirs = grants[0] if grants else ((), ())
    effects = effects_place(stage)
    extras = (
        tuple(stage.output_artifacts),
        stage.actor.cost_tier,
        stage.ephemeral_artifacts_waiver,
        tuple(sorted(rules)),
        tuple(sorted(add_dirs)),
        tuple(sorted(effects[0])) if effects else (),
    )
    if not any(extras):
        return keys
    reviewed = _sha256_hex(repr((whole, extras)))
    return {name: reviewed if key == whole else key for name, key in keys.items()}


def _supplied_by(doc: PlanDoc, b: int, s: int) -> tuple[Supply, ...]:
    """The typed edges of stage `b` on stage `s`."""
    return tuple(sup for sup in _stage_by_index(doc, b).supplies if sup.on == s)


def _edge_rows(doc: PlanDoc, b: int, s: int) -> tuple:
    """The pair's edge set as a comparable value: the (element, artifact, delivery)
    of every typed edge of `b` on `s`, and whether the reliance is also a raw
    `depends_on` that no typed edge restates."""
    rows = tuple(sorted(
        (sup.element or "", sup.artifact or "", sup.delivery or "")
        for sup in _supplied_by(doc, b, s)
    ))
    return rows, s in doc.raw_depends_on.get(b, ())


def pair_content(doc: PlanDoc, pair_id: str) -> dict[str, str]:
    """The four content digests a CURRENT review of `pair_id` must match, from plan
    content alone (StageNorm / meta / order digests, never rendered text, so a
    render-code change with unchanged plan content moves none of them):

    - `base_norm`: the elements of the base node the edges supply -- for a stage
      base, the element keys the typed edges name (the whole-stage key when an edge
      is element-less); for `base-<s>`, the (id, text, derivation) of the
      requirements the coverage entries on `s` name.
    - `service_iface_norm`: the service's `interface_token` (full carry digest for a
      source or blank-interface stage).
    - `service_norm`: the service's construction -- the whole-stage key when the
      bundle shows it in full (a source or blank-interface stage); for `base-<s>`
      the `criterion` key of `s`, the part the coverage entries name; empty when
      the reviewer was shown only the declared product.
    - `edge_norm`: the pair's edge set -- (element, artifact, delivery) per typed
      edge, or (requirement id, control) per coverage entry.

    Raises ValueError for an unknown pair, PlanError for a cyclic reliance graph."""
    from .stage_norm import interface_token
    b, s = parse_pair(doc, pair_id)
    service = _stage_by_index(doc, int(s))
    service_keys = _reviewed_stage_keys(service)
    iface = interface_token(service)
    if b == PAIR_BASE_NODE:
        entries = plan_coverage_entries(doc).get(int(s), ())
        wanted = {req_id for req_id, _ in entries}
        order = doc.meta.order
        requirements = tuple(
            (r.id, r.text, r.derivation)
            for r in (order.requirements if order is not None else ())
            if r.id in wanted
        )
        return {
            "base_norm": _sha256_hex(repr(("requirements", requirements))),
            "service_iface_norm": iface,
            "service_norm": service_keys["criterion"],
            "edge_norm": _sha256_hex(repr(("coverage", entries))),
        }
    base = _stage_by_index(doc, int(b))
    base_keys = _reviewed_stage_keys(base)
    supplied = _supplied_by(doc, int(b), int(s))
    elements = sorted({_element_digest(base, base_keys, sup.element) for sup in supplied})
    if not supplied or any(sup.element is None for sup in supplied):
        elements = sorted(set(elements) | {base_keys[WHOLE_STAGE_ELEMENT]})
    return {
        "base_norm": _sha256_hex(repr(("elements", tuple(elements)))),
        "service_iface_norm": iface,
        "service_norm": (
            "" if _service_shown_as_declared_product(doc, s)
            else service_keys[WHOLE_STAGE_ELEMENT]
        ),
        "edge_norm": _sha256_hex(repr(("edges", _edge_rows(doc, int(b), int(s))))),
    }


def pair_currency_hash(doc: PlanDoc, pair_id: str) -> str:
    """sha256 of the `pair_content` digests of `pair_id` -- what a whole-plan record
    keeps per pair in `reviewed_pair_currency`."""
    content = pair_content(doc, pair_id)
    return _sha256_hex("\n".join(f"{k}={content[k]}" for k in PAIR_CONTENT_KEYS))


def unit_content(doc: PlanDoc, node: "int | str") -> dict:
    """What a CURRENT review of the unit of `node` was shown, from plan content
    alone: `unit:base` -- the meta's goal, done criterion, final checks and the
    order; `unit:<n>` -- every field of stage `n` (its whole-stage key) and its full
    edge set, outbound (what it supplies from, its raw `depends_on`) and inbound
    (what other stages' typed edges and the coverage entries take from it)."""
    if node == PAIR_BASE_NODE:
        return {"meta": plan_meta_digest(doc), "extra": order_extra_digest(doc.meta)}
    n = int(node)
    stage = _stage_by_index(doc, n)
    outbound = tuple(sorted(
        (sup.on, sup.element or "", sup.artifact or "", sup.delivery or "")
        for sup in stage.supplies
    ))
    inbound = tuple(sorted(
        (s.index, sup.element or "", sup.artifact or "", sup.delivery or "")
        for s in doc.stages for sup in s.supplies if sup.on == n
    ))
    reliance = tuple(sorted(doc.raw_depends_on.get(n, ())))
    consumed = tuple(sorted(
        s.index for s in doc.stages if n in doc.raw_depends_on.get(s.index, ())
    ))
    return {
        "stage": _reviewed_stage_keys(stage)[WHOLE_STAGE_ELEMENT],
        "outbound": outbound,
        "depends_on": reliance,
        "inbound": inbound,
        "depended_on_by": consumed,
        "coverage": plan_coverage_entries(doc).get(n, ()),
    }


def unit_currency_hash(doc: PlanDoc, unit: str) -> str:
    """sha256 of `unit_content` for the unit id `unit` -- the currency value a unit
    record and a whole-plan baseline keep. Raises ValueError for an unknown unit."""
    node = parse_unit(doc, unit)
    return _sha256_hex(repr((unit, sorted(unit_content(doc, node).items()))))


def review_currency_hash(doc: PlanDoc, review_id: str) -> str:
    """The currency hash of a unit or pair id, recomputed from `doc` alone (no
    record). For a pair shown only as its declared product it ignores the
    service's construction by construction (`pair_content`)."""
    if is_unit_id(review_id):
        return unit_currency_hash(doc, review_id)
    return pair_currency_hash(doc, review_id)


def plan_interface_digests(doc: PlanDoc, indices=None) -> dict[int, str]:
    """`{stage index: interface digest}` for `indices` (default every stage)."""
    return {
        s.index: stage_interface_digest(doc, s) for s in doc.stages
        if indices is None or s.index in indices
    }


def moved_interfaces(doc: PlanDoc, baseline_interfaces: dict, moved: set[int]) -> set[int]:
    """The members of `moved` whose interface digest differs from the baseline's. A
    stage the baseline records no interface for (any record written before they were
    kept) counts as moved: the interface cannot be shown unchanged, so the wider answer."""
    recorded = {str(k): v for k, v in (baseline_interfaces or {}).items()}
    return {i for i, d in plan_interface_digests(doc, moved).items() if recorded.get(str(i)) != d}


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


# Moves of these two are not question targets (no `stage:<n>.<name>` names them): they are the
# consumer-facing identities of a stage, tracked beside the question vocabulary.
PSEUDO_ELEMENTS = frozenset({INTERFACE_ELEMENT, CARRY_ELEMENT})

# The names that move when only a stage's `criterion` fields do: the element itself, the
# whole-stage keys (the reserved entry and the names sharing it) and the carry digest.
# Any other name moving -- `interface` (title, result image, criterion type, done criterion,
# output artifacts), `executor`, `material` (the edges) -- means the edit left the control.
CRITERION_CONFINED_ELEMENTS = frozenset(
    {"criterion", CARRY_ELEMENT, WHOLE_STAGE_ELEMENT}
    | {name for name, fields in _ELEMENT_FIELDS.items() if fields is _WHOLE_STAGE_DEFINITION}
)


def stage_norm_keys(stage) -> dict[str, str]:
    """`stage_element_keys` plus the two identities a consumer of the stage relies on:
    its interface token and its carry digest (`stage_norm`)."""
    from .stage_norm import StageNorm, interface_token
    keys = stage_element_keys(stage)
    keys[INTERFACE_ELEMENT] = interface_token(stage)
    keys[CARRY_ELEMENT] = StageNorm.from_stage(stage).carry_digest()
    return keys


def _final_check_place(doc: PlanDoc) -> list:
    return [
        (fc.command, fc.expected_exit, fc.label,
         _normalize_string(fc.venue), _normalize_string(fc.kind), fc.landed)
        for fc in doc.meta.final_check
    ]


def final_check_digest(doc: PlanDoc) -> str:
    """Digest of the plan's final checks, which sit outside `plan_meta_digest`."""
    return hashlib.sha256(repr(_final_check_place(doc)).encode("utf-8")).hexdigest()


def _final_check_identity(check) -> tuple:
    """A final check's identity: what it verifies and where, not the command that does."""
    return (check.label, _normalize_string(check.kind), _normalize_string(check.venue),
            check.expected_exit, check.landed)


def acceptance_requirement_bindings(doc: PlanDoc) -> dict[str, str]:
    """`{requirement id: digest}` of what each order requirement is accepted against: its
    text and, per coverage entry (order-insensitive), the deliverable the entry names -- the
    interface token of a named stage, the identity of a named final check, the entry's own
    text for any other control. A control-only edit (a verify command, a method) moves no
    binding; a moved deliverable, requirement text or coverage entry moves exactly the
    requirements it concerns. Empty when the plan declares no order."""
    from .controls import COVERAGE_GRAMMARS, FINAL_CHECK
    from .stage_norm import interface_token
    order = doc.meta.order
    if order is None:
        return {}
    stages = {s.index: s for s in doc.stages}
    checks = doc.meta.final_check

    def token(entry: str) -> tuple:
        for grammar in COVERAGE_GRAMMARS:
            match = grammar.pattern.match(entry)
            if match is None:
                continue
            n = int(match.group(1))
            if grammar is FINAL_CHECK:
                if 1 <= n <= len(checks):
                    return ("final_check", _final_check_identity(checks[n - 1]))
                return ("final_check", n, "missing")
            stage = stages.get(n)
            if stage is None:
                return ("stage", n, "missing")
            return ("stage", n, interface_token(stage))
        return ("entry", entry)

    bindings: dict[str, str] = {}
    for req in order.requirements:
        tokens = sorted(repr(token(e)) for e in order.coverage.get(req.id, ()))
        payload = repr((req.text, tuple(tokens)))
        bindings[req.id] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return bindings


def stage_element_baseline(doc: PlanDoc) -> dict[str, dict[str, str]]:
    """`{str(stage index): {element: key}}` -- what a record keeps so a later `norm_delta_from`
    can name the elements that moved, not just the stages."""
    return {str(s.index): stage_norm_keys(s) for s in doc.stages}


def _order_place_digest(doc: PlanDoc) -> str:
    return hashlib.sha256(repr(order_place(doc.meta)).encode("utf-8")).hexdigest()


def norm_baseline(doc: PlanDoc) -> dict:
    """Everything `norm_delta_from` compares a document against."""
    return {
        "meta": plan_meta_digest(doc),
        "order": _order_place_digest(doc),
        "final_check": final_check_digest(doc),
        "stages": stage_element_baseline(doc),
    }


@dataclass(frozen=True)
class NormDelta:
    """How a plan's norm moved against a baseline: the one answer every consumer of "what
    changed" reads instead of diffing the documents itself.

    `elements` holds, per stage present in the document, the names whose key moved (the
    question vocabulary, the reserved whole-stage entry, and `interface` / `carry`); a stage
    absent from the baseline is in `added` with every element moved. `order_moved` is a
    sub-case of `meta_moved` (the order rides in the meta digest)."""
    meta_moved: bool
    order_moved: bool
    final_check_moved: bool
    added: frozenset
    removed: frozenset
    elements: dict

    @property
    def moved_stages(self) -> frozenset:
        return frozenset(self.elements)

    @property
    def interface_moved(self) -> frozenset:
        return frozenset(i for i, names in self.elements.items() if INTERFACE_ELEMENT in names)

    def question_elements(self, index: int) -> frozenset:
        """The moved elements of stage `index` a question can be bound to."""
        return self.elements.get(index, frozenset()) - PSEUDO_ELEMENTS

    @property
    def any_moved(self) -> bool:
        return bool(self.meta_moved or self.order_moved or self.final_check_moved
                    or self.added or self.removed or self.elements)


def norm_delta_from(baseline: dict, doc: PlanDoc) -> NormDelta:
    """The `NormDelta` of `doc` against `baseline` (a `norm_baseline` value, typically
    round-tripped through JSON, so stage indices are compared as strings). A missing entry
    compares as moved: the wider answer."""
    recorded = {str(k): v for k, v in (baseline.get("stages") or {}).items()}
    elements: dict[int, frozenset] = {}
    added = set()
    present = set()
    for s in doc.stages:
        present.add(str(s.index))
        keys = stage_norm_keys(s)
        old = recorded.get(str(s.index))
        if old is None:
            added.add(s.index)
            elements[s.index] = frozenset(keys)
            continue
        moved = frozenset(name for name, key in keys.items() if old.get(name) != key)
        if moved:
            elements[s.index] = moved
    removed = frozenset(int(k) for k in recorded if k not in present)
    return NormDelta(
        meta_moved=(baseline.get("meta") or "") != plan_meta_digest(doc),
        order_moved=(baseline.get("order") or "") != _order_place_digest(doc),
        final_check_moved=(baseline.get("final_check") or "") != final_check_digest(doc),
        added=frozenset(added),
        removed=removed,
        elements=elements,
    )


def norm_delta(old: PlanDoc, new: PlanDoc) -> NormDelta:
    """Pure document-vs-document form of `norm_delta_from`."""
    return norm_delta_from(norm_baseline(old), new)


def _venue_for(doc: PlanDoc) -> str:
    """The venue `derive_stage_grants` resolves DR-E/in-venue paths against —
    `delivery_worktree` when declared else `repo_root`, mirroring
    `PlanFrame.resolve_check_venue`'s identical fallback in state.py — with a
    last-resort "." so a plan declaring neither (permitted; every verify_command
    then runs in the invoker's own cwd) still gets a non-empty venue string
    rather than the malformed absolute path an empty one would build in DR-E."""
    return doc.meta.delivery_worktree or doc.meta.repo_root or "."


def _effective_grants_for_stage(stage, venue: str) -> "StageGrants":
    """DECLARED plus DERIVED, in that order — the set `grant_covers_call` checks an
    actual tool call against and the set `_grants_grew` compares across a replan.
    `derive_stage_grants` never sees the declared half (it derives from the stage's
    OTHER fields only), so the two lists are concatenated here rather than inside
    grants.py, keeping that module's derivation pure of any notion of "already
    declared"."""
    declared = stage.grants if getattr(stage, "grants", None) else StageGrants()
    derived, _dropped = _grants.derive_stage_grants(stage, venue=venue)
    return StageGrants(
        allow=list(declared.allow) + list(derived.allow),
        add_dirs=list(declared.add_dirs) + list(derived.add_dirs),
    )


def _grants_effective_map(doc: PlanDoc, *, venue: str | None = None) -> dict[int, tuple]:
    v = venue if venue is not None else _venue_for(doc)
    return {s.index: _effective_grants_for_stage(s, v).effective_tuple() for s in doc.stages}


def _grants_grew(old: PlanDoc, new: PlanDoc, *, relax_verify_identity: bool = False) -> bool:
    """Whether any stage's EFFECTIVE (declared+derived) grant set grew from `old` to
    `new` — a strictly wider Bash/Edit rule set, or a strictly wider set of add_dirs.
    A stage present only in `new` (an added stage) is compared against the empty
    grant set, so its own declared/derivable grants always count as growth —
    consistent with an added stage already forcing 'substantive' via
    `_structural_signature`'s differing stage-index sets, and cheap insurance if that
    ever changes independently. A SHRINKING or unchanged grant set is deliberately
    not growth: narrowing what a stage may touch never needs the re-approval a
    widening does.

    Both maps are derived against `new`'s OWN venue, not each document's own
    `_venue_for` result: DR-E bakes the venue string into its `Edit(//{venue}/...)`
    rule literal, so comparing each doc against its own venue would read a bare
    `repo_root`/`delivery_worktree` relocation — already excluded from
    `_structural_signature` and (for `delivery_worktree`) from `diff_plans`' prose
    keys — as a rule that "grew" purely because the two literals differ textually,
    not because anything the stage may touch actually widened.

    `relax_verify_identity` admits a rewritten verify_command that keeps its
    `grants.bash_rule_identity`; the caller sets it only when the autonomy boundary
    (`cli._kind_within_boundary`) will also judge the refinement."""
    shared_venue = _venue_for(new)
    old_map = _grants_effective_map(old, venue=shared_venue)
    new_map = _grants_effective_map(new, venue=shared_venue)
    old_stages = {s.index: s for s in old.stages}
    delta = norm_delta(old, new)
    for stage in new.stages:
        new_rules, new_dirs = new_map[stage.index]
        old_rules, old_dirs = old_map.get(stage.index, (frozenset(), frozenset()))
        grown_rules = new_rules - old_rules
        criterion_only = (not delta.order_moved
                          and delta.elements.get(stage.index, frozenset())
                          <= CRITERION_CONFINED_ELEMENTS)
        if relax_verify_identity and grown_rules and stage.index in old_stages and criterion_only:
            # DR-V derives one literal per verify_command segment, so rewriting a command
            # always adds literals; only a segment running a program/script the stage did
            # not already run is wider. See `grants.bash_rule_identity`. Applies only to a
            # stage whose delta is confined to its criterion (and an unmoved order): any
            # other move keeps the plain set difference.
            known = {
                _grants.bash_rule_identity(r.rule, shared_venue)
                for r in _derived_verify_rules(old_stages[stage.index], shared_venue)
            }
            verify_rules = {r.rule for r in _derived_verify_rules(stage, shared_venue)}
            identities = {r: _grants.bash_rule_identity(r, shared_venue) for r in grown_rules if r in verify_rules}
            grown_rules = {
                r for r in grown_rules
                if r not in verify_rules or identities[r] is None or identities[r] not in known
            }
        if grown_rules or (new_dirs - old_dirs):
            return True
    return False


def _derived_verify_rules(stage, venue: str) -> list:
    derived, _dropped = _grants.derive_stage_grants(stage, venue=venue)
    return [r for r in derived.allow if r.provenance == "derived:DR-V"]


def plan_has_any_grants(doc: PlanDoc) -> bool:
    """Whether ANY stage's effective (declared+derived) grant set is non-empty —
    shared by the present-plan grants-block containment check and the approve
    grants_sha256 binding check (both only fire on a plan that actually grants
    something; a plan with zero grants anywhere needs neither)."""
    return any(rules or dirs for rules, dirs in _grants_effective_map(doc).values())


def grants_sha256(doc: PlanDoc) -> str:
    """Stable sha256 hex digest of the plan's EFFECTIVE (declared+derived) grant set,
    one entry per stage index. `present-plan` stamps this onto the receipt and
    `approve` re-derives and compares it, so a materialization-layer change
    (grants.py's derivation rules) or an out-of-band plan edit between presentation
    and approval is never silently carried forward as already-reviewed. Same
    `repr(...)`-then-sha256 style as the other digests in this module; the map is
    sorted by stage index and each stage's rule/add_dir tuples are themselves
    sorted (via `effective_tuple()`) so the digest never depends on iteration or
    declaration order."""
    payload = repr(tuple(
        (idx, tuple(sorted(rules)), tuple(sorted(dirs)))
        for idx, (rules, dirs) in sorted(_grants_effective_map(doc).items())
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def materialized_grant_entries(doc: PlanDoc, *, venue: str | None = None) -> dict[str, dict]:
    """`{str(stage index): {"declared", "derived", "dropped"}}` -- the entry dicts a
    dispatch hands a child, derived once against `doc`'s venue. Bound next to
    `grants_sha256` so the set the hash covers is the set dispatch reads, instead of a
    re-derivation that depends on the engine code and venue filesystem of the moment."""
    venue = venue if venue is not None else _venue_for(doc)
    entries: dict[str, dict] = {}
    for stage in doc.stages:
        declared = stage.grants if getattr(stage, "grants", None) else StageGrants()
        derived, dropped = _grants.derive_stage_grants(stage, venue=venue)
        entries[str(stage.index)] = {
            "declared": [r.to_dict() for r in declared.allow]
            + [a.to_dict() for a in declared.add_dirs],
            "derived": [r.to_dict() for r in derived.allow]
            + [a.to_dict() for a in derived.add_dirs],
            "dropped": list(dropped),
        }
    return entries


def _entry_key(entry: dict) -> tuple:
    """Identity of one stored grant entry, provenance label ignored (as
    `StageGrants.effective_tuple` ignores it)."""
    if "rule" in entry:
        return ("rule", entry["rule"])
    return ("dir", entry["path"], entry["mode"])


def _effective_entry_keys(stage_entries: dict) -> frozenset:
    return frozenset(
        _entry_key(e) for e in list(stage_entries.get("declared") or [])
        + list(stage_entries.get("derived") or [])
    )


def entries_grants_sha256(entries: dict[str, dict]) -> str:
    """`grants_sha256` computed from stored entries instead of a plan: the digest of
    their declared+derived rules and add_dirs. `materialized_grant_entries(doc)` yields
    the same digest as `grants_sha256(doc)` (pinned by a test), so a stored set and the
    hash bound next to it can be re-checked against each other at dispatch without
    touching the venue filesystem. `declared` is read here -- the hash covers it -- while
    dispatch itself takes the declared half from the hash-verified plan snapshot."""
    def projection(stage_entries: dict) -> tuple:
        every = list(stage_entries.get("declared") or []) + list(stage_entries.get("derived") or [])
        return (tuple(sorted({e["rule"] for e in every if "rule" in e})),
                tuple(sorted({(e["path"], e["mode"]) for e in every if "rule" not in e})))

    payload = repr(tuple(
        (int(idx), *projection(s))
        for idx, s in sorted(entries.items(), key=lambda kv: int(kv[0]))
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refined_grant_entries(
    stored: dict[str, dict], old: PlanDoc | None, new: PlanDoc,
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """The entries to bind after a refinement replan applies `new` over `old` (the
    snapshot it replaces) to a session whose approved entries are `stored`, plus
    `{stage index: [rule/path withheld]}`.

    Entries are re-derived against today's venue filesystem, which may have moved since
    approval (#338). Both `old` and `new` are derived against `new`'s venue -- the
    `_grants_grew` / `diff_plans` convention -- so the comparison isolates what the plan
    changed from what the filesystem did, and a relocation of `repo_root` /
    `delivery_worktree` on its own moves no stage's inputs: it never admits a rule only
    the new venue's filesystem proposes. A stage's re-derived set is split by cause:

      * the stage's grant inputs did not move (`old` derives the same set as `new` for
        it, both read now): the stored entries stay exactly as approved;
      * they moved: what `new` derives and `old` did not is the plan's own change -- the
        diff layer already classified it (growth is substantive, a relaxed verify
        identity is admitted) -- and what `new` derives that `old` also derives but the
        approval never stored is venue drift. The result keeps the stored entries `new`
        still derives plus the plan's own additions; drift is withheld and reported.
        A stored entry `new` no longer derives is dropped: a stage that moved AND whose
        venue relocated loses its venue-path entries until it is re-approved (under-,
        never over-granting).

    `old` unreadable: nothing separates the plan's change from drift, so nothing new is
    admitted -- stored entries `new` still derives, and declared grants from `new`."""
    new_venue = _venue_for(new)
    now = materialized_grant_entries(new, venue=new_venue)
    before = materialized_grant_entries(old, venue=new_venue) if old is not None else {}
    out: dict[str, dict] = {}
    withheld: dict[str, list[str]] = {}
    for idx, n in now.items():
        s = stored.get(idx)
        o = before.get(idx)
        if s is None:
            out[idx] = n
            continue
        if o is not None and _effective_entry_keys(o) == _effective_entry_keys(n):
            out[idx] = s
            continue
        admitted = {_entry_key(e) for e in s.get("derived") or []}
        if o is not None:
            admitted |= {_entry_key(e) for e in n["derived"]} - {_entry_key(e) for e in o["derived"]}
        out[idx] = {
            "declared": n["declared"],
            "derived": [e for e in n["derived"] if _entry_key(e) in admitted],
            "dropped": n["dropped"],
        }
        held = [e.get("rule") or f"{e['path']} ({e['mode']})" for e in n["derived"]
                if _entry_key(e) not in admitted]
        if held:
            withheld[idx] = held
    return out, withheld


def diff_plans(old: PlanDoc, new: PlanDoc, *, relax_verify_identity: bool = False) -> str:
    """Return 'no_change' | 'refinement' | 'substantive'. `relax_verify_identity`: see
    `_grants_grew`."""
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
             # Same footing as `cost_tier`: engine-consumed at dispatch, outside the
             # structural signature, so an exemption-only edit must not diff as 'no_change'.
             *((("guard_exempt_paths", tuple(s.actor.guard_exempt_paths)),)
               if s.actor.guard_exempt_paths else ()),
             # Same footing as `cost_tier` above — engine-consumed (the ephemeral-
             # artifacts submission check reads it) but outside `_structural_signature`/
             # `stage_carry_key`/`stage_question_key` (no Question.target names it), so
             # without this a waiver-only edit would diff as 'no_change' and be silently
             # dropped rather than carried by `_apply_refined_stage_fields`.
             *((("ephemeral_artifacts_waiver", s.ephemeral_artifacts_waiver),)
               if s.ephemeral_artifacts_waiver else ()),
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
             *procedure_place(s),
             # Adding a negative control (or waiving one) changes what the stage's
             # positive check is trusted to certify — without this place, that edit
             # diffs as 'no_change'.
             *negative_control_place(s))
            for s in doc.stages
        ]
    _fc = _final_check_place
    # `order_place` is the meta-level sibling of the `knowledge_place`/`preconditions_place`
    # splices above, and it is here for the identical reason: without it a re-worded
    # requirement, a corrected functional place or a coverage entry pointed at a different
    # control would diff as 'no_change' and be silently dropped. Its scope half is already
    # in `_structural_signature`, so what reaches this line is only the wording.
    # `_structural_signature` already catches a changed DECLARED grant (via
    # `grants_place`), but a plan that adds no new [stage.grants] line can still
    # widen what a stage will actually be spawned with — an output_artifacts edit
    # that derives a new Edit rule, or a `verify_command` edit that derives a
    # wider DR-V, say — without moving a single field `_structural_signature`
    # OR the prose/`_fc`/`order_place` keys below compare, or while only moving
    # a field the prose keys below DO compare. Checked HERE, before the prose
    # comparison, and unconditionally: grant edits and effective-set growth are
    # substantive. A `verify_command` edit is judged by what it lets the stage RUN,
    # not by its text: DR-V derives one literal per segment, so a rewritten command
    # always adds literals, and with `relax_verify_identity` `_grants_grew` counts one as
    # growth only when its program (for an interpreter: its script) is new to the stage --
    # and only when the stage's norm delta is confined to its criterion. Arguments to an
    # already-granted program are then a refinement; the boundary check
    # (`_kind_within_boundary`), which runs exactly when the caller sets the flag, still
    # re-approves an unresolved or out-of-set command. "Did the EFFECTIVE grant
    # set grow" is not folded into `_structural_signature` itself because it is not a
    # pure function of the two docs' own bytes alone (it also calls the same deriver
    # dispatch will).
    if _grants_grew(old, new, relax_verify_identity=relax_verify_identity):
        return "substantive"
    if (_prose(old) != _prose(new) or old.meta.goal != new.meta.goal
            or old.meta.repo_root != new.meta.repo_root
            or _fc(old) != _fc(new)
            or order_place(old.meta) != order_place(new.meta)):
        return "refinement"
    return "no_change"
