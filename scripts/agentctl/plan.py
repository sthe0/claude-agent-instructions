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

    [[stage]]
    index = 1
    title = "Scaffold package"
    executor = "in_thread"               # or "spawn:developer"
    expected_result_image = "package imports, status runs on empty state"
    criterion_type = "measurable"
    done_criterion = "python3 -m agentctl status exits 0"
    depends_on = []                       # optional
    output_artifacts = ["scripts/agentctl/"]  # optional

For substantive plans (meta.weight_class = "substantive") every stage must also
carry the 8-element activity-structure fields:

    material = "..."
    means = "..."
    method = "..."
    conditions = "..."
    invariants = "..."
    capability_required = "..."          # optional even for substantive

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

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .state import CriterionType, Stage, StageStatus


@dataclass
class PlanMeta:
    task_id: str
    goal: str = ""
    done_criterion: str = ""
    criterion_type: str = CriterionType.MEASURABLE.value
    weight_class: str | None = None


@dataclass
class PlanDoc:
    meta: PlanMeta
    stages: list[Stage] = field(default_factory=list)


class PlanError(Exception):
    """The TOML plan is missing required structure."""


# Extra stage fields required for substantive plans (8-element activity structure).
_SUBSTANTIVE_STAGE_FIELDS = ("material", "means", "method", "conditions", "invariants")
_PRINCIPLE_SUBFIELDS = ("statement", "source", "confidence", "refutation")


def _validate_substantive_stage(s: dict, index: int) -> None:
    """Raise PlanError if a substantive stage is missing any activity-structure field."""
    for field_name in _SUBSTANTIVE_STAGE_FIELDS:
        if not s.get(field_name):
            raise PlanError(
                f"stage {index} missing {field_name!r} (required for substantive plans)"
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


def parse_plan(data: dict) -> PlanDoc:
    """Pure: a parsed-TOML dict -> PlanDoc. No filesystem."""
    if "meta" not in data:
        raise PlanError("plan missing [meta] table")
    m = data["meta"]
    if not m.get("task_id"):
        raise PlanError("[meta] missing task_id")
    raw_weight = m.get("weight_class")
    meta = PlanMeta(
        task_id=str(m["task_id"]),
        goal=str(m.get("goal", "")),
        done_criterion=str(m.get("done_criterion", "")),
        criterion_type=str(m.get("criterion_type", CriterionType.MEASURABLE.value)),
        weight_class=str(raw_weight) if raw_weight is not None else None,
    )

    raw_stages = data.get("stage", [])
    if not raw_stages:
        raise PlanError("plan defines no [[stage]] entries")

    is_substantive = meta.weight_class is not None and meta.weight_class.lower() == "substantive"

    stages: list[Stage] = []
    for i, s in enumerate(raw_stages, start=1):
        index = int(s.get("index", i))
        for required in ("title", "executor", "expected_result_image", "done_criterion"):
            if not s.get(required):
                raise PlanError(f"stage {index} missing {required!r}")
        if is_substantive:
            _validate_substantive_stage(s, index)
        stages.append(
            Stage(
                index=index,
                title=str(s["title"]),
                executor=str(s["executor"]),
                expected_result_image=str(s["expected_result_image"]),
                criterion_type=str(s.get("criterion_type", CriterionType.MEASURABLE.value)),
                done_criterion=str(s["done_criterion"]),
                depends_on=[int(d) for d in s.get("depends_on", [])],
                output_artifacts=[str(a) for a in s.get("output_artifacts", [])],
                status=StageStatus.PENDING.value,
            )
        )

    indices = [s.index for s in stages]
    if len(set(indices)) != len(indices):
        raise PlanError(f"duplicate stage indices: {indices}")
    return PlanDoc(meta=meta, stages=stages)


def load_plan(path: str | Path) -> PlanDoc:
    p = Path(path)
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_plan(data)


def _structural_signature(doc: PlanDoc) -> dict:
    """The fields whose change makes a replan substantive."""
    return {
        "done_criterion": doc.meta.done_criterion,
        "criterion_type": doc.meta.criterion_type,
        "weight_class": doc.meta.weight_class,
        "stages": {
            s.index: (s.executor, tuple(sorted(s.depends_on)), s.done_criterion, s.criterion_type)
            for s in doc.stages
        },
    }


def diff_plans(old: PlanDoc, new: PlanDoc) -> str:
    """Return 'no_change' | 'refinement' | 'substantive'."""
    if _structural_signature(old) != _structural_signature(new):
        return "substantive"
    # structurally identical — any prose change (titles, expected-result images) is a refinement
    old_prose = [(s.index, s.title, s.expected_result_image) for s in old.stages]
    new_prose = [(s.index, s.title, s.expected_result_image) for s in new.stages]
    if old_prose != new_prose or old.meta.goal != new.meta.goal:
        return "refinement"
    return "no_change"
