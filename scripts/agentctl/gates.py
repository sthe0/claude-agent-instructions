"""Gate registry: the hard approval/resolution gates the engine enforces.

A gate is a named GateRecord on the SessionState plus a guardian predicate that
says whether the state is allowed to pass through it. The two gates mirror the
prose hard gates:

  - plan_approval : PLAN_READY -> APPROVED needs explicit user approval. The
    engine cannot infer approval from silence; `armed` once a plan is submitted,
    `passed` only when cli.approve records an explicit approver.
  - resolution    : RESOLUTION -> RESOLVED needs every stage PASSED and an
    explicit user confirmation (measurable: check ran; acceptance: user accepted).

Guardians return a list of human-readable blockers ([] == may pass). cli.py calls
the guardian before flipping `passed`, so an illegal pass is impossible.

Purity: everything here is PURE in the sense that matters for a gate — it reads
recorded state and (for the two content-binding guardians) a plan/observation's own
bytes via hashlib, but NEVER shells out, opens a socket, or talks to the network. The
subprocess-shaped cognition (the thinker review, the acceptance judge) lives in the
impure cli layer; a guardian only reads the RECORD that cognition left behind.
scripts/tests/ast_purity.py mechanizes this: it admits file I/O but rejects any
{subprocess, socket, urllib, requests, http} reach, and verify-agentctl asserts it
over this module.

Fail-open judge, fail-closed gate — the load-bearing asymmetry. The acceptance judge
(advisor.acceptance_judge) is fail-open: a disabled/errored/timed-out judge returns NO
verdict rather than a false pass, so it can never HANG or wrongly-block a coordination
step. Because the judge fails open, the gate must fail CLOSED: acceptance_review_blockers
treats a missing verdict as a blocker (no StageReview => blocked), so "the judge didn't
run" degrades to "not yet accepted", never to "accepted by default". The two directions
compose to: an unavailable judge stalls the pass safely instead of waving it through.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

from lib import config_root
from lib import hook_wiring

from . import delivery
from .config import Thresholds
from .plan import PlanError, changed_parts, load_plan, order_place, stage_question_key
from .state import Node, SessionState, StageStatus, WeightClass
from .state import plan_review_concern_ids as _plan_review_concern_ids
from .state import plan_review_scope_for_stage as _plan_review_scope_for_stage
from .state import plan_review_scope_stage_index as _plan_review_scope_stage_index
from .state import PLAN_PRESENTATION_KIND_ESSENCE as _PLAN_PRESENTATION_KIND_ESSENCE
from .state import Stage as _Stage
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import normalize_string as _normalize_string

# Verdict vocabulary for the plan-review gate. `pass` clears it; `override` clears
# it only as the user's explicit deadlock escape (requires reviewer + note);
# anything else (`revise`, unknown) blocks.
_PLAN_REVIEW_PASS = "pass"
_PLAN_REVIEW_REVISE = "revise"
_PLAN_REVIEW_OVERRIDE = "override"
PLAN_REVIEW_VERDICTS = (_PLAN_REVIEW_PASS, _PLAN_REVIEW_REVISE, _PLAN_REVIEW_OVERRIDE)

# Verdict vocabulary for the acceptance-review gate — the same shape as plan-review:
# `pass` clears; `override` clears only as the user's explicit escape (reviewer + note);
# `revise`/unknown blocks. The cheap judge maps yes->pass, no->revise.
_STAGE_REVIEW_PASS = "pass"
_STAGE_REVIEW_REVISE = "revise"
_STAGE_REVIEW_OVERRIDE = "override"
STAGE_REVIEW_VERDICTS = (_STAGE_REVIEW_PASS, _STAGE_REVIEW_REVISE, _STAGE_REVIEW_OVERRIDE)

# Verdict vocabulary for the code-review gate — the same shape as acceptance-review:
# `pass` clears; `override` clears only as the user's explicit escape (reviewer + note);
# `revise`/unknown blocks.
_CODE_REVIEW_PASS = "pass"
_CODE_REVIEW_REVISE = "revise"
_CODE_REVIEW_OVERRIDE = "override"
CODE_REVIEW_VERDICTS = (_CODE_REVIEW_PASS, _CODE_REVIEW_REVISE, _CODE_REVIEW_OVERRIDE)


def plan_approval_blockers(state: SessionState) -> list[str]:
    out: list[str] = []
    if not state.plan_path:
        out.append("no plan artifact submitted")
    if not state.plan_verified:
        out.append("plan not verified (structure check failed or not run)")
    return out


def resolution_blockers(state: SessionState) -> list[str]:
    out: list[str] = []
    if not state.stages:
        out.append("no stages defined")
    unpassed = [s.index for s in state.stages if s.outcome.status != StageStatus.PASSED.value]
    if unpassed:
        out.append(f"stages not PASSED: {unpassed}")
    out.extend(_acceptance_review_resolution_blockers(state))
    return out


def acceptance_active(state: SessionState) -> bool:
    """Whether resolution requires a recorded plan-level AcceptanceReview.

    Scoped exactly like stage_review_active/code_review_active: chat/small-change
    sessions never pay this cost; SUBSTANTIVE sessions always do. AGENTCTL_ACCEPTANCE
    overrides in both directions ("1" forces on, "0" forces off). Deliberately its OWN
    env var rather than reusing AGENTCTL_STAGE_REVIEW — the per-stage judge gate and
    the plan-level acceptance gate are two distinct Defect-2 halves (control, repeated
    per stage; acceptance, once for the whole plan) and must be independently
    killable. Env-only reads, no file/subprocess I/O, so the gate stays pure."""
    env = os.environ.get("AGENTCTL_ACCEPTANCE")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def _acceptance_review_resolution_blockers(state: SessionState) -> list[str]:
    """Precondition guardian folded into resolution_blockers: the order's customer
    must have recorded a plan-level AcceptanceReview comparing the delivered PRODUCT
    against every declared requirement — the acceptance half of Defect 2 (control
    compares result with goal at every stage, repeatedly; acceptance compares product
    with order once, and is recorded). PURE: file I/O only (re-reading the plan via
    plan.load_plan, itself pure — see plan.py's own import list), never a
    subprocess/socket/network reach.

    Inactive (chat / small-change / AGENTCTL_ACCEPTANCE=0) => [] always. Active
    checks, in order:
      - a review must exist — else blocked (fail-CLOSED: an all-PASSED session with
        no acceptance is not resolved, only controlled);
      - review.plan_sha256 must equal state.accepted_plan_digest — a mismatch means
        the plan was replaced (accept, then approve/replan on a new plan) since the
        review was written, so the review is STALE and is treated as though absent
        (same blocker as the missing-review case, not a distinct message — the
        session's observable state is "no current acceptance" either way);
      - the CURRENT plan must be READABLE — an absent plan_path, or bytes that no
        longer load, means the order this review claims to have satisfied cannot be
        re-read, and the two checks below would then run against an empty requirement
        list and pass vacuously. Blocked instead: the gate refuses what it cannot
        check, rather than degrading into "an AcceptanceReview object exists";
      - every requirement id the CURRENT plan's [meta.order] declares must carry a
        verdict — read fresh rather than trusted from write time, though a matched
        digest above already implies the plan (and so the order) has not changed
        since the review was written. A plan that loads but declares no [meta.order]
        contributes no ids, and this check is then genuinely empty rather than
        degraded — an orderless plan has nothing to accept against, and only a
        non-substantive session forced active by AGENTCTL_ACCEPTANCE=1 can be in
        that position (the submission seam requires an order of every substantive
        plan);
      - every verdict must be 'pass' — a single 'fail' blocks resolution outright;
        acceptance is the product-against-order check, and a failing requirement is
        not the engine's to wave through.

    Deliberately never reads state.acceptance_bypass: a bypass is a resolution
    OUTCOME the engine surfaces (verify-final), never a resolution PRECONDITION the
    engine evaluates — see AcceptanceBypass's docstring for why."""
    if not acceptance_active(state):
        return []
    review = state.acceptance_review
    if review is None:
        return [
            "no AcceptanceReview recorded — the order's customer must record "
            "acceptance (agentctl accept) before resolution"
        ]
    if (review.plan_sha256 or "") != (state.accepted_plan_digest or ""):
        return [
            "no AcceptanceReview recorded — the recorded review is stale (it was "
            "written against a different plan version than the one currently "
            "accepted) and is treated as absent; re-run accept on the current plan"
        ]
    doc = None
    if state.plan_path:
        try:
            doc = load_plan(state.plan_path, strict=False)
        except (OSError, PlanError):
            doc = None
    if doc is None:
        return [
            "the accepted plan cannot be read "
            f"({state.plan_path or 'no plan_path on this session'}), so the order this "
            "AcceptanceReview claims to satisfy cannot be re-read; restore the plan file "
            "and re-run accept"
        ]
    order = doc.meta.order
    requirement_ids = [r.id for r in order.requirements] if order is not None else []
    verdicted = {v.requirement_id: v.verdict for v in review.verdicts}
    missing = [rid for rid in requirement_ids if rid not in verdicted]
    if missing:
        return [
            f"AcceptanceReview omits declared requirement id(s) {missing} — every "
            "order requirement needs a verdict before resolution"
        ]
    failing = [rid for rid, v in verdicted.items() if v != "pass"]
    if failing:
        return [
            f"AcceptanceReview carries a non-pass verdict on requirement id(s) "
            f"{sorted(failing)} — resolution is blocked until every requirement passes"
        ]
    return []


def difficulty_blockers(state: SessionState) -> list[str]:
    """Precondition guardian for `replan` while in the DIAGNOSING sub-spine: the
    overcome-difficulty cycle (declaration -> investigation -> critique) must be
    complete before a plan may be re-normed. This is an INTERNAL command
    precondition, NOT a tool-intercepting gate — it is deliberately absent from
    GUARDIANS so verify-agentctl does not require a hook to cover it. [] == ok."""
    if state.node != Node.DIAGNOSING.value:
        return []  # replan outside the difficulty cycle (e.g. spawn REPLAN marker) is unconstrained
    d = state.difficulty
    if d is None:
        return ["difficulty cycle not started: run declare, then investigate, then critique"]
    missing: list[str] = []
    if d.declaration is None:
        missing.append("declaration (run: declare)")
    if d.investigation is None:
        missing.append("investigation (run: investigate)")
    if d.critique is None:
        missing.append("critique (run: critique)")
    if missing:
        return ["difficulty record incomplete — replan blocked until: " + ", ".join(missing)]
    # Shape enforcement: presence of the three sections is not enough — the record
    # must be well-formed. Mechanical shape only (non-empty fields, hypothesis count,
    # distinctness, anti-template); the engine never judges the *quality* of the content.
    shape: list[str] = []
    decl = d.declaration
    for label, value in (("expected", decl.expected), ("actual", decl.actual), ("mismatch", decl.mismatch)):
        if not (value or "").strip():
            shape.append(f"declaration.{label} is empty")
    good_hyps = [h for h in (d.investigation.hypotheses or []) if (h or "").strip()]
    if len(good_hyps) < 2:
        shape.append(f"investigation needs >=2 hypotheses (have {len(good_hyps)})")

    # Hypothesis distinctness: they must be pairwise distinct after normalization
    distinct_hyps = set(_normalize_string(h) for h in good_hyps)
    if len(distinct_hyps) < len(good_hyps):
        shape.append(f"investigation hypotheses must be distinct after normalization (have {len(good_hyps)}, but only {len(distinct_hyps)} distinct)")

    # Declaration anti-template: fields must not be placeholders and must be distinct
    normalized_decl = {
        "expected": _normalize_string(decl.expected),
        "actual": _normalize_string(decl.actual),
        "mismatch": _normalize_string(decl.mismatch),
    }

    for label, norm_value in normalized_decl.items():
        if norm_value in _PLACEHOLDER_SET:
            shape.append(f"declaration.{label} is a placeholder (must be a real observation: {norm_value!r})")

    # Check if expected == actual (normalized) and non-empty
    if normalized_decl["expected"] == normalized_decl["actual"] and normalized_decl["expected"]:
        shape.append("declaration fields must be distinct (expected and actual must differ)")

    if shape:
        return ["difficulty record under-specified — replan blocked: " + "; ".join(shape)]
    return []


def normalization_blockers(state: SessionState) -> list[str]:
    """Precondition guardian for `replan` at DIAGNOSING closure: a difficulty is a
    norm-failure, and because activity is constituted by reproduction, closing one
    REQUIRES re-norming the reproducible factor it exposed (перенормирование). Like
    difficulty_blockers this is an INTERNAL command precondition — deliberately absent
    from GUARDIANS so verify-agentctl requires no hook. [] == may close.

    Scoped to the DIAGNOSING-closure path: [] outside DIAGNOSING, and [] while the
    difficulty cycle is still incomplete (difficulty_blockers owns that case — this
    gate never double-reports it). Once the cycle is complete, a Normalization record
    (a non-empty factor) is required; its absence blocks unless cmd_replan's explicit
    --normalization-waiver escape is taken (a one-off, non-reproducible factor). The
    LEVEL (note/leaf/principle) is payoff-gated cognition the gate never inspects."""
    if state.node != Node.DIAGNOSING.value:
        return []
    d = state.difficulty
    if d is None or not d.complete():
        return []  # difficulty_blockers owns the incomplete-cycle case
    n = d.normalization
    if n is None or not (n.factor or "").strip():
        return ["difficulty closure requires re-norming — run: normalize (record the "
                "reproducible factor), or replan --normalization-waiver <reason> if the "
                "factor is genuinely one-off"]
    return []


def failure_address_blockers(state: SessionState) -> list[str]:
    """Precondition guardian for `replan` at DIAGNOSING closure: a затруднение is overcome
    by fixing its обеспечение, and the fault-address is ambiguous until ROUTED — inadequate
    РЕСУРСНОЕ обеспечение ('ресурсное': материал/средство) or inadequate НОРМАТИВНОЕ
    обеспечение ('нормативное': норма/способ), or explicitly not_applicable. Two special
    cases of ONE act («норма — тоже ресурс»), both reducing reflexively to знание — NOT an
    is/ought tag (ADR-0004 §R2). Like difficulty_blockers/normalization_blockers it is an
    INTERNAL command precondition — deliberately absent from GUARDIANS so verify-agentctl
    requires no hook. PURE: reads only the recorded Critique; no subprocess/socket/network.
    [] == ok.

    Scoped to the DIAGNOSING-closure path: [] outside DIAGNOSING, and [] while the
    difficulty cycle is still incomplete (difficulty_blockers owns that case). Once the
    cycle is complete, the routing must be DECIDED — a bare None (omission) blocks, while an
    EXPLICIT not_applicable is a legal opt-out that clears. The gate checks ONLY non-None:
    ANY recorded value clears, so a legacy record carrying an OLD сущее/должное value (the
    rejected v3 R2 typing) is grandfathered, never re-blocked. Bogus values never reach a
    persisted record — cmd_critique and the argparse `choices` reject them at write time."""
    if state.node != Node.DIAGNOSING.value:
        return []
    d = state.difficulty
    if d is None or not d.complete():
        return []  # difficulty_blockers owns the incomplete-cycle case
    fa = d.critique.failure_address
    if fa is None:
        return ["difficulty closure requires routing the fault — record the critique with "
                "--failure-address (ресурсное: inadequate ресурсное обеспечение, "
                "материал/средство | нормативное: inadequate нормативное обеспечение, "
                "норма/способ | not_applicable: routing does not apply)"]
    return []


def plan_review_active(state: SessionState) -> bool:
    """Whether the thinker-review gate applies to this session.

    Scoped by weight class alone: chat and small-change sessions never pay the
    review cost; SUBSTANTIVE sessions always do. AGENTCTL_PLAN_REVIEW overrides in
    both directions ("1" forces on, "0" forces off). Deliberately NOT routed
    through advisor.resolve_enabled: the advisor is an optional cost knob and its
    kill switch (AGENTCTL_ADVISOR=0 / advisor-mode=off) must not silently defeat a
    mandatory review gate — the gate's only off switch is its own env var.
    Env-only reads, no file/subprocess I/O, so the gate stays pure."""
    env = os.environ.get("AGENTCTL_PLAN_REVIEW")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def _plan_review_content_stale(pr, target_plan: str) -> str | None:
    # #16: the coordinator edits plans in place, so a same-path binding is not a
    # content binding — recompute the plan's sha256 and reject a drift. Fail-open:
    # an unreadable target degrades to the path-only binding above, never wedging
    # the gate on a transient read error.
    if not pr.plan_sha256:
        return None
    try:
        current = hashlib.sha256(Path(target_plan).read_bytes()).hexdigest()
    except OSError:
        return None
    if current == pr.plan_sha256:
        return None
    return (
        "thinker review is stale — the plan content at "
        f"{target_plan!r} changed since it was reviewed; re-run plan-review"
    )


def _risk_acceptance_stale(ra, doc) -> bool:
    """Mirrors how the review the acceptance answers would itself judge staleness
    at that scope: a moved order/meta always invalidates; a moved stage invalidates
    only an acceptance scoped to that stage — a whole-plan concern's acceptance
    survives an unrelated stage edit, exactly as the whole-plan review's own
    verdict does (see _plan_review_blockers_coverage)."""
    meta_moved, moved_stages = changed_parts(doc, {"meta": ra.meta_digest, "stages": ra.stage_keys})
    if meta_moved:
        return True
    stage_index = _plan_review_scope_stage_index(ra.scope)
    return stage_index is not None and stage_index in moved_stages


def _risk_acceptance_superseded(ra, state: SessionState) -> bool:
    """True for a non-stale acceptance whose concern id survived a plan edit but
    whose text at that id no longer matches what was actually accepted — a
    rephrased/replaced concern at the same id, distinct from `_risk_acceptance_stale`
    (which drops an acceptance whose plan VERSION moved; this instead flags one
    whose version is current but whose concern PROSE moved under it)."""
    review = state.plan_stage_reviews.get(ra.scope) if ra.scope else state.plan_review
    if review is None:
        return True
    ids = _plan_review_concern_ids(review)
    if ra.concern_id not in ids:
        return True
    current_text = review.concerns[ids.index(ra.concern_id)]
    return _normalize_string(current_text) != _normalize_string(ra.concern_text)


def _concern_discharged(scope: str, concern_id: str, concern_text: str, state: SessionState, doc) -> bool:
    return any(
        ra.scope == scope
        and ra.concern_id == concern_id
        and ra.concern_text
        and _normalize_string(ra.concern_text) == _normalize_string(concern_text)
        and not _risk_acceptance_stale(ra, doc)
        for ra in state.risk_acceptances
    )


def _plan_review_verdict_blockers(pr, *, state: SessionState | None = None, doc=None) -> list[str]:
    if pr.verdict == _PLAN_REVIEW_PASS:
        # CONTRACT INVERSION (reviewer-attested binding): plan_sha256 is now the
        # digest the REVIEWER attested via --plan-digest, not an engine auto-
        # compute. An EMPTY hash on the pass path means the reviewer supplied no
        # proof it read the plan — so a sibling-session reviewer that could not
        # read the plan cannot bind a pass; block and let the difficulty surface.
        # The override branch below is NOT reached by this — the deadlock escape
        # stays attestation-free.
        if not pr.plan_sha256:
            return [
                "thinker review is not attested — the reviewer supplied no "
                "plan-digest proving it read the plan; re-run plan-review with "
                "--plan-digest"
            ]
        return []
    if pr.verdict == _PLAN_REVIEW_OVERRIDE:
        missing = []
        if not (pr.reviewer or "").strip():
            missing.append("reviewer")
        if not (pr.note or "").strip():
            missing.append("note")
        if missing:
            return ["thinker review override requires a non-empty " + " and ".join(missing) + " (the user's explicit escape reason)"]
        return []
    default = [f"thinker review verdict is {pr.verdict!r} — plan blocked until a passing review (or an explicit override) is recorded"]
    # A revise verdict clears only when EVERY concern is discharged — an empty
    # concerns list must never vacuously discharge (nothing to check is not the
    # same as everything checked out), and with no state/doc to check acceptances
    # against, discharge cannot be established at all.
    if pr.verdict != _PLAN_REVIEW_REVISE or not pr.concerns or state is None or doc is None:
        return default
    if all(
        _concern_discharged(pr.scope, cid, text, state, doc)
        for cid, text in zip(_plan_review_concern_ids(pr), pr.concerns)
    ):
        return []
    return default


def _plan_review_blockers_whole(pr, target_plan: str | None, *, state: SessionState | None = None, doc=None) -> list[str]:
    if pr is None:
        return ["no thinker review recorded — run: plan-review (thinker verdict required before this plan is approved/applied)"]
    if not target_plan or pr.plan_path != target_plan:
        return [
            "thinker review is stale — it examined "
            f"{pr.plan_path!r} but the target plan is {target_plan!r}; re-run plan-review on the current plan"
        ]
    stale = _plan_review_content_stale(pr, target_plan)
    if stale:
        return [stale]
    return _plan_review_verdict_blockers(pr, state=state, doc=doc)


def _plan_review_baseline(pr) -> dict:
    return {"meta": pr.reviewed_meta_digest, "stages": pr.reviewed_stage_keys}


def _plan_review_blockers_coverage(state: SessionState, target_plan: str, doc) -> list[str]:
    """The whole-plan review covers everything it passed on the day its recorded
    keys still match; a moved stage owes its own stage-scoped pass at the CURRENT
    key. A moved meta/order always demands a fresh whole-plan review — a
    stage-scoped reviewer never saw the order, so it cannot re-cover a meta
    change no matter how current its own stage's key is."""
    whole = state.plan_review
    if whole is None:
        return ["no thinker review recorded — run: plan-review (thinker verdict required before this plan is approved/applied)"]
    if not target_plan or whole.plan_path != target_plan:
        return [
            "thinker review is stale — it examined "
            f"{whole.plan_path!r} but the target plan is {target_plan!r}; re-run plan-review on the current plan"
        ]
    meta_moved, moved_stages = changed_parts(doc, _plan_review_baseline(whole))
    if meta_moved:
        return [
            "thinker review is stale — the plan's meta/order changed since it was "
            "reviewed; re-run plan-review"
        ]
    blockers = _plan_review_verdict_blockers(whole, state=state, doc=doc)
    if blockers:
        return blockers
    for index in sorted(moved_stages):
        scope = _plan_review_scope_for_stage(index)
        spr = state.plan_stage_reviews.get(scope)
        if spr is None or spr.plan_path != target_plan:
            return [
                f"stage {index} changed since the whole-plan review; needs its own "
                f"pass — run: plan-review --scope {scope}"
            ]
        stage_meta_moved, stage_moved = changed_parts(doc, _plan_review_baseline(spr))
        if stage_meta_moved:
            return [
                f"thinker review for stage {index} is stale — the plan's meta/order "
                "changed since it was reviewed; re-run plan-review (a stage-scoped "
                "review cannot cover a meta change)"
            ]
        if index in stage_moved:
            return [
                f"thinker review is stale — stage {index} changed again since "
                f"{scope!r} was reviewed; re-run plan-review --scope {scope}"
            ]
        blockers = _plan_review_verdict_blockers(spr, state=state, doc=doc)
        if blockers:
            return blockers
    return []


#: Message substituted for whatever `plan_review_blockers` would otherwise return once
#: the round-release fires (see `plan_review_round_release_active`). Names the two
#: decisions the ORDER, not the engine, must resolve — a scope/risk question is the
#: customer's to answer, so this never clears the block by itself; it only stops
#: demanding a further review and routes to an explicit choice instead.
_PLAN_REVIEW_ROUND_RELEASE_MESSAGE = (
    "review round budget exhausted at round {rounds} (Rule-of-Three — config.md's "
    "effort-replan-absolute, reused) — the requirement for a further thinker review is "
    "released; decide directly: approve with the recorded accepted risks, or cut scope "
    "and resubmit"
)


def plan_review_round_release_active(state: SessionState | None, thr: Thresholds | None = None) -> bool:
    """True once `state.plan_review_rounds` (PLAN_READY resubmissions since the last
    approval) has reached the Rule-of-Three threshold this stage reuses rather than
    duplicating — config.md's `effort-replan-absolute`. Past this point
    `plan_review_blockers` stops demanding another review pass and routes to the user
    instead (see `_PLAN_REVIEW_ROUND_RELEASE_MESSAGE`)."""
    if state is None:
        return False
    thr = thr if thr is not None else Thresholds()
    return state.plan_review_rounds >= thr.effort_replan_absolute()


def plan_review_blockers(state: SessionState, target_plan: str | None) -> list[str]:
    """Precondition guardian for `approve` and every `replan`: a thinker review with
    a passing (or user-overridden) verdict, BOUND to the exact plan version being
    approved/applied, must have been recorded. This is an INTERNAL command
    precondition mirroring difficulty_blockers — deliberately absent from GUARDIANS
    so verify-agentctl requires no new hook to cover it. [] == may pass.

    Inactive (chat / small-change / AGENTCTL_PLAN_REVIEW=0) => [] always: the gate
    is byte-identical to absent for non-substantive sessions.

    With no stage-scoped review recorded (state.plan_stage_reviews empty), this
    reduces to the whole-plan-only check `_plan_review_blockers_whole` ran alone —
    same branches, same messages, as before stage-scoped reviews existed at all;
    `doc` (schema 28, for accepted-risk discharge) is still loaded and threaded
    through on this path, but no branch below it depends on the load having
    succeeded. Once a stage-scoped review exists, coverage is delegated to
    `_plan_review_blockers_coverage`, which checks the whole-plan record's own
    attestation/verdict directly (`_plan_review_verdict_blockers`) rather than via
    `_plan_review_blockers_whole` — the byte-hash staleness check in that helper
    would trip on any unrelated edit and defeat per-stage coverage, so staleness
    here is decided solely by `changed_parts` against the recorded meta/stage keys.

    Round release wraps the OUTERMOST result: whatever combination of "no review",
    "stale", or "verdict blocked" branches produced a non-empty list, past the round
    threshold every one of them collapses to the single routing message — the review
    requirement is released as one event, not per sub-reason."""
    if not plan_review_active(state):
        return []
    doc = None
    if target_plan:
        try:
            doc = load_plan(target_plan)
        except (OSError, PlanError):
            doc = None
    if not state.plan_stage_reviews or doc is None:
        blockers = _plan_review_blockers_whole(state.plan_review, target_plan, state=state, doc=doc)
    else:
        blockers = _plan_review_blockers_coverage(state, target_plan, doc)
    if blockers and plan_review_round_release_active(state):
        return [_PLAN_REVIEW_ROUND_RELEASE_MESSAGE.format(rounds=state.plan_review_rounds)]
    return blockers


def plan_review_delta(state: SessionState, doc) -> "tuple[bool, set[int]]":
    """What a reviewer still needs to look at in `doc`, independent of any
    verdict/attestation check: (whole_plan_needed, stage indices still needing
    their own pass). No whole-plan review yet recorded reads the same as one
    whose meta/order moved — both mean "review the whole thing". Backs the
    read-only `plan-review-delta` command in place of a raw digest dump.

    NOT reused by `_plan_review_blockers_coverage`, despite computing a related
    gap over the same baseline: that gate additionally binds each review to
    `target_plan` (a path check this function has no parameter for), fails fast
    on the first uncovered part instead of enumerating all of them, and folds
    in the verdict/attestation check this function deliberately excludes. The
    two share only their building blocks (`_plan_review_baseline`,
    `changed_parts`), not a call path."""
    whole = state.plan_review
    baseline = _plan_review_baseline(whole) if whole is not None else {"meta": "", "stages": {}}
    meta_moved, moved_stages = changed_parts(doc, baseline)
    if meta_moved:
        return True, set()
    needing: set[int] = set()
    for index in moved_stages:
        spr = state.plan_stage_reviews.get(_plan_review_scope_for_stage(index))
        if spr is None:
            needing.add(index)
            continue
        stage_meta_moved, stage_moved = changed_parts(doc, _plan_review_baseline(spr))
        if stage_meta_moved or index in stage_moved:
            needing.add(index)
    return False, needing


def plan_presentation_active(state: SessionState) -> bool:
    """Whether the plan-presentation gate applies to this session.

    Scoped exactly like plan_review_active: SUBSTANTIVE sessions always pay it;
    chat/small-change never do. AGENTCTL_PLAN_PRESENTATION overrides in both
    directions ("1" forces on, "0" forces off). Deliberately NOT routed through
    advisor.resolve_enabled — see plan_review_active's docstring for why a
    mandatory gate must not share an optional cost knob's kill switch. Env-only
    reads, no file/subprocess I/O, so this predicate itself stays pure (the
    guardian it gates is not fully pure — see plan_presentation_blockers)."""
    env = os.environ.get("AGENTCTL_PLAN_PRESENTATION")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def _plan_presentation_for(state: SessionState, kind: str):
    """The most-recently-recorded PlanPresentation for `kind`, or None.
    cmd_present_plan supersedes rather than appends, so in practice at most one
    match exists per kind; last-wins mirrors _stage_review_for regardless."""
    match = [p for p in state.plan_presentations if p.kind == kind]
    return match[-1] if match else None


DELIVERY_HOOK_BASENAME = "hook-plan-delivery-gate.py"

_NO_STAMP_GENERIC = (
    "no delivery proof recorded — the plan was presented but nothing "
    "confirms it reached the user; either let the delivery hook verify "
    "the turn's transcript, or run confirm-delivery --by <you> "
    "--note <why> --escape-reason <" +
    "|".join(delivery.DELIVERY_ESCAPE_REASONS) + "> as the escape"
)


def _no_stamp_blocker(probe) -> str:
    """The no-delivery-proof refusal, diagnosed when the diagnosis is certain.

    The generic text above offers a first remedy — "let the delivery hook verify
    the turn's transcript" — that is unreachable in precisely the situation that
    most often triggers this branch: the hook is not registered in the root this
    session loads from, so it never ran and never could. A gate demanding proof
    whose only producer is absent, while suggesting you wait for that producer,
    is why this whole task exists.

    So on this branch only, ask whether the hook is wired. WIRED or UNKNOWN keep
    today's wording byte-for-byte — then the generic refusal is the honest one,
    and an UNKNOWN dressed up as a diagnosis would be a confident claim from
    evidence that does not support it. Any exception degrades to the same
    wording: a gate that raised because a settings file is odd would be a worse
    failure than the message it was improving.

    The VERDICT never changes here. This function decides what the gate SAYS.
    """
    try:
        wiring = probe(DELIVERY_HOOK_BASENAME)
    except Exception:
        return _NO_STAMP_GENERIC
    if getattr(wiring, "status", None) != hook_wiring.ABSENT:
        return _NO_STAMP_GENERIC
    # The scope comes from the probe, not from this sentence: how far an ABSENT
    # reaches depends on whether the project member was read, which only the
    # probe knows.
    return (
        "no delivery proof recorded — " + DELIVERY_HOOK_BASENAME + " is not "
        f"registered in {wiring.absence_scope()}, so no "
        "automated proof can come from that evidence domain; either run this "
        "task under claude-task / claude-agent, where the hook IS wired, or run "
        "confirm-delivery --by <you> --note <why> --escape-reason " +
        delivery.ESCAPE_HOOK_NOT_INSTALLED + " as the escape"
    )


def plan_presentation_blockers(
    state: SessionState,
    target_plan: str | None,
    *,
    probe=None,
) -> list[str]:
    """Precondition guardian for `approve`: the plan must have been PRESENTED to
    the user (a receipt exists, bound to the exact plan version) AND that
    presentation must be PROVEN DELIVERED (a delivery stamp exists, bound to the
    exact receipt). This is an INTERNAL command precondition mirroring
    plan_review_blockers/acceptance_review_blockers — the third instance of that
    charter — deliberately absent from GUARDIANS (its (state, target_plan)
    signature does not fit the one-argument `guardian(state)` GUARDIANS
    dispatches), so verify-agentctl's guardian-hook-coverage rule does not apply
    to it. [] == may pass.

    Not fully pure like the rest of this module: the DELIVERY half reads the
    session's own delivery-stamp sidecar via delivery.read_stamp (file I/O
    only — no subprocess/socket/network, so ast_purity's admitted-reach set
    still holds).

    Inactive (chat / small-change / AGENTCTL_PLAN_PRESENTATION=0) => [] always.
    RECEIPT-side checks fail OPEN on missing observables, mirroring
    plan_review_blockers:
      - no essence-kind receipt at all -> blocker naming `present-plan --kind
        essence` (a `full` receipt never substitutes — an essence rendering is
        always required; `full` is the additional, stage-enumerated form for
        stages that need it, not a replacement);
      - receipt.plan_path != target_plan -> stale (wrong plan version);
      - receipt.plan_sha256 != current sha256 of target_plan -> stale content;
        fail-open on OSError / empty stored hash (mirrors plan_review_blockers's
        `if pr.plan_sha256:` legacy-degradation guard).

    DELIVERY fails CLOSED — the one deliberate departure from the surrounding
    fail-open discipline, matching the module docstring's "fail-open judge,
    fail-closed gate" asymmetry: absence of proof that a POSITIVE EVENT
    (delivery) occurred is not the same kind of ambiguity as a missing-but-
    possibly-true fact (a transient read error on an existing review), so it
    degrades to "not yet delivered", never "delivered by default". Admissible
    ONLY because cmd_confirm_delivery is a reachable, audit-logged, per-plan-
    version escape (see its own docstring) — without that escape a disabled/
    uninstalled delivery hook would brick every substantive session at
    PLAN_READY forever, the exact bypass-trainer shape the fail-closed design
    must avoid. A missing, stale, superseded, or UNREADABLE stamp all block
    identically (delivery.read_stamp already collapses every unreadable/corrupt
    case to None) — this gate never distinguishes "corrupt sidecar" from
    "never verified", because both mean the same thing here: no usable proof.

    `probe` is a seam, defaulting to hook_wiring.probe: on the no-stamp branch
    the refusal is diagnosed against the ACTIVE harness root (see
    _no_stamp_blocker). It is injectable so a test can pin which answer it gets
    — without that, the two pre-existing assertions on this branch would consult
    whatever root the test machine happens to run under and mean different
    things on different machines. It is consulted ONLY on the branch already
    about to block, so the added filesystem read is off every passing path."""
    if not plan_presentation_active(state):
        return []
    receipt = _plan_presentation_for(state, _PLAN_PRESENTATION_KIND_ESSENCE)
    if receipt is None:
        return [
            "no plan presentation recorded — run: present-plan --kind essence "
            "(the plan must be shown to the user before it can be approved)"
        ]
    if not target_plan or receipt.plan_path != target_plan:
        return [
            "plan presentation is stale — it presented "
            f"{receipt.plan_path!r} but the target plan is {target_plan!r}; "
            "re-run present-plan on the current plan"
        ]
    if receipt.plan_sha256:
        try:
            current = hashlib.sha256(Path(target_plan).read_bytes()).hexdigest()
        except OSError:
            current = None
        if current is not None and current != receipt.plan_sha256:
            return [
                "plan presentation is stale — the plan content at "
                f"{target_plan!r} changed since it was presented; re-run present-plan"
            ]

    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    stamp = delivery.read_stamp(state_file) if state_file is not None else None
    if stamp is None:
        return [_no_stamp_blocker(probe if probe is not None else hook_wiring.probe)]
    if stamp.plan_sha256 != receipt.plan_sha256 or stamp.rendering_sha256 != receipt.rendering_sha256:
        return [
            "delivery proof is stale — it verified a different plan/rendering "
            "than the current presentation receipt; re-present and re-verify "
            "(or confirm-delivery --by <you> --note <why> --escape-reason "
            "<" + "|".join(delivery.DELIVERY_ESCAPE_REASONS) + ">)"
        ]
    if stamp.source == delivery.SOURCE_HOOK:
        return []
    if stamp.source == delivery.SOURCE_OVERRIDE:
        missing = []
        if not (stamp.by or "").strip():
            missing.append("by")
        if not (stamp.note or "").strip():
            missing.append("note")
        if missing:
            return [
                "delivery override requires a non-empty " + " and ".join(missing) +
                " (the user's explicit escape reason) — re-run confirm-delivery"
            ]
        return []
    return [f"delivery stamp source is {stamp.source!r} — expected 'hook' or 'override'"]


def stage_review_active(state: SessionState) -> bool:
    """Whether the acceptance-review judge gate applies to this session.

    Scoped exactly like plan_review_active: chat/small-change sessions never pay the
    judge cost; SUBSTANTIVE sessions always do. AGENTCTL_STAGE_REVIEW overrides in both
    directions ("1" forces on, "0" forces off). Deliberately NOT routed through
    advisor.resolve_enabled — the advisor is an optional cost knob whose kill switch
    must not silently defeat a mandatory gate; the gate's only off switch is its own
    env var. Env-only reads, no file/subprocess I/O, so the gate stays pure."""
    env = os.environ.get("AGENTCTL_STAGE_REVIEW")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def effort_active(state: SessionState) -> bool:
    """Whether a firing `effort.divergence()` may ACT (transition the session into
    DIAGNOSING) — never whether effort.py accounts. arm/rederive/refresh_spend run
    unconditionally at their call sites regardless of this flag; only the fire sites in
    cli.py read it, exactly like stage_review_active gates its judge, not the
    accumulation it reads. AGENTCTL_EFFORT overrides in both directions ("1" forces on,
    "0" forces off); the weight_class fallback mirrors the sibling gates above for
    convention only — a session that never called cmd_approve never armed (effort.py's
    ARMED-ONLY), so this fallback is moot in practice but kept for the same reason the
    others have it: an explicit escape hatch that doesn't depend on inferring intent
    from the absence of a var."""
    env = os.environ.get("AGENTCTL_EFFORT")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def _stage_review_for(state: SessionState, stage_index: int):
    """The most-recently-recorded StageReview for `stage_index`, or None. Last-wins so
    a manual override recorded after a judge verdict supersedes it."""
    match = [r for r in state.stage_reviews if r.stage_index == stage_index]
    return match[-1] if match else None


def acceptance_review_blockers(state: SessionState, stage: "_Stage") -> list[str]:
    """Precondition guardian for `record-result --status passed` on an acceptance_review
    stage: a recorded StageReview with a passing (or user-overridden) verdict, BOUND to
    the exact observation bytes being recorded, must exist. An INTERNAL command
    precondition mirroring plan_review_blockers — deliberately ABSENT from GUARDIANS so
    verify-agentctl requires no new hook. PURE: reads ONLY the recorded StageReview and
    hashes the observation's own bytes; never a subprocess/socket/network reach. [] == ok.

    Inactive (chat / small-change / AGENTCTL_STAGE_REVIEW=0) => [] always. Active checks:
      - a review must exist — else the gate is unmet (fail-CLOSED: a fail-open judge that
        produced no verdict leaves no review, and that must block, never pass);
      - it must be bound to the observation being recorded (observation_sha256 == the
        sha256 of stage.criterion.observation) — a verdict granted to a different
        observation is stale; empty stored hash degrades to verdict-only (legacy);
      - the verdict must be `pass`, or `override` with a non-empty reviewer AND note
        (the explicit user escape); `revise`/unknown blocks."""
    if not stage_review_active(state):
        return []
    review = _stage_review_for(state, stage.index)
    if review is None:
        return [
            "no acceptance judge verdict recorded — the cheap judge produced no verdict "
            "(disabled/errored/timed out) or none was recorded; an acceptance pass is "
            "blocked until a passing verdict (or an explicit override) binds to the observation"
        ]
    observation = getattr(stage.criterion, "observation", "") or ""
    expected = hashlib.sha256(observation.encode("utf-8")).hexdigest()
    if review.observation_sha256 and review.observation_sha256 != expected:
        return [
            "acceptance judge verdict is stale — it judged a different observation than the "
            "one being recorded; re-judge the current observation"
        ]
    if review.verdict == _STAGE_REVIEW_PASS:
        return []
    if review.verdict == _STAGE_REVIEW_OVERRIDE:
        missing = []
        if not (review.reviewer or "").strip():
            missing.append("reviewer")
        if not (review.note or "").strip():
            missing.append("note")
        if missing:
            return ["acceptance override requires a non-empty " + " and ".join(missing) + " (the user's explicit escape reason)"]
        return []
    return [f"acceptance judge verdict is {review.verdict!r} — pass blocked until a passing verdict (or an explicit override) is recorded"]


def code_review_active(state: SessionState) -> bool:
    """Whether the code-reviewer gate applies to this session.

    Scoped exactly like stage_review_active: chat/small-change sessions never pay the
    reviewer cost; SUBSTANTIVE sessions always do. AGENTCTL_CODE_REVIEW overrides in both
    directions ("1" forces on, "0" forces off). Deliberately NOT routed through
    advisor.resolve_enabled — the advisor is an optional cost knob whose kill switch
    must not silently defeat a mandatory gate; the gate's only off switch is its own
    env var. Env-only reads, no file/subprocess I/O, so the gate stays pure."""
    env = os.environ.get("AGENTCTL_CODE_REVIEW")
    if env == "1":
        return True
    if env == "0":
        return False
    return state.weight_class == WeightClass.SUBSTANTIVE.value


def _code_review_for(state: SessionState, stage_index: int):
    """The most-recently-recorded CodeReview for `stage_index`, or None. Last-wins so
    a manual override recorded after a code-reviewer verdict supersedes it."""
    match = [r for r in state.code_reviews if r.stage_index == stage_index]
    return match[-1] if match else None


def code_review_blockers(
    state: SessionState, stage: "_Stage", expected_code_sha256: str | None = None
) -> list[str]:
    """Precondition guardian for `record-result --status passed` on a needs_control()
    (spawn:developer) stage: a recorded CodeReview with a passing (or user-overridden)
    verdict must exist. An INTERNAL command precondition mirroring
    acceptance_review_blockers — deliberately ABSENT from GUARDIANS so verify-agentctl
    requires no new hook. PURE: reads only the recorded CodeReview and compares two
    caller-supplied digests; never a subprocess/socket/network reach — gates.py cannot
    recompute a git sha itself. [] == ok.

    Inactive (chat / small-change / AGENTCTL_CODE_REVIEW=0) => [] always. Active checks:
      - a review must exist — else the gate is unmet (fail-CLOSED, mirroring
        acceptance_review_blockers: no CodeReview => blocked, never a default pass);
      - if BOTH the recorded review.code_sha256 and the caller-supplied
        `expected_code_sha256` (the record-result --code-ref value, when the cli layer
        passes one through) are non-empty and they differ, the verdict is stale — it
        reviewed a different code revision than the one now being recorded; either side
        empty degrades to verdict-only (legacy / unbound review);
      - the verdict must be `pass`, or `override` with a non-empty reviewer AND note
        (the explicit user escape); `revise`/unknown blocks."""
    if not code_review_active(state):
        return []
    review = _code_review_for(state, stage.index)
    if review is None:
        return [
            "no code-reviewer verdict recorded — spawn the `code-reviewer` specialization "
            "to review this stage's diff, then record with `agentctl code-review`; a "
            "spawn:developer stage cannot be recorded passed until a passing verdict "
            "(or an explicit override) exists"
        ]
    if review.code_sha256 and expected_code_sha256 and review.code_sha256 != expected_code_sha256:
        return [
            "code-reviewer verdict is stale — it reviewed a different code revision than "
            "the one being recorded; re-run code-review on the current diff"
        ]
    if review.verdict == _CODE_REVIEW_PASS:
        return []
    if review.verdict == _CODE_REVIEW_OVERRIDE:
        missing = []
        if not (review.reviewer or "").strip():
            missing.append("reviewer")
        if not (review.note or "").strip():
            missing.append("note")
        if missing:
            return ["code-review override requires a non-empty " + " and ".join(missing) + " (the user's explicit escape reason)"]
        return []
    return [f"code-reviewer verdict is {review.verdict!r} — pass blocked until a passing verdict (or an explicit override) is recorded"]


def _landed_sort_key(landed) -> tuple:
    """Fixed-shape, orderable stand-in for a `LandedSpec | None` inside a
    `sorted(...)`-built tuple. `LandedSpec` is a plain dataclass with no
    `__lt__`, so embedding it directly would raise TypeError the moment two
    stages/final_checks tie on every earlier field and `sorted` falls back to
    comparing it. The sentinel ("", "", -1) sorts before any real spec, whose
    `delivered_stage` is always >= 1 (R5)."""
    if landed is None:
        return ("", "", -1)
    return (landed.target, landed.remote, landed.delivered_stage)


def _refs_projection(subject) -> tuple:
    """The subject's two structural ref projections (material_refs/knowledge_refs) as
    ONE surface component — or the EMPTY tuple when the stage declares neither, so a
    plan written before these fields existed reproduces its schema-23 surface exactly
    (the declared-only rule the verify_venue_at_final component follows, for the same
    reason: an absent field must be indistinguishable from a field that never existed).

    Grouped rather than spliced as two components because two independently conditional
    splices collide: (material_refs=["x"], knowledge_refs=[]) and (material_refs=[],
    knowledge_refs=["x"]) would flatten to the same single component. Rendered as a
    STRING rather than a nested tuple for a second reason, this one about the `sorted`
    in `_operative_surface`: with two conditional components of DIFFERENT types, two
    stages tying on every unconditional field — one declaring only verify_venue_at_final,
    the other only refs — reach a str-vs-tuple comparison and raise TypeError. Any third
    conditional component must likewise be a string.

    Each list is SORTED: re-ordering the same refs is not a re-selection, so a shuffle
    must not satisfy the CHANGE half.

    Entries are STRIPPED but NOT passed through `_normalize_string`, which is the one
    place this component departs from every other string component in the surface. A ref
    is a structural identifier — a path, or a `path:Symbol` — not prose, and it belongs
    with the landed payload's `target`/`remote` rather than with `material`: `Stage` and
    `stage` are two symbols, and a tree tracks `Gates.py` and `gates.py` as two files
    whatever the host filesystem folds. Casefolding them would make a genuine re-selection
    between two case-distinct referents invisible to the CHANGE half, blocking the replan
    that stage 4 exists to admit. Surrounding whitespace is the only authoring artifact of
    a TOML list entry, so it is the only thing normalized away; interior whitespace is left
    alone, since a ref has no legitimate reason to carry it and collapsing it would silently
    equate two identifiers that differ. This also matches how `plan.py::stage_carry_key`
    already compares the same two lists (raw, at :1057) — one field, one identity rule."""
    if not (subject.material_refs or subject.knowledge_refs):
        return ()
    return (
        repr(
            (
                tuple(sorted(r.strip() for r in subject.material_refs)),
                tuple(sorted(r.strip() for r in subject.knowledge_refs)),
            )
        ),
    )


def _operative_surface(doc) -> tuple:
    """The plan's operative surface: what the engine executes or dispatches on,
    as opposed to its prose (title/goal/done_criterion/expected_result_image/
    material/knowledge/conditions/invariants/principle) — the latter is
    deliberately excluded so no amount of narrative rewriting can satisfy the
    CHANGE half below. That exclusion is why a re-SELECTED material enters here
    only through its typed projection: `material_refs`/`knowledge_refs` cannot be
    reworded, only re-declared, so admitting them makes a re-selection observable
    without making the CHANGE half satisfiable by prose (defect 4). Admitting the
    `material` prose itself would restore the blocker's appearance while destroying
    the guarantee. The residual is that a projection is a DECLARATION: appending a
    path satisfies the gate without any re-selection having happened, and the
    projection is coarse enough that two different transformations of one file look
    alike. Per stage: means, method, procedure, verify_command, expected_exit, the
    declared check venue/kind and its landed payload, the executor, and the two ref
    projections. Plan level: repo_root, delivery_worktree and every final_check's
    (command, expected_exit, venue, kind, landed payload).
    Every string component passes through `_normalize_string` so a whitespace-
    or-case-only rephrasing does not register as a change; expected_exit stays
    a literal int comparison. `target`/`remote` inside a landed payload are
    compared raw (git ref names are case-sensitive)."""
    stage_surface = sorted(
        (
            _normalize_string(s.means.means),
            _normalize_string(s.means.method),
            _normalize_string(s.criterion.verify_command or ""),
            s.criterion.expected_exit,
            _normalize_string(s.criterion.verify_venue),
            _normalize_string(s.actor.executor),
            _normalize_string(s.criterion.verify_kind),
            _landed_sort_key(s.criterion.landed),
            # Declared-only (not `... or ""`), so an absent field reproduces the
            # schema-23 operative surface exactly — uniform with the plan.py keys.
            *((_normalize_string(s.criterion.verify_venue_at_final),)
              if s.criterion.verify_venue_at_final else ()),
            # The sequence of operations, beside the `means`/`method` cluster it belongs
            # to and NOT with the excluded prose: it is the one field an executor may
            # replace on his own authority, so a replan that removes a difficulty by
            # re-sequencing has changed something real and must be able to say so here.
            # A string, per the typing constraint `_refs_projection` documents above, and
            # a TAGGED one for the reason `plan.procedure_place` records: two conditional
            # components of the same type collide, so an untagged procedure would compare
            # equal to a `verify_venue_at_final` carrying the same word.
            *(("procedure:" + _normalize_string(s.means.procedure),)
              if s.means.procedure else ()),
            *_refs_projection(s.subject),
        )
        for s in doc.stages
    )
    final_check_surface = sorted(
        (
            _normalize_string(fc.command),
            fc.expected_exit,
            _normalize_string(fc.venue),
            _normalize_string(fc.kind),
            _landed_sort_key(fc.landed),
        )
        for fc in doc.meta.final_check
    )
    meta_surface = (
        _normalize_string(doc.meta.repo_root or ""),
        _normalize_string(doc.meta.delivery_worktree or ""),
        final_check_surface,
    )
    return (stage_surface, meta_surface)


def replan_coverage_blockers(old_doc, new_doc, critique) -> list[str]:
    """Verify the critique's similarities/differences split is COVERED by the
    corrected plan — the dataflow, NOT the cognitive item->field mapping (that
    stays prose: the gate never decides WHICH stage an item belongs to).

      - PRESERVE: every declared similarity (critique.invariants_to_preserve) must
        appear as a substring of the new plan's conditions + invariants text;
        missing => a blocker naming the item.
      - CHANGE: if any difference is declared (critique.differences_to_remove is
        non-empty), the plan's operative surface (`_operative_surface`: per-stage
        means/method/verify_command/expected_exit/verify_venue/executor and the
        material_refs/knowledge_refs projections, plus [meta] repo_root/
        delivery_worktree/final_check) must differ from the old plan's — proof
        something the engine executes or dispatches on was re-selected to remove
        the difference; unchanged => one blocker. A means/method-only diff is one
        member of that surface, not the whole of it: a correction that instead
        lives entirely in verify_command, a final_check, a re-selected material
        or [meta] also satisfies this half.

    Declared-item-scoped: empty lists pass vacuously, so a critique that records no
    split (or, via the cmd_replan guard, a replan with no difficulty present)
    behaves exactly as before. Membership is substring after `_normalize_string`
    on both sides (casefold + collapsed whitespace) — an honest rephrasing of the
    same invariant passes; a genuinely absent one still blocks.

    Unlike the two hard gates this takes PlanDocs, not just state — it is therefore
    NOT registered in GUARDIANS and is called directly from cmd_replan."""
    out: list[str] = []
    if critique is None:
        return out
    haystack = " \n ".join(
        part
        for s in new_doc.stages
        for part in (s.conditions or "", s.subject.invariants or "")
    )
    norm_haystack = _normalize_string(haystack)
    for item in critique.invariants_to_preserve:
        if not (item or "").strip():
            continue
        if _normalize_string(item) not in norm_haystack:
            out.append(
                f"similarity to preserve not carried into any stage conditions/invariants: {item!r}"
            )
    diffs = [d for d in critique.differences_to_remove if (d or "").strip()]
    if diffs:
        if _operative_surface(old_doc) == _operative_surface(new_doc):
            out.append(
                "differences_to_remove is non-empty but the plan's operative surface "
                "(means/method, verify_command/expected_exit/verify_venue, executor, "
                "material_refs/knowledge_refs, final_check, [meta] repo_root/"
                "delivery_worktree) did not change — a difference cannot be "
                "removed without changing what the engine executes or "
                "dispatches on"
            )
    return out


#: The places a renormalization may not reach, as (dotted path on the stage, what the
#: author is losing by editing it). Each is a NORM: the requirement on the way of acting,
#: how the result is judged, the image it is judged against. Named individually rather
#: than as "everything but the procedure" so the message can say WHICH norm was touched;
#: the residual check below is what makes the list's incompleteness harmless.
_RENORM_PROTECTED = (
    ("means.method", "the requirement on the way of acting"),
    ("means.means", "the instruments the plan fixed"),
    ("subject.result", "the result image the stage is judged against"),
    ("criterion.criterion_type", "how the result is judged"),
    ("criterion.done_criterion", "the done criterion"),
    ("criterion.verify_command", "the check that decides the stage"),
    ("criterion.expected_exit", "the exit code the check is read against"),
    ("criterion.verify_venue", "the tree the check observes"),
    ("criterion.verify_kind", "the kind of check"),
    ("criterion.verify_venue_at_final", "the tree the final check observes"),
)


def _dotted(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def renormalization_blockers(old_doc, new_doc) -> list[str]:
    """Why `new_doc` is not a renormalization of `old_doc`. [] == it is one.

    A RENORMALIZATION is the executor exercising the authority `Means.procedure` gives
    him: he replaces the SEQUENCE of operations proposed for meeting the stage's
    requirement, on his own reading of the code, without the review and approval a
    replan re-arms. What he may not do under that authority is edit the requirement
    itself, the criterion that decides the stage, the image the result is compared
    against, or the goal every stage-8 observation is compared to — those are the
    customer's and the planner's, and reaching them under a light path would make the
    approval a formality anyone could route around.

    So the verdict is not "did anything change" but "is the procedure the ONLY thing
    that changed". Two halves:

    * NAMED refusals (`_RENORM_PROTECTED` plus the meta surface), so the message can
      tell the author which norm he touched and what it costs to move it properly.
    * Two RESIDUAL totality checks, one per side, which are what make this gate honest
      rather than a list someone must remember to extend. Per stage: the old stage is
      copied, ONLY its `means.procedure` is set to the new value, and
      `_renorm_stage_residual` of that transplant must equal the new stage's. Per plan:
      `_meta_place(old) == _meta_place(new)` over every field of `plan.PlanMeta`. Each
      residual is pinned by a test that goes red when a field is added and not covered
      (test_renormalization.py), because both are hand-written membership lists and a
      universal claim no code establishes is exactly the substitution this engine's own
      docstrings name as costing a claim its universality.
      The stage residual is also the answer to whether the light path can re-select
      `material_refs` or `knowledge_refs` and walk around the coverage gate stage 4
      built: it cannot, because those refs are inside `plan.stage_question_key` (via
      `plan.knowledge_place`), which the residual carries.

    Pure — dataclass reads and two digests, no I/O, in keeping with this module."""
    out: list[str] = []
    old_by_index = {s.index: s for s in old_doc.stages}
    new_by_index = {s.index: s for s in new_doc.stages}
    if set(old_by_index) != set(new_by_index):
        # Adding or dropping a stage is a re-decomposition of the work, not a
        # re-sequencing inside it — and with the stage sets unequal the per-stage
        # comparison below has nothing to say, so this returns rather than accumulates.
        return [
            "a renormalization may not add or remove a stage: "
            f"{sorted(old_by_index)} -> {sorted(new_by_index)}. Replacing the SEQUENCE "
            "of operations inside a stage is the executor's; re-cutting the work into "
            "stages is the plan's — replan without --renormalize"
        ]
    for index in sorted(new_by_index):
        old_stage, new_stage = old_by_index[index], new_by_index[index]
        for dotted, what in _RENORM_PROTECTED:
            if _dotted(old_stage, dotted) != _dotted(new_stage, dotted):
                out.append(
                    f"stage {index}: a renormalization may not edit `{dotted}` — that is "
                    f"{what}, not the sequence of operations proposed for meeting it. "
                    f"Drop --renormalize and replan it through the review and approval "
                    f"it is owed"
                )
        transplant = copy.deepcopy(old_stage)
        transplant.means.procedure = new_stage.means.procedure
        if _renorm_stage_residual(transplant) != _renorm_stage_residual(new_stage):
            out.append(
                f"stage {index}: something other than `means.procedure` changed — a "
                f"renormalization is an edit the new sequence alone accounts for, and "
                f"this one does not. Replan without --renormalize"
            )
    for dotted, what in (
        ("goal", "the goal every stage's observation is compared against"),
        ("done_criterion", "the plan's done criterion"),
        ("repo_root", "the tree the plan is authored against"),
        ("delivery_worktree", "the tree the work is delivered in"),
    ):
        if _dotted(old_doc.meta, dotted) != _dotted(new_doc.meta, dotted):
            out.append(
                f"[meta] a renormalization may not edit `{dotted}` — that is {what}. "
                f"Replan without --renormalize"
            )
    if _final_check_surface(old_doc.meta) != _final_check_surface(new_doc.meta):
        out.append(
            "[meta] a renormalization may not edit `final_check` — that is how the whole "
            "plan is judged. Replan without --renormalize"
        )
    if order_place(old_doc.meta) != order_place(new_doc.meta):
        out.append(
            "[meta] a renormalization may not edit `[meta.order]` — the order is the "
            "customer's, and nothing an executor does to his own sequence changes it. "
            "Replan without --renormalize"
        )
    if _meta_place(old_doc.meta) != _meta_place(new_doc.meta):
        out.append(
            "[meta] something outside the sequence of operations changed in the plan's "
            "[meta] table — a renormalization is an edit the new sequence alone accounts "
            "for, and this one does not. Replan without --renormalize"
        )
    return out


def _renorm_stage_residual(stage) -> tuple:
    """A stage's WHOLE definition as a comparable value — the per-stage residual.

    `plan.stage_question_key` is most of it, and would have been all of it but for its
    own scope: that key answers whether a disposed Question still targets the same
    bytes, and a Question.target may only name a stage field the plan's author writes
    as an activity element. Two engine-consumed fields fall outside that and are spliced
    on here, because a renormalization is defined by what it does NOT touch:

    * `actor.cost_tier` — the dispatch budget label and the effort-divergence estimate's
      input. Re-tiering a stage from `small` to `large` under the light path would move
      the norm the divergence trigger reads a stage's overrun against.
    * `output_artifacts` — the paths the verify-command reachability lint reads as
      produced-by-this-plan. Re-declaring them changes which green a check can reach.

    Deliberately outside, and the only things outside: `index` (the key both sides are
    matched ON, so a change there is an added/removed stage, refused above), and the
    mutable execution RECORD `outcome` / `criterion.observation` / `control` — a plan doc
    loaded from TOML carries the defaults for those, and the live state's copies are what
    this path exists to leave alone.

    Hand-written, like every membership list of this family, and pinned the same way:
    `test_the_stage_residual_exhausts_the_stage_s_field_set` goes red when a field is
    added to `Stage` and to neither the key nor the two splices above."""
    return (
        stage_question_key(stage),
        stage.actor.cost_tier,
        tuple(stage.output_artifacts),
    )


def _meta_place(meta) -> tuple:
    """Every field of `plan.PlanMeta`, normalized into a comparable value — the plan-level
    residual, and the reason the named [meta] refusals above may stay a short list.

    Without it the meta side is a bare enumeration, and an enumeration is exactly what a
    light path must not rest on: `weight_class` was outside the named four, so an offered
    plan re-declaring a substantive session's plan as `small_change` — the grade the whole
    approval spine keys on — passed as "a re-sequencing". So the totality claim is made
    here and the named rows keep only the job they are good at, naming the norm.

    Hand-written, but for a narrower reason than `plan.order_place`'s: eight of these ten
    fields (`task_id` through `delivery_worktree`) are plain scalars or optional strings,
    already hashable and comparable as `meta.X` with no transformation at all — a
    `dataclasses.fields` derivation could emit those as-is. Only two actually need custom
    handling: `final_check` and `order` are themselves compound structures, routed through
    `_final_check_surface`/`order_place` for the same reason those helpers exist. The list
    stays hand-written regardless, because a derived walk would still have to dispatch
    `final_check`/`order` away from the plain fields, and because the totality claim needs
    its own pin either way: `test_the_meta_residual_exhausts_plan_meta_s_field_set` goes
    red the day a field is added to PlanMeta and not listed here.

    `final_check` rides through `_final_check_surface`, so a label-only edit is caught by
    neither this nor the named refusal above — labels are how a check is spoken about,
    not what it checks, and the operative surface is deliberately what both compare."""
    return (
        meta.task_id,
        meta.goal,
        meta.done_criterion,
        meta.criterion_type,
        meta.weight_class,
        meta.external_research,
        meta.repo_root,
        meta.delivery_worktree,
        _final_check_surface(meta),
        order_place(meta),
    )


def _final_check_surface(meta) -> tuple:
    """Every final_check as a comparable tuple, in declaration order.

    Order is kept (unlike `_operative_surface`, which sorts): there the question is
    whether the SET of checks was re-selected, here it is whether the [meta] block was
    edited at all, and re-ordering the plan's final checks is an edit."""
    return tuple(
        (fc.command, fc.expected_exit, fc.venue, fc.kind, _landed_sort_key(fc.landed))
        for fc in meta.final_check
    )


# gate name -> guardian predicate
GUARDIANS = {
    "plan_approval": plan_approval_blockers,
    "resolution": resolution_blockers,
}


def blockers(state: SessionState, gate_name: str) -> list[str]:
    guardian = GUARDIANS.get(gate_name)
    if guardian is None:
        return [f"unknown gate {gate_name!r}"]
    return guardian(state)
