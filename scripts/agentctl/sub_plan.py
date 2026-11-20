"""The executor's sub-plan record: what a stage's executor sets down in answer to the stage
description, kept apart from the description itself.

Difficulty removed: a stage used to hold the order (the product, its place, the checks) and the
means for it in one record, so the planner decided the method for an executor who knew the
material better, and the executor's own plan had no record of its own. The stage description
(`state.Stage.place` and the fields around it) now states only the result; this record is the
executor's side: its means, method and procedure, the conditions on the means with their securing
marks, one estimate, the plans compared with the departure from them, how each binding condition
the stage carries is kept, and the setter's decision on each.

Two carriers hold the same record: a frame pushed by `push-subplan` (the record sits in the
session that builds the plan, beside the parent's stages), and a linked child's own contour (the
record sits in the child's session). `read` is the one reader over both, so the parent reads a
record the same way at every level and no check knows which carrier it is looking at.

Pure: no file or process I/O. The parse is total (a malformed value degrades to its empty form
and is named in `malformed`); what a record must carry is the submission seam's grade
(`submission._stage_order_violations`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

SETTER_VALUES = ("kept", "changed", "withdrawn")

# Entries a digest of the record leaves out: a review or an objection is a record ABOUT the
# sub-plan, and counting one as a change to it would reopen the plan each time it was reviewed.
DIGEST_EXCLUDED = ("reviews", "objections")


class SubPlanError(Exception):
    """A write the record refuses."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rows(value, malformed: list[str], name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        malformed.append(name)
        return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []
    return value


@dataclass
class Estimate:
    """The one estimate of the sub-plan: the executor's figure and who confirmed it."""
    minutes: float | None = None
    usd: float | None = None
    author: str = ""
    confirmer: str = ""

    @property
    def agreed(self) -> bool:
        return bool(self.confirmer)

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(minutes=_number(d.get("minutes")), usd=_number(d.get("usd")),
                   author=_text(d.get("author")), confirmer=_text(d.get("confirmer")))


@dataclass
class ComparedPlans:
    """The plans this one was compared with. Either entries ({ref, departure}: the plan
    compared and how this one departs from it) or `none` with the id of the search that found
    nothing to compare; the id must resolve in the session that carries the record."""
    entries: list[dict] = field(default_factory=list)
    none: bool = False
    search_id: str = ""

    @classmethod
    def from_dict(cls, d: dict, malformed: list[str]) -> "ComparedPlans":
        entries = [{"ref": _text(r.get("ref")), "departure": _text(r.get("departure"))}
                   for r in _rows(d.get("entries"), malformed, "compared_plans.entries")]
        return cls(entries=entries, none=bool(d.get("none", False)),
                   search_id=_text(d.get("search_id")))


@dataclass
class HowKept:
    """How the plan keeps one binding condition of the stage."""
    condition: str = ""
    how: str = ""


@dataclass
class SetterDecision:
    """The decision on one binding condition: kept, changed or withdrawn, by the setter's
    leave or the user's. `objection` names the in-flight objection the decision answers and is
    empty for a decision that answers none."""
    condition: str = ""
    value: str = ""
    setter: str = ""
    decided_by: str = ""
    objection: str = ""


@dataclass
class BuildingPlace:
    """Where the sub-plan was built: the session, and for a frame the interval of history
    entries between the push and the pop (`popped` None while the frame is open)."""
    session: str = ""
    pushed: int | None = None
    popped: int | None = None


@dataclass
class SubPlanRecord:
    stage: int = 0
    means: str = ""
    method: str = ""
    procedure: str = ""
    conditions: str = ""
    estimate: Estimate | None = None
    compared_plans: ComparedPlans | None = None
    how_kept: list[HowKept] = field(default_factory=list)
    setter_decisions: list[SetterDecision] = field(default_factory=list)
    building_place: BuildingPlace | None = None
    reviews: list[dict] = field(default_factory=list)
    objections: list[dict] = field(default_factory=list)
    resource_request: list[dict] = field(default_factory=list)
    parent_resource_approval: str = ""
    user_subplan_approval: str = ""
    malformed: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubPlanRecord":
        """Total: every malformation degrades to the empty form and is named in `malformed`."""
        malformed: list[str] = []
        raw_stage = d.get("stage")
        stage = raw_stage if isinstance(raw_stage, int) and not isinstance(raw_stage, bool) else 0
        if raw_stage is not None and not stage:
            malformed.append("stage")
        raw_estimate = d.get("estimate")
        estimate = Estimate.from_dict(raw_estimate) if isinstance(raw_estimate, dict) else None
        if raw_estimate is not None and estimate is None:
            malformed.append("estimate")
        raw_compared = d.get("compared_plans")
        compared = (ComparedPlans.from_dict(raw_compared, malformed)
                    if isinstance(raw_compared, dict) else None)
        if raw_compared is not None and compared is None:
            malformed.append("compared_plans")
        raw_place = d.get("building_place")
        place = None
        if isinstance(raw_place, dict):
            pushed, popped = raw_place.get("pushed"), raw_place.get("popped")
            place = BuildingPlace(
                session=_text(raw_place.get("session")),
                pushed=pushed if isinstance(pushed, int) else None,
                popped=popped if isinstance(popped, int) else None)
        elif raw_place is not None:
            malformed.append("building_place")
        return cls(
            stage=stage,
            means=_text(d.get("means")), method=_text(d.get("method")),
            procedure=_text(d.get("procedure")), conditions=_text(d.get("conditions")),
            estimate=estimate, compared_plans=compared,
            how_kept=[HowKept(_text(r.get("condition")), _text(r.get("how")))
                      for r in _rows(d.get("how_kept"), malformed, "how_kept")],
            setter_decisions=[
                SetterDecision(_text(r.get("condition")), _text(r.get("value")),
                               _text(r.get("setter")), _text(r.get("decided_by")),
                               _text(r.get("objection")))
                for r in _rows(d.get("setter_decisions"), malformed, "setter_decisions")],
            building_place=place,
            reviews=list(_rows(d.get("reviews"), malformed, "reviews")),
            objections=list(_rows(d.get("objections"), malformed, "objections")),
            resource_request=list(_rows(d.get("resource_request"), malformed, "resource_request")),
            parent_resource_approval=_text(d.get("parent_resource_approval")),
            user_subplan_approval=_text(d.get("user_subplan_approval")),
            malformed=tuple(malformed),
        )


def from_meta(meta: dict) -> SubPlanRecord | None:
    """The record a plan's `[meta]` table carries: `[meta.sub_plan]` with `[meta.compared_plans]`
    and `[[meta.how_kept]]` beside it. None when the plan has no `[meta.sub_plan]`."""
    raw = meta.get("sub_plan")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return SubPlanRecord(malformed=("sub_plan",))
    merged = dict(raw)
    for key in ("compared_plans", "how_kept"):
        if key in meta:
            merged[key] = meta[key]
    return SubPlanRecord.from_dict(merged)


def digest(record: SubPlanRecord) -> str:
    """Identity of the record as a plan: a change to any authored part changes it; a review or
    an objection added to it does not."""
    body = {k: v for k, v in record.to_dict().items() if k not in DIGEST_EXCLUDED}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def agree_estimate(record: SubPlanRecord, estimate: Estimate) -> bool:
    """Set the record's estimate. Once agreed, the same figure written again changes nothing
    (False) and a different figure is refused: the confirmer agreed to that figure."""
    current = record.estimate
    if current is not None and current.agreed:
        if (current.minutes, current.usd) == (estimate.minutes, estimate.usd):
            return False
        raise SubPlanError(
            f"the estimate was agreed as {current.minutes} min / {current.usd} usd by "
            f"{current.confirmer}; a different figure needs a new agreement, not a rewrite")
    record.estimate = estimate
    return True


def is_decided_later(stage) -> bool:
    """A stage whose sub-plan is decided when the stage is reached is the stage `lazy` marks."""
    return bool(stage.lazy)


def record_key(stage_index: int, level: int | None = None) -> str:
    """Where a record sits in `SessionState.sub_plan_records`. A record a frame carries is
    keyed by the stack level of the plan whose stage it describes (`level`) beside the stage
    number, so a frame nested in a frame does not overwrite the record of the same stage number
    one level up; the record a child contour carries describes the parent session's stage and
    is keyed by the stage number alone (`level` None)."""
    return str(stage_index) if level is None else f"{level}:{stage_index}"


def store(state, record: SubPlanRecord, level: int | None = None) -> None:
    """Keep `record` in `state` under its parent stage (see `record_key`); a stored record's
    reviews and objections survive a rewrite of the authored parts."""
    key = record_key(record.stage, level)
    previous = state.sub_plan_records.get(key)
    if previous:
        kept = SubPlanRecord.from_dict(previous)
        record.reviews = record.reviews or kept.reviews
        record.objections = record.objections or kept.objections
        record.building_place = record.building_place or kept.building_place
    state.sub_plan_records[key] = record.to_dict()


def frame_push_position(state, stage_index: int) -> int | None:
    """The history position of the push that opened the frame building `stage_index`: the
    newest `push_subplan` entry naming it, or None when no frame was pushed for it."""
    for position in range(len(state.history) - 1, -1, -1):
        entry = state.history[position]
        if entry.get("event") == "push_subplan" and entry.get("originating_stage") == stage_index:
            return position
    return None


def record_carried_by(state, doc) -> None:
    """Store the record a submitted plan carries, with the place it was built: this session,
    and, when the plan runs in a pushed frame, the interval that opens at the push. A plan
    carrying no record stores nothing."""
    record = doc.meta.sub_plan
    if record is None or record.stage <= 0:
        return
    level = len(state.plan_stack) - 1 if state.plan_stack else None
    pushed = frame_push_position(state, record.stage) if state.plan_stack else None
    previous = state.sub_plan_records.get(record_key(record.stage, level)) or {}
    place = previous.get("building_place")
    if pushed is not None and not (place and place.get("pushed") == pushed):
        record.building_place = BuildingPlace(session=state.session_id, pushed=pushed)
    elif pushed is None and state.parent_link:
        record.building_place = BuildingPlace(session=state.session_id)
    store(state, record, level)


def close_frame_interval(state, stage_index: int, popped: int) -> None:
    """End the building-place interval of the record for `stage_index` at history position
    `popped`: the frame that built it has been popped, so the plan whose stage it describes
    is the current one again."""
    raw = state.sub_plan_records.get(record_key(stage_index, len(state.plan_stack)))
    place = (raw or {}).get("building_place")
    if place and place.get("popped") is None and place.get("session") == state.session_id:
        place["popped"] = popped


def add_review(state, stage_index: int, review: dict, level: int | None = None) -> None:
    """Add a review to the record of `stage_index` (see `record_key`); no other stage's record
    is touched."""
    key = record_key(stage_index, level)
    if key not in state.sub_plan_records:
        raise SubPlanError(f"no sub-plan record for stage {stage_index}")
    state.sub_plan_records[key].setdefault("reviews", []).append(dict(review))


def linked_children(state, store_) -> list:
    """The sessions linked directly below `state` (empty without a store). Loads the store's
    sessions, so a caller that reads several stages takes this once and passes it to `read`."""
    if store_ is None:
        return []
    from .cli_common import _linked_children
    return _linked_children(store_, state.session_id)


def read(state, stage_index: int, store_=None, children=None) -> SubPlanRecord | None:
    """The record of stage `stage_index` of the plan `state` is running, wherever its carrier
    is: a frame this session pushed (keyed by the plan's own stack level) or the contour of the
    child linked at that stage (`parent_link.stage_index`), whose own record describes the
    stage it was linked at. Each level is read only for the stages of its own plan: a record a
    deeper session holds under the same stage number describes that session's plan, not this
    one. `children` are the linked sessions when the caller already has them. A stage with
    records in the contours of more than one linked child has no one record to read:
    `SubPlanError` names the sessions, never a silent pick of the first."""
    raw = state.sub_plan_records.get(record_key(stage_index, len(state.plan_stack)))
    if raw:
        return SubPlanRecord.from_dict(raw)
    if children is None:
        children = linked_children(state, store_)
    found = [(child.session_id, child.sub_plan_records[record_key(stage_index)])
             for child in children
             if (child.parent_link or {}).get("stage_index") == stage_index
             and child.sub_plan_records.get(record_key(stage_index))]
    if len(found) > 1:
        raise SubPlanError(f"stage {stage_index} has a sub-plan record in the contours of "
                           f"{len(found)} linked children ({', '.join(s for s, _ in found)}): "
                           f"no one record to read")
    return SubPlanRecord.from_dict(found[0][1]) if found else None


RELEASING_VALUES = ("changed", "withdrawn")


def parent_set_ids(conditions, carried_above) -> frozenset[str]:
    """The ids, among the binding conditions `conditions` stated on the stage a child builds,
    that the parent level set itself: the ones that level does not itself carry from above
    (`carried_above`, the conditions of the stage the parent level builds). Decided by id, never
    by the label a condition carries: a level that restates an inherited condition under its own
    name has not set it."""
    above = {c["id"] for c in carried_above or ()}
    return frozenset(c["id"] for c in conditions or () if c["id"] not in above)


def released_ids(record: SubPlanRecord, conditions, carried_above) -> frozenset[str]:
    """The conditions `record`, the record of the parent level, changed or withdrew among those
    that level set itself (`parent_set_ids`): a decision on a condition it carries from above
    releases nothing."""
    own = parent_set_ids(conditions, carried_above)
    return frozenset(d.condition for d in record.setter_decisions
                     if d.value in RELEASING_VALUES and d.condition in own)
