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
from dataclasses import asdict, dataclass, field
from enum import Enum

SCHEMA_VERSION = 6


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


class StageStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PASSED = "PASSED"
    FAILED = "FAILED"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


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
    replans exactly as before."""
    functional_ground: str
    replanning_task: str
    invariants_to_preserve: list[str] = field(default_factory=list)
    differences_to_remove: list[str] = field(default_factory=list)


@dataclass
class Difficulty:
    """One active difficulty: a plan-vs-reality divergence being worked through.
    `complete()` is the precondition `replan` checks (see gates.difficulty_blockers)."""
    declaration: Declaration | None = None
    investigation: Investigation | None = None
    critique: Critique | None = None

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
        return cls(
            declaration=Declaration(**decl) if decl else None,
            investigation=Investigation(**inv) if inv else None,
            critique=Critique(**crit) if crit else None,
        )


# --- the 8 activity elements, grouped by the ontology's clusters -------------
# Each cluster is a typed sub-structure of Stage; the grouping makes the model
# self-documenting and splits the immutable DECLARATION (subject/means/actor/
# criterion/principle/conditions) from the mutable execution RECORD (outcome).
@dataclass
class Subject:
    """The material worked on and the result image it should become."""
    material: str
    result: str
    invariants: str | None = None


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
    command (the default) the engine keeps its flag-only behaviour."""
    criterion_type: str  # CriterionType value
    done_criterion: str
    verify_command: str | None = None
    expected_exit: int = 0


@dataclass
class Principle:
    """The refutable principle the stage rests on (confidence is a Confidence value)."""
    statement: str
    source: str
    confidence: str
    refutation: str


@dataclass
class Supply:
    """A typed provision edge: stage `on` supplies `element` (optionally a named
    `artifact`) to the stage that owns this Supply. The SOLE source of stage
    edges — Stage.depends_on is a derived projection over these."""
    on: int
    element: str | None = None
    artifact: str | None = None


@dataclass
class Outcome:
    """The mutable execution record — distinct from the immutable declaration."""
    status: str = StageStatus.PENDING.value
    actual: str | None = None
    fail_digests: list[str] = field(default_factory=list)


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
    supplies: list[Supply] = field(default_factory=list)
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
                criterion=Criterion(**d["criterion"]),
                principle=Principle(**d["principle"]) if d.get("principle") else None,
                conditions=d.get("conditions"),
                supplies=[Supply(**s) for s in d.get("supplies", [])],
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
            ),
            principle=None,  # flat states predate the principle element
            conditions=d.get("conditions"),
            supplies=[Supply(on=int(x)) for x in d.get("depends_on", [])],
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
    route: str | None = None
    node: str = Node.CLASSIFIED.value
    blocked_from: str | None = None
    plan_path: str | None = None
    plan_verified: bool = False
    partition: "Partition | None" = None
    permission_request: "PermissionRequest | None" = None
    difficulty: "Difficulty | None" = None
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
        data.setdefault("plugins", {})            # migration: schema <=5 has no plugin layer
        data.setdefault("plugins_archive", {})
        data["stages"] = [Stage.from_dict(s) for s in data.get("stages", [])]
        decomp = data.get("partition")
        data["partition"] = Partition(**decomp) if decomp else None
        pr = data.get("permission_request")
        data["permission_request"] = PermissionRequest(**pr) if pr else None
        data["difficulty"] = Difficulty.from_dict(data.get("difficulty"))
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "SessionState":
        return cls.from_dict(json.loads(text))
