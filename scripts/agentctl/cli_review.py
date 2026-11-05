"""Review and acceptance subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import json
import os
import shlex
from . import (
    advisor,
    gates,
    grounding_gate,
    protected_actions,
    carry_forward,
    order_approvals,
    plan_store,
    task_accumulator,
)
from .directive import Directive
from .dispatch import Runner
from .plan import (
    PlanError,
    acceptance_requirement_bindings,
    change_tier,
    grants_sha256,
    load_order_draft,
    load_plan,
    load_plan_with_digest,
    understanding_digest,
    is_unit_id,
    pair_part_tokens,
    parse_pair,
    parse_unit,
    part_digest_map,
    plan_meta_digest,
    plan_interface_digests,
    plan_stage_digests,
    review_ids,
    CONDITION_MARKERS,
    ConcernFormatError,
    PlanDoc,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    customer_question_defect,
    pair_binding,
    pair_content,
    parse_concern,
    unit_currency_hash,
    unit_node,
)
from .render import render_plan_md, render_stages_md
from .state import (
    AcceptanceBypass,
    AcceptanceReview,
    CodeReview,
    CONCERN_OPEN,
    PlanReview,
    plan_review_concern_ids,
    plan_review_pair_scope,
    plan_review_scope_stage_index,
    ProtectedGrantAcceptance,
    RiskAcceptance,
    StageReview,
    AGENT_ACTOR,
    CHALLENGE_DISMISSED,
    CHALLENGE_OPEN,
    ORDER_REVIEW_SCOPE,
    OrderReview,
    UnderstandingChallenge,
    CONCERN_FIXED,
    CONCERN_RECORDED,
    CONCERN_SUPERSEDED,
    ConcernRecord,
    PlanPairReview,
    plan_review_scope_for_pair,
    plan_review_scope_for_stage,
    RequirementVerdict,
    SessionState,
)
from .store import StateStore
import datetime as dt
import time
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from .config import Thresholds
from .cli_common import (
    _digest,
    _plan_file_sha256,
    _observation_sha256,
    _record_stage_review,
    _ledgered_order_key,
    _difficulty_id,
    _agent_self_decision_refusal,
    _since_user_backstop_gate,
    _record_backstop_crossing,
    _crossing_data,
    _plan_review_release_active,
    _sync_release_anchor,
    _autonomy_for,
    _plan_from_snapshot,
    _review_baseline,
    _log_gate,
    _run_check,
    _require,
    _clear_since_user_decision,
    _note_round_release,
    _evaluate_review_trigger,
    _utcnow,
    _baseline_plan_for_tier,
    _with_advisories,
)


ESCALATION_LEDGER_ENV = "AGENTCTL_ESCALATION_LEDGER"


DEFAULT_ESCALATION_LEDGER = Path.home() / ".local" / "log" / "claude-plan-review-escalations.jsonl"


def _plan_refs_problem(target: str) -> str | None:
    """Why the references of the plan at `target` do not all resolve (the reference named),
    or None when they do. A plan without references always resolves."""
    try:
        plan_store.visited_refs(target)
    except PlanError as exc:
        return str(exc)
    return None


def _plan_file_bytes(target: str | None) -> int | None:
    """Byte size of a plan file, or None when there is no readable file.

    Mirrors _plan_file_sha256's best-effort contract. cmd_plan_review logs this on
    the plan_review history event so a round's fix size is computable as a byte
    delta between consecutive events of the same family, without re-reading the
    plan archive after the fact."""
    if not target:
        return None
    try:
        return Path(target).stat().st_size
    except OSError:
        return None


# `--reviewer` is free text (30+ observed spellings of "thinker" alone), which makes a
# reviewer-by-role query over history unanswerable without re-parsing every event by
# hand. D9 capture: a best-effort canonical token alongside the untouched raw string —
# never REPLACING it, since a normalization that destroys its input cannot be
# re-derived when the mapping below turns out wrong (e.g. a new role appears).
_REVIEWER_CANONICAL_TOKENS = (
    "thinker", "developer", "code-reviewer", "planner", "tech-writer", "coordinator", "user",
)


def _normalize_reviewer_token(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    for token in _REVIEWER_CANONICAL_TOKENS:
        if token in s:
            return token
    return "other"


def _record_code_review(state: SessionState, review: CodeReview) -> None:
    """Store a CodeReview, one per stage_index (last-wins). Unlike
    _record_stage_review there is no automated judge path to protect a human
    override from — every code-review record is code-reviewer/human-authored via
    cmd_code_review, so a later record for the same stage always replaces the
    prior one."""
    state.code_reviews = [r for r in state.code_reviews if r.stage_index != review.stage_index]
    state.code_reviews.append(review)


def _consume_override_record(state: SessionState, scope: str) -> None:
    """Spend the open difficulty record on the agent-authored override just recorded,
    so it cannot discharge a second decision, and log which record it was for the
    disclosure the user sees."""
    did = _difficulty_id(state.difficulty)
    if did is not None and did not in state.agent_ack_difficulty_ids:
        state.agent_ack_difficulty_ids.append(did)
    state.log("agent_review_override_record", scope=scope, difficulty_id=did)


def _settle_override_decision(state: SessionState, review, scope: str,
                              crossing: dict | None = None) -> None:
    """What a recorded review does to the self-decision machinery. An agent-authored
    override spends the record that earned it, and records the backstop crossing it
    made, if any. A user's override restarts the since-user backstops, and so does the
    release it ends: a non-agent override or an attested whole-plan pass leaves no
    release event for a later record to be measured against."""
    reviewer = (review.reviewer or "").strip()
    if review.verdict == gates._PLAN_REVIEW_OVERRIDE:
        if reviewer.casefold() == AGENT_ACTOR:
            _consume_override_record(state, scope)
            _record_backstop_crossing(state, crossing)
            return
        _clear_since_user_decision(state, reviewer)
        state.review_release_difficulty_id = None
    elif review.verdict == gates._PLAN_REVIEW_PASS and review.plan_sha256 and not scope:
        state.review_release_difficulty_id = None


def _review_trigger_record_open(state: SessionState) -> bool:
    """True while the open difficulty record is the one a review-cost trigger opened and
    that record is complete -- the trigger-cycle path to an agent-authored plan-review
    override, in place of the round release."""
    fire = state.review_trigger_fire
    diff = state.difficulty
    return (fire is not None and diff is not None
            and diff.uid == fire.get("difficulty_uid") and diff.complete())


def _plan_review_override_refusal(
    state: SessionState, prev, reviewer: str, doc,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an override by `reviewer`: a refusal when it may not be
    recorded, else None and the since-user backstop crossing an agent-authored one would
    be (None below the backstop and for any other author). `prev` is the prior record
    of the SAME scope and plan path (or None); `doc` is the plan under review (None when
    it does not load). An override is the customer's escape from a reviewer's `revise`
    deadlock: never by that reviewer itself, by the coordinator only while a
    round/friction release names the decision as its own, and by the order's customer_id
    when one is declared."""
    if (prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE
            and reviewer and reviewer == (prev.reviewer or "").strip()):
        return (
            f"override must come from a distinct reviewer: {reviewer!r} is the "
            "reviewer whose 'revise' verdict it would override (the user is the "
            "expected override author)"
        ), None
    crossing = None
    order = doc.meta.order if doc is not None else None
    if reviewer.casefold() == AGENT_ACTOR:
        # The override never counts as a passing first verdict for the autonomy
        # boundary (it is not written to the ledger).
        release_live = _plan_review_release_active(state)
        trigger_cycle = _review_trigger_record_open(state)
        if not release_live and not trigger_cycle:
            return (
                "an agent-authored override is refused: it is available only once the "
                "review-round or cross-axis friction release is active, or on the complete "
                "difficulty record a review-cost trigger opened"
            ), None
        # The trigger's record opened at the fire, so it is fresh by construction; the
        # release anchor, set at the first observation, may name that very record and
        # would refuse it as predating the release.
        release_active = release_live and not trigger_cycle
        cause = "release" if release_active else "trigger"
        if release_live:
            _sync_release_anchor(state)
        backstopped, crossing = _since_user_backstop_gate(
            state, lambda: doc, decision="plan_review_override",
            axis="review_rounds_since_user_decision",
            backstop=Thresholds().review_round_backstop(),
            what="recording a plan-review override",
            cleared_by="a user approval, fire-acknowledgement, renegotiation or plan-review override")
        if backstopped is not None:
            return f"an agent-authored override is refused: {backstopped}", None
        refused = _agent_self_decision_refusal(
            state, state.review_release_difficulty_id if release_active else None,
            event=f"the {cause}", decision="override")
        if refused is not None:
            return (
                f"an agent-authored override is refused: {refused}. Run the reflection exit "
                "first (declare, investigate, critique with differences_to_remove, normalize "
                f"with a real factor) after the {cause}, then override"
            ), None
    elif order is not None and order.customer_id and reviewer != order.customer_id:
        return (
            f"override reviewer {reviewer!r} does not match order customer_id "
            f"{order.customer_id!r}; record it as the customer of record, or correct --reviewer"
        ), None
    return None, crossing


def _scope_stage(scope: str) -> "int | None":
    """The stage a `stage:<n>` or `topo:unit:<n>` scope is about, else None."""
    index = plan_review_scope_stage_index(scope)
    if index is not None:
        return index
    pair = plan_review_pair_scope(scope)
    if pair is not None and is_unit_id(pair):
        node = unit_node(pair)
        return node if isinstance(node, int) else None
    return None


def _review_vouch(state: SessionState, doc, scope: str) -> "tuple[int, bool] | None":
    """`(stage, sole)` when a pass at `scope` reviews a stage `plan-review-delta` reports as
    owed for `doc`: `sole` is True when that stage is the only thing owed, so its pass is the
    whole review of the edit. None for any other scope. Read before the pass is stored,
    while the delta still lists the stage."""
    index = _scope_stage(scope)
    if doc is None or index is None:
        return None
    whole_plan, owed = gates.plan_review_delta(state, doc)
    if whole_plan or index not in owed:
        return None
    return index, owed == {index}


def _record_first_thinker_verdict(state: SessionState, doc, review, scope: str,
                                  *, vouch: "tuple[int, bool] | None" = None) -> None:
    """Write the thinker's first verdict on the changed unresolved commands into the
    order's ledger, once per (approved version, changed identities) key. A whole-plan pass,
    or a stage-scoped one that is the whole review owed (`vouch` sole), is that verdict
    for the whole key once every other stage holding a changed command has its own passing
    first verdict (an override there is no pass); a stage-scoped pass beside other owed
    stages is the first verdict only on that stage's own commands, under that stage's key.
    An override or an unattested review never is."""
    if doc is None or (scope and vouch is None) or not review.plan_sha256:
        return
    if review.verdict not in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_REVISE):
        return
    key = _ledgered_order_key(state)
    if key is None:
        return
    autonomy = _autonomy_for(state, doc)
    verdict_key = autonomy["first_verdict_key"]
    if vouch is not None:
        recorded = order_approvals.get(key).get("first_thinker_verdicts") or {}
        others_passed = gates.every_stage_vouched(
            autonomy["first_verdict_stage_keys"], autonomy["unresolved_changed_commands"],
            recorded, skip=vouch[0])
        if not (vouch[1] and others_passed):
            verdict_key = autonomy["first_verdict_stage_keys"].get(vouch[0])
    if verdict_key is None:
        return
    order_approvals.record_first_thinker_verdict(key, verdict_key, {
        "verdict": review.verdict, "plan_sha256": review.plan_sha256, "reviewer": review.reviewer,
        **({"scope": scope} if scope else {}),
    })


def _count_review_round(state: SessionState, plan_sha256: str, *, thinker_record: bool = False) -> None:
    """Count one review round of the task and mirror the new total into `state`.

    `thinker_record=False` is a pairwise record: the accumulator adds a round only
    when `plan_sha256` is not the version it last counted (one pairwise run over N
    pairs, or a rerun on the same version, is one round). `thinker_record=True` is a
    whole-plan or stage thinker spawn, a round in itself. The compare-and-increment
    is the accumulator's own critical section, so this never reads the digest first.
    An empty digest (unreadable plan) counts nothing — same fail-direction as
    `_count_plan_review_round`."""
    if not plan_sha256:
        return
    # The accumulator compares digests by equality: give it one digest per plan lineage
    # (`plan-convert-live` links the forms of one plan), so a conversion is not a new version.
    plan_sha256 = gates.plan_lineage_root(state, plan_sha256)
    digest = {"force_digest" if thinker_record else "count_if_digest_differs": plan_sha256}
    totals = task_accumulator.add(
        state.task_id, "review_rounds", 1, session_id=state.session_id, now=_utcnow(),
        mirror_axis="review_rounds_since_user_decision", **digest,
    )
    state.review_rounds = totals["per_axis_totals"]["review_rounds"]
    _sync_release_anchor(state)


def _record_review_questions(state: SessionState, review_id: str, questions: list[str]) -> list[str]:
    """Upsert the customer questions one review act returned as premise candidates
    `qrev-<review id>-<N>` — 'raised', addressed to the review id — and return the
    ids that now stand for them, in order. The review id is `unit:<node>`, a pair
    id, `stage:<n>` or `whole`, so a question is traceable to the act that raised it.

    A statement already on file under the same review id keeps its row and the
    disposition recorded against it (the thinker repeating a question on a
    re-review is not new work for the coordinator); a new statement takes the next
    unused number, so no earlier row is renumbered. Other review ids and the legacy
    `qenum-` rows are never touched. Returns [] when the premise plugin is not armed
    — the questions are then recorded nowhere, the same as every other premise verb,
    and the recording directive says so (`customer_questions_unrecorded`)."""
    bag = state.plugins.get("premise")
    texts = [q.strip() for q in questions or [] if q and q.strip()]
    if bag is None or not texts:
        return []
    candidates = bag.setdefault("candidates", [])
    number = re.compile(re.escape(f"qrev-{review_id}-") + r"(\d+)")
    ids: list[str] = []
    for text in texts:
        standing = next(
            (c for c in candidates
             if number.fullmatch(c.get("id") or "") and c.get("statement") == text), None)
        if standing is None:
            taken = [int(m.group(1)) for c in candidates
                     if (m := number.fullmatch(c.get("id") or ""))]
            standing = {"id": f"qrev-{review_id}-{max(taken, default=0) + 1}",
                        "statement": text, "disposition": "raised", "reason": "",
                        "question": "", "target": review_id}
            candidates.append(standing)
        ids.append(standing["id"])
    state.log("review_questions_recorded", review=review_id, candidates=ids)
    return ids


def _append_escalation_ledger(rows: list[dict]) -> None:
    """Append condition-4 gap-ledger lines. Fail-open like _write_quality_row: a
    ledger I/O error never blocks the review record that already happened. The path
    is resolved per call so a test (or operator) override takes effect immediately."""
    if not rows:
        return
    path = Path(os.environ.get(ESCALATION_LEDGER_ENV) or DEFAULT_ESCALATION_LEDGER)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _condition4_ledger_rows(
    state: SessionState, doc: PlanDoc, target: str, review: PlanPairReview,
    concerns: list[str], outcome: str,
) -> list[dict]:
    marker = CONDITION_MARKERS[3]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return [
        {
            "ts": now,
            "session": state.session_id,
            "plan_path": target,
            "task_id": doc.meta.task_id,
            "unit": review.pair,
            "dependency_stage": review.service,
            "condition": marker.rstrip(":"),
            "concern": concern,
            "concern_sha256": hashlib.sha256(concern.encode("utf-8")).hexdigest(),
            "outcome": outcome,
        }
        for concern in concerns
        if concern.lstrip().startswith(marker)
    ]


@dataclass
class _ConcernPlan:
    """What the severity rules made of one review record's concerns, before it is stored."""

    parsed: list
    parts: list
    effective: list
    verdict: str
    part_digests: dict
    regression_command: str
    delta_changed_parts: list = field(default_factory=list)

    @property
    def bodies(self) -> list[str]:
        return [pc.body for pc in self.parsed]

    @property
    def severities(self) -> list[str]:
        return [pc.severity for pc in self.parsed]

    @property
    def downgraded(self) -> bool:
        return any(
            pc.severity == SEVERITY_BLOCKING and eff != gates.EFFECTIVE_BLOCKING
            for pc, eff in zip(self.parsed, self.effective)
        )


def _ledger_scope(scope: str) -> str:
    index = plan_review_scope_stage_index(scope) if scope else None
    return plan_review_scope_for_stage(index) if index is not None else scope


def _parse_review_concerns(state: SessionState, raw: list[str], verdict: str):
    """Every `--concern` split into severity / restated id / body, or the refusal text.
    Untagged, `blocking:` on a pass, and a `re:<id>` naming no ledger concern all refuse."""
    parsed = []
    for text in raw:
        try:
            pc = parse_concern(text)
        except ConcernFormatError as e:
            return None, str(e)
        if pc.severity == SEVERITY_BLOCKING and verdict == gates._PLAN_REVIEW_PASS:
            return None, (
                f"a `pass` review cannot carry a blocking concern ({text[:80]!r}): "
                f"write it as `{SEVERITY_NOTE}: ...` or record a `revise`"
            )
        if pc.restates and pc.restates not in state.concern_ledger:
            return None, (
                f"`re:{pc.restates}` names no recorded concern: a restatement cites the "
                "stable id of an earlier concern (`data.concern_ids` of its plan-review record)"
            )
        parsed.append(pc)
    return parsed, ""


def _parse_review_questions(raw: list[str]):
    """The `--customer-question` texts of one review, or the refusal text: a blank,
    multi-line, field-syntax (`Q:` / `Customer questions:`) or `none` value is refused
    before any record is written, never recorded as an unreadable candidate or dropped."""
    for text in raw:
        defect = customer_question_defect(text)
        if defect:
            return None, f"--customer-question {text.strip()[:80]!r}: {defect}"
    return [text.strip() for text in raw], ""


def _question_data(state: SessionState, args, question_ids: list[str]) -> dict:
    """The directive fields for a review's customer questions: the candidate ids, and
    `customer_questions_unrecorded` when questions were passed but the premise plugin
    is not armed — they are recorded nowhere, which the caller must see. A review
    that passed no question carries neither field, so its directive keeps the shape
    callers had before reviews returned questions."""
    data: dict = {}
    passed = len(getattr(args, "customer_questions", None) or [])
    if passed or question_ids:
        data["customer_question_ids"] = question_ids
    if passed and state.plugins.get("premise") is None:
        data["customer_questions_unrecorded"] = passed
    return data


def _concern_parts(
    body: str, own_parts, *, default_to_own: bool = True, after_marker: bool = False,
) -> list[str]:
    token = gates._concern_part_token(body, after_marker=after_marker)
    if token is None:
        return list(own_parts) if default_to_own else []
    index = plan_review_scope_stage_index(token)
    return [plan_review_scope_for_stage(index) if index is not None else token]


def _changed_parts(parts, digests: dict, prev_digests: dict) -> list[str]:
    """The parts whose current digest is missing or differs from the one a record stored."""
    return [p for p in parts if digests.get(p) is None or prev_digests.get(p) != digests[p]]


def _plan_concern_severity(
    state: SessionState, doc, ledger_scope: str, own_parts, prev, parsed, verdict: str, *,
    attested: bool, regression_command: str, evidence: bool, may_promote: bool,
    judge_delta: bool = False,
) -> _ConcernPlan:
    """`judge_delta`: a topo row re-reviewed after a pass — when some of its own parts changed
    since that pass, a blocking concern naming no changed part is recorded as advisory."""
    digests = part_digest_map(doc) if doc is not None else {}
    prev_digests = prev.part_digests if prev is not None else {}
    delta_changed = (
        _changed_parts(own_parts or (), digests, prev_digests)
        if judge_delta and prev is not None and prev.verdict == gates._PLAN_REVIEW_PASS else []
    )
    open_parts = {
        p for e in state.concern_ledger.values()
        if e.scope == ledger_scope and gates.concern_unresolved(e, state, doc)
        for p in e.parts
    }
    parts_by_concern, effective = [], []
    for pc in parsed:
        parts = _concern_parts(
            pc.body, own_parts or tuple(digests), default_to_own=not delta_changed,
            after_marker=bool(delta_changed),
        )
        changed = set(_changed_parts(
            [p for p in parts if p in own_parts] if delta_changed else parts, digests, prev_digests))
        target = state.concern_ledger.get(pc.restates) if pc.restates else None
        dispositioned = target is not None and (
            target.status == CONCERN_SUPERSEDED
            or gates._concern_discharged(target.scope, target.local_id, target.text, state, doc)
        )
        differs = bool(regression_command) and (
            target is None or regression_command not in target.regression_commands
        )
        parts_by_concern.append(parts)
        effective.append(gates.effective_concern_severity(
            pc.severity, parts, has_prev=prev is not None, changed=changed,
            open_parts=open_parts, restates_dispositioned=dispositioned,
            new_evidence=evidence or differs,
            after_pass_on_changed=bool(delta_changed),
            restates_open=target is not None and gates.concern_unresolved(target, state, doc),
        ))
    effective_verdict = verdict
    if (verdict == gates._PLAN_REVIEW_REVISE and parsed and attested and may_promote
            and gates.EFFECTIVE_BLOCKING not in effective):
        reraised = _blocking_parts(parsed, parts_by_concern)
        still_open = [
            e for e in state.concern_ledger.values()
            if e.scope == ledger_scope and gates.concern_unresolved(e, state, doc)
            and _entry_stays_open(e, digests, reraised)
        ]
        if not still_open:
            effective_verdict = gates._PLAN_REVIEW_PASS
    return _ConcernPlan(parsed, parts_by_concern, effective, effective_verdict, digests,
                        regression_command, delta_changed)


def _blocking_parts(parsed, parts_by_concern) -> set[str]:
    return {
        p for pc, parts in zip(parsed, parts_by_concern)
        if pc.severity == SEVERITY_BLOCKING for p in parts
    }


def _entry_stays_open(entry: ConcernRecord, digests: dict, reraised: set[str]) -> bool:
    """An open concern survives a record unless a part it names changed since it was
    last raised and the record does not re-raise that part as blocking."""
    moved = _changed_parts(entry.parts, digests, entry.raise_digests)
    return not moved or bool(reraised.intersection(entry.parts))


def _stamp_concern_plan(review, cp: _ConcernPlan, raw_verdict: str) -> None:
    review.concerns = cp.bodies
    review.severities = cp.severities
    review.effective_severities = cp.effective
    review.raw_verdict = raw_verdict
    review.part_digests = cp.part_digests
    review.verdict = cp.verdict


def _commit_concern_ledger(state: SessionState, review, cp: _ConcernPlan, ledger_scope: str,
                           local_ids: list[str], seq: int, *, record_only: bool = False) -> list[str]:
    """Close the scope's open concerns this record settles, register this record's own,
    and return their stable ids. A pass/override supersedes every open concern; any other
    record fixes one whose part changed since it was raised and that this record does not
    re-raise, and refreshes the raise digests of one it does re-raise. A concern outside the
    reviewer's own scope never opens. `record_only` registers the concerns as recorded
    without touching the scope's open ones — for a record that does not become the scope's
    authoritative review."""
    settles_all = review.verdict in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_OVERRIDE)
    reraised = _blocking_parts(cp.parsed, cp.parts)
    out_of_scope = set(getattr(review, "out_of_scope_concern_ids", []))
    for entry in state.concern_ledger.values():
        if record_only or entry.scope != ledger_scope or entry.status != CONCERN_OPEN:
            continue
        if settles_all:
            entry.status = CONCERN_SUPERSEDED
        elif _entry_stays_open(entry, cp.part_digests, reraised):
            if reraised.intersection(entry.parts):
                entry.raise_digests = {p: cp.part_digests.get(p, "") for p in entry.parts}
        else:
            entry.status = CONCERN_FIXED
    prefix = plan_review_pair_scope(ledger_scope) or ledger_scope or "plan"
    ids = []
    for i, (pc, parts, eff) in enumerate(zip(cp.parsed, cp.parts, cp.effective)):
        cid = f"{prefix}#{seq}.c{i}"
        ids.append(cid)
        opens = (eff == gates.EFFECTIVE_BLOCKING and not settles_all and not record_only
                 and local_ids[i] not in out_of_scope)
        state.concern_ledger[cid] = ConcernRecord(
            id=cid, scope=ledger_scope, plan_path=review.plan_path, local_id=local_ids[i],
            text=pc.body, severity=pc.severity, effective=eff, parts=list(parts),
            raise_digests={p: cp.part_digests.get(p, "") for p in parts}, record_seq=seq,
            status=CONCERN_OPEN if opens else CONCERN_RECORDED,
            regression_commands=[cp.regression_command] if cp.regression_command else [],
            restates=pc.restates,
        )
        target = state.concern_ledger.get(pc.restates) if pc.restates else None
        if target is not None and cp.regression_command \
                and cp.regression_command not in target.regression_commands:
            target.regression_commands.append(cp.regression_command)
    _record_understanding_challenges(state, review, cp, ids)
    return ids


def _record_understanding_challenges(state: SessionState, review, cp: _ConcernPlan,
                                     ids: list[str]) -> None:
    """Take every blocking `understanding:` concern the reviewer raised in a revise record up
    as a challenge to the agreed order. It is read from the raw concern, after its `C<n>`
    marker, so neither the post-pass or delta downgrade nor a record-only ledger entry hides
    it; a concern outside the reviewer's scope and a settling record open none. A re-raise of
    a challenge that is still open, or that the user dismissed against the understanding
    still agreed, opens nothing; a re-raise of a challenge a new agreement closed, or one
    dismissed against an understanding since changed, opens a fresh one. Notes and advisory
    concerns open none. Inert while the agreement gate is."""
    agreement = state.order_agreement
    if (agreement is None or not gates.order_agreement_active(state)
            or getattr(review, "raw_verdict", "") != gates._PLAN_REVIEW_REVISE):
        return
    out_of_scope = set(getattr(review, "out_of_scope_concern_ids", []))
    for pc, cid in zip(cp.parsed, ids):
        if pc.severity != SEVERITY_BLOCKING \
                or state.concern_ledger[cid].local_id in out_of_scope \
                or gates._concern_part_token(pc.body, after_marker=True) != gates.UNDERSTANDING_TOKEN:
            continue
        prior = gates.challenge_for_restatement(state, pc.restates)
        if prior is not None and (
            prior.status == CHALLENGE_OPEN
            or (prior.status == CHALLENGE_DISMISSED
                and prior.understanding_sha256 == agreement.understanding_sha256)
        ):
            continue
        state.order_challenges.append(UnderstandingChallenge(
            id=cid, text=pc.body, reviewer=getattr(review, "reviewer", "") or "",
            plan_sha256=getattr(review, "plan_sha256", "") or "",
            understanding_sha256=agreement.understanding_sha256, at=time.time()))


def _log_concerns_downgraded(state: SessionState, cp: _ConcernPlan, scope: str, raw_verdict: str) -> None:
    if cp.downgraded or cp.verdict != raw_verdict:
        state.log("plan_review_concerns_downgraded", scope=scope, raw_verdict=raw_verdict,
                  verdict=cp.verdict, severities=cp.severities, effective=cp.effective,
                  **({"changed_parts": cp.delta_changed_parts} if cp.delta_changed_parts else {}))


def _cmd_plan_review_pair(
    args, state: SessionState, target: str, pair: str, *,
    store: StateStore, runner: Runner | None,
) -> Directive:
    """The `--scope topo:<pair>` branch of cmd_plan_review.

    Branches off BEFORE the whole-plan / stage terminal-pass machinery on purpose: a
    pair record is judged only against a CURRENT same-pair record (gates.pair_status),
    never against the nearest whole-plan pass, and it is written only to
    state.plan_pair_reviews — never to plan_review, plan_review_passes or
    plan_stage_reviews, and it never advances plan_review_rounds (it advances the
    task's review_rounds, once per plan version — see _count_review_round). The binding is
    recomputed here from a FRESH load of the evaluated plan, never taken from node
    files materialized at spawn time or from state.stages."""
    def refuse(msg: str) -> Directive:
        return Directive(False, state.node, "noop", msg)

    unsupported = [flag for flag, dest in (("--concern-id", "concern_ids"),
                                           ("--findings-blocking", "findings_blocking"),
                                           ("--findings-nonblocking", "findings_nonblocking"))
                   if getattr(args, dest, None) is not None]
    if unsupported:
        return refuse(
            f"not recorded for a 'topo:' scope: {', '.join(unsupported)} — a pair "
            "concern is identified by its condition marker, and risk-accept does not "
            "take a pair scope"
        )
    if getattr(args, "escalate_pairs", False) and not is_unit_id(pair):
        return refuse(f"--escalate-pairs belongs to a unit review, not to pair {pair!r}")
    try:
        doc, _, live = load_plan_with_digest(target)
        if is_unit_id(pair):
            parse_unit(doc, pair)
        else:
            parse_pair(doc, pair)
    except (OSError, PlanError, ValueError) as e:
        return refuse(f"--scope 'topo:{pair}' cannot be recorded against {target}: {e}")
    verdict = args.verdict
    parsed, parse_error = _parse_review_concerns(state, list(getattr(args, "concerns", None) or []), verdict)
    if parsed is None:
        return refuse(parse_error)
    question_texts, question_error = _parse_review_questions(
        list(getattr(args, "customer_questions", None) or []))
    if question_texts is None:
        return refuse(question_error)
    reviewer = (getattr(args, "reviewer", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    attested = (getattr(args, "plan_digest", None) or "").strip().lower()
    if verdict == gates._PLAN_REVIEW_PASS and not attested:
        return refuse(
            f"a pass for pair {pair!r} needs --plan-digest: a reviewer that cannot attest "
            "the plan bytes it read cannot bind a pair"
        )
    if attested and attested != live:
        return refuse(
            f"--plan-digest {attested!r} does not match a fresh load of {target!r} "
            f"({live!r}): the reviewer read a different or stale plan; re-read the "
            "current plan and re-run plan-review"
        )
    prev = state.plan_pair_reviews.get(pair)
    if prev is not None and prev.plan_path != target:
        prev = None
    crossing = None
    if verdict == gates._PLAN_REVIEW_OVERRIDE:
        if not reviewer or not note:
            return refuse("a pair override needs a non-empty --reviewer and --note")
        refusal, crossing = _plan_review_override_refusal(state, prev, reviewer, doc)
        if refusal:
            return refuse(refusal)
    regression_command = (getattr(args, "regression_command", "") or "").strip()
    regression_exit: int | None = None
    if verdict == gates._PLAN_REVIEW_REVISE and gates.pair_status(state, doc, target, pair) == "current":
        if not regression_command:
            return refuse(
                f"a current pass for pair {pair!r} is overturned only by run-demonstrated "
                "regression evidence: re-run with --regression-command that exits non-zero"
            )
        try:
            _, result = _run_check(regression_command, 0, runner, cwd=state.repo_root)
            regression_exit = result.returncode if result is not None else None
        except Exception:  # noqa: BLE001 - a broken command is no evidence
            regression_exit = None
        if not regression_exit:
            return refuse(
                f"--regression-command did not demonstrate a regression (exit "
                f"{regression_exit!r}): the current pass for pair {pair!r} stands"
            )
    ledger_scope = plan_review_scope_for_pair(pair)
    # With no readable snapshot of the judged version the reviewer was told to judge the row
    # whole, so no delta downgrade may apply.
    delta_visible = prev is not None and _prior_row_diff(store, state, doc, prev, pair)["available"]
    cp = _plan_concern_severity(
        state, doc, ledger_scope, pair_part_tokens(pair), prev,
        parsed, verdict, attested=bool(attested), regression_command=regression_command,
        evidence=bool(regression_exit), may_promote=True, judge_delta=delta_visible,
    )
    concerns = cp.bodies
    if is_unit_id(pair):
        node_fields = {
            "base": parse_unit(doc, pair), "service": "",
            "unit_norm": unit_currency_hash(doc, pair),
        }
    else:
        base, service = parse_pair(doc, pair)
        node_fields = {
            "base": base, "service": service, **pair_binding(doc, pair), **pair_content(doc, pair),
        }
    review = PlanPairReview(
        pair=pair, verdict=verdict, reviewer=reviewer,
        concerns=concerns, note=note, plan_path=target, plan_sha256=attested,
        record_seq=_next_record_seq(state), **node_fields,
    )
    _stamp_concern_plan(review, cp, verdict)
    review.concern_ids = _commit_concern_ledger(
        state, review, cp, ledger_scope, [f"c{i}" for i in range(len(concerns))], review.record_seq)
    ledger = []
    if review.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, review, concerns, "confirmed-gap")
    elif verdict == gates._PLAN_REVIEW_OVERRIDE and prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, prev, prev.concerns, "false-alarm")
    vouch = _review_vouch(state, doc, ledger_scope)
    state.plan_pair_reviews[pair] = review
    released: list[str] = []
    if getattr(args, "escalate_pairs", False):
        released = carry_forward.release_holds(state, unit_node(pair))
    _settle_override_decision(state, review, plan_review_scope_for_pair(pair), crossing)
    _record_first_thinker_verdict(state, doc, review, ledger_scope, vouch=vouch)
    question_ids = _record_review_questions(state, pair, question_texts)
    if _is_counted_review(review.verdict, reviewer):
        _count_review_round(state, live)
    _count_review_act(state, review.verdict, reviewer)
    _log_concerns_downgraded(state, cp, ledger_scope, verdict)
    state.log("plan_pair_review", target=target, pair=pair, verdict=review.verdict,
              raw_verdict=verdict, reviewer=reviewer, record_seq=review.record_seq,
              concern_ids=review.concern_ids,
              plan_sha256=attested, concerns=concerns, note=note,
              regression_command=regression_command, regression_exit=regression_exit,
              ledger_lines=len(ledger), escalated_pairs=released)
    store.save(state)
    _append_escalation_ledger(ledger)
    status = gates.pair_status(state, doc, target, pair)
    return Directive(
        True, state.node, "continue",
        f"pair review recorded for {pair!r} against {target} (verdict={verdict}); "
        f"pair status: {status}"
        + (f"; escalated to the pairs: {', '.join(released)}" if released else ""),
        data={"pair": pair, "pair_status": status, "ledger_lines": len(ledger),
              "escalated_pairs": released,
              "concern_ids": review.concern_ids, **_question_data(state, args, question_ids),
              **_crossing_data(crossing)},
    )


TOPOLOGICAL_COMPOSITION_REVIEWER = "topological-composition"


def _is_counted_review(verdict: str, reviewer: str) -> bool:
    """A reviewer's pass/revise is a review round; an override is a decision, and the
    engine's own composition record is not a review at all."""
    return (
        verdict in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_REVISE)
        and reviewer != TOPOLOGICAL_COMPOSITION_REVIEWER
    )


def _next_record_seq(state) -> int:
    state.next_record_seq += 1
    return state.next_record_seq


def _stamp_whole_plan_baseline(state, review: PlanReview, doc) -> None:
    """Give a whole-plan record its place in the review-record order and, when its
    plan loads, the per-pair binding hashes and per-unit and per-pair currency hashes
    the walk-stale sets are later measured against. A plan whose pairs cannot be
    enumerated leaves them unset, which the gate reads as every unit and pair being
    stale."""
    review.record_seq = _next_record_seq(state)
    if doc is None:
        return
    try:
        review.reviewed_pair_bindings = gates.pair_baseline_bindings(doc)
        review.reviewed_pair_currency = gates.pair_baseline_currency(doc)
        review.reviewed_unit_currency = gates.unit_baseline_currency(doc)
    except PlanError:
        review.reviewed_pair_bindings = None
        review.reviewed_pair_currency = None
        review.reviewed_unit_currency = None


def _count_plan_review_round(state: SessionState, target: str) -> None:
    """The post-approval round count of a whole-plan review (see the comment in
    cmd_plan_review): once per plan VERSION, never for an unreadable plan."""
    if state.approval is not None and state.approval.passed:
        counted = _plan_file_sha256(target)
        if counted and not gates.plan_digest_equivalent(
                state, counted, state.plan_review_counted_digest):
            state.plan_review_rounds += 1
            state.plan_review_counted_digest = counted
            _sync_release_anchor(state)


def _count_review_act(state: SessionState, verdict: str, reviewer: str, *,
                      stage_scope: str = "") -> None:
    """One counted review record (a pass or revise by a real reviewer: not an override,
    not a carried record, not the composition record) is one act of the open cycle. A
    stage's own record also moves that stage's streak -- a pass ends it, a revise extends
    it; whole-plan and pair records never do."""
    if not _is_counted_review(verdict, reviewer):
        return
    if state.review_cycle_open:
        state.review_cycle_acts += 1
    if stage_scope:
        if verdict == gates._PLAN_REVIEW_PASS:
            state.review_stage_streaks.pop(stage_scope, None)
            state.review_trigger_spent.pop(f"stage_streak:{stage_scope}", None)
        else:
            state.review_stage_streaks[stage_scope] = state.review_stage_streaks.get(stage_scope, 0) + 1


def _pair_ledger_groups(state: SessionState, pair: str, target: str) -> "dict[int, list[ConcernRecord]]":
    """The pair scope's ledger entries raised against `target`, grouped by the record that
    raised them, each group in concern order — how a record taken before events carried
    `concern_ids` is found."""
    scope = plan_review_scope_for_pair(pair)
    groups: dict[int, list[ConcernRecord]] = {}
    for entry in state.concern_ledger.values():
        if entry.scope == scope and entry.plan_path == target:
            groups.setdefault(entry.record_seq, []).append(entry)
    for group in groups.values():
        group.sort(key=lambda e: (len(e.local_id), e.local_id))
    return groups


def _ledger_concern(entry: ConcernRecord, state: SessionState, doc) -> dict:
    return {
        "id": entry.id, "severity": entry.severity, "effective_severity": entry.effective,
        "parts": list(entry.parts), "text": entry.text,
        "unresolved": gates.concern_unresolved(entry, state, doc),
    }


def _prior_row_diff(store: StateStore, state: SessionState, doc, latest, pair: str) -> dict:
    """The diff of the row's parts between the plan version a reviewer judged and `doc`.
    The judged version of a carried record is the one its first carry started from
    (`carried_from_sha`), not the version it was carried onto; with no readable snapshot of
    it the row is `available: False` and the reviewer judges it whole."""
    from .render import row_diff

    judged = latest.carried_from_sha or latest.plan_sha256
    old = _plan_from_snapshot(store, state, judged)
    if old is None:
        return {"available": False, "from_sha": judged, "text": ""}
    return {"available": True, "from_sha": judged, "text": row_diff(old, doc, pair)}


def _walk_target(args, state: SessionState) -> "str | None":
    return getattr(args, "target", None) or state.plan_path


def _parse_verdicts(raw_verdicts: list[str]) -> tuple[list[RequirementVerdict], list[str]]:
    """Parse repeatable ``--verdict '<requirement_id>|<pass|fail|withdrawn>[|<note>]'`` specs
    into typed RequirementVerdict objects. Returns ``(verdicts, errors)`` — a non-empty
    ``errors`` list means the caller must reject with a failing Directive and record
    nothing. Mirrors ``_parse_partition_units``'s pipe-delimited shape and
    ``(parsed, errors)`` return contract.

    ``withdrawn`` records that a requirement was retracted by the customer (its premise
    was flawed, or the work demonstrated it was no longer wanted); it neither blocks
    resolution nor counts as a delivered requirement. A note is MANDATORY for withdrawn:
    the record must name who withdrew the requirement and why, so the ledger is not
    silently missing a delivery.

    Deliberately does NOT cross-check requirement ids against the order here — that
    check is completeness, not parse well-formedness, and belongs to
    ``gates.resolution_blockers`` (which re-reads the order fresh at resolution time
    rather than at write time; see AcceptanceReview's docstring)."""
    verdicts: list[RequirementVerdict] = []
    errors: list[str] = []
    seen: dict[str, int] = {}  # requirement id -> owning position (1-based)
    for pos, spec in enumerate(raw_verdicts, start=1):
        parts = spec.split("|")
        if len(parts) < 2:
            errors.append(
                f"verdict {pos}: expected "
                f"'<requirement_id>|<pass|fail|withdrawn>[|<note>]', got {spec!r}"
            )
            continue
        req_id = parts[0].strip()
        verdict = parts[1].strip()
        note = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else ""
        if not req_id:
            errors.append(f"verdict {pos}: empty requirement id")
        if verdict not in ("pass", "fail", "withdrawn"):
            errors.append(
                f"verdict {pos}: verdict must be 'pass', 'fail', or 'withdrawn', "
                f"got {verdict!r}"
            )
        elif verdict == "withdrawn" and not note:
            errors.append(
                f"verdict {pos}: 'withdrawn' requires a note naming who withdrew the "
                "requirement and why (format: '<id>|withdrawn|<note>')"
            )
        if req_id in seen:
            errors.append(
                f"verdict {pos}: requirement id {req_id!r} already verdicted at position "
                f"{seen[req_id]} (one verdict per requirement)"
            )
        else:
            seen[req_id] = pos
        verdicts.append(RequirementVerdict(requirement_id=req_id, verdict=verdict, note=note))
    return verdicts, errors


def _attested_digest_refusal(state: SessionState, target: str, attested: str) -> Directive | None:
    """The refusal of a `--plan-digest` the engine cannot cross-check against the live
    bytes of `target` (None when nothing is attested or the attestation holds). The
    absent-digest case keeps its fail-open degradation — nothing was claimed, so nothing
    needs checking."""
    if not attested:
        return None
    live = _plan_file_sha256(target)
    if not live:
        return Directive(
            False, state.node, "noop",
            f"--plan-digest {attested!r} cannot be cross-checked: {target!r} is "
            "unreadable, so the engine cannot confirm the reviewer read it; "
            "re-run plan-review once the plan file is readable (or omit "
            "--plan-digest to record an unattested review that does not bind)",
        )
    if attested != live:
        return Directive(
            False, state.node, "noop",
            f"--plan-digest {attested!r} does not match the live plan bytes "
            f"({live!r}) at {target!r}: the reviewer read a different or stale "
            "plan; re-read the current plan and re-run plan-review",
        )
    refs_problem = _plan_refs_problem(target)
    if refs_problem:
        return Directive(
            False, state.node, "noop",
            f"--plan-digest {attested!r} cannot be cross-checked: the plan at {target!r} has a "
            f"reference that does not resolve ({refs_problem}), so the engine cannot confirm "
            "the reviewer read the text the plan stands for; fix the reference and re-run plan-review",
        )
    return None


def _cmd_order_review(args, state: SessionState, *, store: StateStore) -> Directive:
    """`plan-review --scope order`: record a review of an order draft's UNDERSTANDING.

    The record is bound to the engine's own digest of the draft (`understanding_digest`),
    never to the reviewer's word: `--plan-digest`, when given, is only cross-checked
    against the file's bytes. It touches neither `plan_review` nor `plan_stage_reviews`
    (the draft is not the plan), counts no review round, leaves the plan-review release
    as it stands (a user's override only restarts the since-user backstops), and never
    moves the node. An
    override follows the plan scopes' rules — never by the reviewer whose `revise` it
    overrides, and by the order's customer_id when one is declared — except that an
    agent-authored override is refused outright: the release and trigger machinery that
    admits one is about plan-review rounds, which the order phase does not run."""
    target = getattr(args, "target", None) or state.order_draft_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no order draft to review: order-submit a draft first, or pass --target <draft.toml>",
        )
    try:
        doc, _ = load_order_draft(target)
    except (OSError, PlanError) as exc:
        return Directive(
            False, state.node, "noop",
            f"cannot review {target!r} as an order: it does not load as an order draft: {exc}",
        )
    parsed, parse_error = _parse_review_concerns(
        state, list(getattr(args, "concerns", None) or []), args.verdict)
    if parsed is None:
        return Directive(False, state.node, "noop", parse_error)
    question_texts, question_error = _parse_review_questions(
        list(getattr(args, "customer_questions", None) or []))
    if question_texts is None:
        return Directive(False, state.node, "noop", question_error)
    reviewer = (getattr(args, "reviewer", "") or "").strip()
    if args.verdict == gates._PLAN_REVIEW_OVERRIDE:
        if reviewer.casefold() == AGENT_ACTOR:
            return Directive(
                False, state.node, "noop",
                "an agent-authored override of an order review is refused: the override "
                "is the customer's escape from a reviewer's `revise`",
            )
        prev = next((r for r in reversed(state.order_reviews) if r.target == target), None)
        refusal, _ = _plan_review_override_refusal(state, prev, reviewer, doc)
        if refusal:
            return Directive(False, state.node, "noop", refusal)
    attested = (getattr(args, "plan_digest", None) or "").strip().lower()
    refusal_directive = _attested_digest_refusal(state, target, attested)
    if refusal_directive is not None:
        return refusal_directive
    review = OrderReview(
        verdict=args.verdict,
        reviewer=reviewer,
        target=target,
        understanding_sha256=understanding_digest(doc),
        concerns=[pc.body for pc in parsed],
        customer_questions=question_texts,
        note=getattr(args, "note", "") or "",
        plan_sha256=attested,
        transcript=getattr(args, "transcript", None) or "",
        at=time.time(),
    )
    state.order_reviews.append(review)
    question_ids = _record_review_questions(state, ORDER_REVIEW_SCOPE, question_texts)
    if review.verdict == gates._PLAN_REVIEW_OVERRIDE:
        _clear_since_user_decision(state, reviewer)
    state.log("order_review", target=target, verdict=review.verdict, reviewer=reviewer,
              understanding_sha256=review.understanding_sha256)
    store.save(state)
    blockers = gates.order_review_blockers(state, target)
    return Directive(
        True, state.node, "continue",
        f"order review ({review.verdict}) recorded for {target} against understanding "
        f"{review.understanding_sha256[:12]}; "
        + ("it stands for the draft" if not blockers else "the order review gate is still blocked"),
        data={
            "understanding_sha256": review.understanding_sha256,
            "blockers": blockers,
            "concerns": review.concerns,
            **_question_data(state, args, question_ids),
        },
    )

def _grounding_pass_refusal(args, state: SessionState, store: StateStore, runner: Runner | None,
                            warnings: list[str]) -> Directive | None:
    """A refusal of a whole-plan or stage-scoped `pass` while a stage outside the exempt set (the one
    submission uses) keeps a defective grounding the plan does not override. A judge that cannot look
    adds a warning and refuses nothing; only an unverified given is refused by rule alone."""
    if args.verdict != gates._PLAN_REVIEW_PASS:
        return None
    scope = (getattr(args, "scope", None) or "").strip()
    stage_index = plan_review_scope_stage_index(scope) if scope else None
    if scope and stage_index is None:
        return None
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return None
    try:
        doc = load_plan(target)
    except (OSError, PlanError):
        return None
    exempt = grounding_gate.exempt_stages(state, doc, _baseline_plan_for_tier(store, state))
    blockers = grounding_gate.review_pass_blockers(
        doc, exempt, runner=runner, weight_class=state.weight_class, warnings=warnings, only=stage_index)
    if not blockers:
        return None
    return Directive(False, state.node, "fix_plan",
                     "plan-review refuses pass: a grounding defect stands (fix the source or keep it with "
                     "[stage.principle] grounding_override = '<reason>'): " + "; ".join(blockers),
                     data={"problems": blockers})


def cmd_plan_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a thinker review of a plan version (`_record_plan_review`), after refusing a `pass` that
    leaves an un-overridden grounding defect standing."""
    state = _require(store, args.session)
    warnings: list[str] = []
    refusal = _grounding_pass_refusal(args, state, store, runner, warnings)
    d = refusal if refusal is not None else _record_plan_review(args, store=store, runner=runner)
    return _with_advisories(d, warnings)


def _record_plan_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a thinker review of a plan version, backing the plan-review gate.

    The COGNITION (the thinker's reasoning) happens in the thinker leaf; this only
    records its verdict, bound to the plan file it examined (`--target`, defaulting
    to the session's current plan_path — pass the NEW plan for a replan-time review).
    Purely a recorder, mirroring declare/investigate/critique: gates.
    plan_review_blockers enforces bind/verdict at approve/replan, so an incomplete
    override recorded here simply fails to clear the gate rather than erroring.

    The reviewer must pass --plan-digest <hex> (the sha256 of its OWN read of the
    plan); it is cross-checked against the live bytes and stored as the attested
    plan_sha256. A passing verdict does NOT bind without a matching attestation,
    and an attestation the engine cannot cross-check — because the target is
    unreadable — is REFUSED rather than stored on the caller's word.

    --scope 'stage:<n>' binds the review to one stage instead of the whole plan
    (stage 5): the record also carries the engine's OWN digests of the plan's
    meta and per-stage parts at this moment (plan_meta_digest/plan_stage_digests),
    which gates.plan_review_blockers compares against the live plan via
    plan.changed_parts to decide whether this review still covers what it once
    covered. Recording those digests requires a LOADABLE plan — a whole-plan
    review degrades gracefully to a digest-less record on a parse failure (still
    useful as a path/content-hash-bound record), but a stage-scoped review REFUSES:
    it cannot confirm the named stage even exists without parsing the plan."""
    state = _require(store, args.session)
    scope = (getattr(args, "scope", None) or "").strip()
    if scope == ORDER_REVIEW_SCOPE:
        return _cmd_order_review(args, state, store=store)
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to review: submit a plan first, or pass --target <plan.toml>",
        )
    pair = plan_review_pair_scope(scope)
    if pair is not None:
        return _cmd_plan_review_pair(args, state, target, pair, store=store, runner=runner)
    if getattr(args, "escalate_pairs", False):
        return Directive(
            False, state.node, "noop",
            "--escalate-pairs belongs to a unit review (--scope topo:unit:<n>), "
            f"not to scope {scope or 'whole-plan'!r}")
    doc = None
    parse_error = None
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        parse_error = str(e)
    if scope:
        stage_index = plan_review_scope_stage_index(scope)
        if stage_index is None:
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r} is not a recognized scope (expected 'stage:<n>')",
            )
        if doc is None:
            return Directive(
                False, state.node, "noop",
                f"cannot validate --scope {scope!r}: {target} failed to load: {parse_error}",
            )
        if not any(s.index == stage_index for s in doc.stages):
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r}: no stage {stage_index} in {target}",
            )
    parsed, parse_error = _parse_review_concerns(state, list(getattr(args, "concerns", None) or []), args.verdict)
    if parsed is None:
        return Directive(False, state.node, "noop", parse_error)
    question_texts, question_error = _parse_review_questions(
        list(getattr(args, "customer_questions", None) or []))
    if question_texts is None:
        return Directive(False, state.node, "noop", question_error)
    # An override is the USER's escape from a reviewer's `revise` deadlock — the
    # reviewer who issued the blocking verdict cannot override themselves. Checked
    # here, before the record is overwritten and the prior reviewer's identity lost.
    # Scope-aware: an override of a STAGE-scoped revise compares against the prior
    # review of that SAME scope, never against the whole-plan record.
    crossing = None
    if args.verdict == gates._PLAN_REVIEW_OVERRIDE:
        prev = state.plan_stage_reviews.get(scope) if scope else state.plan_review
        if prev is not None and prev.plan_path != target:
            prev = None
        # Mirrors cmd_accept's author/customer_id check; degrades to a pass-through
        # when the plan has no [meta.order] or an empty customer_id, same as cmd_accept.
        refusal, crossing = _plan_review_override_refusal(
            state, prev, (getattr(args, "reviewer", "") or "").strip(), doc,
        )
        if refusal:
            return Directive(False, state.node, "noop", refusal)
    # --plan-digest is the sha256 the REVIEWER computed from its OWN read of the
    # target plan file. Cross-check it against the engine's live digest and REFUSE
    # to record on mismatch (a reviewer that read a different/stale file must not
    # bind a pass). The matching digest becomes PlanReview.plan_sha256 — the field
    # is now REVIEWER-attested, NOT engine-auto-computed. CONTRACT INVERSION: an
    # ABSENT --plan-digest yields plan_sha256="" (unattested); the pass path of
    # gates.plan_review_blockers then BLOCKS on the empty hash (see the inversion
    # note there), so a reviewer that could not read the plan cannot bind a pass.
    #
    # An UNREADABLE target refuses too, because an attestation the engine cannot
    # cross-check is not an attestation: the stored plan_sha256 is what
    # gates._binds_across_path_change accepts as proof a review of some OTHER path
    # examined this plan's bytes, so recording an unverified one lets any caller
    # bind a plan by naming a path nobody read (#195). The absent-digest case keeps
    # its fail-open degradation — nothing was claimed, so nothing needs checking.
    attested = (getattr(args, "plan_digest", None) or "").strip().lower()
    refusal = _attested_digest_refusal(state, target, attested)
    if refusal is not None:
        return refusal
    # Terminal-pass check: a `revise` arriving after a PASS already recorded
    # for this scope this approval cycle only overturns it with run-demonstrated
    # regression evidence — see gates.plan_review_prior_pass /
    # _plan_review_regression_evidence. `prior_pass` is history within the
    # cycle, not "the current authoritative record": it stays found even after
    # a later evidenced revise supersedes it, because the requirement is "a
    # pass has been recorded this cycle", not "the most recent verdict is one".
    prior_pass = gates.plan_review_prior_pass(state, scope, target)
    is_post_pass_revise = args.verdict == gates._PLAN_REVIEW_REVISE and prior_pass is not None
    regression_command = (getattr(args, "regression_command", "") or "").strip()
    regression_exit: int | None = None
    regression_command_error: str | None = None
    if is_post_pass_revise and regression_command:
        try:
            _, result = _run_check(regression_command, 0, runner, cwd=state.repo_root)
            regression_exit = result.returncode if result is not None else None
        except Exception as exc:  # noqa: BLE001 - never let a broken regression command crash recording
            regression_exit = None
            regression_command_error = str(exc)
    concerns = [pc.body for pc in parsed]
    review = PlanReview(
        plan_path=target,
        verdict=args.verdict,
        reviewer=getattr(args, "reviewer", "") or "",
        concerns=concerns,
        note=getattr(args, "note", "") or "",
        plan_sha256=attested,
        scope=scope,
        reviewed_meta_digest=plan_meta_digest(doc) if doc is not None else "",
        reviewed_stage_keys=(
            {str(k): v for k, v in plan_stage_digests(doc).items()} if doc is not None else {}
        ),
        reviewed_interface_keys=(
            {str(k): v for k, v in plan_interface_digests(doc).items()} if doc is not None else {}
        ),
        concern_ids=list(getattr(args, "concern_ids", None) or []),
        regression_command=regression_command if is_post_pass_revise else "",
        regression_exit=regression_exit if is_post_pass_revise else None,
        remedy_tags=[gates._remedy_tag_for_concern(c) for c in concerns],
    )
    review.in_scope_concern_ids, review.out_of_scope_concern_ids = gates.classify_concerns(
        scope, plan_review_concern_ids(review), review.concerns
    )
    if not scope:
        _stamp_whole_plan_baseline(state, review, doc)
    else:
        review.record_seq = _next_record_seq(state)
    evidenced = is_post_pass_revise and gates._plan_review_regression_evidence(prior_pass, review, doc)
    ledger_scope = _ledger_scope(scope)
    concern_plan = _plan_concern_severity(
        state, doc, ledger_scope, (ledger_scope,) if scope else (),
        state.plan_stage_reviews.get(scope) if scope else state.plan_review, parsed, args.verdict,
        attested=bool(attested), regression_command=regression_command,
        evidence=bool(evidenced), may_promote=not (is_post_pass_revise and not evidenced),
    )
    _stamp_concern_plan(review, concern_plan, args.verdict)
    unevidenced = is_post_pass_revise and not evidenced
    review.stable_ids = _commit_concern_ledger(
        state, review, concern_plan, ledger_scope, plan_review_concern_ids(review),
        review.record_seq, record_only=unevidenced)
    question_ids = _record_review_questions(state, scope or "whole", question_texts)
    if unevidenced:
        # The prior PASS stays authoritative — plan_review_passes is never
        # touched here, so gates.plan_review_prior_pass keeps reporting it as
        # found — meaning THIS revise does not reopen the gate by itself
        # (requirement (a)). Recorded as a non-blocking note instead
        # (requirement (b)) — logged under its own event name, with the
        # concern text surfaced verbatim in the Directive so the user (not
        # another thinker round) decides override vs. editing the plan.
        #
        # That is not the same as the gate being clear: the plan may have been
        # EDITED since the pass, which stales its coverage independently of
        # this revise (gates._plan_review_blockers_coverage). Reporting
        # ok/continue while gates.plan_review_blockers is still non-empty would
        # claim the pass is "in force" when it no longer covers the current
        # plan — so the real blockers are computed below and, if non-empty,
        # returned instead of the swallowed-note continue.
        #
        # Whether plan_review/plan_stage_reviews[scope] gets overwritten with
        # THIS revise is conditional, not "never": it matters only when this
        # scope's OWN part actually moved since the prior pass, because that
        # is the one case _plan_review_blockers_coverage will go looking for a
        # record at this scope next — leaving nothing there would make
        # risk-accept/override at this scope find nothing to discharge
        # (gates.plan_review_scope_moved_since_pass's docstring). An unmoved
        # scope has no such lookup coming, so the prior pass stays the sole
        # record there (test_stage_scoped_revise_after_whole_plan_pass_is_terminal).
        remedy_cut = review.remedy_tags.count("cut")
        remedy_add = review.remedy_tags.count("add")
        if _is_counted_review(args.verdict, review.reviewer):
            _count_review_round(state, review.plan_sha256 or _plan_file_sha256(target), thinker_record=True)
        if gates.plan_review_scope_moved_since_pass(prior_pass, doc, scope):
            if scope:
                state.plan_stage_reviews[scope] = review
            else:
                state.plan_review = review
        state.log("plan_review_post_pass_unevidenced", target=target, scope=scope,
                   reviewer=review.reviewer, concerns=review.concerns, note=review.note,
                   regression_command=review.regression_command,
                   regression_exit=review.regression_exit,
                   regression_command_error=regression_command_error,
                   remedy_cut=remedy_cut, remedy_add=remedy_add)
        store.save(state)
        concern_text = "; ".join(review.concerns) if review.concerns else "(no concern text given)"
        data = {
            "plan_review_post_pass_unevidenced": True,
            "prior_pass_reviewer": prior_pass.reviewer,
            "concerns": review.concerns,
            "concern_ids": review.stable_ids,
            **_question_data(state, args, question_ids),
            "remedy_tags": review.remedy_tags,
            "regression_command_error": regression_command_error,
        }
        real_blockers = gates.plan_review_blockers(state, target)
        if real_blockers:
            round_release = _note_round_release(state, real_blockers, store)
            return Directive(
                False, state.node, "plan_review",
                f"thinker revise recorded for {target} at scope {scope!r} does not "
                "overturn the prior PASS (no run-demonstrated regression evidence), "
                "and the plan-review gate is still blocked on its own terms — the "
                "pass is stale, not in force "
                f"(concern raised: {concern_text!r}). This is not another thinker "
                "round: the decision is the user's — override (plan-review "
                "--verdict override --reviewer <you> --note <why it is acceptable>), "
                "or edit the plan as the concern proposes (cut or add).",
                data={**data, "blockers": real_blockers, "plan_review_round_release": round_release},
            )
        return Directive(
            True, state.node, "continue",
            f"thinker revise recorded for {target} at scope {scope!r} but does not "
            "overturn the whole-plan/stage PASS already recorded this approval cycle "
            "(a recorded pass is terminal without run-demonstrated regression "
            "evidence — a --regression-command that actually exits non-zero — naming "
            "a part changed since that pass). "
            f"Concern raised: {concern_text!r}. This is a non-blocking note, not another "
            "thinker round: the decision is the user's — override "
            "(plan-review --verdict override --reviewer <you> --note <why it is "
            "acceptable>), or edit the plan as the concern proposes (cut or add).",
            data=data,
        )
    _log_concerns_downgraded(state, concern_plan, scope, args.verdict)
    vouch = _review_vouch(state, doc, scope)
    if scope:
        state.plan_stage_reviews[scope] = review
    else:
        state.plan_review = review
    if review.verdict == gates._PLAN_REVIEW_PASS and review.plan_sha256:
        # Attested pass only — an unattested one never clears the gate (see the
        # CONTRACT INVERSION note on the pass path of _plan_review_verdict_blockers),
        # so it must not become a terminal pass either.
        state.plan_review_passes[scope] = review
    _settle_override_decision(state, review, scope, crossing)
    _record_first_thinker_verdict(state, doc, review, scope, vouch=vouch)
    # POST-APPROVAL round counting. cmd_submit_plan's increment covers only the
    # pre-approval resubmission loop; review cycles overwhelmingly recur AFTER
    # approval, on the `replan` path, where the same thinker review is demanded and
    # nothing advanced the counter — leaving the round-release valve unreachable
    # exactly where it is needed. The two increments are disjoint in time, not by
    # convention: cmd_submit_plan sets approval.passed = False BEFORE its own
    # increment, so no single call can satisfy both conditions.
    #
    # The unit is a plan VERSION, not a verdict — see plan_review_counted_digest's
    # field comment in state.py for why counting verdicts would fire the release
    # inside a legitimate stage-coverage pass and retire reviews nobody performed.
    #
    # An UNREADABLE plan does not count and leaves the marker untouched. That is the
    # conservative direction here even though over-counting is the usual fail-safe:
    # because the release SUBSTITUTES the outstanding blockers rather than adding to
    # them, an over-count can cancel a review requirement nobody satisfied, whereas an
    # under-count only leaves the user's existing one-sentence override as the exit.
    #
    # Placed BEFORE the blockers call so the verdict that exhausts the budget surfaces
    # the release in its own Directive, rather than one round later.
    _count_plan_review_round(state, target)
    if _is_counted_review(review.verdict, review.reviewer):
        _count_review_round(state, review.plan_sha256 or _plan_file_sha256(target), thinker_record=True)
    _count_review_act(state, review.verdict, review.reviewer, stage_scope=scope)
    blockers =gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("plan_review", target=target, verdict=review.verdict, raw_verdict=args.verdict,
              scope=scope,
              reviewer=review.reviewer,
              reviewer_raw=review.reviewer,
              reviewer_token=_normalize_reviewer_token(review.reviewer),
              plan_sha256=review.plan_sha256,
              plan_bytes=_plan_file_bytes(target),
              concerns=review.concerns,
              note=review.note,
              findings_blocking=getattr(args, "findings_blocking", None),
              findings_nonblocking=getattr(args, "findings_nonblocking", None),
              remedy_cut=review.remedy_tags.count("cut"),
              remedy_add=review.remedy_tags.count("add"),
              regression_command=review.regression_command,
              regression_exit=review.regression_exit,
              regression_command_error=regression_command_error)
    fired = _evaluate_review_trigger(state, store, blockers)
    if fired is not None:
        return fired
    store.save(state)
    if blockers:
        round_release = _note_round_release(state, blockers, store)
        return Directive(
            False, state.node, "plan_review",
            "thinker review recorded but does not clear the gate",
            data={
                "blockers": blockers,
                "concern_ids": review.stable_ids,
                **_question_data(state, args, question_ids),
                "plan_review_round_release": round_release,
                "regression_command_error": regression_command_error,
                **_crossing_data(crossing),
            },
        )
    out_of_scope_note = ""
    if review.out_of_scope_concern_ids:
        out_of_scope_note = (
            f" ({len(review.out_of_scope_concern_ids)} out-of-scope findings "
            "recorded, not blocking)"
        )
    return Directive(
        True, state.node, "continue",
        f"thinker review recorded for {target} (verdict={review.verdict}); "
        "the plan-review gate is now satisfied for this plan version" + out_of_scope_note,
        data={"concern_ids": review.stable_ids, **_question_data(state, args, question_ids),
              **_crossing_data(crossing)},
    )


def cmd_accept_protected_grant(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the user's acceptance of ONE protected action the current plan's grants
    cover (`protected_actions`) — a user-authority verb, so no spawned child or reviewer
    is ever granted it. Purely a recorder: `gates.protected_grant_blockers` re-derives
    the hits and the grants digest at `approve`, so an acceptance bound to a different
    grant set simply fails to clear the gate."""
    state = _require(store, args.session)
    target = state.plan_path
    if not target:
        return Directive(False, state.node, "noop",
                         "no plan to accept a protected grant against: submit a plan first")
    entry_id = (getattr(args, "id", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    if not entry_id or not note:
        return Directive(False, state.node, "noop",
                         "accept-protected-grant requires a non-empty --id and --note")
    if gates._normalize_string(note) in gates._PLACEHOLDER_SET:
        return Directive(False, state.node, "noop",
                         f"--note {note!r} reads as a placeholder, not a reason — say what concretely")
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop",
                         f"cannot record a protected-grant acceptance: {target} failed to load: {e}")
    covered = sorted({hit_id for _stage, _rule, hit_id in protected_actions.protected_grant_hits(doc)})
    if entry_id not in covered:
        return Directive(
            False, state.node, "noop",
            f"the grants of {target} cover no protected action {entry_id!r} "
            f"(covered: {covered or 'none'}) — nothing there to accept",
        )
    digest = grants_sha256(doc)
    if not any(a.entry_id == entry_id and a.grants_sha256 == digest
               for a in state.protected_grant_acceptances):
        state.protected_grant_acceptances.append(ProtectedGrantAcceptance(
            entry_id=entry_id, grants_sha256=digest, plan_path=target, note=note))
    state.log("protected_grant_accept", target=target, entry_id=entry_id, grants_sha256=digest, note=note)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"protected action {entry_id!r} accepted for the current grant set of {target}; "
        "a change to the plan's grants asks again",
        data={"entry_id": entry_id, "grants_sha256": digest},
    )


def cmd_risk_accept(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a customer-facing acceptance of ONE named PlanReview concern's risk —
    the alternative to editing the plan to make a `revise` concern go away. Purely a
    recorder, mirroring cmd_plan_review: gates.plan_review_blockers re-derives
    discharge itself at approve/replan by reading state.risk_acceptances, so a
    mis-bound acceptance recorded here simply fails to clear the gate rather than
    erroring.

    `--basis`/`--risk` mirror premise.py's `assumed` question disposition exactly:
    both are required free text, and neither may be a bare placeholder (see
    gates._PLACEHOLDER_SET). Bound to the plan version at record time via the SAME
    meta/stage-digest snapshot a PlanReview itself carries — gates._risk_acceptance_stale
    reads them via plan.changed_parts identically."""
    state = _require(store, args.session)
    target = state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to accept a risk against: submit a plan first",
        )
    scope = (getattr(args, "scope", None) or "").strip()
    concern_id = (getattr(args, "concern_id", "") or "").strip()
    basis = (getattr(args, "basis", "") or "").strip()
    risk = (getattr(args, "risk", "") or "").strip()
    author = (getattr(args, "author", "") or "").strip()
    missing = [name for name, value in
               (("concern-id", concern_id), ("basis", basis), ("risk", risk), ("author", author))
               if not value]
    if missing:
        return Directive(
            False, state.node, "noop",
            "risk-accept requires a non-empty --" + " and --".join(missing),
        )
    for value, flag in ((basis, "--basis"), (risk, "--risk")):
        if gates._normalize_string(value) in gates._PLACEHOLDER_SET:
            return Directive(
                False, state.node, "noop",
                f"{flag} {value!r} reads as a placeholder, not a reason — say what concretely",
            )
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(
            False, state.node, "noop",
            f"cannot record a risk acceptance: {target} failed to load: {e}",
        )
    held = state.concern_ledger.get(concern_id)
    if held is not None:
        if held.status != CONCERN_OPEN:
            return Directive(
                False, state.node, "noop",
                f"concern {concern_id!r} is {held.status}, not open — nothing there to accept",
            )
        if scope and scope != held.scope:
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r} disagrees with concern {concern_id!r}, which belongs to "
                f"scope {held.scope!r} — drop --scope or pass the matching one",
            )
        accepted_scope, accepted_id, accepted_text = held.scope, held.local_id, held.text
    else:
        if scope:
            stage_index = plan_review_scope_stage_index(scope)
            if stage_index is None:
                return Directive(
                    False, state.node, "noop",
                    f"--scope {scope!r} is not a recognized scope (expected 'stage:<n>')",
                )
            if not any(s.index == stage_index for s in doc.stages):
                return Directive(
                    False, state.node, "noop",
                    f"--scope {scope!r}: no stage {stage_index} in {target}",
                )
        review = state.plan_stage_reviews.get(scope) if scope else state.plan_review
        if review is None:
            return Directive(
                False, state.node, "noop",
                f"no thinker review recorded at scope {scope!r} — nothing there to accept a concern from",
            )
        valid_ids = plan_review_concern_ids(review)
        if concern_id not in valid_ids:
            return Directive(
                False, state.node, "noop",
                f"concern {concern_id!r} is not among scope {scope!r}'s recorded concerns "
                f"{valid_ids!r} — check --concern-id against the review",
            )
        if valid_ids.count(concern_id) > 1:
            return Directive(
                False, state.node, "noop",
                f"concern {concern_id!r} appears {valid_ids.count(concern_id)} times in scope "
                f"{scope!r}'s recorded concerns {valid_ids!r} — ambiguous which one this "
                "acceptance binds to; the review must give each concern a distinct --concern-id",
            )
        accepted_scope, accepted_id = scope, concern_id
        accepted_text = review.concerns[valid_ids.index(concern_id)]
    acceptance = RiskAcceptance(
        scope=accepted_scope,
        concern_id=accepted_id,
        plan_path=target,
        basis=basis,
        risk=risk,
        author=author,
        meta_digest=plan_meta_digest(doc),
        stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        concern_text=accepted_text,
    )
    state.risk_acceptances.append(acceptance)
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("risk_accept", target=target, scope=accepted_scope, concern_id=accepted_id,
              ledger_id=concern_id, author=author, basis=basis, risk=risk)
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "risk acceptance recorded but does not clear the gate",
            data={"blockers": blockers},
        )
    return Directive(
        True, state.node, "continue",
        f"risk acceptance recorded for {target} (scope={accepted_scope!r} concern={accepted_id!r}); "
        "the plan-review gate is now satisfied for this plan version",
    )


def cmd_plan_review_delta(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: what a reviewer needs to look at in the plan RIGHT NOW, given
    what's already been reviewed — the brief `plan-review` itself does not need,
    but a human/thinker preparing to run it does. Replaces hand-computing this
    from a raw digest dump: `data['stages']` names the moved stages, and the
    Directive's markdown is their actual current rendering via render_stages_md
    (or the whole plan via render_plan_md when a meta/order change, or the
    absence of any prior review, means nothing narrower will do)."""
    state = _require(store, args.session)
    target = getattr(args, "plan", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to diff: submit a plan first, or pass --plan <plan.toml>",
        )
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} failed to load: {e}")
    whole_plan_needed, stage_indices, consumer_of = gates.plan_review_delta_detail(state, doc)
    stages = sorted(stage_indices)
    pairs = gates.pairs_hint_for(state, doc, target)
    if whole_plan_needed:
        md = render_plan_md(doc)
        detail = (
            f"whole-plan review needed for {target}: its meta/order changed since "
            "the last whole-plan review, or none has been recorded yet; the topological "
            "route reviews it pair by pair (plan-review-topological.py, then "
            "plan-review-compose)"
        )
    elif stages:
        md = render_stages_md(doc, stages)
        detail = f"stage-scoped review needed for stage(s) {stages} in {target}"
        if consumer_of:
            named = "; ".join(
                f"stage {c}: consumer of {', '.join(str(m) for m in consumer_of[c])}"
                for c in sorted(consumer_of)
            )
            detail += f" ({named} -- its supplier's interface moved)"
    else:
        md = ""
        detail = f"no review gap: every part of {target} is covered by its current review"
    if pairs:
        detail += f"; walk-stale pairs — run: plan-review-topological.py --pairs {','.join(pairs)}"
    data = {"markdown": md, "whole_plan": whole_plan_needed, "stages": stages, "pairs": pairs,
            "consumer_of": {str(c): consumer_of[c] for c in sorted(consumer_of)}}
    baseline, _ = _review_baseline(store, state)
    if baseline is not None:
        change = change_tier(baseline, doc)
        data["tier"] = change.tier
        data["stage_tiers"] = {str(i): c.tier for i, c in sorted(change.stages.items())}
        detail += f"; change tier {change.tier} against the version the session runs"
    return Directive(True, state.node, "inspect", detail, data=data)


def cmd_plan_review_pair_history(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: what an earlier review of one pair said, for the next reviewer of it.

    `data.records` lists every pair record taken against the target plan, oldest first,
    each with the reviewer's own verdict, the engine's effective one and its concerns
    (stable id, raw and effective severity, parts, and `unresolved` — still an open
    blocker no risk acceptance discharges). `data.changed_parts_since_last` names the
    pair's own parts (`pair_part_tokens`) whose digest differs from the one the latest
    record stored — every own part when that record stored none. No timestamp is emitted:
    the output feeds a spawn prompt, which must not vary with wall-clock time."""
    state = _require(store, args.session)
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to read pair history for: submit a plan first, or pass --target <plan.toml>",
        )
    pair = args.pair
    try:
        doc = load_plan(target)
        if is_unit_id(pair):
            parse_unit(doc, pair)
        else:
            parse_pair(doc, pair)
    except (OSError, PlanError, ValueError) as e:
        return Directive(False, state.node, "noop", f"history for {pair!r} in {target}: {e}")
    events = [
        e for e in state.history
        if e.get("event") == "plan_pair_review" and e.get("pair") == pair and e.get("target") == target
    ]
    by_seq = _pair_ledger_groups(state, pair, target)
    claimed = {e["record_seq"] for e in events if e.get("record_seq")}
    claimed.update(
        state.concern_ledger[cid].record_seq for e in events for cid in e.get("concern_ids") or []
        if cid in state.concern_ledger)
    unclaimed = [seq for seq in sorted(by_seq) if seq not in claimed]
    records = []
    for event in events:
        entries = [state.concern_ledger[cid] for cid in event.get("concern_ids") or []
                   if cid in state.concern_ledger]
        if not entries and event.get("record_seq") in by_seq:
            entries = by_seq[event["record_seq"]]
        elif not entries and not event.get("record_seq") and event.get("concerns"):
            texts = list(event["concerns"])
            seq = next((s for s in unclaimed if [e.text for e in by_seq[s]] == texts), None)
            if seq is not None:
                unclaimed.remove(seq)
                entries = by_seq[seq]
        concerns = [_ledger_concern(entry, state, doc) for entry in entries] or [
            {"id": "", "severity": "", "effective_severity": "", "parts": [], "text": text,
             "unresolved": False}
            for text in event.get("concerns") or []
        ]
        records.append({
            "record_seq": event.get("record_seq") or 0,
            "plan_sha256": event.get("plan_sha256", ""),
            "reviewer_verdict": event.get("raw_verdict") or event.get("verdict", ""),
            "effective_verdict": event.get("verdict", ""),
            "concerns": concerns,
        })
    latest = state.plan_pair_reviews.get(pair)
    changed: list[str] = []
    if latest is not None:
        digests = part_digest_map(doc)
        stored = latest.part_digests if latest.plan_path == target else {}
        changed = _changed_parts(pair_part_tokens(pair), digests, stored)
    data = {"pair": pair, "records": records, "changed_parts_since_last": changed}
    if latest is not None and latest.plan_path == target:
        data["prior_diff"] = _prior_row_diff(store, state, doc, latest, pair)
    return Directive(
        True, state.node, "inspect",
        f"{len(records)} recorded review(s) of pair {pair!r} against {target}", data=data,
    )


def cmd_plan_review_walk(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: every review unit and pair of the plan in dependency order (base
    before service; each node's unit before its pairs), with its status, advisory
    readiness and the commands that spawn its reviewer and record its verdict.
    `--target` walks a plan file other than the session's own, computing every digest
    and depth from it. Writes nothing."""
    state = _require(store, args.session)
    target = _walk_target(args, state)
    if not target:
        return Directive(False, state.node, "noop", "no plan to walk: submit a plan first, or pass --target <plan.toml>")
    try:
        doc = load_plan(target)
        rows = gates.pair_walk(state, doc, target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} cannot be walked: {e}")
    quoted = shlex.quote(target)
    levels: list[list[dict]] = []
    for row in rows:
        while len(levels) <= row["level"]:
            levels.append([])
        open_pair = row["status"] not in gates.PAIR_SATISFIED
        levels[row["level"]].append({
            **row,
            "spawn": (f"spawn-specialist.py --kind thinker --plan-brief --review-topo {row['pair']} "
                      f"--plan {quoted}") if open_pair else None,
            "record": (f"agentctl plan-review --session {state.session_id} --target {quoted} "
                       f"--scope topo:{row['pair']} --reviewer thinker --verdict <pass|revise> "
                       "--plan-digest <sha256 of the plan bytes read> "
                       "[--customer-question <Q>]...") if open_pair else None,
        })
    walk = {
        "plan_path": target,
        "discharges": gates.pair_discharged_stages(state, doc, target),
        "levels": levels,
    }
    if getattr(args, "format", "text") == "json":
        return Directive(True, state.node, "inspect", json.dumps(walk, indent=2), data=walk)
    lines = [f"review units and pairs of {target} (base before service; readiness is advisory):"]
    for depth, level in enumerate(levels):
        lines.append(f"level {depth}:")
        for row in level:
            waiting = f" waiting on {','.join(row['waiting'])}" if row["waiting"] else ""
            lines.append(f"  {row['pair']}: {row['status']}, {'ready' if row['ready'] else 'not ready'}{waiting}")
    if walk["discharges"]:
        lines.append("discharged by pairs alone: " + ", ".join(walk["discharges"]))
    return Directive(True, state.node, "inspect", "\n".join(lines), data=walk)


def cmd_plan_review_compose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Compose the unit and pair records into one ordinary whole-plan pass, written
    only when EVERY id of `plan.review_ids` is current or override against `--target` (the
    session's plan by default). Readiness is not consulted. When every pair is a
    pass (none override-satisfied) the composed record is the first thinker verdict
    the autonomy boundary requires and goes to the order ledger; a compose that
    includes an override pair records nothing there — an override never becomes a
    thinker verdict."""
    state = _require(store, args.session)
    target = _walk_target(args, state)
    if not target:
        return Directive(False, state.node, "noop", "no plan to compose: submit a plan first, or pass --target <plan.toml>")
    try:
        doc, _, live = load_plan_with_digest(target)
        pairs = review_ids(doc)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} cannot be composed: {e}")
    status = {pid: gates.pair_status(state, doc, target, pid) for pid in pairs}
    failing = {pid: s for pid, s in status.items() if s not in gates.PAIR_SATISFIED}
    if failing:
        listing = ", ".join(f"{pid} ({s})" for pid, s in failing.items())
        return Directive(
            False, state.node, "plan_review",
            f"compose refused for {target}: every review pair must be current or override; "
            f"failing: {listing}",
            data={"plan_path": target, "failing": failing},
        )
    overridden = [pid for pid, s in status.items() if s == "override"]
    review = PlanReview(
        plan_path=target, verdict=gates._PLAN_REVIEW_PASS, reviewer=TOPOLOGICAL_COMPOSITION_REVIEWER,
        note=f"composed from {len(pairs)} pair review(s): {','.join(pairs)}; overridden: {','.join(overridden)}",
        plan_sha256=live,
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        reviewed_interface_keys={str(k): v for k, v in plan_interface_digests(doc).items()},
    )
    _stamp_whole_plan_baseline(state, review, doc)
    state.plan_review = review
    state.plan_review_passes[""] = review
    if not overridden:
        _record_first_thinker_verdict(state, doc, review, "")
    _count_plan_review_round(state, target)
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("plan_review_compose", target=target, pairs=list(pairs), overridden=overridden,
              plan_sha256=live)
    fired = _evaluate_review_trigger(state, store, blockers)
    if fired is not None:
        return fired
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "composed pass recorded but does not clear the gate",
            data={"blockers": blockers, "plan_path": target},
        )
    return Directive(
        True, state.node, "continue",
        f"composed whole-plan pass recorded for {target} from {len(pairs)} pair review(s); "
        "the plan-review gate is now satisfied for this plan version",
        data={"plan_path": target, "pairs": list(pairs), "overridden": overridden},
    )


def cmd_stage_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a manual review of the active stage's observation, backing the
    acceptance-review judge gate. Mirrors cmd_plan_review — purely a recorder; the
    COGNITION (a human judging, or authoring an override) happens outside. The verdict is
    bound to the observation bytes passed via --observation (defaulting to the stage's
    current observation), so gates.acceptance_review_blockers can reject a drift. The
    automated cheap judge writes an equivalent record inline in record-result; this
    command is the human path (chiefly the override deadlock escape).

    Scope is gates.stage_review_active(state) — the SAME predicate the gate itself
    consumes — not criterion_type. This is a deliberate widening (GitHub issue #145):
    the gate was already broadened past acceptance_review-only stages (Defect 2: control
    compares result with goal at every stage of a SUBSTANTIVE session, see
    cmd_record_result's observation gate), but this escape hatch had not followed, so a
    measurable-criterion stage judge-deadlocked with NO scoped override at all — only the
    session-wide AGENTCTL_STAGE_REVIEW=0 kill switch, which records a strictly weaker,
    unattributed JudgeBypass(kind='killswitch') instead of this command's
    reviewer+note-bound JudgeBypass(kind='override'). Widening the escape to the gate's
    own scope strictly improves auditability; narrowing the gate back down would
    contradict that deliberate broadening instead of resolving the mismatch."""
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to review")
    if not gates.stage_review_active(state):
        return Directive(
            False, state.node, "noop",
            f"the acceptance-judge gate is not active for this session "
            f"(weight_class={state.weight_class}, "
            f"AGENTCTL_STAGE_REVIEW={os.environ.get('AGENTCTL_STAGE_REVIEW', '<unset>')}); "
            "stage-review records an override of that gate and has nothing to override here",
        )
    observation = getattr(args, "observation", None)
    if observation is None:
        observation = stage.criterion.observation or ""
    _record_stage_review(
        state,
        StageReview(
            stage_index=stage.index,
            verdict=args.verdict,
            reviewer=getattr(args, "reviewer", "") or "",
            concerns=list(getattr(args, "concerns", None) or []),
            note=getattr(args, "note", "") or "",
            observation_sha256=_observation_sha256(observation),
        ),
        from_judge=False,
    )
    state.log("stage_review", stage=stage.index, verdict=args.verdict,
              reviewer=getattr(args, "reviewer", "") or "")
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"stage review recorded for stage {stage.index} (verdict={args.verdict}); "
        "record-result --status passed will re-check the acceptance gate against it",
    )


def cmd_code_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a code-reviewer/human review of the active spawn:developer stage's
    produced code, backing the code-review gate. Mirrors cmd_stage_review — purely a
    recorder; the COGNITION (the code-reviewer specialization judging the diff, or a
    human authoring an override) happens outside. There is no automated judge for
    code review (unlike the acceptance path's fail-open cheap judge) — this command
    is the only path that ever writes a CodeReview. The verdict is bound to a
    caller-supplied --code-ref (hashed via _digest, never recomputed from git — gates.py
    stays pure), so gates.code_review_blockers can reject a drift when record-result
    later supplies a different --code-ref."""
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to review")
    if not stage.needs_control():
        return Directive(
            False, state.node, "noop",
            f"stage {stage.index} is not a spawn:developer stage; code-review applies "
            "only to developer-produced code",
        )
    code_ref = getattr(args, "code_ref", None) or None
    not_checked = getattr(args, "not_checked", None) or ""
    if args.verdict == gates.CODE_REVIEW_VERDICTS[0] and not not_checked:
        # CODE_REVIEW_VERDICTS[0] is "pass" — an approving verdict with no stated
        # axis coverage is exactly the omission verify-right-axis-report-honestly
        # forbids: a reviewer approving without running tests and saying nothing
        # about it (issue #92). Refused, not just noted — nothing is recorded.
        return Directive(
            False, state.node, "rejected",
            "pass verdict requires --not-checked (axes not checked, or 'none')",
        )
    if gates._code_review_for(state, stage.index) is not None:
        # A re-review of a stage already reviewed once — the code-review axis's
        # round-release counter (item A / issue #96), mirroring plan_review_rounds'
        # per-resubmission increment; reset by cmd_approve/cmd_replan alongside it.
        state.code_review_rounds += 1
    _record_code_review(
        state,
        CodeReview(
            stage_index=stage.index,
            verdict=args.verdict,
            reviewer=getattr(args, "reviewer", "") or "",
            concerns=list(getattr(args, "concerns", None) or []),
            note=getattr(args, "note", "") or "",
            code_sha256=_digest(code_ref) if code_ref else "",
            not_checked=not_checked,
        ),
    )
    state.log("code_review", stage=stage.index, verdict=args.verdict,
              reviewer=getattr(args, "reviewer", "") or "", not_checked=not_checked)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"code review recorded for stage {stage.index} (verdict={args.verdict}); "
        "record-result --status passed will re-check the code-review gate against it",
    )


def cmd_accept(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the plan-level AcceptanceReview: the ORDER's customer comparing the
    delivered PRODUCT against every declared requirement, once — the acceptance half
    of Defect 2 (control checks result-against-goal per stage, repeatedly;
    acceptance checks product-against-order, once, and is recorded).

    Author-matched at WRITE time against [meta.order].customer_id (a mismatch is
    refused outright — this is not a gate to degrade past, it is a wrong-person
    writing the record). Completeness (every declared requirement id covered) and
    negative-verdict blocking are deferred to gates.resolution_blockers, which
    re-reads the order fresh rather than trusting what was true at write time.

    Corroboration mirrors the stage-level acceptance path: the cheap fail-open judge
    (advisor.acceptance_judge) is consulted unless --bypass is given; an unreachable
    judge refuses the write and directs the caller to --bypass --bypass-reason rather
    than silently waving the review through. A --bypass is recorded as an
    AcceptanceBypass alongside the AcceptanceReview (never standalone — see
    AcceptanceBypass's docstring for why resolution_blockers never reads it)."""
    state = _require(store, args.session)
    # Guarded exactly like _refresh_venue_fields: an absent or unreadable plan_path is a
    # refusal Directive, never a PlanError escaping the CLI. Acceptance IS the comparison
    # against the order that plan declares, so with no plan there is nothing to record
    # against — and gates.resolution_blockers refuses the same shape from the other side.
    doc = None
    plan_digest = ""
    if state.plan_path:
        try:
            doc, _data, plan_digest = load_plan_with_digest(state.plan_path, strict=False)
        except (OSError, PlanError):
            doc = None
    if doc is None:
        return Directive(
            False, state.node, "noop",
            "cannot read the plan to accept against "
            f"({state.plan_path or 'no plan_path on this session'}); acceptance compares the "
            "delivered product with the order that plan declares",
        )
    accepted_digest = state.accepted_plan_digest or ""
    if (not gates.plan_digest_equivalent(state, plan_digest, accepted_digest)
            and (accepted_digest or gates.acceptance_active(state))):
        # The per-requirement binding below is computed from `doc`; a file that is not
        # the accepted version would bind the review to bytes the engine never approved
        # while stamping it with the accepted digest. An acceptance-active session with
        # no accepted digest has no version to bind to, so it is refused the same way.
        return Directive(
            False, state.node, "noop",
            f"plan edited since it was accepted ({state.plan_path}); replan first so the "
            "engine's accepted version and the file agree, then accept against it"
            if accepted_digest else
            f"no accepted plan version is recorded for this session ({state.plan_path}); "
            "submit and approve the plan before accepting against it",
        )
    order = doc.meta.order
    author = getattr(args, "author", "") or ""
    if order is not None and order.customer_id and author != order.customer_id:
        return Directive(
            False, state.node, "noop",
            f"acceptance author {author!r} does not match order customer_id "
            f"{order.customer_id!r}; record it as the customer of record, or correct --author",
        )
    verdicts, verdict_errors = _parse_verdicts(getattr(args, "verdict", None) or [])
    if verdict_errors:
        return Directive(
            False, state.node, "noop",
            "invalid --verdict argument(s): " + "; ".join(verdict_errors),
            data={"errors": verdict_errors},
        )
    bypass = bool(getattr(args, "bypass", False))
    bypass_reason = getattr(args, "bypass_reason", "") or ""
    note = getattr(args, "note", "") or ""
    if bypass and not bypass_reason:
        return Directive(
            False, state.node, "noop",
            "--bypass requires --bypass-reason (a bypass is a reasoned override, not a shrug)",
        )
    if not verdicts:
        # Not a bypass-only rule: a verdictless review on the ordinary path records that
        # nothing was compared, and resolution's completeness check cannot catch it —
        # `missing` is empty whenever the review omits nothing because the order declares
        # nothing. Refuse at write time, where the emptiness is still visible.
        return Directive(
            False, state.node, "noop",
            "a bypass requires an accompanying AcceptanceReview: supply at least one --verdict"
            if bypass else
            "acceptance requires at least one --verdict: a review with no verdicts compares "
            "nothing against the order",
        )
    judge_reason = "no judge attempted (--bypass)"
    if not bypass:
        expected_text = "; ".join(
            f"{r.id}: {r.text}" if r.text else r.id for r in (order.requirements if order else [])
        )
        observed_text = note or "; ".join(
            f"{v.requirement_id}:{v.verdict}" for v in verdicts
        )
        judge_runner = runner if runner is not None else advisor.subprocess_runner
        verdict, judge_reason = advisor.acceptance_judge(
            observed_text, expected_text, judge_runner, enabled=True,
            timeout=advisor._ACCEPTANCE_JUDGE_TIMEOUT_S,
        )
        if verdict is None:
            return Directive(
                False, state.node, "noop",
                f"acceptance judge unreachable ({judge_reason}); re-run with "
                "--bypass --bypass-reason '<why this acceptance stands without judge "
                "corroboration>'",
                data={"reason": judge_reason},
            )
    verdicted_ids = {v.requirement_id for v in verdicts}
    state.acceptance_review = AcceptanceReview(
        author=author, verdicts=verdicts, note=note,
        plan_sha256=state.accepted_plan_digest or "",
        requirement_bindings={
            rid: binding for rid, binding in acceptance_requirement_bindings(doc).items()
            if rid in verdicted_ids
        },
    )
    if bypass:
        state.acceptance_bypass = AcceptanceBypass(
            reason=bypass_reason, reviewer=author, note=note,
        )
    state.log("accept", author=author, verdicts=len(verdicts), bypass=bypass)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"acceptance review recorded ({len(verdicts)} verdict(s), bypass={bypass}); "
        "resolution will re-check completeness, verdicts, and plan-digest freshness",
    )


def register(sub, add):
    sp = add("plan-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.PLAN_REVIEW_VERDICTS), required=True,
                    help="pass = clears the gate; revise = blocks; override = user's "
                         "explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (the user, for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a concern the thinker raised, written `blocking: [re:<concern-id>] "
                         "<concern>` or `note: [re:<concern-id>] <concern>` (repeatable; an "
                         "untagged concern is refused, `blocking:` on a pass is refused, "
                         "`re:<concern-id>` must name a recorded concern)")
    sp.add_argument("--concern-id", dest="concern_ids", action="append", default=None,
                    help="stable id for the --concern at the same position (repeatable, "
                         "positionally paired); omitted concerns get a derived id "
                         "(c0, c1, ...) via state.plan_review_concern_ids — risk-accept "
                         "binds to this id, never to the concern's prose")
    sp.add_argument("--customer-question", dest="customer_questions", action="append",
                    default=None,
                    help="a question only the customer can decide that this review raised "
                         "(repeatable); recorded as a premise candidate "
                         "`qrev-<review id>-<N>` that blocks approve until dispositioned "
                         "with question-candidate-dispose — apart from --concern, which "
                         "is a plan remark the coordinator fixes itself")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--target", default=None,
                    help="plan file reviewed (defaults to the session's current plan_path; "
                         "pass the NEW plan for a replan-time review)")
    sp.add_argument("--plan-digest", dest="plan_digest", default=None,
                    help="sha256 the REVIEWER computed from its OWN read of the target "
                         "plan; cross-checked against the live bytes and stored as the "
                         "attested plan_sha256. A passing verdict does NOT bind without "
                         "it — a reviewer that could not read the plan cannot attest.")
    sp.add_argument("--transcript", default=None,
                    help="with --scope order: path to the reviewer's transcript, stored "
                         "on the order review record")
    sp.add_argument("--scope", default=None,
                    help="'order' to review an order draft's understanding (--target is "
                         "the draft; defaults to the submitted one), 'stage:<n>' to bind "
                         "this review to one stage instead of the "
                         "whole plan, or 'topo:<id>' (a pair, e.g. topo:3-1 or "
                         "topo:base-2; a unit, e.g. topo:unit:base or topo:unit:3) to "
                         "record one pair or unit review; omitted "
                         "(or '') means whole-plan, the only kind that existed before stage 5")
    sp.add_argument("--escalate-pairs", dest="escalate_pairs", action="store_true",
                    help="with a unit scope: re-open the pairs touching that unit that a "
                         "tier-2 edit carried without a review (held), so each is owed a "
                         "review of the edited content like a tier-3 edit")
    sp.add_argument("--findings-blocking", dest="findings_blocking", type=int, default=None,
                    help="count of blocking findings this round produced (audit trail)")
    sp.add_argument("--findings-nonblocking", dest="findings_nonblocking", type=int,
                    default=None,
                    help="count of non-blocking findings this round produced (audit trail)")
    sp.add_argument("--regression-command", dest="regression_command", default=None,
                    help="a command the engine runs in repo_root to demonstrate a "
                         "regression against a plan version that already carries a "
                         "recorded whole-plan/stage PASS this approval cycle. Required "
                         "(and must exit non-zero) for a 'revise' after such a pass to "
                         "block approve/replan — a command that was never run, or that "
                         "exits 0, is refused as evidence. Ignored on every other "
                         "verdict/situation.")
    sp = add("plan-review-delta"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default=None,
                    help="plan file to diff against recorded reviews (defaults to the "
                         "session's current plan_path)")
    sp = add("plan-review-walk"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file to walk (defaults to the session's current plan_path); "
                         "every digest, pair and depth is computed from it")
    sp.add_argument("--format", choices=("text", "json"), default="text",
                    help="'json' emits the walk as {plan_path, discharges, levels}")
    sp = add("plan-review-pair-history"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file the pair's records were taken against (defaults to the "
                         "session's current plan_path)")
    sp.add_argument("--pair", required=True,
                    help="the review pair id (as listed by plan-review-walk)")
    sp = add("plan-review-compose"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file to compose a whole-plan pass for (defaults to the "
                         "session's current plan_path; pass the NEW plan at replan time)")
    sp = add("risk-accept"); sp.add_argument("--session", required=True)
    sp.add_argument("--scope", default=None,
                    help="'' or 'stage:<n>' — the review scope the accepted concern was "
                         "raised in (must match a recorded plan-review's --scope)")
    sp.add_argument("--concern-id", dest="concern_id", default="",
                    help="the concern id this acceptance answers (see plan-review's "
                         "--concern-id, or its derived c0/c1/... form)")
    sp.add_argument("--basis", default="",
                    help="why the risk is being accepted rather than fixed — mirrors "
                         "question-dispose --disposition assumed's --basis")
    sp.add_argument("--risk", default="",
                    help="what could go wrong by accepting rather than fixing — mirrors "
                         "question-dispose --disposition assumed's --risk")
    sp.add_argument("--author", default="",
                    help="who is accepting the risk — an identity token, not narrative")
    sp = add("accept-protected-grant"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="the protected-action id the plan's grants cover (shown in the grants "
                         "block of present-plan, e.g. push-trunk)")
    sp.add_argument("--note", required=True,
                    help="why the user accepts that the grants cover this action")
    sp = add("stage-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.STAGE_REVIEW_VERDICTS), required=True,
                    help="pass = clears the acceptance gate; revise = blocks; override = "
                         "user's explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (the user, for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a blocking concern the reviewer raised (repeatable; audit trail)")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--observation", default=None,
                    help="the observation being reviewed (defaults to the stage's current "
                         "observation); binds the verdict to these exact bytes")
    sp = add("code-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.CODE_REVIEW_VERDICTS), required=True,
                    help="pass = clears the code-review gate; revise = blocks; override = "
                         "user's explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (code-reviewer, or the user for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a blocking concern the reviewer raised (repeatable; audit trail)")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--code-ref", dest="code_ref", default=None,
                    help="the reviewed-code revision/digest the reviewer names; binds the "
                         "verdict so a later record-result with a different --code-ref is stale")
    sp.add_argument("--not-checked", dest="not_checked", default=None,
                    help="axes this review did NOT check (e.g. 'tests'), or 'none' — required "
                         "for a pass verdict, so an unchecked axis is an explicit field rather "
                         "than a silent omission (verify-right-axis-report-honestly)")
    sp = add("accept"); sp.add_argument("--session", required=True)
    sp.add_argument("--author", default="",
                    help="acceptance author id; must match [meta.order].customer_id when set")
    sp.add_argument("--verdict", dest="verdict", action="append", default=None,
                    help="requirement verdict as '<requirement_id>|<pass|fail|withdrawn>[|<note>]'; "
                         "repeatable, one per declared order requirement; "
                         "'withdrawn' requires a non-empty note naming who withdrew it and why")
    sp.add_argument("--note", default="",
                    help="free-text note on the acceptance review as a whole")
    sp.add_argument("--bypass", action="store_true",
                    help="record an AcceptanceBypass alongside the review, for when the "
                         "acceptance judge is unreachable; requires --bypass-reason and "
                         "at least one --verdict")
    sp.add_argument("--bypass-reason", dest="bypass_reason", default="",
                    help="why this acceptance stands without judge corroboration "
                         "(required with --bypass)")
