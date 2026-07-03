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
"""
from __future__ import annotations

import os

from . import advisor
from .state import Node, SessionState, StageStatus
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import normalize_string as _normalize_string

# Verdict vocabulary for the plan-review gate. `pass` clears it; `override` clears
# it only as the user's explicit deadlock escape (requires reviewer + note);
# anything else (`revise`, unknown) blocks.
_PLAN_REVIEW_PASS = "pass"
_PLAN_REVIEW_OVERRIDE = "override"
PLAN_REVIEW_VERDICTS = (_PLAN_REVIEW_PASS, "revise", _PLAN_REVIEW_OVERRIDE)


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
    return out


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


def plan_review_active(state: SessionState) -> bool:
    """Whether the thinker-review gate applies to this session.

    Scoped to substantive work exactly like the advisor (reusing
    advisor.resolve_enabled so the two share one activation rule): chat and
    small-change sessions never pay the review cost. AGENTCTL_PLAN_REVIEW overrides
    in both directions ("1" forces on, "0" forces off — the analogue of
    AGENTCTL_ADVISOR); absent the override, resolve_enabled's config-mode +
    weight-class rule decides. Env-only reads, no file/subprocess I/O, so the gate
    stays pure."""
    env = os.environ.get("AGENTCTL_PLAN_REVIEW")
    if env == "1":
        return True
    if env == "0":
        return False
    return advisor.resolve_enabled(state.weight_class)


def plan_review_blockers(state: SessionState, target_plan: str | None) -> list[str]:
    """Precondition guardian for `approve` and every `replan`: a thinker review with
    a passing (or user-overridden) verdict, BOUND to the exact plan version being
    approved/applied, must have been recorded. This is an INTERNAL command
    precondition mirroring difficulty_blockers — deliberately absent from GUARDIANS
    so verify-agentctl requires no new hook to cover it. [] == may pass.

    Inactive (chat / small-change / AGENTCTL_PLAN_REVIEW=0) => [] always: the gate
    is byte-identical to absent for non-substantive sessions. Active checks:
      - a review must exist (state.plan_review) — else the gate is unmet;
      - it must be bound to `target_plan` (pr.plan_path == target_plan) — a review
        of an earlier plan version is stale and does not clear a later one;
      - the verdict must be `pass`, or `override` with a non-empty reviewer AND note
        (the explicit user deadlock escape); `revise`/unknown blocks."""
    if not plan_review_active(state):
        return []
    pr = state.plan_review
    if pr is None:
        return ["no thinker review recorded — run: plan-review (thinker verdict required before this plan is approved/applied)"]
    if not target_plan or pr.plan_path != target_plan:
        return [
            "thinker review is stale — it examined "
            f"{pr.plan_path!r} but the target plan is {target_plan!r}; re-run plan-review on the current plan"
        ]
    if pr.verdict == _PLAN_REVIEW_PASS:
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
    return [f"thinker review verdict is {pr.verdict!r} — plan blocked until a passing review (or an explicit override) is recorded"]


def replan_coverage_blockers(old_doc, new_doc, critique) -> list[str]:
    """Verify the critique's similarities/differences split is COVERED by the
    corrected plan — the dataflow, NOT the cognitive item->field mapping (that
    stays prose: the gate never decides WHICH stage an item belongs to).

      - PRESERVE: every declared similarity (critique.invariants_to_preserve) must
        appear as a substring of the new plan's conditions + invariants text;
        missing => a blocker naming the item.
      - CHANGE: if any difference is declared (critique.differences_to_remove is
        non-empty), the multiset of (means, method) across the new stages must
        differ from the old plan's — proof some means/method was re-selected to
        remove the difference; unchanged => one blocker.

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
        old_mm = sorted((s.means.means, s.means.method) for s in old_doc.stages)
        new_mm = sorted((s.means.means, s.means.method) for s in new_doc.stages)
        if old_mm == new_mm:
            out.append(
                "differences_to_remove is non-empty but no stage means/method changed "
                "— a difference cannot be removed without re-selecting a means/method"
            )
    return out


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
