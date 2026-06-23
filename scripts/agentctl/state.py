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

SCHEMA_VERSION = 2


class Node(str, Enum):
    CLASSIFIED = "CLASSIFIED"
    ROUTED = "ROUTED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    APPROVED = "APPROVED"
    DECOMPOSED = "DECOMPOSED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLUTION = "RESOLUTION"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"


# Nodes at or past EXECUTING on the spawn path — once here, a SPAWN route must
# have recorded its decomposition assessment.
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
class Decomposition:
    """The M1–M4 decomposition assessment recorded between APPROVED and EXECUTING
    on the spawn path. The markers are cognitive inputs; the verdict is computed
    by decompose.verdict()."""
    m1: bool = False
    m2: bool = False
    m3: bool = False
    m4: bool = False
    m3_severe: bool = False
    m4_severe: bool = False
    verdict: str = ""


@dataclass
class Stage:
    index: int
    title: str
    executor: str  # "in_thread" | "spawn:<spec>"
    expected_result_image: str
    criterion_type: str  # CriterionType value
    done_criterion: str
    depends_on: list[int] = field(default_factory=list)
    output_artifacts: list[str] = field(default_factory=list)
    actual: str | None = None
    status: str = StageStatus.PENDING.value
    fail_digests: list[str] = field(default_factory=list)

    def is_spawn(self) -> bool:
        return self.executor.startswith("spawn:")

    def spawn_kind(self) -> str | None:
        return self.executor.split(":", 1)[1] if self.is_spawn() else None


@dataclass
class SessionState:
    session_id: str
    task_id: str
    goal: str = ""
    overall_done_criterion: str = ""
    overall_criterion_type: str = CriterionType.MEASURABLE.value
    weight_class: str | None = None
    route: str | None = None
    node: str = Node.CLASSIFIED.value
    blocked_from: str | None = None
    plan_path: str | None = None
    plan_verified: bool = False
    decomposition: "Decomposition | None" = None
    approval: GateRecord = field(default_factory=lambda: GateRecord("plan_approval"))
    resolution: GateRecord = field(default_factory=lambda: GateRecord("resolution"))
    stages: list[Stage] = field(default_factory=list)
    current_stage: int | None = None
    recursion_depth: int = 0
    artifacts: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
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
            if any(s.status != StageStatus.PASSED.value for s in self.stages):
                raise InvariantError("node=RESOLVED requires every stage PASSED")
        if self.route == Route.SPAWN.value and self.weight_class != WeightClass.SUBSTANTIVE.value:
            raise InvariantError("route=SPAWN requires weight_class=SUBSTANTIVE")
        if (
            self.route == Route.SPAWN.value
            and self.node in _EXECUTION_NODES
            and self.decomposition is None
        ):
            raise InvariantError(
                "route=SPAWN at or past EXECUTING requires decomposition (run decompose)"
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
        passed = {s.index for s in self.stages if s.status == StageStatus.PASSED.value}
        out = []
        for s in self.stages:
            if s.status == StageStatus.PENDING.value and all(d in passed for d in s.depends_on):
                out.append(s)
        return out

    def all_stages_passed(self) -> bool:
        return bool(self.stages) and all(
            s.status == StageStatus.PASSED.value for s in self.stages
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
        data["stages"] = [Stage(**s) for s in data.get("stages", [])]
        decomp = data.get("decomposition")
        data["decomposition"] = Decomposition(**decomp) if decomp else None
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "SessionState":
        return cls.from_dict(json.loads(text))
