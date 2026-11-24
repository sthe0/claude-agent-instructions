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
    """The one estimate of the sub-plan: the executor's figure with the basis it rests on, and
    who confirmed it. `confirmer` is the executor's own confirmation of the record; the parent's
    agreement lives in the parent-side copy (`estimate_copy`), never here. `corrections` is the
    parent-side copy's list, mirrored for readers of the record: the record cannot write it
    (`confirm_estimate`)."""
    minutes: float | None = None
    usd: float | None = None
    author: str = ""
    confirmer: str = ""
    basis: str = ""
    corrections: list[dict] = field(default_factory=list)

    @property
    def agreed(self) -> bool:
        return bool(self.confirmer)

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        raw = d.get("corrections")
        return cls(minutes=_number(d.get("minutes")), usd=_number(d.get("usd")),
                   author=_text(d.get("author")), confirmer=_text(d.get("confirmer")),
                   basis=_text(d.get("basis")),
                   corrections=[dict(r) for r in raw if isinstance(r, dict)]
                   if isinstance(raw, list) else [])


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


# What a user's approval of a sub-plan covers. The estimate is the agreement of its author and
# confirmer, a review or an objection is a record about the plan, and the approval fields are the
# engine's own: none of them is part of the plan the user approved.
APPROVAL_EXCLUDED = DIGEST_EXCLUDED + (
    "building_place", "setter_decisions", "malformed", "estimate",
    "parent_resource_approval", "user_subplan_approval")


def approval_digest(record: SubPlanRecord) -> str:
    """The identity of the sub-plan the user approves: the same for the record a frame carries
    and the record a child contour carries (the place it was built is not part of it)."""
    body = {k: v for k, v in record.to_dict().items() if k not in APPROVAL_EXCLUDED}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _dir_entry(item) -> tuple[str, str]:
    if isinstance(item, dict):
        return (str(item.get("path", "")), str(item.get("mode", "")))
    if isinstance(item, (list, tuple)):
        return (str(item[0]) if item else "", str(item[1]) if len(item) > 1 else "")
    return (str(item), "")


def request_parts(rows) -> dict:
    """The allow rules and added directories a resource request names, normalized and sorted:
    the only parts of a request the parent approves."""
    allow: set[str] = set()
    dirs: set[tuple[str, str]] = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        allow.update(str(r) for r in (row.get("allow") or ()) if isinstance(r, str))
        dirs.update(_dir_entry(d) for d in (row.get("add_dirs") or ()))
    return {"allow": sorted(allow), "add_dirs": [list(d) for d in sorted(dirs)]}


def resource_request_digest(rows) -> str:
    return hashlib.sha256(json.dumps(request_parts(rows), sort_keys=True).encode()).hexdigest()


def _dir_within(path: str, mode: str, approved: list[list[str]]) -> bool:
    return any(mode == m and (path == p or path.startswith(p.rstrip("/") + "/"))
               for p, m in approved)


def request_within(rows, approved: dict) -> list[str]:
    """Why `rows` is not inside the approved set `{allow, add_dirs}` (request_parts' shape):
    the one in-set test the agent's approval of a stage's resources applies; [] when inside."""
    live = request_parts(rows)
    approved_dirs = [list(d) for d in approved.get("add_dirs") or ()]
    out = [f"allow rule {rule!r} is outside the approved set"
           for rule in live["allow"] if rule not in set(approved.get("allow") or ())]
    out += [f"added directory {path!r} ({mode or 'no mode'}) is outside the approved set"
            for path, mode in live["add_dirs"] if not _dir_within(path, mode, approved_dirs)]
    return out


def approval_key(state, stage_index: int) -> str:
    """Where the approvals of the stage `stage_index` of the plan `state` is running sit in
    `SessionState.stage_approvals`: the stack level of that plan beside the stage number, as
    `read` keys a frame's record."""
    return f"{len(state.plan_stack)}:{stage_index}"


def approval_entry(state, stage_index: int) -> dict:
    """The engine's last-written approvals of one stage (empty when it wrote none)."""
    return state.stage_approvals.get(approval_key(state, stage_index)) or {}


def write_parent_approval(state, stage_index: int, rows, *, widens_set: bool = True) -> dict:
    """Record the parent's approval of the resources and permissions `rows` name for a stage.
    The user's approval (`widens_set`) also sets the approved set later requests are tested
    against; an agent's approval inside that set leaves it as it was."""
    entry = state.stage_approvals.setdefault(approval_key(state, stage_index), {})
    entry["parent_resource_approval"] = resource_request_digest(rows)
    if widens_set:
        entry["approved"] = request_parts(rows)
    return entry


def write_user_approval(state, stage_index: int, record: SubPlanRecord) -> dict:
    """Record the user's approval of the presented sub-plan `record`: its digest, with the
    digest and the content of the request it carries."""
    entry = state.stage_approvals.setdefault(approval_key(state, stage_index), {})
    entry["user_subplan_approval"] = approval_digest(record)
    entry["user_subplan_request"] = resource_request_digest(record.resource_request)
    entry["user_subplan_set"] = request_parts(record.resource_request)
    entry["user_subplan_estimate"] = entry.get("presented_estimate") or estimate_digest(record.estimate)
    return entry


def write_presented_estimate(state, stage_index: int, estimate: Estimate | None) -> None:
    """Record the estimate of the stage as it stood when a presentation showed it to the user: the
    estimate's own digest (a record's estimate is outside the sub-plan's approval digest), which
    every act that agrees the stage's first figure is checked against (`shown_estimate`). A
    presentation that showed no estimate clears the digest of an earlier one."""
    if estimate is None:
        state.stage_approvals.get(approval_key(state, stage_index), {}).pop("presented_estimate", None)
        return
    state.stage_approvals.setdefault(approval_key(state, stage_index), {})[
        "presented_estimate"] = estimate_digest(estimate)


def shown_estimate(state, stage) -> str | None:
    """The digest of the estimate the user saw for `stage` and an agreement of its first figure
    must equal: for a decided-later stage the one in the sub-plan the user approved, else the one
    last presented; None when the stage was never presented (nothing was shown to bind to)."""
    entry = approval_entry(state, stage.index)
    return entry.get("user_subplan_estimate" if is_decided_later(stage) else "presented_estimate")


def set_planning_mark(state, stage_index: int, planning: bool) -> None:
    """Mark (or clear) the stage as dispatched only to be planned. Clearing writes nothing for a
    stage that never carried the mark."""
    key = approval_key(state, stage_index)
    if planning:
        state.stage_approvals.setdefault(key, {})["planning"] = True
    elif key in state.stage_approvals:
        state.stage_approvals[key]["planning"] = False


# --- The parent-side copy of a stage's estimate ------------------------------------------------
#
# The agreed figure, its basis, the corrections and the figure the user approved sit in the engine
# state of the session that ordered the stage, inside that stage's `stage_approvals` entry under
# `estimates[<order revision>]`, and only that session's engine writes them: the agreeing acts of
# `cli_approval` and the `estimate-correct` verb. The executor's record carries the same figure
# for readers, but a figure in the record prices nothing until the copy agrees it. An entry of an
# earlier order revision stays as a record and counts for nothing (`estimate_copy`).

AUTHORITY_USER = "user"
AUTHORITY_AGENT = "agent"
AUTHORITY_PARENT = "parent"
AUTHORITY_TIER = "size-class-tier-price"
TIER_BASIS = "size-class tier price"


def order_revision(state) -> int:
    """The revision of the order the copy is keyed by: the number of agreements it has replaced."""
    return len(state.order_agreement_history)


def estimate_copy(state, stage_index: int, level: int | None = None) -> dict:
    """The current order revision's copy of the estimate of stage `stage_index` of the plan at
    stack level `level` (the plan the session is running when None); empty when none counts."""
    key = approval_key(state, stage_index) if level is None else f"{level}:{stage_index}"
    estimates = (state.stage_approvals.get(key) or {}).get("estimates") or {}
    return estimates.get(str(order_revision(state))) or {}


def _copy_slot(state, stage_index: int, level: int | None = None) -> dict:
    key = approval_key(state, stage_index) if level is None else f"{level}:{stage_index}"
    entry = state.stage_approvals.setdefault(key, {})
    return entry.setdefault("estimates", {}).setdefault(str(order_revision(state)), {})


def figure_of(minutes, usd) -> dict:
    """A figure as the copy keeps it: both scales, None where the estimate names none."""
    return {"minutes": _number(minutes), "usd": _number(usd)}


def figure_args(figure: dict) -> str:
    """The `--minutes` / `--usd` arguments of `estimate-correct` for `figure`, leaving out a scale
    it names none of."""
    return " ".join(f"--{flag} {figure[key]}" for flag, key in (("minutes", "minutes"), ("usd", "usd"))
                    if figure.get(key) is not None)


def agreed_figure(state, stage_index: int) -> dict | None:
    """The figure the parent agreed for the stage, None while none is agreed (a held or merely
    confirmed figure prices nothing)."""
    copy = estimate_copy(state, stage_index)
    return figure_of(copy.get("minutes"), copy.get("usd")) if copy.get("agreed") else None


def user_figure(state, stage_index: int) -> dict | None:
    """The figure the user approved for the stage, None when the user decided none."""
    return estimate_copy(state, stage_index).get("user_approved_figure")


def held_figure(state, stage_index: int) -> dict | None:
    """The figure proposed past the allowed overrun and waiting for the user's decision."""
    return estimate_copy(state, stage_index).get("held")


def write_confirmed_copy(state, stage_index: int, estimate: Estimate,
                         level: int | None = None) -> dict:
    """Record a child contour's estimate as read through the link: confirmed by its executor,
    not agreed, so no budget follows. An agreed copy is left as it stands."""
    slot = _copy_slot(state, stage_index, level)
    if not slot.get("agreed"):
        slot.update(figure_of(estimate.minutes, estimate.usd), basis=estimate.basis,
                    author=estimate.author, agreed=False)
        slot.setdefault("corrections", [])
    return slot


def agree_copy(state, stage_index: int, *, minutes, usd, basis: str, author: str, agreer: str,
               authority: str, user_approved: dict | None = None) -> dict:
    """The parent's agreement of a figure: it prices the stage from now on. `user_approved` is the
    figure the user approved when the act is the user's (it sets the user line); an agent's or a
    parent's agreement never moves it."""
    slot = _copy_slot(state, stage_index)
    slot.update(figure_of(minutes, usd), basis=basis, author=author, agreer=agreer,
                authority=authority, agreed=True, held=None)
    slot.setdefault("corrections", [])
    if user_approved is not None:
        slot["user_approved_figure"] = dict(user_approved)
    return slot


def hold_figure(state, stage_index: int, *, minutes, usd, basis: str, author: str,
                at: float = 0.0, escalated: bool = False) -> dict:
    """Keep a figure past the allowed overrun beside the copy, unapplied, until the user decides.
    `at` is when it was held: the user's decision is an answer given after it. `escalated` marks a
    figure a linked child could not put to the user: it is held in the parent-side copy as well, so
    the dispatch gate refuses the stage until the child's parent decides."""
    slot = _copy_slot(state, stage_index)
    slot["held"] = {**figure_of(minutes, usd), "basis": basis, "author": author, "at": at}
    if escalated:
        slot["held"]["escalated"] = True
    slot.setdefault("corrections", [])
    return slot


def drop_held(state, stage_index: int) -> None:
    """Forget the figure held for the user, unapplied: the user's decision to keep the agreed
    figure (`fire-acknowledge --decision continue`) declines it."""
    _copy_slot(state, stage_index)["held"] = None


def mark_frozen_in(state, stage_index: int, contributes: dict) -> None:
    """Record what the task estimate the user approved (`order_effort_frozen`) already holds of
    the stage: the composed estimate then counts only a figure's change since."""
    _copy_slot(state, stage_index)["frozen_in"] = dict(contributes)


def clear_frozen_in(state, stage_index: int) -> None:
    """Forget what a task estimate held of the stage: the estimate about to be frozen is built
    afresh, so no earlier freeze's figures are in it."""
    estimate_copy(state, stage_index).pop("frozen_in", None)


def add_correction(state, stage_index: int, correction: dict) -> None:
    """Append a correction to the copy: the one writer of `corrections` (`estimate-correct`)."""
    _copy_slot(state, stage_index).setdefault("corrections", []).append(dict(correction))


def estimate_digest(estimate: Estimate | None) -> str:
    """The digest of a figure and its basis: what an agreement is checked against."""
    if estimate is None:
        return ""
    text = json.dumps([estimate.minutes, estimate.usd, estimate.basis.strip()])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copies_of(state) -> dict:
    """The current copy of each stage's estimate of the plan `state` is running, by stage number."""
    return {stage.index: estimate_copy(state, stage.index) for stage in state.stages}


def consumed_answers(state, stage_index: int) -> list[float]:
    """The ledger answers (their times) that already licensed a decision of the stage, under any
    order revision: an answer is the user's one act, and a re-agreed order does not return it."""
    return list((state.stage_approvals.get(approval_key(state, stage_index)) or {})
                .get("answers_used") or [])


def consume_answer(state, stage_index: int, at: float) -> None:
    state.stage_approvals.setdefault(approval_key(state, stage_index), {}) \
        .setdefault("answers_used", []).append(at)


def confirm_estimate(record: SubPlanRecord, writer: str, parent_copy: dict | None) -> None:
    """The guard every write of a record's estimate passes (`record_carried_by` is the one writer,
    for a frame's record and a child contour's alike). A structural check, not a judgement of the
    figure: an estimate with no basis is refused. Before the parent agreed a figure the write
    replaces figure and basis freely and names the writer as author and confirmer; once agreed
    the same figure and basis is a no-op and a different one is refused. The record cannot write
    a correction: entries it carries must be the copy's, and it takes the copy's list."""
    estimate = record.estimate
    if estimate is None:
        return
    if not estimate.basis.strip():
        raise SubPlanError(
            f"stage {record.stage}'s estimate states no basis: say what the figure rests on")
    copy = parent_copy or {}
    known = copy.get("corrections") or []
    foreign = [c for c in estimate.corrections if c not in known]
    if foreign:
        raise SubPlanError(
            f"stage {record.stage}'s estimate carries a correction the record cannot write: only "
            f"`estimate-correct` run by the session that ordered the stage writes one")
    estimate.corrections = [dict(c) for c in known]
    if copy.get("agreed"):
        if (estimate.minutes, estimate.usd, estimate.basis.strip()) != (
                copy.get("minutes"), copy.get("usd"), (copy.get("basis") or "").strip()):
            raise SubPlanError(
                f"stage {record.stage}'s estimate was agreed as {copy.get('minutes')} min / "
                f"{copy.get('usd')} usd on the basis {copy.get('basis')!r}; a different figure or "
                f"basis is a correction, agreed through `estimate-correct`, not a rewrite")
        return
    estimate.author = estimate.confirmer = writer


def subplan_approval_problem(entry: dict, record: SubPlanRecord | None, *,
                             child_way: str | None = None) -> str | None:
    """Why the user's approval in `entry` does not cover the live `record`, or None when it does.
    The verbs it names are the root's (`present-plan`, `approve-subplan`); a linked child runs
    neither, so `child_way` -- what the child does instead -- replaces them."""
    if not entry.get("user_subplan_approval"):
        return ("the user has not approved a sub-plan for it ("
                + (child_way or "present it with present-plan --kind subplan --stage <n>, then "
                   "approve-subplan --by user") + ")")
    if record is None:
        return ("the sub-plan the user approved is no longer on record ("
                + (child_way or "build it again with dispatch --subplan-only or push-subplan, "
                   "present it, then approve-subplan --by user") + ")")
    if approval_digest(record) != entry["user_subplan_approval"]:
        return ("the sub-plan changed since the user approved it ("
                + (child_way or "approve-subplan --by user again") + ")")
    return None


#: The kind of the review record that judges the method of one stage's sub-plan (the pair review
#: judges the stage orders and their cover; method concerns go here).
METHOD_REVIEW_KIND = "method"
_METHOD_REVIEW_PASSING = ("pass", "override")

#: The engine writes a stage's method review (`record_method_review`) only from a whole-plan
#: `plan-review` whose reviewer attests `--plan-digest`, the sha256 of the sub-plan file's bytes
#: they read (checked against the live bytes, which include `meta.sub_plan`). The pairwise route
#: (`plan-review-compose`) writes none: its pair bundles never show the method.
METHOD_REVIEW_WAY = (
    "the thinker reads the sub-plan file itself, method included, and records `plan-review "
    "--verdict pass --reviewer thinker --plan-digest <sha256 of the sub-plan file they read> "
    "--target <sub-plan file>`; the user's override gives it the same way, with `--plan-digest` "
    "(an override without it clears the plan-review gate and writes no method review); the "
    "review is for the sub-plan as it stands, so it is asked again after the method, procedure, "
    "means or conditions change; a pair-by-pair composition (`plan-review-compose`) does not "
    "give it, because no pair shows the method")


def method_review_recorded(record: SubPlanRecord | None) -> bool:
    """Whether the record carries a method review that did not ask for a revision and was given
    for the record as it stands: a review bound to another digest judged a method since rewritten.
    An estimate correction is not a rewrite (`APPROVAL_EXCLUDED`)."""
    if record is None:
        return False
    bound = approval_digest(record)
    return any(
        review.get("kind") == METHOD_REVIEW_KIND and review.get("verdict") in _METHOD_REVIEW_PASSING
        and review.get("bound_to") == bound
        for review in record.reviews)


def _unless_stale_method_review(reviews: list[dict], record: SubPlanRecord) -> list[dict]:
    """`reviews` without the method reviews given for a digest other than the record's own."""
    bound = approval_digest(record)
    return [r for r in reviews if r.get("kind") != METHOD_REVIEW_KIND or r.get("bound_to") == bound]


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
        record.reviews = record.reviews or _unless_stale_method_review(kept.reviews, record)
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


def _parent_copy(state, stage_index: int, level: int | None, store_) -> dict:
    """The parent-side copy the record of `stage_index` is guarded against: this session's own
    for a frame (it ordered the stage), the linked parent's for a child contour."""
    if level is not None:
        return estimate_copy(state, stage_index, level)
    link = state.parent_link or {}
    if store_ is None or not link.get("parent_session"):
        return {}
    try:
        parent = store_.load(link["parent_session"])
    except Exception:
        return {}
    return estimate_copy(parent, link.get("stage_index", stage_index), link.get("parent_level"))


def record_carried_by(state, doc, store_=None) -> None:
    """Store the record a submitted plan carries, with the place it was built: this session,
    and, when the plan runs in a pushed frame, the interval that opens at the push. A plan
    carrying no record stores nothing. The estimate it carries passes `confirm_estimate`; a
    refusal is raised as `SubPlanError` before anything is stored."""
    record = doc.meta.sub_plan
    if record is None or record.stage <= 0:
        return
    level = len(state.plan_stack) - 1 if state.plan_stack else None
    if any(r.get("kind") == METHOD_REVIEW_KIND for r in record.reviews):
        raise SubPlanError(
            f"stage {record.stage}'s record carries a `{METHOD_REVIEW_KIND}` review: that review is "
            f"written by the engine, never by the record it judges; {METHOD_REVIEW_WAY}")
    confirm_estimate(record, state.session_id, _parent_copy(state, record.stage, level, store_))
    previous_reviews = (state.sub_plan_records.get(record_key(record.stage, level)) or {}).get(
        "reviews") or []
    engine = [r for r in _unless_stale_method_review(previous_reviews, record)
              if r.get("kind") == METHOD_REVIEW_KIND]
    record.reviews = engine + (record.reviews
                               or [r for r in previous_reviews if r.get("kind") != METHOD_REVIEW_KIND])
    if level is not None and record.estimate is not None:
        write_confirmed_copy(state, record.stage, record.estimate, level)
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


def record_method_review(state, store_, doc, *, verdict: str, reviewer: str, plan_sha256: str,
                         at: float) -> bool:
    """The engine's writer of the stage's method review: a thinker's whole-plan verdict on the
    sub-plan file `doc` (passing, or the user's override) is written into the stored record it
    describes, bound to that record's approval digest, wherever the record sits (this session's
    frame or the contour of the child linked at the stage). The record is written only when it
    equals what the reviewer read; a record the executor changed since is left unreviewed.
    Returns whether a review was written."""
    read_as = doc.meta.sub_plan if doc is not None else None
    if read_as is None or read_as.stage <= 0 or verdict not in _METHOD_REVIEW_PASSING:
        return False
    bound = approval_digest(read_as)
    review = {"kind": METHOD_REVIEW_KIND, "verdict": verdict, "by": reviewer,
              "plan_sha256": plan_sha256, "bound_to": bound, "at": at}
    stage = read_as.stage
    keys = [record_key(stage, len(state.plan_stack) - 1 if state.plan_stack else None),
            record_key(stage, len(state.plan_stack))]
    for key in dict.fromkeys(keys):
        raw = state.sub_plan_records.get(key)
        if raw and approval_digest(SubPlanRecord.from_dict(raw)) == bound:
            raw.setdefault("reviews", []).append(review)
            return True
    for child in linked_children(state, store_):
        raw = child.sub_plan_records.get(record_key(stage))
        if ((child.parent_link or {}).get("stage_index") == stage and raw
                and approval_digest(SubPlanRecord.from_dict(raw)) == bound):
            raw.setdefault("reviews", []).append(review)
            store_.save(child)
            return True
    return False


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
    one. `children` are the linked sessions when the caller already has them. A stage planned
    by more than one linked child (a second planning dispatch, a work dispatch after a planning
    one) is read from the child that was handed the stage last: the handoff record's creation
    time decides, the session id breaks a tie, so every verb reads the same record."""
    raw = state.sub_plan_records.get(record_key(stage_index, len(state.plan_stack)))
    if raw:
        return SubPlanRecord.from_dict(raw)
    if children is None:
        children = linked_children(state, store_)
    found = [((child.parent_link.get("created_at") or "", child.session_id),
              child.sub_plan_records[record_key(stage_index)])
             for child in children
             if (child.parent_link or {}).get("stage_index") == stage_index
             and child.sub_plan_records.get(record_key(stage_index))]
    return SubPlanRecord.from_dict(max(found, key=lambda item: item[0])[1]) if found else None


def mirror_into_record(state, stage_index: int, store_, *, minutes, usd, basis: str, who: str,
                       corrections: list[dict]) -> None:
    """Write an agreed correction into the record of the stage: the frame's own record in `state`,
    or the record of the child contour linked at the stage (saved through `store_`). The writer is
    `estimate-correct` only; the figure is the copy's, so the record guard (`confirm_estimate`)
    finds the record equal to the agreed figure on the next carrier write."""
    def update(raw: dict) -> None:
        raw["estimate"] = {**(raw.get("estimate") or {}), "minutes": _number(minutes),
                           "usd": _number(usd), "basis": basis, "author": who, "confirmer": who,
                           "corrections": [dict(c) for c in corrections]}

    key = record_key(stage_index, len(state.plan_stack))
    if state.sub_plan_records.get(key):
        update(state.sub_plan_records[key])
        return
    found = [child for child in linked_children(state, store_)
             if (child.parent_link or {}).get("stage_index") == stage_index
             and child.sub_plan_records.get(record_key(stage_index))]
    if not found:
        return
    child = max(found, key=lambda c: ((c.parent_link or {}).get("created_at") or "", c.session_id))
    update(child.sub_plan_records[record_key(stage_index)])
    store_.save(child)


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
