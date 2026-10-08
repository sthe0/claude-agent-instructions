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
from pathlib import Path
from typing import ClassVar

from .grants import StageGrants
from .script_effects import StageEffectDeclaration

SCHEMA_VERSION = 44  # 34: PlanFrame gains parent_repo_root/parent_delivery_worktree/
                     # parent_venue_captured (pop-subplan venue-substitution guard)
                     # 35: PlanFrame also gains plugins/plugins_archive custody
                     # 36: Stage gains `grants` (declared [stage.grants]); SessionState
                     # gains runtime_grants/approved_grants_sha256/planning_misses/
                     # materialization_defects/settings_drift (permission-grant model)
                     # 37: PlanReview gains regression_command/regression_exit/remedy_tags;
                     # SessionState gains plan_review_passes
                     # 38: PlanReview gains in_scope_concern_ids/out_of_scope_concern_ids
                     # (advisory-scope classification of a stage-scoped review's concerns)
                     # 39: SessionState gains cwd_drift -- an uncovered-at-raw-text Bash
                     # denial that becomes covered once rebased from a drifted transcript
                     # cwd back to the stage venue (a planning artifact, not a genuine
                     # grant-coverage gap; see `_classify_transcript_denials`)
                     # 40: Stage gains `effects` (declared [[stage.effects]] — resource
                     # model, checkpoint c)
                     # 41: SessionState gains order_effort_flushed/order_effort_base/
                     # order_effort_frozen/agent_ack_difficulty_ids (autonomy boundary:
                     # order-keyed spend/wall-clock accumulation, frozen user-approved
                     # estimate, agent-acknowledged difficulty records,
                     # renegotiation_ceiling_difficulty_id)
                     # 42: SessionState gains plan_pair_reviews -- topological per-pair
                     # plan-review records (PlanPairReview), a third record family
                     # alongside plan_review/plan_stage_reviews that binds to the
                     # seven plan.pair_binding digests instead of whole-plan
                     # content and can discharge stage:<n> obligations via scoped
                     # discharge (see PlanPairReview's docstring); PlanReview gains
                     # reviewed_pair_bindings (pair id -> sha256 of its seven-digest
                     # binding) and record_seq, PlanPairReview gains record_seq, both
                     # stamped from SessionState.next_record_seq
                     # 43: SessionState gains review_rounds -- the mirror of the task's
                     # never-reset review-round total (task_accumulator axis of the
                     # same name) that the plan-review round valve reads
                     # 44: PlanReview/PlanPairReview gain per-concern severities,
                     # effective severities, raw_verdict, part_digests and stable ids;
                     # SessionState gains concern_ledger (blocking/note severity model)

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


# The reserved actor identity for engine-authored acts — `resolve-permission
# --by agent`'s self-grant path (order_approvals.boundary_resources coverage
# check, never a customer-authored write to the ledger itself) and nothing
# else. Never a valid `cmd_approve --by` (an approval must be customer-
# authored to mean anything — self-approval would let the engine stamp its
# own permission) and never a valid `[meta.order].customer_id` (a plan
# claiming the engine AS its own customer is the same defect from the order
# side — submission.py's `_order_violations` refuses it). Compared
# case-foldedly everywhere it is checked, so `Agent`/`AGENT`/`agent` are all
# the same reserved identity.
AGENT_ACTOR = "agent"


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
# "did this commit reach trunk" assertion — monotone containment (ancestry, else
# `git cherry` patch equivalence, merges failing closed) against a commit FROZEN at record-result time — from a declared
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


@dataclass(repr=False)
class LandedSpec:
    """The declarative payload of a `kind = "landed"` check (schema 23): assert
    that the commit stage `delivered_stage` delivered is contained in `target`
    (and its `remote` remote-tracking ref) — never SHA equality, never a
    live-resolved head. See plan.py's `_parse_landed_spec` for the validation
    rules this must satisfy before construction. `provider` names the VCS that
    answers: "git" (built in) or a plugin loaded through landed_providers.py."""
    target: str
    delivered_stage: int
    remote: str = "origin"
    provider: str = "git"

    def __repr__(self) -> str:
        # The default provider is left out so a git spec's repr (hashed into
        # stage digests) stays byte-identical to the pre-provider shape.
        extra = "" if self.provider == "git" else f", provider={self.provider!r}"
        return (f"LandedSpec(target={self.target!r}, delivered_stage="
                f"{self.delivered_stage!r}, remote={self.remote!r}{extra})")

    def refs_phrase(self) -> str:
        """Where the delivered work must be, as plan renderings and descriptions say it."""
        if self.provider == "git":
            return f"`{self.target}` and `{self.remote}/{self.target}`"
        return f"`{self.target}` (as reported by landed provider `{self.provider}`)"

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
    key -> default), which degrades the gate to the prior path-only binding.

    `scope` (schema 27) is "" for a whole-plan review (the only kind that used to
    exist) or `plan_review_scope_for_stage(n)` for a review of stage n alone.
    `reviewed_meta_digest`/`reviewed_stage_keys` are the ENGINE's own digests of the
    plan's meta and per-stage parts AT RECORD TIME (plan.plan_meta_digest /
    plan.plan_stage_digests) — distinct from the REVIEWER-attested `plan_sha256`
    above. Empty/default on legacy records and on any record recorded without a
    loadable plan, which is what makes plan.changed_parts read them as "everything
    moved" (see gates.plan_review_blockers).

    `concern_ids` (schema 28) is the stable identity of each entry in `concerns`,
    positionally paired — a RiskAcceptance binds to `concern_id`, never to the prose
    in `concerns`, so rephrasing a concern cannot silently rebind or unbind an
    acceptance. Shorter than `concerns` (or empty) on any record whose concerns were
    given no explicit id; `plan_review_concern_ids` fills the gap with a
    position-derived id, which is also what a legacy pre-schema-28 record gets in
    full.

    `regression_command`/`regression_exit` (schema 37) are set only on a
    `revise` recorded after a whole-plan/stage PASS already stands this cycle: the
    command the reviewer supplied to demonstrate the regression, and the actual
    exit code the engine observed running it in `repo_root` — never trusted from
    the reviewer's say-so. `remedy_tags` (schema 37) is the leading `cut:`/
    `add:` tag parsed off each entry in `concerns`, positionally paired like
    `concern_ids`; `""` where a concern carries no such tag."""
    plan_path: str
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    plan_sha256: str = ""
    scope: str = ""
    reviewed_meta_digest: str = ""
    reviewed_stage_keys: dict[str, str] = field(default_factory=dict)
    concern_ids: list[str] = field(default_factory=list)
    regression_command: str = ""
    regression_exit: "int | None" = None
    remedy_tags: list[str] = field(default_factory=list)
    in_scope_concern_ids: list[str] = field(default_factory=list)
    out_of_scope_concern_ids: list[str] = field(default_factory=list)
    # Schema 42: pair id -> sha256 of that pair's seven-digest pair_binding at record
    # time, for EVERY pair of the reviewed plan, whatever the verdict. None on a
    # record written before the field existed (gates read that as "every pair is
    # walk-stale"). `record_seq` totally orders review records of every kind.
    reviewed_pair_bindings: "dict[str, str] | None" = None
    record_seq: int = 0
    # Schema 44: the severity the reviewer wrote on each concern (`blocking`/`note`,
    # positionally paired with `concerns`), the engine's effective severity after the
    # freeze rules (`blocking`/`note`/`advisory`), the reviewer's own verdict when the
    # engine recorded a different one (`verdict` is the effective one every gate reads),
    # the plan's per-part digests at record time, and each concern's stable ledger id.
    # All empty on a legacy record, which reads as: every concern blocking, every part
    # changed.
    severities: list[str] = field(default_factory=list)
    effective_severities: list[str] = field(default_factory=list)
    raw_verdict: str = ""
    part_digests: dict[str, str] = field(default_factory=dict)
    stable_ids: list[str] = field(default_factory=list)

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
            scope=d.get("scope", ""),
            reviewed_meta_digest=d.get("reviewed_meta_digest", ""),
            reviewed_stage_keys=dict(raw) if isinstance(raw := d.get("reviewed_stage_keys"), dict) else {},
            concern_ids=list(d.get("concern_ids", [])),
            regression_command=d.get("regression_command", ""),
            regression_exit=d.get("regression_exit"),
            remedy_tags=list(d.get("remedy_tags", [])),
            in_scope_concern_ids=list(d.get("in_scope_concern_ids", [])),
            out_of_scope_concern_ids=list(d.get("out_of_scope_concern_ids", [])),
            reviewed_pair_bindings=(
                dict(rpb) if isinstance(rpb := d.get("reviewed_pair_bindings"), dict) else None
            ),
            record_seq=d.get("record_seq") or 0,
            severities=list(d.get("severities", [])),
            effective_severities=list(d.get("effective_severities", [])),
            raw_verdict=d.get("raw_verdict", ""),
            part_digests=dict(pd) if isinstance(pd := d.get("part_digests"), dict) else {},
            stable_ids=list(d.get("stable_ids", [])),
        )


_PLAN_REVIEW_STAGE_SCOPE_PREFIX = "stage:"


def plan_review_scope_for_stage(index: int) -> str:
    return f"{_PLAN_REVIEW_STAGE_SCOPE_PREFIX}{index}"


def plan_review_scope_stage_index(scope: str) -> "int | None":
    if not scope.startswith(_PLAN_REVIEW_STAGE_SCOPE_PREFIX):
        return None
    rest = scope[len(_PLAN_REVIEW_STAGE_SCOPE_PREFIX):]
    return int(rest) if rest.isdigit() else None


def plan_review_concern_ids(pr: "PlanReview") -> list[str]:
    """Every concern's stable id, positional over `pr.concerns`: an explicit
    `concern_ids[i]` where recorded, else the derived legacy id `c<i>`."""
    ids = pr.concern_ids
    return [ids[i] if i < len(ids) and ids[i] else f"c{i}" for i in range(len(pr.concerns))]


_PLAN_REVIEW_PAIR_SCOPE_PREFIX = "topo:"

# The seven plan.pair_binding digests a pair record carries, in the fixed order
# gates.pair_status reports the first moved one (`edge` right after `context`).
PAIR_BINDING_KEYS = (
    "context_digest", "edge_digest", "base_key", "base_file_digest",
    "service_key", "service_interface_digest", "service_file_digest",
)


def plan_review_scope_for_pair(pair: str) -> str:
    return f"{_PLAN_REVIEW_PAIR_SCOPE_PREFIX}{pair}"


def plan_review_pair_scope(scope: str) -> "str | None":
    """The pair id a `topo:<pair>` scope names, or None when `scope` is not a
    topo scope at all -- callers still validate that the id is one of
    `plan.review_pairs` of the evaluated plan (this only parses the shape)."""
    if not scope.startswith(_PLAN_REVIEW_PAIR_SCOPE_PREFIX):
        return None
    return scope[len(_PLAN_REVIEW_PAIR_SCOPE_PREFIX):] or None


# One thinker review of a single base-service PAIR (b, s) -- one reliance edge, base
# b relying on service s; the nodes are stage indices plus the synthetic `plan` and
# `base` (see plan.review_pairs). Recorded by cmd_plan_review's `--scope topo:<pair>`
# branch, kept in SessionState.plan_pair_reviews keyed by pair id (never in
# plan_review / plan_stage_reviews / plan_review_passes, which stay reserved for
# whole-plan and stage:<n> records). ONE record type serves every pair, the order
# pair (`base-plan`) and the plan-coverage pairs (`plan-<s>`) included: `base` and
# `service` hold a stage index, "plan" or "base", and only plan.pair_binding's node
# keys differ.
#
# A pair record binds to exactly the bytes its reviewer could see: the seven digests
# of plan.pair_binding, recomputed by the engine from a fresh load of the evaluated
# plan at record time (never read off the materialized view files or state.stages).
# `plan_sha256` is audit only -- currency is decided by the binding digests via
# gates.pair_status, never by the whole-plan sha.
#
# `plan_path` is the path this record was computed against (the session's registered
# plan, or a --target override) -- a record for a different path counts as `missing`
# for evaluating any other path (WALK/COMPOSE never mix plan files).
@dataclass
class PlanPairReview:
    pair: str
    base: "int | str"
    service: "int | str"
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    plan_path: str = ""
    plan_sha256: str = ""
    context_digest: str = ""
    edge_digest: str = ""
    base_key: str = ""
    base_file_digest: str = ""
    service_key: str = ""
    service_interface_digest: str = ""
    service_file_digest: str = ""
    record_seq: int = 0
    # Schema 44, as on PlanReview. `concern_ids` are the stable ledger ids
    # `<pair>#<record_seq>.c<i>` a later `re:<id>` names.
    concern_ids: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    effective_severities: list[str] = field(default_factory=list)
    raw_verdict: str = ""
    part_digests: dict[str, str] = field(default_factory=dict)

    def binding(self) -> dict[str, str]:
        """The stored digests, in the shape plan.pair_binding returns."""
        return {key: getattr(self, key) for key in PAIR_BINDING_KEYS}

    @classmethod
    def from_dict(cls, d: dict | None) -> "PlanPairReview | None":
        if not d:
            return None
        return cls(
            pair=d["pair"],
            base=d.get("base", ""),
            service=d.get("service", ""),
            verdict=d["verdict"],
            reviewer=d.get("reviewer", ""),
            concerns=list(d.get("concerns", [])),
            note=d.get("note", ""),
            plan_path=d.get("plan_path", ""),
            plan_sha256=d.get("plan_sha256", ""),
            record_seq=d.get("record_seq") or 0,
            concern_ids=list(d.get("concern_ids", [])),
            severities=list(d.get("severities", [])),
            effective_severities=list(d.get("effective_severities", [])),
            raw_verdict=d.get("raw_verdict", ""),
            part_digests=dict(pd) if isinstance(pd := d.get("part_digests"), dict) else {},
            **{key: d.get(key, "") for key in PAIR_BINDING_KEYS},
        )


# One reviewer concern in the session's concern ledger (schema 44), keyed by its stable
# id in SessionState.concern_ledger. The review families keep only their LATEST record
# per scope, so what an earlier record raised — and whether it is still open — lives
# here: `status` is `open` for an effective-blocking concern nothing has discharged,
# `fixed` once its part changed and the next record on the scope did not re-raise it,
# `superseded` once a later pass/override on the scope closed it, `recorded` for a
# concern that never blocked (note or advisory). A risk acceptance is NOT a status: it
# is read from SessionState.risk_acceptances at use time, so it keeps its own staleness
# rules. `parts` are the part tokens the concern names (its explicit token, else the
# scope's own parts); `raise_digests` are those parts' digests at the last record that
# carried the concern, the baseline "the part changed" is measured from.
CONCERN_OPEN = "open"
CONCERN_FIXED = "fixed"
CONCERN_SUPERSEDED = "superseded"
CONCERN_RECORDED = "recorded"


@dataclass
class ConcernRecord:
    id: str
    scope: str
    plan_path: str
    local_id: str
    text: str
    severity: str
    effective: str
    parts: list[str] = field(default_factory=list)
    raise_digests: dict[str, str] = field(default_factory=dict)
    record_seq: int = 0
    status: str = "recorded"
    regression_commands: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ConcernRecord | None":
        if not d or not d.get("id"):
            return None
        return cls(
            id=d["id"], scope=d.get("scope", ""), plan_path=d.get("plan_path", ""),
            local_id=d.get("local_id", ""), text=d.get("text", ""),
            severity=d.get("severity", ""), effective=d.get("effective", ""),
            parts=list(d.get("parts", [])),
            raise_digests=dict(rd) if isinstance(rd := d.get("raise_digests"), dict) else {},
            record_seq=d.get("record_seq") or 0, status=d.get("status", "recorded"),
            regression_commands=list(d.get("regression_commands", [])),
        )


# A recorded, attributed acceptance of one PlanReview concern's risk (schema 28) —
# the customer-facing alternative to editing the plan to make a `revise` concern go
# away. `concern_id` is the key, but the id alone is not the binding: `concern_text`
# (schema 29) pins the acceptance to the concern's prose at record time, and
# gates._concern_discharged requires that text still match the concern currently
# at that id — a rephrased or replaced concern at the same positional id stops
# discharging rather than silently rebinding to it. Also binds to the exact plan
# version via the same meta/stage-digest snapshot PlanReview itself carries —
# plan.changed_parts reads reviewed_meta_digest/reviewed_stage_keys (aliased here as
# meta_digest/stage_keys) identically for both records, so an acceptance recorded
# against one plan version does not survive a later edit to the part its concern
# lives in. `basis`/`risk` mirror premise.py's `assumed` question disposition
# exactly (same two required free-text fields, same anti-placeholder check) —
# accepting a risk is the same kind of act as assuming one.
@dataclass
class RiskAcceptance:
    scope: str
    concern_id: str
    plan_path: str
    basis: str
    risk: str
    author: str
    meta_digest: str = ""
    stage_keys: dict[str, str] = field(default_factory=dict)
    concern_text: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskAcceptance | None":
        if not d:
            return None
        return cls(
            scope=d["scope"],
            concern_id=d["concern_id"],
            plan_path=d.get("plan_path", ""),
            basis=d.get("basis", ""),
            risk=d.get("risk", ""),
            author=d.get("author", ""),
            meta_digest=d.get("meta_digest", ""),
            stage_keys=dict(raw) if isinstance(raw := d.get("stage_keys"), dict) else {},
            concern_text=d.get("concern_text", ""),
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
    path-only fallback). `reviewer` is the judge tag ("judge:acceptance"; older
    records keep "judge:haiku") for an automated verdict or a human name for a manual/override record."""
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
    name for a manual/override record. `not_checked` is the axes this review did
    NOT check (or "none") — cli.cmd_code_review refuses to record a `pass`
    verdict without it (an unchecked axis must be explicit, never silently
    implied by an approving verdict); empty on a `revise`/`override` record,
    where the field does not apply."""
    stage_index: int
    verdict: str
    reviewer: str
    concerns: list[str] = field(default_factory=list)
    note: str = ""
    code_sha256: str = ""
    not_checked: str = ""

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
            not_checked=d.get("not_checked", ""),
        )


# The stage-6 re-attest carry-forward record (schema 32): built fresh, in full, on
# every substantive replan, one entry per stage the replan found ELIGIBLE to
# re-attest — i.e. its immediately-prior live outcome was PASSED. Structurally the
# same charter as StageReview/CodeReview (one typed record per stage, looked up by
# a last-wins scan), but this one is not itself a verdict: `dispatch --re-attest`
# still re-runs the stage's own control before trusting it, so the stash only
# carries what a normal dispatch would otherwise have to re-derive from scratch —
# whether the replan touched the stage's operative surface, and what to carry
# forward if it re-attests. Kept on SessionState rather than as new Stage/Outcome
# fields specifically so it never enters the Stage-leaf totality tests
# (test_renormalization/test_question_key_scope/test_contract_coverage all
# enumerate `leaf_paths(Stage)`; SessionState has no such test).
@dataclass
class ReattestStash:
    """One stage's re-attest eligibility, computed at the substantive replan that
    re-armed it.

    `stage_index` binds the stash to its stage. `operative_surface_matched` is
    True iff `plan.stage_reattest_key` was IDENTICAL between the stage's prior
    (PASSED) definition and its just-replanned one — condition 2 of the three the
    route checks; a False entry is kept (not dropped) so a refusal can name the
    specific reason rather than reporting "no stash" for a stage that had one.
    `prior_outcome`/`prior_control` are what re-attest carries forward on success
    — copies, not aliases, of the stage's outcome/control at the moment PASSED was
    last recorded, so a later mutation of the stash's own copy (the `[re_attested]`
    marker appended to `.actual`) can never also mutate the ORIGINAL record it was
    copied from. `reattest_digest` is `plan.stage_reattest_digest` of the
    JUST-REPLANNED stage — re-validated against the LIVE stage at dispatch time
    (not merely trusted from this record) so a further plan edit made during the
    PLAN_READY window, after this stash was built, is caught rather than trusted
    stale."""
    stage_index: int
    operative_surface_matched: bool
    prior_outcome: Outcome
    prior_control: str | None
    reattest_digest: str

    @classmethod
    def from_dict(cls, d: dict | None) -> "ReattestStash | None":
        if not d:
            return None
        return cls(
            stage_index=int(d["stage_index"]),
            operative_surface_matched=bool(d.get("operative_surface_matched", False)),
            prior_outcome=Outcome(**d["prior_outcome"]) if d.get("prior_outcome") else Outcome(),
            prior_control=d.get("prior_control"),
            reattest_digest=d.get("reattest_digest", ""),
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
# Third instance of the same charter (see the module comment above): proof that
# a proposed replan's diff rendering was shown to the user, bound to the
# PROPOSED plan's bytes (the OLD side of the diff is already pinned
# independently by state.plan_snapshot_path, which cmd_replan diffs against).
# Gated by gates.replan_authorization_blockers, recorded by cmd_present_plan
# exactly like the other two kinds.
PLAN_PRESENTATION_KIND_REPLAN_DIFF = "replan_diff"
PLAN_PRESENTATION_KINDS = (
    PLAN_PRESENTATION_KIND_ESSENCE,
    PLAN_PRESENTATION_KIND_FULL,
    PLAN_PRESENTATION_KIND_REPLAN_DIFF,
)

# Language-independent ASCII marker a plan-approval AskUserQuestion option must
# embed (label or description) to show the full plan. Checked by
# hook-plan-delivery-gate.py's _has_show_full_plan_option, emitted by
# cli.cmd_present_plan's essence Directive — single-sourced here so the two can
# never drift apart.
SHOW_FULL_PLAN_MARKER = "[show-full-plan]"

# Language-independent ASCII marker a replan-diff rendering must embed so the
# delivery hook (extended by stage 4) can recognize the turn as carrying a
# replan-authorization presentation, exactly mirroring SHOW_FULL_PLAN_MARKER —
# single-sourced here so the emitter and the checker can never drift apart.
AUTHORIZE_REPLAN_MARKER = "[authorize-replan]"


@dataclass
class PlanPresentation:
    plan_path: str
    kind: str  # PLAN_PRESENTATION_KIND_ESSENCE | PLAN_PRESENTATION_KIND_FULL
    plan_sha256: str
    rendering_sha256: str
    rendering_text: str
    presented_ts: float
    # sha256 of the plan's effective (declared+derived) grant set AT PRESENTATION
    # TIME (schema 36) — `approve` re-derives the live plan's grants_sha256 and
    # refuses on mismatch, so a materialization-layer change or an unreviewed
    # plan edit between present-plan and approve can never silently carry a
    # wider grant into an approved stage. None on every pre-schema-36 receipt
    # (absent key -> None via from_dict's .get), which approve treats as "no
    # grants digest was ever bound" rather than "matches" (fail-closed).
    grants_sha256: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "PlanPresentation":
        return cls(
            plan_path=d["plan_path"],
            kind=d["kind"],
            plan_sha256=d["plan_sha256"],
            rendering_sha256=d["rendering_sha256"],
            rendering_text=d["rendering_text"],
            presented_ts=d["presented_ts"],
            grants_sha256=d.get("grants_sha256"),
        )


# Bypass-visibility record (schema 14): every time an acceptance_review stage is
# recorded PASSED WITHOUT a genuine passing judge verdict — because the kill switch
# disabled the gate, or a human override cleared it — one JudgeBypass is appended and
# NEVER cleared by a later passing review. verify-final refuses a clean bill while any
# entry exists unless it prints them; the resolution summary surfaces them verbatim.
@dataclass
class JudgeBypass:
    stage_index: int
    kind: str  # "killswitch" | "override" | "fail_open"
    reviewer: str = ""
    note: str = ""
    # Set only for kind="fail_open": binds the bypass to the exact observation
    # the judge call failed on, so a fail-open recorded for an EARLIER (superseded)
    # observation cannot wave through a later, unjudged one — see
    # gates.acceptance_review_blockers and cli._record_bypass. "killswitch"/"override"
    # leave this "" and dedupe exactly as before (see _record_bypass's docstring).
    observation_sha256: str = ""


@dataclass
class RequirementVerdict:
    requirement_id: str
    verdict: str  # "pass" | "fail"
    note: str = ""


# Plan-level acceptance record (schema 26): the ORDER's customer accepts the delivered
# PRODUCT against the declared requirements — once, at resolution — distinct from the
# per-stage control comparisons (StageReview/CodeReview compare an in-progress RESULT
# against that stage's own criterion, every stage, throughout execution). `author`
# must match [meta.order].customer_id (cmd_accept refuses to write otherwise, since
# only the customer who placed the order can accept its product). `verdicts` must
# name every requirement id the order declares (cmd_accept refuses an incomplete
# write; resolution_blockers rechecks defensively — see that function's docstring
# for why the recheck is not redundant). `plan_sha256` is stamped from
# state.accepted_plan_digest at write time, so a later replace-the-plan-through-
# approve leaves this review pointing at superseded bytes; resolution_blockers
# treats that mismatch as an absent review (fail-closed, not a stale pass).
@dataclass
class AcceptanceReview:
    author: str
    verdicts: list[RequirementVerdict] = field(default_factory=list)
    note: str = ""
    plan_sha256: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "AcceptanceReview | None":
        if d is None:
            return None
        d = dict(d)
        d["verdicts"] = [RequirementVerdict(**v) for v in d.get("verdicts", [])]
        return cls(**d)


# Bypass-visibility record (schema 26) for the plan-level acceptance gate — same role
# as JudgeBypass, one level up: recorded when cmd_accept writes an AcceptanceReview
# without judge corroboration (the corroborating judge was unreachable) and a human
# supplies --bypass-reason. cmd_accept refuses to write a bypass with no accompanying
# AcceptanceReview, so the two always arrive together. NEVER read by
# resolution_blockers itself — the resolution gate has exactly one blocking
# condition (a complete, all-passing, fresh AcceptanceReview); this is a permanent,
# separately-surfaced visibility record for verify-final, not a second path to a pass.
@dataclass
class AcceptanceBypass:
    reason: str
    reviewer: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "AcceptanceBypass | None":
        if d is None:
            return None
        return cls(**d)


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
    """The instruments, and the two different things a plan has to say about their use.

    `means` are the instruments themselves — the element the plan fixes and a stage may
    not re-select mid-flight.

    `method` is the REQUIREMENT on the way of acting: what the transformation must be an
    instance of (the pattern to follow, the abstraction to extend, where the change
    lands). It is the planner's and the customer's, and it moves only through the review
    and approval a replan re-arms.

    `procedure` is the SEQUENCE of operations proposed for meeting that requirement. It
    is the executor's own: reading the code routinely shows a better order, and replacing
    it costs no re-approval — `cli.cmd_replan --renormalize`, which the engine refuses
    the moment the edit reaches the method, the criterion, the result image, or the goal
    every stage's observation is compared against.

    Until this field existed this docstring defined one as the other — "the fixed
    instruments (means) and the procedure over them (method)" — so a plan had a single
    place for a norm and a proposal, and the proposal, being the concrete one, took it.
    An executor who then found a better order either followed a worse one or quietly
    rewrote what he was held to, and neither is visible in the diff as what it is.

    Optional on the TYPE and required of a substantive stage at the SUBMISSION seam
    (submission.py), like every requirement added since the corpus was frozen: 55 stored
    plans predate the field and must keep loading byte-for-byte."""
    means: str
    method: str
    procedure: str = ""


@dataclass
class Actor:
    """Who executes the stage and what capability that demands."""
    executor: str  # "in_thread" | "spawn:<spec>"
    capability_required: str | None = None
    # Declared dispatch-budget tier (schema 25): "small" | "medium" | "large",
    # optional. Resolution order at dispatch is flag > this declaration > the
    # "medium" default. Absent on legacy pre-schema-25 states (absent key ->
    # dataclass default via Stage.from_dict's Actor(**d["actor"]) splat), so a
    # stage that never declared a tier dispatches exactly as before.
    cost_tier: str | None = None
    # Repo-relative paths forwarded to the spawned child as repeated
    # `--guard-exempt` flags (dispatch mechanics, not a plan ontology element).
    # Absent key -> empty list, so a stage that never declared it dispatches
    # exactly as before.
    guard_exempt_paths: list[str] = field(default_factory=list)


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
    # A second shell command, run in the same venue as verify_command against a
    # known-bad input, that submission.py requires for a measurable shell stage
    # (or a `negative_control_waiver` reason in its place) before the plan can
    # be submitted: a check that cannot be shown to fail on bad input certifies
    # nothing. cmd_record_result runs it only after verify_command itself has
    # gone green, and treats a matching exit code as the stage NOT passing.
    negative_control: str | None = None
    negative_control_waiver: str | None = None

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
    derivation: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Requirement":
        return cls(
            id=str(d.get("id", "")),
            text=str(d.get("text", "")),
            derivation=str(d.get("derivation", "")),
        )


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
    consumer that needs the order reads it from the plan.

    That is a claim about RE-MATERIALIZATION alone, and it has already been read too
    widely once. The order is still plan CONTENT, so every function deciding what counts as
    a CHANGE to the plan does have to know about it: `plan.order_scope` (substantive tier),
    `plan.order_place` (refinement tier), and `plugins_premise._plan_content_digest`, which
    decides whether a discharged question enumeration survives a replan — the last was
    missed on exactly this reasoning.

    `requires_traceability` is unlike every other field here: it is not a part of the
    order, it is the plan's own opt-in into a submission-seam GRADE that reads other
    parts of the order (submission.py's `_element_traceability_violations` and
    `_requirement_derivation_violations` — R3/R5). Every other requirement this engine
    has added since the corpus was frozen binds unconditionally at the submission seam,
    on the reasoning `Means.procedure` states: optional on the type, required of a
    substantive plan at submission, and a handful of stored plans predating the field
    simply start failing their next submission-seam re-read. That reasoning does not
    survive R3/R5: measured against every order-bearing plan in `~/.claude-agent/plans/`
    at authoring time, ALL of them (135/135) would newly fail BOTH checks, because
    neither `[stage].material/result/...` naming a requirement id nor
    `[meta.order].requirements[*].derivation` existed as an authored convention before
    R3/R5 did. A requirement that breaks every existing instance of the thing it grades,
    rather than the handful a smaller addition breaks, is not the same event — it needs
    a plan to say it was AUTHORED under the convention, not merely to happen to satisfy
    it. False by default: every plan on disk before this field existed lacks the key and
    reads false, so R3/R5 apply to none of them; a plan sets it true once its stages
    actually carry requirement ids and every requirement carries a derivation."""
    customer_id: str = ""
    customer: str = ""
    functional_place: str = ""
    requirements: list[Requirement] = field(default_factory=list)
    # See the docstring above. Read only by submission.py's R3/R5 gate; `_order_violations`
    # never reads it, because the four ordinary order parts stay required unconditionally.
    requires_traceability: bool = False
    # requirement id -> the controls that decide it. Values are lists of prose
    # references (a stage's verify_command, a final_check) — a machine reads the KEYS
    # for totality; whether an entry's named control really decides the requirement is
    # review, not something this type can settle.
    coverage: dict[str, list[str]] = field(default_factory=dict)
    # Names of the [meta.order] keys the raw table CARRIED but `from_dict` could not read
    # in the shape this type declares. Recorded, not authored: totality degrades a
    # malformed key to its empty form, which at the submission seam is indistinguishable
    # from an absent one — so without this record the seam reports "missing
    # 'requirements'" for a key plainly present, sending the author to write again what
    # is already there instead of to fix its shape. Only `from_dict` ever sets it.
    malformed: tuple[str, ...] = ()
    # (dropped, total) entry counts for the PARTIAL case of a malformed `requirements`
    # list — some entries were tables and survived, some were not and were dropped.
    # None for every other case (no malformation, or the TOTAL case: not a list at all,
    # or a list none of whose entries survived) — those are exhausted by `malformed`
    # alone and have no count worth naming. Not an authored part any more than
    # `malformed` is: it is the same malformation's own drop count, not a place of its
    # own. Only `from_dict` ever sets it.
    requirements_dropped: tuple[int, int] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        """Rebuild an Order from a raw TOML/JSON table, TOTALLY: every malformation
        degrades to the empty form rather than raising. That totality is the property
        the loader path depends on — a plan whose [meta.order] is nonsense must still
        load, and be refused at the seam where refusals belong. What was degraded is
        recorded in `malformed` so that seam can say WHICH defect it found."""
        malformed: list[str] = []
        requirements_dropped: tuple[int, int] | None = None

        raw_reqs = d.get("requirements")
        if isinstance(raw_reqs, list):
            reqs = [Requirement.from_dict(r) for r in raw_reqs if isinstance(r, dict)]
            if len(reqs) != len(raw_reqs):  # a bare sentence among the tables
                malformed.append("requirements")
                if reqs:  # some entries survived — the PARTIAL case, not the total one
                    requirements_dropped = (len(raw_reqs) - len(reqs), len(raw_reqs))
        else:
            reqs = []
            if raw_reqs is not None:  # a string, or [meta.order.requirements] as a table
                malformed.append("requirements")

        raw_cov = d.get("coverage")
        if isinstance(raw_cov, dict):
            cov = {
                str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
                for k, v in raw_cov.items()
            }
        else:
            cov = {}
            if raw_cov is not None:
                malformed.append("coverage")

        return cls(
            customer_id=str(d.get("customer_id", "")),
            customer=str(d.get("customer", "")),
            functional_place=str(d.get("functional_place", "")),
            requirements=reqs,
            coverage=cov,
            malformed=tuple(malformed),
            requirements_dropped=requirements_dropped,
            requires_traceability=bool(d.get("requires_traceability", False)),
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
    # Effort-divergence custody (schema 25) — see effort.py's SUB-PLAN CUSTODY. Snapshotted
    # by cmd_push_subplan, restored by cmd_pop_subplan; effort.py never reads a PlanFrame.
    effort_estimate: dict | None
    effort_baseline: dict | None
    effort_actuals: dict
    effort_fires: list[dict]
    effort_spend_seen: dict
    # Review-round custody (schema 30). The counter and its counted-version marker belong
    # to the plan under review, so a service sub-plan neither spends the parent's budget
    # nor inherits it: cmd_push_subplan snapshots them here and zeroes the live pair,
    # cmd_pop_subplan restores them. This is why the counter's monotonicity invariant is
    # scoped to one plan-stack level — a pop restoring the parent's smaller count is
    # custody, not a decrease. Defaults carry legacy frames (absent keys), which restore
    # the same zeroed pair a fresh push would produce.
    plan_review_rounds: int = 0
    plan_review_counted_digest: str = ""
    # Code-review round custody (item A / GitHub issue #96) — mirrors plan_review_rounds
    # above: the service sub-plan's own code-review rounds are a separate budget from the
    # parent's, so cmd_push_subplan snapshots and zeroes, cmd_pop_subplan restores.
    code_review_rounds: int = 0
    # Venue-substitution guard (schema 34): the exact (repo_root, delivery_worktree)
    # pair _sync_venue_from_plan read off the PARENT plan file at push time, so pop can
    # tell "the parent file's venue fields moved out from under the pushed child" apart
    # from "nothing changed" or "the file could not be read at push" — closing the one
    # other post-approval route from an edited plan FILE to live state (a parent edited
    # while its child is pushed, then popped, silently re-deriving a venue nobody
    # approved). `parent_venue_captured` is load-bearing on its own: an empty captured
    # value is the common shape (most plans declare repo_root and no delivery_worktree),
    # so "captured empty" and "not captured" must stay distinguishable, or a
    # delivery_worktree later ADDED to a parent that declared none is invisible to the
    # comparison. All three default so a legacy frame (pre-34) loads with captured=False,
    # i.e. no comparison possible — cmd_pop_subplan re-derives from the plan file exactly
    # as it always has.
    parent_repo_root: str = ""
    parent_delivery_worktree: str = ""
    parent_venue_captured: bool = False
    # Plugin custody (schema 35). Every registered auto-activating plugin (premise,
    # ledger, obligations, review_dispatch, experience, tracker) declares scope='task'
    # in plugins.py, promising retirement at a task boundary — a pushed sub-plan IS a
    # new task (task_id becomes f'sub:{...}'), so these fields finally enforce that
    # promise across push/pop the same way plan_review_rounds above enforces it for
    # review rounds. plugins_archive travels with plugins because
    # plugins.auto_activate_for's suppression guard treats archive-presence the same
    # as active-presence (`if name in state.plugins or name in state.plugins_archive:
    # continue`) — sharing it across the boundary would let a first sub-plan's
    # terminal-retired plugin silently suppress that same plugin for a later sub-plan.
    plugins: dict[str, dict] = field(default_factory=dict)
    plugins_archive: dict[str, dict] = field(default_factory=dict)


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
    # Frozen alongside delivered_head: `merge-base(delivery HEAD, <remote>/<target>)` at
    # freeze time. The landed check's task-proof range is `delivered_base..delivered_head`
    # and the squash test's merge base is this value too, so both stay constant as trunk
    # moves. None on a state frozen before this field existed (legacy rule: only the
    # delivered commit's own message is inspected).
    delivered_base: str | None = None
    # Green-check cache: the venue tree identity (HEAD sha + a diff against HEAD
    # covering staged and unstaged changes + untracked files' contents, see
    # cli._venue_tree_identity) at the last time this stage's verify_command was
    # actually RUN, and whether that run was green. A later record-result call whose
    # identity still matches AND whose cached result was green skips re-running the
    # command (cli._cached_check_hit) — a red result is never cached, so a genuinely
    # broken check is always re-run rather than trusted to still be broken.
    # `checked_tree_ok` covers the WHOLE check, not just the positive command: when
    # the criterion also declares a `negative_control`, it is True only once the
    # control has also been shown to fail on this exact tree (or the control is
    # waived) — never on the positive command's result alone. A cache hit then skips
    # re-running BOTH commands, so a stage carrying a negative_control keeps the same
    # "unchanged tree re-queries only the judge" guarantee as one without. Absent on
    # every state predating this field (defaults via from_dict), so legacy states
    # load unchanged.
    checked_tree_identity: str | None = None
    checked_tree_ok: bool | None = None
    # Unconditional per-stage attempt counter: incremented on every
    # record-result call regardless of outcome, so a diagnosing session can see every
    # attempt made against this stage, including ones a gate blocked before any
    # verification ran.
    record_attempts: int = 0


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
    # Escape hatch, mirroring criterion.negative_control_waiver's shape: names why an
    # output_artifacts entry legitimately resolves under exempt_paths.scratch_roots()
    # (submission.py's ephemeral-artifact check accepts a flagged entry once this is
    # non-empty). None on every plan authored before this field, which is byte-identical
    # to "no waiver declared".
    ephemeral_artifacts_waiver: str | None = None
    outcome: Outcome = field(default_factory=Outcome)
    # General control-criterion attestation (element #3 of the plan activity ontology).
    # Optional on any stage; required non-empty for spawn:developer when recording passed,
    # because review is the value the control criterion takes for the developer special case.
    control: str | None = None
    # Declared [stage.grants] (schema 36) — validated by grants.validate_grants at plan
    # load, never trusted unvalidated. None on every plan authored before this field (and
    # on any stage that declares no grants block at all), which is byte-identical to "no
    # declared grants": derive_stage_grants still runs at dispatch time, so an old plan's
    # spawned children keep receiving the same DERIVED grants they always did. See
    # grants.py's module docstring for why this is the sole validated entry point.
    grants: StageGrants | None = None
    # Declared [[stage.effects]] (resource model, checkpoint c): the plan author's
    # own claim that a specific, digest-pinned script resolves this stage's calls
    # through a named resolver — trusted only while the live script's bytes still
    # match the pinned digest (script_effects.resolve_script enforces that at
    # resolve time; this field only carries the declaration). Empty on every plan
    # authored before this field, byte-identical to "no declared effects".
    effects: list[StageEffectDeclaration] = field(default_factory=list)

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
                ephemeral_artifacts_waiver=d.get("ephemeral_artifacts_waiver"),
                outcome=Outcome(**d["outcome"]) if d.get("outcome") else Outcome(),
                control=d.get("control"),
                grants=StageGrants.from_dict(d.get("grants")),
                effects=[StageEffectDeclaration.from_dict(e) for e in d.get("effects", [])],
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
                guard_exempt_paths=list(d.get("guard_exempt_paths", [])),
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
            ephemeral_artifacts_waiver=d.get("ephemeral_artifacts_waiver"),
            outcome=Outcome(
                status=d.get("status", StageStatus.PENDING.value),
                actual=d.get("actual"),
                fail_digests=list(d.get("fail_digests", [])),
            ),
            control=d.get("control"),
            grants=StageGrants.from_dict(d.get("grants")),
            effects=[StageEffectDeclaration.from_dict(e) for e in d.get("effects", [])],
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
    # The plan's own [meta].task_id — the id stage commits' `Task:` trailers are written
    # with. Distinct from `task_id`, which `agentctl start --task` sets and nothing binds
    # to the plan. Stamped by cli._sync_venue_from_plan; "" on a state predating the field.
    plan_task_id: str = ""
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
    # Stage-scoped thinker reviews (schema 27), keyed by PlanReview.scope — the
    # per-part sibling of plan_review above, which stays the whole-plan (scope "")
    # slot. Empty on legacy states (absent key -> dataclass default via from_dict),
    # which is what makes the coverage gate in gates.py fall back to plan_review
    # alone, unchanged.
    plan_stage_reviews: dict[str, "PlanReview"] = field(default_factory=dict)
    # Historical record (schema 37) of the LAST attested PASS recorded per scope
    # this approval cycle, keyed like plan_stage_reviews (whole-plan under ""). Unlike
    # plan_review/plan_stage_reviews (the CURRENT authoritative record, which a
    # resubmission's staleness-clear or a later revise can move on), this is never
    # cleared mid-cycle — only reset at cmd_approve/cmd_replan — so
    # gates.plan_review_prior_pass keeps reporting a pass as "found" even after the
    # current record has moved past it. Whether ANY pass has landed this cycle (the
    # round-release message template selector) is derived as `bool(plan_review_passes)`
    # rather than tracked as a separate flag. Empty on legacy states (absent key ->
    # dataclass default via from_dict).
    plan_review_passes: dict[str, "PlanReview"] = field(default_factory=dict)
    # Topological per-PAIR thinker reviews (schema 42), keyed by pair id ('3-1',
    # 'plan-7', 'base-plan') — a third record family alongside
    # plan_review/plan_stage_reviews, never merged into either. A pair record binds
    # to the seven plan.pair_binding digests rather than to the whole plan's
    # content, so it never advances plan_review_rounds and is never itself a
    # stage:<n>/whole-plan record; it can only DISCHARGE a stage:<n> obligation via
    # gates.py's scoped-discharge logic (see PlanPairReview's own docstring for the
    # exact binding). Empty on legacy states (absent key -> dataclass default via
    # from_dict); a legacy `plan_topo_reviews` key is dropped at load.
    plan_pair_reviews: dict[str, "PlanPairReview"] = field(default_factory=dict)
    # Monotonic counter stamped (then incremented) into `record_seq` at every
    # review-record write -- whole-plan, composed or per-pair -- so records carry a
    # total order that does not depend on wall-clock time.
    next_record_seq: int = 0
    # Every concern any review record raised, by stable id (schema 44) — see ConcernRecord.
    # Never reset: a `re:<id>` must resolve across approve/replan, and a concern's
    # status outlives the record that raised it. Empty on legacy states.
    concern_ledger: dict[str, "ConcernRecord"] = field(default_factory=dict)
    # Recorded risk acceptances discharging `revise` concerns (schema 28) — see
    # RiskAcceptance's docstring for the binding. Empty on legacy pre-schema-28
    # states (absent key -> dataclass default via from_dict), which is what makes
    # gates._plan_review_verdict_blockers fall back to today's unconditional block
    # on a `revise` verdict, unchanged.
    risk_acceptances: list["RiskAcceptance"] = field(default_factory=list)
    # Review-round counter (schema 30), advanced on BOTH review paths:
    #   * pre-approval — cmd_submit_plan increments it on every resubmission at
    #     PLAN_READY (the revise_plan self-loop) made while a review record stands;
    #     cmd_approve resets it to 0 on a successful approval.
    #   * post-approval — cmd_plan_review increments it per plan VERSION reviewed once
    #     the approval gate has passed (the `replan` loop, which is where review cycles
    #     actually recur); cmd_replan resets it and plan_review_counted_digest together.
    # The two paths are disjoint in time, not merely by convention: cmd_submit_plan sets
    # approval.passed = False BEFORE its own increment, so no single call can satisfy
    # both conditions. Read by gates.plan_review_round_release_active against
    # config.md's effort-replan-absolute threshold, which it reuses. 0
    # on legacy states (absent key -> dataclass default via from_dict's cls(**data)).
    plan_review_rounds: int = 0
    # The task's total of review rounds — one per reviewed plan version or thinker
    # record, never reset by approve/replan/push/pop (task-level, not plan-level) —
    # mirrored from task_accumulator's `review_rounds` axis whenever cli.py loads the
    # session (`_require`) and after each increment. The accumulator, not this field,
    # is the source of truth: gates.py stays pure and reads only this mirror, as
    # max(plan_review_rounds, review_rounds). 0 on legacy states (absent key ->
    # dataclass default via from_dict's cls(**data)).
    review_rounds: int = 0
    # sha256 of the plan bytes whose review last advanced plan_review_rounds on the
    # post-approval path (schema 30). The counted UNIT is a plan VERSION, not a recorded
    # verdict: gates._plan_review_blockers_coverage surfaces ONE uncovered stage at a
    # time, so an honest first coverage pass over a plan with three moved stages records
    # three verdicts against identical bytes. Counting verdicts would fire the release
    # part-way through that pass — and because the release SUBSTITUTES the whole blocker
    # list rather than adding to it, "stage 3 has not been reviewed" would be replaced by
    # "no further review is required", retiring a requirement nobody satisfied. Empty on
    # legacy states (absent key -> dataclass default via from_dict's cls(**data)).
    plan_review_counted_digest: str = ""
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
    # Code-review round counter (item A / GitHub issue #96) — the code-review axis's
    # analog of plan_review_rounds above: this axis previously had no round-release
    # valve at all. Incremented by cmd_code_review each time a verdict is recorded for
    # a stage that already had one (a re-review, not the first pass); reset alongside
    # plan_review_rounds by cmd_approve and cmd_replan. Read by
    # gates.code_review_round_release_active and by gates.cross_axis_friction_release_active
    # (which sums this axis with plan_review_rounds and the plan-enumerate axis's own
    # counter against ONE shared ceiling). 0 on legacy states (absent key -> dataclass
    # default via from_dict's cls(**data)).
    code_review_rounds: int = 0
    # Plan-level acceptance record backing the resolution gate's acceptance check
    # (schema 26): the ORDER's customer accepting the delivered PRODUCT against the
    # order's declared requirements, once, at resolution — distinct from every
    # per-stage control comparison above, which compares an in-progress RESULT
    # against that stage's own criterion throughout execution, not the product
    # against the order. None until cmd_accept records one; legacy pre-schema-26
    # states load with None (absent key -> dataclass default via from_dict), so a
    # substantive session's resolution gate has no observable and blocks
    # (fail-closed) until an AcceptanceReview is recorded. acceptance_bypass is the
    # paired, permanent visibility record for a judge-unreachable bypass (see
    # AcceptanceBypass's docstring for why resolution_blockers never reads it).
    acceptance_review: "AcceptanceReview | None" = None
    acceptance_bypass: "AcceptanceBypass | None" = None
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
    # Effort-divergence trigger (schema 25) — effort.py owns every read and write of
    # these; nothing else interprets them, and effort.py's module docstring is the
    # canonical statement of the mechanism (THE WINDOW; ARMED-ONLY, AND ARMED AT MOST
    # ONCE; RE-ARM; MONOTONE ACTUALS; SUB-PLAN CUSTODY — each a literal heading in that
    # docstring). All six load as their zero value on legacy states (absent key
    # -> dataclass default via from_dict's cls(**data)).
    #   effort_estimate    the CURRENT plan's declared cost per ratio scale (effort.rederive).
    #   effort_baseline    the actual vector snapshotted at arming (effort.arm); None until
    #                      then is the ARMED-ONLY sentinel — see effort.py.
    #   effort_actuals     the monotone accumulators ('spend_usd', 'active_minutes').
    #   effort_fires       one record per firing (scale, multiple, history_len).
    #   effort_spend_seen  plan_path -> ledger total already booked, keyed BY PATH.
    #   user_prompt_count  interactions actual, stamped by hook-engine-start.py.
    #   effort_crossings   RECORD_ONLY_SCALES observations (effort.record_crossing) —
    #                      SESSION-scoped like user_prompt_count itself, deliberately NOT
    #                      a PlanFrame field: a record-only crossing describes the whole
    #                      session's interaction count, not one plan's, so it must survive
    #                      sub-plan push/pop untouched rather than snapshot/reset with the
    #                      rest of the effort custody block.
    effort_estimate: dict | None = None
    effort_baseline: dict | None = None
    effort_actuals: dict = field(default_factory=dict)
    effort_fires: list[dict] = field(default_factory=list)
    effort_spend_seen: dict = field(default_factory=dict)
    user_prompt_count: int = 0
    effort_crossings: list[dict] = field(default_factory=list)
    # Order-keyed effort custody (autonomy boundary). Only meaningful for a session
    # whose order has a user-approved version in order_approvals' ledger.
    #   order_effort_flushed  the actual spend/wall_clock already moved into the ledger
    #                         (None until the first approve of this session).
    #   order_effort_base     the ledger's window total at this session's last spend/
    #                         wall_clock fire — the order-level twin of effort_baseline.
    #   order_effort_frozen   the user-approved spend/wall_clock estimate rederive keeps.
    #   agent_ack_difficulty_ids  ids of difficulty records an agent-authored
    #                         fire-acknowledge / renegotiation already consumed.
    order_effort_flushed: dict | None = None
    order_effort_base: dict = field(default_factory=dict)
    order_effort_frozen: dict | None = None
    agent_ack_difficulty_ids: list[str] = field(default_factory=list)
    #   renegotiation_ceiling_difficulty_id  id of the difficulty record open when the
    #                         diagnosing-replan ceiling last refused a replan; an agent
    #                         `continue` needs a record declared after it.
    renegotiation_ceiling_difficulty_id: str | None = None
    # DIAGNOSING-renegotiation audit trail (GitHub #177) — one record per customer
    # decision at the diagnosing_replan round-release gate, keyed by string (decision,
    # note, by, ts, task_replan_count_at_decision). Same plain list[dict] shape as
    # effort_fires above; absent key on legacy states defaults to [] via from_dict's
    # cls(**data).
    renegotiations: list[dict] = field(default_factory=list)
    # coordination host (schema 31): claude|cursor, None until bound.
    runtime_host: str | None = None
    # Stage-6 re-attest carry-forward records (schema 32): rebuilt FROM SCRATCH by
    # every substantive replan (never accumulated across replans — the only cost
    # of a gap is an unnecessary full re-dispatch, never an incorrect PASS), one
    # ReattestStash per stage the replan found PASSED immediately prior. Empty on
    # legacy pre-schema-32 states (absent key -> dataclass default via from_dict),
    # so `dispatch --re-attest` has no observable and falls back to a normal
    # dispatch (fail-closed toward the more expensive, never the cheaper, path).
    reattest_stash: list[ReattestStash] = field(default_factory=list)
    # Permission-grant model custody (schema 36) — grants.py is the sole validation/
    # coverage authority; these fields are the durable record of what it decided.
    # Plain dict-of-list-of-dict, same shape discipline as effort_fires/renegotiations
    # above (no dataclass wrapping): each row is written once and only ever appended
    # to or read, never field-by-field mutated, so a typed dataclass would buy nothing
    # here that from_dict couldn't already give it by leaving the value alone.
    #   runtime_grants            str(stage_index) -> list of RuleGrant/AddDirGrant
    #                              dicts (provenance "runtime") resolve-permission has
    #                              granted for that stage during THIS execution. Keyed
    #                              by index for lookup, but each entry also stamps the
    #                              stage_title it was granted under; a substantive
    #                              replan that renumbers stages re-keys every entry to
    #                              whichever new-snapshot stage shares that title (see
    #                              `_rekey_runtime_grants`, run on every `approve`) —
    #                              zero or several title matches drops the entry rather
    #                              than guessing, since only THAT case (the stage the
    #                              grant was about no longer has a stable identity in
    #                              the new plan) is where silently carrying it forward
    #                              would misattribute it.
    #   approved_grants_sha256    sha256 of the effective (declared+derived) grant set
    #                              hashed at the last successful `approve`, over the
    #                              plan snapshot approve just froze. `stage-grants` only
    #                              emits DERIVED grants when the live plan snapshot
    #                              re-hashes to this value — see grants.py's docstring
    #                              on why a derivation-code change or unapproved plan
    #                              drift must never silently grant something new.
    #   planning_misses           one dict per denial classified NOT covered by the
    #                              stage's effective grant set (asked_user: bool, ts,
    #                              stage_index, tool_name, tool_input digest).
    #   materialization_defects   one dict per denial classified COVERED by the
    #                              effective grant set but denied anyway (grant
    #                              existed, materialization into --settings/--add-dir
    #                              failed) — routes the stage to FAILED -> DIAGNOSING,
    #                              never a re-ask.
    #   settings_drift            one dict per stage whose enumerate_live_settings()
    #                              hash differs between immediately-before-launch and
    #                              immediately-after-exit (a live settings document
    #                              changed underneath the spawn).
    #   cwd_drift                 one dict per Bash denial classified NOT covered at
    #                              its raw transcript text, but WOULD be covered if
    #                              the command were rebased from the transcript's
    #                              recorded cwd back to the stage venue -- a planning
    #                              artifact (the child ran from a subdirectory), not
    #                              a genuine grant-coverage gap. Never asked_user;
    #                              never promoted by the live PERMISSION-REQUEST path
    #                              (that path only ever scans planning_misses).
    # All six are absent on every pre-schema-36 (cwd_drift: pre-schema-39) state
    # (absent key -> dataclass default via from_dict's cls(**data)): {}/None/[]/[]/[]/[]
    # is exactly "no grant activity has ever been recorded", true of every session
    # that predates these fields.
    runtime_grants: dict[str, list[dict]] = field(default_factory=dict)
    approved_grants_sha256: str | None = None
    planning_misses: list[dict] = field(default_factory=list)
    materialization_defects: list[dict] = field(default_factory=list)
    settings_drift: list[dict] = field(default_factory=list)
    cwd_drift: list[dict] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    # Not a dataclass field (ClassVar): the number of history events that were already
    # present when this object was built — what store.stamp_new_history uses to tell a
    # pre-existing event from one appended since. Kept out of the field set so asdict /
    # to_json / `SessionState(**asdict(s))` never see it. __post_init__ sets it per
    # instance, so every construction route (from_json, dataclasses.replace, a direct
    # call with a history) treats its existing events as already-persisted and none of
    # them is ever stamped with a later save time.
    _loaded_history_len: ClassVar[int] = 0

    def __post_init__(self) -> None:
        self._loaded_history_len = len(self.history)
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
        monotone containment of the commit `spec.delivered_stage` delivered (the LITERAL string frozen on that
        stage's Outcome.delivered_head, never re-resolved here) against both
        the local `target` ref and its local `remote/target` remote-tracking
        ref. Before any containment test the check proves the commits are THIS
        task's: a line exactly `Task: <plan task id>` must appear in the
        messages of `delivered_base..delivered_head` (an empty range is red);
        with no frozen `delivered_base` (state frozen by the pre-trailer
        engine) the delivered commit's own message must carry it. The id is
        the plan's [meta].task_id (`plan_task_id`, else read off the plan
        file, else the session's `task_id`) — the id stage commits' trailers
        are written with. Containment is ancestry (`git merge-base
        --is-ancestor`, the fast path) or, when that answers "not an
        ancestor", patch equivalence: no merge commit in `<ref>..<delivered>`
        (git cherry skips merges, so they fail closed) and `git cherry <ref>
        <delivered>` printing no `+` line — the rebase landing
        (land-branch.py is fast-forward only, so a moved trunk forces a
        rebase beforehand). When cherry reports a `+` line, or the range has a
        merge, a squash test runs: the patch-id of the combined diff
        `delivered_base..delivered_head` must equal the patch-id of some
        non-merge commit in `delivered_base..<ref>` (the frozen base, so the
        comparison is constant over time; legacy state without one uses
        `merge-base <ref> <delivered>`).
        Known reds: a rebase or squash whose diff text changed (conflict
        resolution, context drift); an empty delivered commit is not
        distinguished by cherry. Returns (command, refusal): `command` is the exact string a
        caller passes as `bash -c <command>` — it carries its own
        `git -C <repo_root>`, so it needs no cwd/`cd`; `refusal` is set
        instead when the check cannot even be attempted (no repo_root, an
        unknown delivered_stage, or that stage has not yet frozen a delivered
        commit) — the caller must surface this as a legible refusal, never a
        stage failure.

        No network call, no SHA equality, no live `rev-parse`: both ancestry
        and patch equivalence are monotone in a shared trunk (which only ever
        gains commits) and in this frozen commit (which never changes once
        stamped; if it is pruned and gc'd a re-check refuses with exit 97
        rather than answering), so a check that goes
        green cannot later go red without a history rewrite — see CheckKind's
        module comment for the 20-incident background this replaces."""
        if spec.provider != "git":
            return self._render_provider_landed_command(spec)
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
        git = f"git -C {repo_root}"
        err = LANDED_GIT_ERROR_EXIT
        task_id = self._landed_task_id()
        if not task_id:
            return None, (
                "landed check cannot name the task whose commits it proves "
                "(no plan [meta].task_id and no session task_id)"
            )
        trailer = f"Task: {task_id}"
        base = stage.outcome.delivered_base
        if base:
            messages = f"{git} log --format=%B {shlex.quote(f'{base}..{delivered}')}"
            scope = f"{base}..{delivered}"
            merge_base = shlex.quote(base)
        else:
            messages = f"{git} log -1 --format=%B {commit}"
            scope = delivered
            merge_base = f'$({git} merge-base "$R" {commit}) || exit {err}'
        missing = shlex.quote(f"landed check: no line {trailer!r} in the messages of {scope}")
        patch_id = "patch-id --stable | cut -d' ' -f1"
        command = (
            f"t=$({messages}) || exit {err}; "
            f"printf '%s\\n' \"$t\" | grep -qxF -- {shlex.quote(trailer)}; s=$?; "
            f'if [ "$s" -eq 1 ]; then printf \'%s\\n\' {missing} >&2; exit 1; fi; '
            f'[ "$s" -eq 0 ] || exit {err}; '
            f"for R in {target} {remote_target}; do "
            f'{git} merge-base --is-ancestor {commit} "$R"; s=$?; '
            f'[ "$s" -eq 0 ] && continue; '
            f'[ "$s" -eq 1 ] || exit {err}; '
            f'm=$({git} rev-list --merges "$R"..{commit}) || exit {err}; '
            f'if [ -z "$m" ]; then '
            f'c=$({git} cherry "$R" {commit}) || exit {err}; '
            f"printf '%s\\n' \"$c\" | grep -q '^+'; s=$?; "
            f'[ "$s" -eq 1 ] && continue; '
            f'[ "$s" -eq 0 ] || exit {err}; '
            "fi; "
            f"MB={merge_base}; "
            f'd=$({git} diff --no-ext-diff "$MB" {commit}) || exit {err}; '
            f"P=$(set -o pipefail; printf '%s\\n' \"$d\" | {git} {patch_id}) || exit {err}; "
            f'[ -n "$P" ] || exit 1; '
            f"L=$(set -o pipefail; {git} log -p --no-merges --no-ext-diff "
            f"--format='commit %H' \"$MB\"..\"$R\" | {git} {patch_id}) || exit {err}; "
            f"printf '%s\\n' \"$L\" | grep -qxF -- \"$P\"; s=$?; "
            f'[ "$s" -eq 0 ] && continue; '
            f'[ "$s" -eq 1 ] && exit 1; '
            f"exit {err}; "
            "done; exit 0"
        )
        return command, None

    def _render_provider_landed_command(self, spec: "LandedSpec") -> tuple[str | None, str | None]:
        """The landed check of a non-git provider: a call into
        scripts/landed-provider-check.py with the token the provider froze at
        record-result. The CLI maps the provider's True/False/None to 0/1/97.
        Refuses (never a stage failure) when the check cannot even be attempted."""
        try:
            stage = self.stage(spec.delivered_stage)
        except KeyError:
            return None, (
                f"landed check's delivered_stage {spec.delivered_stage} does "
                "not name an existing stage"
            )
        token = stage.outcome.delivered_head
        if not token:
            return None, (
                f"stage {spec.delivered_stage} has not yet frozen a delivery token "
                f"(provider {spec.provider!r}: record-result must run for that stage, "
                "with a resolvable delivery venue and an installed provider plugin, "
                "before this landed check can be evaluated)"
            )
        # The engine's own copy, not repo_root's: the checked plan's repo may be any
        # project, and an unlanded Core delivery has no such script in canon yet.
        script = shlex.quote(str(Path(__file__).resolve().parents[1] / "landed-provider-check.py"))
        return (
            f"python3 {script} --provider={shlex.quote(spec.provider)} "
            f"--token={shlex.quote(token)} --target={shlex.quote(spec.target)}"
        ), None

    def _landed_task_id(self) -> str:
        """The id the landed check's `Task:` trailer must carry: the plan's own
        [meta].task_id. `plan_task_id` is stamped from the plan on every route that
        (re)reads it; a state saved by the engine before that field existed reads the
        plan file instead, and only an unreadable plan falls back to the session's
        `task_id` (set by `agentctl start --task`, bound to the plan by nothing)."""
        if self.plan_task_id:
            return self.plan_task_id
        if self.plan_path:
            from .plan import PlanError, load_plan
            try:
                from_plan = load_plan(self.plan_path, strict=False).meta.task_id
            except (OSError, PlanError):
                from_plan = ""
            if from_plan:
                return from_plan
        return self.task_id

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
        data["plan_stage_reviews"] = {
            scope: r for scope, v in (data.get("plan_stage_reviews") or {}).items()
            if (r := PlanReview.from_dict(v)) is not None
        }
        data["plan_review_passes"] = {
            scope: r for scope, v in (data.get("plan_review_passes") or {}).items()
            if (r := PlanReview.from_dict(v)) is not None
        }
        data.pop("plan_topo_reviews", None)
        data["plan_pair_reviews"] = {
            pair: r for pair, v in (data.get("plan_pair_reviews") or {}).items()
            if (r := PlanPairReview.from_dict(v)) is not None
        }
        data["concern_ledger"] = {
            cid: r for cid, v in (data.get("concern_ledger") or {}).items()
            if (r := ConcernRecord.from_dict(v)) is not None
        }
        data["risk_acceptances"] = [
            r for r in (RiskAcceptance.from_dict(x) for x in data.get("risk_acceptances", [])) if r is not None
        ]
        data["stage_reviews"] = [
            r for r in (StageReview.from_dict(x) for x in data.get("stage_reviews", [])) if r is not None
        ]
        data["judge_bypassed"] = [JudgeBypass(**b) for b in data.get("judge_bypassed", [])]
        data["code_reviews"] = [
            r for r in (CodeReview.from_dict(x) for x in data.get("code_reviews", [])) if r is not None
        ]
        data["reattest_stash"] = [
            r for r in (ReattestStash.from_dict(x) for x in data.get("reattest_stash", [])) if r is not None
        ]
        data["acceptance_review"] = AcceptanceReview.from_dict(data.get("acceptance_review"))
        data["acceptance_bypass"] = AcceptanceBypass.from_dict(data.get("acceptance_bypass"))
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
                effort_estimate=f.get("effort_estimate"),
                effort_baseline=f.get("effort_baseline"),
                effort_actuals=f.get("effort_actuals") or {},
                effort_fires=f.get("effort_fires") or [],
                effort_spend_seen=f.get("effort_spend_seen") or {},
                plan_review_rounds=f.get("plan_review_rounds") or 0,
                plan_review_counted_digest=f.get("plan_review_counted_digest") or "",
                code_review_rounds=f.get("code_review_rounds") or 0,
                parent_repo_root=f.get("parent_repo_root") or "",
                parent_delivery_worktree=f.get("parent_delivery_worktree") or "",
                parent_venue_captured=bool(f.get("parent_venue_captured", False)),
                plugins=f.get("plugins") or {},
                plugins_archive=f.get("plugins_archive") or {},
            )
            for f in data.get("plan_stack", [])
        ]
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "SessionState":
        return cls.from_dict(json.loads(text))
