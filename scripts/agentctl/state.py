"""Durable typed session state for the coordination engine.

SessionState is the machine-written record persisted as JSON (the author-written
artifact is the TOML plan; see plan.py). Invariants from the plan are enforced in
code so an illegal state cannot be constructed or loaded:

  - node == EXECUTING            => approval.passed
  - node == RESOLVED             => resolution.passed and every stage PASSED
  - route == SPAWN               => weight_class == SUBSTANTIVE
  - weight_class == CHAT         => node terminal at ROUTED (never advances)

The (de)serialization is intentionally plain (asdict / field-wise rebuild) so that
from_json(to_json(s)) == s holds and the JSON stays a faithful mirror of the
dataclass — the seam store.py persists.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass, field, fields
from enum import Enum

SCHEMA_VERSION = 24

# Mirrors max-recursion-depth in ~/.claude/config.md — the nesting cap that
# prevents unbounded service-sub-plan recursion.
_MAX_PLAN_STACK = 5


class Node(str, Enum):
    CLASSIFIED = "CLASSIFIED"
    ROUTED = "ROUTED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    APPROVED = "APPROVED"
    PARTITIONED = "PARTITIONED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLUTION = "RESOLUTION"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"
    DIAGNOSING = "DIAGNOSING"  # difficulty cycle active: declare -> investigate -> critique -> replan


# Nodes at or past EXECUTING on the spawn path — once here, a SPAWN route must
# have recorded its partition assessment.
_EXECUTION_NODES = frozenset(
    {
        Node.EXECUTING.value,
        Node.VERIFYING.value,
        Node.RESOLUTION.value,
        Node.RESOLVED.value,
    }
)


class WeightClass(str, Enum):
    CHAT = "CHAT"
    SMALL_CHANGE = "SMALL_CHANGE"
    SUBSTANTIVE = "SUBSTANTIVE"


class Route(str, Enum):
    DIRECT = "DIRECT"        # chat: answer in-thread, terminal at ROUTED
    IN_THREAD = "IN_THREAD"  # small change: execute in-thread, no plan gate
    SPAWN = "SPAWN"          # substantive: planner/developer specialists


class CriterionType(str, Enum):
    MEASURABLE = "measurable"
    ACCEPTANCE_REVIEW = "acceptance_review"


# The declared check venue for a stage's verify_command or a [[final_check]]
# (schema 22). "repo_root" always means the canonical checkout; "delivery"
# (the default) means the plan's delivery venue — SessionState.resolve_check_venue
# is the one place that resolves either value to a concrete cwd, shared by
# cmd_dispatch and all three verify sites so they observe the same tree.
class CheckVenue(str, Enum):
    DELIVERY = "delivery"
    REPO_ROOT = "repo_root"


# The check-kind discriminator for a stage Criterion or a FinalCheck (schema 23).
# SHELL (default) is today's free-text verify_command/command executed literally.
# LANDED synthesizes the ONE durable check the engine trusts for a point-in-time
# "did this commit reach trunk" assertion — monotone `git merge-base --is-ancestor`
# containment against a commit FROZEN at record-result time — from a declared
# LandedSpec, so SHA equality, a literal commit range, or a live-resolved head
# (the shapes behind 20 recorded false-fail incidents, experience leaf
# 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md) become
# unrepresentable rather than merely discouraged. Deliberately no enum for "source
# of the delivered commit" — Outcome.delivered_head is the only one that may ever
# exist.
class CheckKind(str, Enum):
    SHELL = "shell"
    LANDED = "landed"


# The exit code the landed-check synthesizer reserves for "git could not answer
# the question" (an unresolvable target/remote ref, a broken repository) —
# distinct from exit 1 (git's genuine "not an ancestor") so every verify site can
# map ONLY this code to a legible refusal instead of a false-failed stage. Chosen
# to collide with nothing else on the same exit-code axis: `git merge-base
# --is-ancestor` itself uses 0/1; git ref-resolution/usage errors use 128/129;
# the shell uses 126/127/128+N; pytest uses 0-5.
LANDED_GIT_ERROR_EXIT = 97


@dataclass
class LandedSpec:
    """The declarative payload of a `kind = "landed"` check (schema 23): assert
    that the commit stage `delivered_stage` delivered is contained in `target`
    (and its `remote` remote-tracking ref) — never SHA equality, never a
    live-resolved head. See plan.py's `_parse_landed_spec` for the validation
    rules this must satisfy before construction."""
    target: str
    delivered_stage: int
    remote: str = "origin"

    @classmethod
    def from_dict(cls, d: dict) -> "LandedSpec":
        """Rebuild a LandedSpec from its JSON dict, ignoring unknown keys —
        mirrors Principle.from_dict's tolerant-reconstruction template."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class StageStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PASSED = "PASSED"
    FAILED = "FAILED"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# The legal values of a Critique.failure_address (R2 — the fault-address of преодоление
# затруднения). A затруднение is overcome by fixing its ОБЕСПЕЧЕНИЕ, and the address is one
# of two special cases of that ONE act: inadequate РЕСУРСНОЕ обеспечение ('ресурсное' —
# материал/средство: the model of the material, or the tool, was wrong) or inadequate
# НОРМАТИВНОЕ обеспечение ('нормативное' — норма/способ: the goal, or the method to reach it,
# was wrong). These are NOT two ontologies — «норма — тоже ресурс» (нормативное обеспечение
# ⊂ обеспечение деятельности) — and both reduce reflexively to знание. This is deliberately
# NOT an is/ought (сущее/должное) tag: the value set is its OWN, decoupled from the retired
# StatementKind enum a-priori-typing dropped in ADR-0004 (§R2 records the reframe). The
# сущее/должное character of a fault is a POST-HOC product of критика, not carried here.
# `not_applicable` is the one legal sentinel for an EXPLICIT opt-out (the critique states the
# routing does not apply), kept distinct from a bare None omission so the gate can
# discriminate the two.
#
# «Both reduce reflexively to знание» is a statement about the ADDRESS, not about the
# REPAIR. Знание is a functional place of its own — upstream of both обеспечения, since it
# is what a норма is selected against and what a ресурс is judged adequate by — so the act
# that closes the difficulty may land on знание directly rather than on the ресурс or the
# норма that merely carried the fault. That landing place is recorded on its own axis,
# NORMALIZATION_DESTINATIONS, and is orthogonal to this two-valued address: the address
# says which обеспечение failed, the destination says which functional place the re-norming
# repairs. A single value cannot carry both without collapsing the distinction.
FAILURE_ADDRESS_VALUES = ("ресурсное", "нормативное", "not_applicable")


class InvariantError(Exception):
    """A SessionState violates a documented coordination invariant."""


@dataclass
class GateRecord:
    name: str
    armed: bool = False
    passed: bool = False
    by: str | None = None
    note: str | None = None

    def blocks(self) -> bool:
        return self.armed and not self.passed


# Execution modes a delivery unit may take (org-neutral — NO tracker vocabulary):
#   inline  — delivered in the root session
#   spawn   — a specialist process WITHIN the root task (same tracking unit)
#   subtask — a SEPARATE task/session with an INHERITED plan slice (own tracking);
#             its per-environment materialization (tracker subticket, child session,
#             …) is an observer's job, not the core's.
PARTITION_UNIT_MODES = ("inline", "spawn", "subtask")


@dataclass
class PartitionUnit:
    """One delivery unit: a GROUP OF STAGES of the already-approved plan, routed to
    an execution context + tracking mode. The planner defines the stages (work
    structure); partition only GROUPS approved stages and ROUTES each group, so the
    boundary with decomposition stays sharp.

    `stages` are the approved-plan stage indices this unit delivers; `mode` is one of
    PARTITION_UNIT_MODES; `ref` is the org-neutral reference the environment's
    materialization assigns (tracker key, issue URL, child session id) — None until
    materialized. No tracker vocabulary lives here: 'subtask' is the generic
    separate-task mode, materialized per environment by a plugin observer."""
    title: str
    stages: list[int] = field(default_factory=list)
    mode: str = "inline"
    ref: str | None = None


@dataclass
class Partition:
    """The M1–M4 partition assessment recorded between APPROVED and EXECUTING
    on the spawn path. The markers are cognitive inputs; the verdict is computed
    by partition.verdict()."""
    m1: bool = False
    m2: bool = False
    m3: bool = False
    m4: bool = False
    m3_severe: bool = False
    m4_severe: bool = False
    verdict: str = ""
    # Per-unit delivery routing (optional): each unit groups a set of approved-plan
    # stage indices and records HOW that group executes (inline | spawn | subtask).
    # Empty by default — sessions that never record units serialize and render
    # byte-identically to before. Recorded via `partition` / `partition-units`;
    # validated against the loaded plan by the CLI (existing indices, disjoint sets).
    units: list["PartitionUnit"] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Partition":
        """Rebuild a Partition from its JSON dict, reconstructing nested `units` as
        typed PartitionUnit objects rather than raw dicts. An absent 'units' key
        (legacy/pre-units state) yields [] via the dataclass default, so old
        state.json loads byte-compatibly."""
        d = dict(d)
        units = [PartitionUnit(**u) for u in d.pop("units", [])]
        return cls(**d, units=units)


@dataclass
class PermissionRequest:
    """A specialist's pending PERMISSION-REQUEST, parked while the manager asks the
    user. Transient (not a gate): the engine records the requested action, then
    clears it on resolve-permission."""
    action: str
    stage_index: int
    raw: str = ""


# --- the difficulty record (overcome-difficulty sub-spine) -------------------
# The deterministic SHELL of the overcome-difficulty cycle: the engine enforces
# the ordering (declaration -> investigation -> critique) and that each phase
# produced its artifact, while the COGNITION (what the divergence is, the >=2
# hypotheses, the functional-ground critique) lives in the overcome-difficulty
# skill. Each section is filled by its command (declare / investigate / critique)
# and the record gates `replan`: a plan may not be re-normed until the cycle is
# complete. Sections are artifact-EXISTENCE checks, not artifact-correctness.
@dataclass
class Declaration:
    """Phase 1: name the divergence — expected vs actual and the mismatch."""
    expected: str
    actual: str
    mismatch: str


@dataclass
class Investigation:
    """Phase 2: localize the divergence to the smallest expectation/actual pair,
    carrying a portfolio of >=2 candidate hypotheses (the overcome-difficulty skill
    requires more than one so the diagnosis is not a single-track guess)."""
    localized_expectation: str
    localized_actual: str
    hypotheses: list[str] = field(default_factory=list)


@dataclass
class Critique:
    """Phase 3: the functional ground and the replanning task it induces.

    The similarities/differences split the overcome-difficulty skill produces is
    recorded structurally so the engine can verify replan COVERAGE (gates.
    replan_coverage_blockers): similarities -> conditions/invariants that must be
    PRESERVED, differences -> means/method that must CHANGE. Both default to []
    so a critique that omits them (and any pre-change persisted state) loads and
    replans exactly as before.

    `failure_address` (schema 17, R2; values reframed schema 19 per ADR-0004 §R2) types
    the fault-address of преодоление затруднения: the затруднение is overcome by fixing its
    обеспечение, either inadequate РЕСУРСНОЕ обеспечение ('ресурсное' — материал/средство) or
    inadequate НОРМАТИВНОЕ обеспечение ('нормативное' — норма/способ), or explicitly
    not_applicable. Two special cases of ONE act («норма — тоже ресурс»), both reducing
    reflexively to знание — NOT an is/ought tag. A legal value (for a NEW write) is one of
    FAILURE_ADDRESS_VALUES; None is the untouched-legacy default (a critique recorded before
    schema 17, or one not yet routed). A legacy record carrying an OLD сущее/должное value
    (the rejected v3 R2 typing) also loads unchanged — the field stores any string, and the
    gate checks only non-None, so it is grandfathered, never re-blocked. gates.
    failure_address_blockers blocks difficulty closure on a bare None (omission) — an explicit
    not_applicable is a legal opt-out, not an omission — so the routing is DECIDED, never
    silently skipped, mirroring normalization_blockers over the reproducible-factor act."""
    functional_ground: str
    replanning_task: str
    invariants_to_preserve: list[str] = field(default_factory=list)
    differences_to_remove: list[str] = field(default_factory=list)
    failure_address: str | None = None


# The levels of the renorming act, ordered by payoff. A reproducible factor MUST be
# re-normed (the ACT is mandatory); WHICH level — an in-head note, a memory leaf, or a
# generalized principle — is payoff-gated by rediscovery-threshold-min and stays the
# coordinator's cognition, so `level` may be None (a note below the leaf threshold).
NORMALIZATION_LEVELS = ("note", "leaf", "principle")

# The functional PLACE a renorming act lands on — a CLOSED vocabulary and its own axis,
# orthogonal to NORMALIZATION_LEVELS. The two answer different questions and must share no
# member: `level` asks how generally the record is written down (a payoff question about the
# artifact), `destination` asks which place in the activity the act actually repairs (a
# structural question about the деятельность). Conflating them is what made "principle"
# read as both the most general recording level AND the thing being changed, which quietly
# denied that знание is a place a renorming can land on at all.
#
# The members are the functional places обеспечение деятельности decomposes into (see the
# FAILURE_ADDRESS_VALUES comment above for the same decomposition on the fault-address
# axis): ресурсное обеспечение splits into материал and средство, нормативное обеспечение
# into норма and способ, and знание stands upstream of both as the place a норма is
# selected against and a ресурс judged adequate by. Closed because an open destination is
# not a place — it is free text, and the axis then records nothing checkable.
NORMALIZATION_DESTINATIONS = ("материал", "средство", "норма", "способ", "знание")


@dataclass
class Normalization:
    """Phase 4 (closure): the renorming act (перенормирование). A difficulty is a
    norm-failure (провал нормы = SIGNAL); because activity is constituted by
    reproduction, a REPRODUCIBLE factor left un-normed simply re-fails — so closing a
    difficulty REQUIRES re-norming that factor (the ACT is mandatory-if-reproducible).
    `factor` names the reproducible cause; `level` (note/leaf/principle) is the payoff-
    gated recording level and may be None; `destination` (a NORMALIZATION_DESTINATIONS
    member) names the functional place the act lands on and is INDEPENDENT of `level` —
    any destination is recordable at any level. Both default to None so a pre-field
    record loads unchanged. Recorded by cmd_normalize; gates cmd_replan (see
    gates.normalization_blockers). A one-off (non-reproducible) factor takes the
    explicit --normalization-waiver escape instead of a record."""
    factor: str
    level: str | None = None
    destination: str | None = None


@dataclass
class Difficulty:
    """One active difficulty: a plan-vs-reality divergence being worked through.
    `complete()` is the precondition `replan` checks (see gates.difficulty_blockers)."""
    declaration: Declaration | None = None
    investigation: Investigation | None = None
    critique: Critique | None = None
    normalization: Normalization | None = None

    def complete(self) -> bool:
        return (
            self.declaration is not None
            and self.investigation is not None
            and self.critique is not None
        )

    @classmethod
    def from_dict(cls, d: dict | None) -> "Difficulty | None":
        if not d:
            return None
        decl = d.get("declaration")
        inv = d.get("investigation")
        crit = d.get("critique")
        # normalization defaults to None so any pre-SCHEMA_VERSION-16 persisted state
        # (which never carried the key) loads unchanged — the grandfather migration.
        norm = d.get("normalization")
        return cls(
            declaration=Declaration(**decl) if decl else None,
            investigation=Investigation(**inv) if inv else None,
            critique=Critique(**crit) if crit else None,
            normalization=Normalization(**norm) if norm else None,
        )


# --- the plan-review record (thinker-review gate) ----------------------------
# The deterministic SHELL of the thinker-review gate: the engine records that a
# thinker reasoning pass reviewed a specific plan VERSION and returned a verdict,
# and binds that verdict to the exact plan_path so a stale review cannot approve a
# later plan. The COGNITION (the thinker's actual reasoning, whether the plan is
# sound) lives in the thinker leaf; the engine only checks the record exists, is
# bound to the target plan, and carries a passing (or user-overridden) verdict.
# Recorded by cmd_plan_review; gates cmd_approve and every cmd_replan (see
# gates.plan_review_blockers). An artifact-EXISTENCE + binding check, never a
# judgement of the review's quality.
@dataclass
class PlanReview:
    """One thinker review of a plan version. `plan_path` binds the verdict to the
    exact plan it examined — a review of an earlier plan does not clear the gate for
    a later one. `verdict` is one of pass / revise / override (an override is the
    user's explicit deadlock escape, which requires a non-empty `reviewer` and
    `note`). `concerns` carries the thinker's blocking points for the audit trail.

    `plan_sha256` (schema 13, #16) is the sha256 of the reviewed plan file's bytes:
    plan_path binds the verdict to a NAME, but the coordinator edits plans in place,
    so a same-path rewrite would inherit a PASS granted to different bytes. The gate
    recomputes the hash and rejects a content drift. Empty on legacy records (absent
    key -> default), which degrades the gate to the prior path-only binding."""
    plan_path: str
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    plan_sha256: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "PlanReview | None":
        if not d:
            return None
        return cls(
            plan_path=d["plan_path"],
            verdict=d["verdict"],
            reviewer=d.get("reviewer", ""),
            concerns=list(d.get("concerns", [])),
            note=d.get("note", ""),
            plan_sha256=d.get("plan_sha256", ""),
        )


# The acceptance-review analogue of PlanReview (schema 14): one recorded verdict on
# an acceptance_review stage's observation, backing the acceptance-review gate
# (gates.acceptance_review_blockers). Structurally mirrors PlanReview — the COGNITION
# (the cheap external judge, or a human's override) happens in the cli layer; this
# only records the verdict, bound to the exact observation bytes it examined.
# `observation_sha256` is the sha256 of those bytes: the gate recomputes it over the
# observation being recorded and rejects a drift, so a PASS granted to one observation
# cannot silently clear a different one.
@dataclass
class StageReview:
    """One judge/human review of an acceptance_review stage's observation.

    `stage_index` binds the verdict to its stage. `verdict` is one of
    pass / revise / override (an override is the user's explicit deadlock escape,
    which requires a non-empty `reviewer` and `note`). `observation_sha256` binds the
    verdict to the exact observation bytes that were judged; empty on a record that
    declined to bind (degrades the gate to verdict-only, mirroring PlanReview's
    path-only fallback). `reviewer` is the judge tag ("judge:haiku") for an
    automated verdict or a human name for a manual/override record."""
    stage_index: int
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    observation_sha256: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "StageReview | None":
        if not d:
            return None
        return cls(
            stage_index=int(d["stage_index"]),
            verdict=d["verdict"],
            reviewer=d.get("reviewer", ""),
            concerns=list(d.get("concerns", [])),
            note=d.get("note", ""),
            observation_sha256=d.get("observation_sha256", ""),
        )


# The code-review analogue of StageReview (schema 21): one recorded verdict on a
# spawn:developer stage's produced code, backing the code-review gate
# (gates.code_review_blockers). Structurally the same charter — the COGNITION (the
# code-reviewer specialization, or a human's override) happens outside this module;
# this only records the verdict. Unlike StageReview, the reviewed-code digest is
# CALLER-SUPPLIED at record time (a `--code-ref` value derived from a git revision
# or diff), never recomputed here — gates.py stays pure (no subprocess/git reach).
@dataclass
class CodeReview:
    """One code-reviewer/human review of a spawn:developer stage's diff.

    `stage_index` binds the verdict to its stage. `verdict` is one of
    pass / revise / override (an override is the user's explicit deadlock escape,
    which requires a non-empty `reviewer` and `note`). `code_sha256` binds the
    verdict to the exact reviewed-code digest the caller supplied when recording
    it; empty on a record that declined to bind (degrades the gate to
    verdict-only, mirroring StageReview's observation-only fallback). `reviewer`
    is the reviewer tag ("code-reviewer") for an automated verdict or a human
    name for a manual/override record."""
    stage_index: int
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    code_sha256: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "CodeReview | None":
        if not d:
            return None
        return cls(
            stage_index=int(d["stage_index"]),
            verdict=d["verdict"],
            reviewer=d.get("reviewer", ""),
            concerns=list(d.get("concerns", [])),
            note=d.get("note", ""),
            code_sha256=d.get("code_sha256", ""),
        )


# The plan-presentation receipt (schema 20): proof that a specific plan version's
# rendering was (attempted to be) shown to the user. Structurally the third
# instance of the PlanReview/StageReview charter — an artifact-EXISTENCE +
# binding check, never a judgement of whether the rendering was faithful (that
# perception stays with the coordinator/tech-writer, never the engine). Recorded
# by cmd_present_plan; gates cmd_approve via gates.plan_presentation_blockers.
# Bound three ways, one per field beyond plan_path/kind:
#   - plan_sha256      : WHICH PLAN VERSION was presented — recomputed at gate
#                         time so a later edit doesn't inherit an earlier receipt
#                         (mirrors PlanReview.plan_sha256, #16).
#   - rendering_sha256  : the EXACT presented bytes — the delivery hook verifies
#                         the turn's actual transcript output hashes to this
#                         before stamping delivery (agentctl/delivery.py), so
#                         this receipt is proof of INTENT and the delivery stamp
#                         is proof of DELIVERY; the two are deliberately
#                         separate records with separate storage (see
#                         delivery.py's module docstring for why).
#   - presented_ts      : WHEN the receipt was stamped, same epoch-float
#                         convention as plan_submitted_ts.
# New at schema 20 — no earlier session ever recorded one — so presented_ts (and
# every other field) carries NO default: a legacy dict can never satisfy
# from_dict. Grandfathering therefore happens at the LIST level only
# (SessionState.plan_presentations: absent key -> empty list via from_dict's
# data.get(..., [])), never at the individual-record level.
PLAN_PRESENTATION_RENDERING_CAP_BYTES = 64 * 1024

PLAN_PRESENTATION_KIND_ESSENCE = "essence"
PLAN_PRESENTATION_KIND_FULL = "full"
PLAN_PRESENTATION_KINDS = (PLAN_PRESENTATION_KIND_ESSENCE, PLAN_PRESENTATION_KIND_FULL)

# Language-independent ASCII marker a plan-approval AskUserQuestion option must
# embed (label or description) to show the full plan. Checked by
# hook-plan-delivery-gate.py's _has_show_full_plan_option, emitted by
# cli.cmd_present_plan's essence Directive — single-sourced here so the two can
# never drift apart.
SHOW_FULL_PLAN_MARKER = "[show-full-plan]"


@dataclass
class PlanPresentation:
    plan_path: str
    kind: str  # PLAN_PRESENTATION_KIND_ESSENCE | PLAN_PRESENTATION_KIND_FULL
    plan_sha256: str
    rendering_sha256: str
    rendering_text: str
    presented_ts: float

    @classmethod
    def from_dict(cls, d: dict) -> "PlanPresentation":
        return cls(
            plan_path=d["plan_path"],
            kind=d["kind"],
            plan_sha256=d["plan_sha256"],
            rendering_sha256=d["rendering_sha256"],
            rendering_text=d["rendering_text"],
            presented_ts=d["presented_ts"],
        )


# Bypass-visibility record (schema 14): every time an acceptance_review stage is
# recorded PASSED WITHOUT a genuine passing judge verdict — because the kill switch
# disabled the gate, or a human override cleared it — one JudgeBypass is appended and
# NEVER cleared by a later passing review. verify-final refuses a clean bill while any
# entry exists unless it prints them; the resolution summary surfaces them verbatim.
@dataclass
class JudgeBypass:
    stage_index: int
    kind: str  # "killswitch" | "override"
    reviewer: str = ""
    note: str = ""


# --- the 8 activity elements, grouped by the ontology's clusters -------------
# Each cluster is a typed sub-structure of Stage; the grouping makes the model
# self-documenting and splits the immutable DECLARATION (subject/means/actor/
# criterion/principle/conditions) from the mutable execution RECORD (outcome).
@dataclass
class Subject:
    """The material worked on and the result image it should become.

    `material_refs` and `knowledge_refs` are the two structural projections of the
    material prose, and their contract is a DIVISION, not a check: `material_refs` names
    what the stage TRANSFORMS, `knowledge_refs` names what it RELIES ON and leaves alone.
    A symbol appearing in both is a smell — the stage is rewriting the very thing it
    reasons from — and the plan must justify it in prose; it is deliberately NOT a
    submission refusal (see submission.py's module docstring for the three reasons)."""
    material: str
    result: str
    invariants: str | None = None
    material_refs: list[str] = field(default_factory=list)
    knowledge_refs: list[str] = field(default_factory=list)


@dataclass
class Means:
    """The fixed instruments (means) and the procedure over them (method)."""
    means: str
    method: str


@dataclass
class Actor:
    """Who executes the stage and what capability that demands."""
    executor: str  # "in_thread" | "spawn:<spec>"
    capability_required: str | None = None


@dataclass
class Criterion:
    """How the result is judged: criterion type + the concrete done criterion.

    For a measurable criterion the done_criterion MAY be made executable: when
    `verify_command` is set, the engine runs it and accepts the stage as passed
    only if the process exit code equals `expected_exit` (default 0). This moves
    "verify the right axis, report honestly" from discipline into an invariant —
    the model is removed from the trust path for the measurable subset. Absent a
    command (the default) the engine keeps its flag-only behaviour.

    For an acceptance_review criterion, `observation` records WHAT the reviewer
    actually saw — distinct from `result` (the expected image). The engine requires
    a non-empty, non-echoed observation when recording a passed acceptance stage so
    the "actual" side is never just a restatement of the target."""
    criterion_type: str  # CriterionType value
    done_criterion: str
    verify_command: str | None = None
    expected_exit: int = 0
    observation: str = ""
    # The declared check venue (CheckVenue value, schema 22): which tree
    # verify_command runs in, resolved via SessionState.resolve_check_venue.
    # Defaults to "delivery" so an un-annotated stage keeps observing the
    # same tree dispatch wrote to (the venue-symmetry fix), not repo_root.
    verify_venue: str = "delivery"
    # The check-kind discriminator (CheckKind value, schema 23): "shell"
    # (default) executes verify_command literally; "landed" ignores
    # verify_command (plan.py's R1 forbids one being declared) and instead
    # runs the engine-synthesized containment check described by `landed`.
    verify_kind: str = "shell"
    landed: "LandedSpec | None" = None
    # The optional SECOND venue (CheckVenue value, schema 24), read ONLY by
    # SessionState.resolve_final_check_venue at verify-final; cmd_dispatch and
    # cmd_record_result always resolve `verify_venue` instead, because during
    # execution the delivery venue is the only tree where an un-landed change
    # exists. None (the default) means "same as verify_venue" (V4) — the
    # back-compat identity that keeps every plan authored before this field
    # existed byte-identical. Set it (typically to "repo_root") for a check
    # whose delivery venue disappears once the change lands — see plan.py's
    # V1-V4 validation rules and README.md for the two-moment rationale.
    verify_venue_at_final: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Criterion":
        """Rebuild a Criterion from its JSON dict, reconstructing a nested
        `landed` table as a typed LandedSpec rather than a raw dict (mirrors
        Partition.from_dict's `units` handling) so kind="landed" code can read
        `criterion.landed.target` by attribute, not by key."""
        d = dict(d)
        landed = d.pop("landed", None)
        return cls(landed=LandedSpec.from_dict(landed) if landed else None, **d)


@dataclass
class Principle:
    """The refutable principle the stage rests on (confidence is a Confidence value).

    Element 7 is used AS a норма (должное) — the stage rests on it, and what a stage rests
    on is not checked for truth in the course of resting on it. That is a statement about
    the FUNCTIONAL PLACE the principle occupies here, not about what the principle IS: the
    same statement can be знание elsewhere (it has a source, a derivation, and a refutation
    condition — the three marks of a claim about how things are), and `refutation` only
    means anything because it can be. What ADR-0004 dropped is the a-priori `statement_kind`
    TAG, a per-statement typing that tried to settle знание-vs-норма once and for all on the
    statement itself; the сущее-vs-должное character of a fault stays a POST-HOC product of
    критика at difficulty closure, living in the two refutation MODES and R2's routing.
    Nothing here denies the principle a знание reading — the stage's own знание place is
    `Stage.knowledge`, and a principle may well be where that knowledge came from."""
    statement: str
    source: str
    # `derivation` sits adjacent to `source` because the pair is one checkable unit:
    # source answers "does the cited ground exist", derivation answers "does the claim
    # actually follow from it" — the second half of a twice-checkable premise. The field
    # defaults to "" only to satisfy dataclass ordering (a defaulted field may not precede
    # a non-defaulted one, so confidence/refutation default too); real requiredness for a
    # substantive stage is enforced dict-level in plan._validate_substantive_stage, so the
    # default never weakens that gate.
    derivation: str = ""
    confidence: str = ""
    refutation: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Principle":
        """Rebuild a Principle from its JSON dict, ignoring unknown keys so a legacy
        plan/session carrying the retired `statement_kind` field still loads (grandfather
        — the a-priori principle-typing was dropped in ADR-0004; the key is tolerated and
        ignored, never re-required). Load-time tolerance IS the migration: no data rewrite."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Supply:
    """A typed provision edge: stage `on` supplies `element` (optionally a named
    `artifact`) to the stage that owns this Supply. The SOLE source of stage
    edges — Stage.depends_on is a derived projection over these."""
    on: int
    element: str | None = None
    artifact: str | None = None


@dataclass
class FinalCheck:
    """A typed end-to-end check the engine runs at verify-final.

    Runs via `bash -c` in the venue named by `venue` (a CheckVenue value,
    resolved via SessionState.resolve_check_venue), default "delivery".
    `label` is a human-readable name for failure messages; when empty the
    command string is used instead."""
    command: str
    expected_exit: int = 0
    label: str = ""
    # schema 22 — see Criterion.verify_venue for the shared default rationale.
    venue: str = "delivery"
    # schema 23 — see Criterion.verify_kind / .landed for the shared rationale.
    # A landed FinalCheck carries command = "" (required str, never None) since
    # the engine synthesizes the check rather than running an author command.
    kind: str = "shell"
    landed: "LandedSpec | None" = None

    @classmethod
    def from_dict(cls, d: dict) -> "FinalCheck":
        """Rebuild a FinalCheck from its JSON dict — see Criterion.from_dict
        for why the nested `landed` table needs explicit reconstruction."""
        d = dict(d)
        landed = d.pop("landed", None)
        return cls(landed=LandedSpec.from_dict(landed) if landed else None, **d)


@dataclass
class Requirement:
    """One requirement of the order, as an id/text PAIR rather than a sentence.

    The `id` is load-bearing, not decoration: the coverage map keys on it, the
    submission-time totality check ranges over it, and the acceptance record binds a
    verdict to it. Leaving it as the first token of a prose sentence would commit the
    very defect the typed order removes one level down — the order would be typed while
    its only machine-readable key stayed prose someone has to parse back out."""
    id: str
    text: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Requirement":
        return cls(id=str(d.get("id", "")), text=str(d.get("text", "")))


@dataclass
class Order:
    """The order a plan serves, typed: the customer it came from, the functional place
    it fills, the requirements on the product, and the map from each requirement to the
    control that decides it.

    `customer` is a PAIR. `customer_id` is the machine-comparable identifier an
    acceptance author is checked against; `customer` is the prose naming the position
    that identifier stands for. Comparing an author against a paragraph is either
    vacuous or absurd, so the two are separate fields rather than one.

    Every field defaults to its empty form and nothing here validates: this object is
    built on the LENIENT loader path, where it must be incapable of refusing a plan (see
    plan.parse_plan). Requiredness is a submission-seam grade — submission.py's
    `_order_violations` — for the same reason every other new requirement binds there.

    NOT persisted in SessionState. The engine holds no cached copy of the order, so
    there is no meta-level analogue of `_apply_refined_stage_fields` to keep in step; a
    consumer that needs the order reads it from the plan."""
    customer_id: str = ""
    customer: str = ""
    functional_place: str = ""
    requirements: list[Requirement] = field(default_factory=list)
    # requirement id -> the controls that decide it. Values are lists of prose
    # references (a stage's verify_command, a final_check) — a machine reads the KEYS
    # for totality; whether an entry's named control really decides the requirement is
    # review, not something this type can settle.
    coverage: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        """Rebuild an Order from a raw TOML/JSON table, TOTALLY: every malformation
        degrades to the empty form rather than raising. That totality is the property
        the loader path depends on — a plan whose [meta.order] is nonsense must still
        load, and be refused at the seam where refusals belong."""
        raw_reqs = d.get("requirements")
        reqs = [
            Requirement.from_dict(r)
            for r in (raw_reqs if isinstance(raw_reqs, list) else [])
            if isinstance(r, dict)
        ]
        raw_cov = d.get("coverage")
        cov = {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
            for k, v in (raw_cov if isinstance(raw_cov, dict) else {}).items()
        }
        return cls(
            customer_id=str(d.get("customer_id", "")),
            customer=str(d.get("customer", "")),
            functional_place=str(d.get("functional_place", "")),
            requirements=reqs,
            coverage=cov,
        )


@dataclass
class PlanFrame:
    """A snapshot of the parent execution context pushed onto plan_stack when a
    service sub-plan starts. Restored in full on pop so the parent resumes exactly
    where it left off. `originating_stage` is the parent stage whose missing element
    the sub-plan supplies; it is marked PASSED on successful pop."""
    plan_path: str | None
    node: str
    task_id: str
    goal: str
    overall_done_criterion: str
    overall_criterion_type: str
    weight_class: str | None
    route: str | None
    repo_root: str | None
    delivery_worktree: str | None
    final_check: list[FinalCheck]
    partition: "Partition | None"
    approval: GateRecord
    resolution: GateRecord
    stages: list["Stage"]
    current_stage: int | None
    originating_stage: int


@dataclass
class Outcome:
    """The mutable execution record — distinct from the immutable declaration."""
    status: str = StageStatus.PENDING.value
    actual: str | None = None
    fail_digests: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int | None = None
    spawn_count: int = 0
    # The ONE field a landed check (schema 23) may ever read as its delivered
    # commit source — frozen once at record-result time, never re-resolved, so
    # the containment predicate stays monotone on both axes (trunk AND the
    # delivered commit). None until a stage with a landed-referencing check
    # records a result; absent on every pre-schema-23 state (default via
    # from_dict), so legacy states load unchanged.
    delivered_head: str | None = None


@dataclass
class CostRollup:
    """Aggregated execution cost for the whole plan, surfaced at verify-final/resolve."""
    total_cost_usd: float | None = None
    total_duration_ms: int | None = None
    spawn_count: int = 0
    attributed_stages: int = 0
    note: str = ""


@dataclass
class Stage:
    index: int
    title: str
    subject: Subject
    means: Means
    actor: Actor
    criterion: Criterion
    principle: Principle | None = None
    conditions: str | None = None
    # What must already be true before this stage may START — as opposed to `conditions`,
    # which is what must hold OF THE WORLD for the stage's own transformation to go
    # through. Two different questions about two different moments: the first is inherited
    # from outside the stage (an earlier stage finished, an access granted, a tree clean),
    # the second is a property of the situation the transformation runs in. With only one
    # field for both, `conditions` silently degenerates into "the stage before me is done"
    # — which `depends_on` already records structurally — and the plan then declares no
    # transformation conditions at all while appearing to. Optional at load so every
    # already-authored plan loads unchanged; required of a SUBSTANTIVE stage at the
    # submission seam (submission.py), which is also where the degenerate `conditions` is
    # refused — the two arrive together on purpose, so an author told to move a sentence
    # out of `conditions` always has this place to move it into.
    preconditions: str | None = None
    # The знание the stage acts FROM: what must already be known for the declared method
    # over the declared means to reach the result image. A functional place of its own,
    # upstream of both the norm (element 7, what the stage rests on) and the selection of
    # means — not a restatement of either. Optional at load so every already-authored plan
    # loads unchanged; required of a SUBSTANTIVE stage at the submission seam
    # (submission.py), where an incoming `knowledge` supply edge is an accepted alternative
    # to declaring it locally — a stage supplied its knowledge by an earlier stage has the
    # place filled, just not by itself.
    knowledge: str | None = None
    supplies: list[Supply] = field(default_factory=list)
    # Paths this stage produces (green-reachability targets for verify-command lint).
    # Optional and tolerant: a plan omitting it loads unchanged.
    output_artifacts: list[str] = field(default_factory=list)
    outcome: Outcome = field(default_factory=Outcome)
    # General control-criterion attestation (element #3 of the plan activity ontology).
    # Optional on any stage; required non-empty for spawn:developer when recording passed,
    # because review is the value the control criterion takes for the developer special case.
    control: str | None = None

    @property
    def depends_on(self) -> list[int]:
        """Derived: the set of stages this one waits on, projected from supplies."""
        return sorted({s.on for s in self.supplies})

    def is_spawn(self) -> bool:
        return self.actor.executor.startswith("spawn:")

    def spawn_kind(self) -> str | None:
        return self.actor.executor.split(":", 1)[1] if self.is_spawn() else None

    def needs_control(self) -> bool:
        """True iff a non-empty control attestation is required to record status=passed.

        Review is the control criterion of a developer-actor stage: a reviewer is a
        special case of the controller, a developer a special case of the executor.
        The precondition fires only for spawn:developer + passed; failed records and
        all non-developer stages are unaffected."""
        return self.is_spawn() and self.spawn_kind() == "developer"

    def has_control(self) -> bool:
        """True iff a non-empty control attestation has been recorded."""
        return bool(self.control and self.control.strip())

    @classmethod
    def from_dict(cls, d: dict) -> "Stage":
        """Rebuild a Stage from its JSON dict. Accepts BOTH the grouped shape
        (asdict of this class) and the legacy FLAT shape (top-level executor/
        status/depends_on/...) written by a prior schema — the migration shim
        lets current live state load unchanged."""
        d = dict(d)
        if "subject" in d or "actor" in d:  # grouped (current) shape
            return cls(
                index=int(d["index"]),
                title=str(d["title"]),
                subject=Subject(**d["subject"]),
                means=Means(**d["means"]),
                actor=Actor(**d["actor"]),
                criterion=Criterion.from_dict(d["criterion"]),
                principle=Principle.from_dict(d["principle"]) if d.get("principle") else None,
                conditions=d.get("conditions"),
                preconditions=d.get("preconditions"),
                knowledge=d.get("knowledge"),
                supplies=[Supply(**s) for s in d.get("supplies", [])],
                output_artifacts=list(d.get("output_artifacts", [])),
                outcome=Outcome(**d["outcome"]) if d.get("outcome") else Outcome(),
                control=d.get("control"),
            )
        # legacy FLAT shape -> nested groups (migration shim)
        return cls(
            index=int(d["index"]),
            title=str(d["title"]),
            subject=Subject(
                material=d.get("material", ""),
                result=d.get("expected_result_image", ""),
                invariants=d.get("invariants"),
                material_refs=list(d.get("material_refs", [])),
                knowledge_refs=list(d.get("knowledge_refs", [])),
            ),
            means=Means(means=d.get("means", ""), method=d.get("method", "")),
            actor=Actor(
                executor=d["executor"],
                capability_required=d.get("capability_required"),
            ),
            criterion=Criterion(
                criterion_type=d.get("criterion_type", CriterionType.MEASURABLE.value),
                done_criterion=d.get("done_criterion", ""),
                verify_command=d.get("verify_command"),
                expected_exit=int(d.get("expected_exit", 0)),
                observation=d.get("observation", ""),
            ),
            principle=None,  # flat states predate the principle element
            conditions=d.get("conditions"),
            preconditions=d.get("preconditions"),
            knowledge=d.get("knowledge"),
            supplies=[Supply(on=int(x)) for x in d.get("depends_on", [])],
            output_artifacts=list(d.get("output_artifacts", [])),
            outcome=Outcome(
                status=d.get("status", StageStatus.PENDING.value),
                actual=d.get("actual"),
                fail_digests=list(d.get("fail_digests", [])),
            ),
            control=d.get("control"),
        )


@dataclass
class SessionState:
    session_id: str
    task_id: str
    goal: str = ""
    overall_done_criterion: str = ""
    overall_criterion_type: str = CriterionType.MEASURABLE.value
    weight_class: str | None = None
    # Directory the engine runs each stage's verify_command in (from plan [meta].
    # repo_root). None inherits the invoker's cwd — byte-identical to the pre-field
    # behaviour, so live states predating the field load unchanged.
    repo_root: str | None = None
    # The linked worktree a worktree-delivered change is authored in (from plan
    # [meta].delivery_worktree). None (default) = no worktree-venue signal,
    # byte-identical to pre-field behaviour; live states predating the field load
    # unchanged. Backs plan.check_venue_warnings.
    delivery_worktree: str | None = None
    # Typed end-to-end checks run at verify-final after per-stage re-runs.
    # Absent in legacy states (schema_version <= 7): from_dict defaults to [].
    final_check: list[FinalCheck] = field(default_factory=list)
    route: str | None = None
    node: str = Node.CLASSIFIED.value
    blocked_from: str | None = None
    plan_path: str | None = None
    plan_verified: bool = False
    partition: "Partition | None" = None
    permission_request: "PermissionRequest | None" = None
    difficulty: "Difficulty | None" = None
    # The thinker-review record backing the plan-review gate (schema 12): the last
    # thinker review + its verdict, bound to the plan version it examined. None until
    # a review is recorded; legacy pre-schema-12 states load with None (absent key ->
    # dataclass default via from_dict), so the gate has no observable and — for a
    # substantive session — blocks approval/replan until a review is recorded.
    plan_review: "PlanReview | None" = None
    # The acceptance-review judge records backing the acceptance-review gate (schema
    # 14): one StageReview per acceptance_review stage that has been judged, and one
    # JudgeBypass per gate bypass (kill switch / override). Both default to [] — legacy
    # pre-schema-14 states load with empty lists (absent key -> dataclass default via
    # from_dict), so the gate has no observable and blocks a substantive acceptance
    # pass until a judge verdict is recorded (fail-closed).
    stage_reviews: list[StageReview] = field(default_factory=list)
    judge_bypassed: list[JudgeBypass] = field(default_factory=list)
    # Code-reviewer records backing the code-review gate (schema 21): one
    # CodeReview per spawn:developer stage that has been reviewed. Empty on
    # legacy pre-schema-21 states (absent key -> dataclass default via
    # from_dict), so the gate has no observable and blocks a substantive
    # spawn:developer stage's PASSED record until a review is recorded
    # (fail-closed).
    code_reviews: list[CodeReview] = field(default_factory=list)
    # Plan-presentation receipts backing the plan-presentation gate (schema 20):
    # one PlanPresentation per (plan_path, kind) currently in force — a fresh
    # cmd_present_plan call SUPERSEDES (never appends) the prior receipt for the
    # same key, so this list holds at most one "essence" and one "full" entry at
    # a time; the audit trail of every presentation lives in state.log's history
    # instead. Empty on legacy pre-schema-20 states (absent key -> dataclass
    # default via from_dict's cls(**data)), so the gate has no observable and —
    # for a substantive session — blocks approval until a receipt is recorded.
    plan_presentations: list[PlanPresentation] = field(default_factory=list)
    approval: GateRecord = field(default_factory=lambda: GateRecord("plan_approval"))
    resolution: GateRecord = field(default_factory=lambda: GateRecord("resolution"))
    stages: list[Stage] = field(default_factory=list)
    current_stage: int | None = None
    recursion_depth: int = 0
    artifacts: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    # Plugin layer (schema 6): non-core sub-state-machines attach here per session.
    # `plugins` is the ACTIVE set — name -> that plugin's opaque state bag; presence
    # of a key == activated. `plugins_archive` holds bags of auto-retired plugins
    # (terminal reached) for audit. Both are free-form dict-of-dict so asdict /
    # cls(**data) round-trips them untouched; the framework lives in plugins.py.
    plugins: dict[str, dict] = field(default_factory=dict)
    plugins_archive: dict[str, dict] = field(default_factory=dict)
    # Service sub-plan frame stack (schema 9): each push-subplan appends a PlanFrame
    # snapshot of the parent; pop-subplan restores it. Empty list is byte-identical
    # to pre-schema-9 behaviour — legacy states load with [].
    plan_stack: list[PlanFrame] = field(default_factory=list)
    # Aggregated execution cost, populated at verify-final/resolve. None until then.
    # Absent in pre-schema-10 states: from_dict defaults to None.
    cost: "CostRollup | None" = None
    # Turn-boundary timestamps (time.time(), schema 10) backing the plan-delivery
    # gate (hook-plan-delivery-gate.py): last_user_prompt_ts is stamped by
    # hook-engine-start.py on every UserPromptSubmit; plan_submitted_ts is stamped
    # by cmd_submit_plan. plan_submitted_ts >= last_user_prompt_ts at node PLAN_READY
    # means the plan was submitted THIS turn — the user cannot have seen it yet, so
    # a same-turn approval AskUserQuestion is denied. Both None on legacy states
    # (absent key -> dataclass default via from_dict's cls(**data)): the gate then
    # has no observable and fails open (allow).
    last_user_prompt_ts: float | None = None
    plan_submitted_ts: float | None = None
    # The immutable snapshot of the plan AS APPROVED (#8): cmd_approve copies the
    # plan file into the state dir and records its content hash here, so cmd_replan
    # diffs the corrected plan against what was APPROVED — not against plan_path,
    # which the coordinator may edit in place (an in-place edit would otherwise
    # self-diff to no_change and silently drop the correction). Both None until the
    # first approve; legacy pre-snapshot states load with None (absent key ->
    # dataclass default via from_dict's cls(**data)), so cmd_replan falls back to
    # plan_path (the prior behaviour) and old state.json loads byte-compatibly.
    plan_snapshot_path: str | None = None
    plan_snapshot_hash: str | None = None
    # The sha256 of the plan bytes this session has ACCEPTED — stamped at each of the
    # three submission seams (submit-plan, replan's new side, approve's refresh) and
    # nowhere else, so it always names bytes that passed submission validation.
    #
    # Distinct from plan_snapshot_hash, which is not a synonym: that one hashes the bytes
    # frozen AT APPROVE as the replan diff baseline, and stays pinned to that approval
    # while the coordinator edits plan_path. This one moves with every accepted entry,
    # including the two (submit-plan, replan) that never write a snapshot at all — so a
    # later reader asking "which bytes is this session actually running" reads this, and
    # one asking "which bytes was the replan measured against" reads that. None until the
    # first submission; legacy states load with None (absent key -> dataclass default via
    # from_dict's cls(**data)).
    #
    # Named `accepted_` and not `plan_digest` because `--plan-digest` / `args.plan_digest`
    # is an unrelated pre-existing thing: the digest a REVIEWER attests to at plan-review.
    # Two fields spelled the same, one session-owned and one caller-supplied, read as one.
    accepted_plan_digest: str | None = None
    # The tracker key classify detected (#11): persisted so the tracker plugin's
    # auto_activate predicate can read it without re-deriving it from task_id.
    # None on legacy states and on sessions with no tracker-key-shaped task id
    # (absent key -> dataclass default via from_dict's cls(**data)).
    tracker_key: str | None = None
    # The deliverable kind classify was told (claim-provenance ledger): persisted so
    # the ledger plugin's auto_activate predicate can read it without re-deriving it.
    # '' on legacy states and on sessions where the coordinator did not pass
    # --deliverable-kind (absent key -> dataclass default via from_dict's cls(**data)).
    deliverable_kind: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.check_invariants()

    # --- invariants -------------------------------------------------------
    def check_invariants(self) -> None:
        if self.node == Node.EXECUTING.value and not self.approval.passed:
            raise InvariantError("node=EXECUTING requires approval.passed")
        if self.node == Node.RESOLVED.value:
            if not self.resolution.passed:
                raise InvariantError("node=RESOLVED requires resolution.passed")
            if any(s.outcome.status != StageStatus.PASSED.value for s in self.stages):
                raise InvariantError("node=RESOLVED requires every stage PASSED")
        if self.route == Route.SPAWN.value and self.weight_class != WeightClass.SUBSTANTIVE.value:
            raise InvariantError("route=SPAWN requires weight_class=SUBSTANTIVE")
        if (
            self.route == Route.SPAWN.value
            and self.node in _EXECUTION_NODES
            and self.partition is None
        ):
            raise InvariantError(
                "route=SPAWN at or past EXECUTING requires partition (run partition)"
            )
        if self.weight_class == WeightClass.CHAT.value and self.node not in (
            Node.CLASSIFIED.value,
            Node.ROUTED.value,
        ):
            raise InvariantError("weight_class=CHAT is terminal at ROUTED")
        if len(self.plan_stack) > _MAX_PLAN_STACK:
            raise InvariantError(
                f"plan_stack depth {len(self.plan_stack)} exceeds _MAX_PLAN_STACK={_MAX_PLAN_STACK}"
            )

    # --- check venue --------------------------------------------------------
    def resolve_check_venue(self, venue: str) -> str | None:
        """Resolve a declared CheckVenue value to a concrete cwd — the ONE
        resolver cmd_dispatch and all three verify sites (cmd_record_result,
        cmd_verify_final's per-stage re-run, cmd_verify_final's final_check
        loop) call, so they observe the same tree instead of dispatch writing
        to delivery_worktree while verification silently checks repo_root
        (the venue-asymmetry defect this method removes).

        "repo_root" always resolves to the canonical checkout. Anything else
        (including the "delivery" default) resolves to the plan's delivery
        venue: delivery_worktree when declared, else repo_root. With
        delivery_worktree unset this is byte-identical to repo_root for every
        plan that never declared one (152/171 existing plans as of schema 22)."""
        if venue == CheckVenue.REPO_ROOT.value:
            return self.repo_root
        return self.delivery_worktree or self.repo_root

    def resolve_final_check_venue(self, criterion: "Criterion") -> str | None:
        """Resolve a stage Criterion's check venue AT VERIFY-FINAL only:
        `criterion.verify_venue_at_final` when declared, else
        `criterion.verify_venue` (V4 — the identity that keeps a plan authored
        before schema 24 byte-identical). Delegates the venue -> cwd resolution
        to resolve_check_venue, the ONE function that knows how to turn a
        CheckVenue value into a concrete tree; this accessor only picks WHICH
        of the two declared fields verify-final reads. cmd_dispatch and
        cmd_record_result never call this — they always resolve verify_venue,
        because during execution the delivery venue is the only tree where the
        un-landed change exists."""
        return self.resolve_check_venue(criterion.verify_venue_at_final or criterion.verify_venue)

    # --- landed check synthesis --------------------------------------------
    def render_landed_command(self, spec: "LandedSpec") -> tuple[str | None, str | None]:
        """Render the ONE durable shell script for a `kind = "landed"` check:
        monotone `git merge-base --is-ancestor` containment of the commit
        `spec.delivered_stage` delivered (the LITERAL string frozen on that
        stage's Outcome.delivered_head, never re-resolved here) against both
        the local `target` ref and its local `remote/target` remote-tracking
        ref. Returns (command, refusal): `command` is the exact string a
        caller passes as `bash -c <command>` — it carries its own
        `git -C <repo_root>`, so it needs no cwd/`cd`; `refusal` is set
        instead when the check cannot even be attempted (no repo_root, an
        unknown delivered_stage, or that stage has not yet frozen a delivered
        commit) — the caller must surface this as a legible refusal, never a
        stage failure.

        No network call, no SHA equality, no live `rev-parse`: ancestry is
        monotone in a shared trunk (which only ever gains commits) and in this
        frozen commit (which never changes once stamped), so a check that goes
        green cannot later go red without a history rewrite — see CheckKind's
        module comment for the 20-incident background this replaces."""
        if not self.repo_root:
            return None, (
                "landed check requires [meta].repo_root to be set (nothing to "
                "resolve the target/remote refs against)"
            )
        try:
            stage = self.stage(spec.delivered_stage)
        except KeyError:
            return None, (
                f"landed check's delivered_stage {spec.delivered_stage} does "
                "not name an existing stage"
            )
        delivered = stage.outcome.delivered_head
        if not delivered:
            return None, (
                f"stage {spec.delivered_stage} has not yet frozen a delivered "
                "commit (record-result must run for that stage, with a "
                "resolvable delivery venue, before this landed check can be "
                "evaluated)"
            )
        repo_root = shlex.quote(self.repo_root)
        commit = shlex.quote(delivered)
        target = shlex.quote(spec.target)
        remote_target = shlex.quote(f"{spec.remote}/{spec.target}")
        command = (
            f"for R in {target} {remote_target}; do "
            f'git -C {repo_root} merge-base --is-ancestor {commit} "$R"; s=$?; '
            f'[ "$s" -eq 0 ] && continue; '
            f'[ "$s" -eq 1 ] && exit 1; '
            f"exit {LANDED_GIT_ERROR_EXIT}; "
            "done; exit 0"
        )
        return command, None

    # --- stage helpers ----------------------------------------------------
    def stage(self, index: int) -> Stage:
        for s in self.stages:
            if s.index == index:
                return s
        raise KeyError(f"no stage with index {index}")

    def active_stage(self) -> Stage | None:
        if self.current_stage is None:
            return None
        return self.stage(self.current_stage)

    def ready_stages(self) -> list[Stage]:
        """PENDING stages whose dependencies are all PASSED."""
        passed = {s.index for s in self.stages if s.outcome.status == StageStatus.PASSED.value}
        out = []
        for s in self.stages:
            if s.outcome.status == StageStatus.PENDING.value and all(d in passed for d in s.depends_on):
                out.append(s)
        return out

    def all_stages_passed(self) -> bool:
        return bool(self.stages) and all(
            s.outcome.status == StageStatus.PASSED.value for s in self.stages
        )

    def log(self, event: str, **fields) -> None:
        self.history.append({"event": event, **fields})

    # --- (de)serialization ------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        data = dict(data)
        data["approval"] = GateRecord(**data["approval"])
        data["resolution"] = GateRecord(**data["resolution"])
        data.pop("self_improvement", None)  # legacy field (schema <=4); self-improvement now runs on the standard spine
        # `plan_digest` was renamed to `accepted_plan_digest` (same meaning). from_dict ends
        # in cls(**data) and filters nothing, so without this a state.json written before the
        # rename dies on load with an unexpected-keyword TypeError and no recovery edge. The
        # value CARRIES OVER rather than being dropped: it is not recomputable from anything
        # else on the state (the accepted bytes may already have been edited in place), and
        # dropping it would silently answer "which bytes is this session running" with None.
        # No SCHEMA_VERSION bump goes with it — the migration is keyed on the key's presence,
        # and states written by the renaming commit already claim the current version, so a
        # bump could not discriminate them anyway. `setdefault` and not a truthiness test:
        # no writer produces a payload carrying both keys (the rename was atomic), and for
        # the shape that cannot occur the deliberate reading is that a present new key wins
        # even when null — the legacy value is the older claim of the two.
        legacy_digest = data.pop("plan_digest", None)
        if legacy_digest is not None:
            data.setdefault("accepted_plan_digest", legacy_digest)
        data.setdefault("plugins", {})            # migration: schema <=5 has no plugin layer
        data.setdefault("plugins_archive", {})
        data["final_check"] = [FinalCheck.from_dict(fc) for fc in data.get("final_check", [])]
        data["stages"] = [Stage.from_dict(s) for s in data.get("stages", [])]
        decomp = data.get("partition")
        data["partition"] = Partition.from_dict(decomp) if decomp else None
        pr = data.get("permission_request")
        data["permission_request"] = PermissionRequest(**pr) if pr else None
        data["difficulty"] = Difficulty.from_dict(data.get("difficulty"))
        data["plan_review"] = PlanReview.from_dict(data.get("plan_review"))
        data["stage_reviews"] = [
            r for r in (StageReview.from_dict(x) for x in data.get("stage_reviews", [])) if r is not None
        ]
        data["judge_bypassed"] = [JudgeBypass(**b) for b in data.get("judge_bypassed", [])]
        data["code_reviews"] = [
            r for r in (CodeReview.from_dict(x) for x in data.get("code_reviews", [])) if r is not None
        ]
        data["plan_presentations"] = [
            PlanPresentation.from_dict(x) for x in data.get("plan_presentations", [])
        ]
        cost_raw = data.get("cost")
        data["cost"] = CostRollup(**cost_raw) if cost_raw else None
        data["plan_stack"] = [
            PlanFrame(
                plan_path=f.get("plan_path"),
                node=f["node"],
                task_id=f.get("task_id", ""),
                goal=f.get("goal", ""),
                overall_done_criterion=f.get("overall_done_criterion", ""),
                overall_criterion_type=f.get("overall_criterion_type", CriterionType.MEASURABLE.value),
                weight_class=f.get("weight_class"),
                route=f.get("route"),
                repo_root=f.get("repo_root"),
                delivery_worktree=f.get("delivery_worktree"),
                final_check=[FinalCheck.from_dict(fc) for fc in f.get("final_check", [])],
                partition=Partition.from_dict(f["partition"]) if f.get("partition") else None,
                approval=GateRecord(**f["approval"]),
                resolution=GateRecord(**f["resolution"]),
                stages=[Stage.from_dict(s) for s in f.get("stages", [])],
                current_stage=f.get("current_stage"),
                originating_stage=int(f["originating_stage"]),
            )
            for f in data.get("plan_stack", [])
        ]
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "SessionState":
        return cls.from_dict(json.loads(text))
