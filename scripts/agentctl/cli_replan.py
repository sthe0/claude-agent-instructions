"""Replan, effort-fire acknowledgement and coverage subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

from . import (
    effort,
    gates,
    advisor,
    carry_forward,
    grant_shadow,
    grounding_gate,
    order_approvals,
    plugins,
    premise,
    task_accumulator,
)
from .config import Thresholds
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import Runner
from .plan import (
    change_tier,
    load_plan,
    PlanError,
    entries_grants_sha256,
    grants_sha256,
    load_plan_with_digest,
    materialized_grant_entries,
    plan_meta_element_keys,
    refined_grant_entries,
    replan_class,
    stage_norm_keys,
    stage_reattest_digest,
)
from .state import (
    AGENT_ACTOR,
    EFFORT_FIRE_FINISHED_WORK,
    NORMALIZATION_LEVELS,
    is_settled,
    is_skipped,
    GateRecord,
    Node,
    Normalization,
    Outcome,
    ReattestStash,
    SessionState,
    StageStatus,
    WeightClass,
)
from .store import StateStore
from . import effort_segments
from .cli_common import _judge_and_refuse
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .machine import transition
from .cli_common import (
    _order_agreement_refusal,
    _plan_file_sha256,
    _load_prior_plan,
    _write_plan_snapshot,
    _write_plan_version_snapshot,
    _snapshot_approved_plan,
    _replan_baseline_path,
    _ledgered_order_key,
    _effort_cross_totals,
    _flush_order_effort,
    _order_customer_id,
    _difficulty_id,
    _archive_difficulty,
    _agent_self_decision_refusal,
    _since_user_backstop_gate,
    _record_backstop_crossing,
    _crossing_data,
    _self_approve_detail,
    _autonomy_for,
    _is_agent_override,
    _agent_review_override,
    _baseline_plan_for_tier,
    _frozen_edge_stages,
    _review_baseline,
    _carry_records,
    _log_settled_rows,
    _verdict_survives,
    _autonomy_directive_data,
    _apply_refined_stage_fields,
    _sync_venue_from_plan,
    _log_gate,
    _with_advisories,
    _submission_advice,
    _submission_problems,
    _clear_open_fires_if_customer,
    _effort_fire_escalation_data,
    _fire_directive_figures,
    _note_mandated_review,
    _renew_arming,
    _settle_mandated_review,
    _effort_fire_refusal,
    _require,
    _clear_since_user_decision,
    _replan_class_data,
    _park_node,
    _enumeration_escape_counts,
    _note_round_release,
    _review_cycle_arrive,
    _cost_rows,
    _refresh_spend,
    _rematch_stage_uids,
    _store_continuation_base,
    continuation_decision,
    _second_current_stage_refusal,
    _utcnow,
    _effort_now,
    _normalize_record_fields,
    _log_normalize_event,
)


def _snapshot_plan_version(store: StateStore, state: SessionState, plan_path: str) -> str | None:
    """Read `plan_path`'s current bytes, content-address them via
    `_write_plan_version_snapshot`, and return the sha256 digest -- the read+write pair
    `submit_plan` and every `replan` kind call so EVERY submitted plan version (not just
    approved ones) resolves to an actual file on disk. Best-effort: None on an unreadable
    file, leaving the caller to log the event without a digest rather than raise out of a
    command that has already decided to accept/apply these bytes."""
    try:
        data = Path(plan_path).read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    _write_plan_version_snapshot(store, state, data, digest, plan_path=state.plan_path or plan_path)
    return digest


def _refresh_approved_grant_snapshot(
    state: SessionState, store: StateStore, doc, data: bytes, digest: str,
) -> None:
    """Re-snapshot the just-applied plan bytes and rebind the approved grant set
    (`approved_grants_sha256` and the entries it covers) to them, mirroring
    `cmd_approve`'s own snapshot+hash binding (see its "Bind the approved grant set"
    comment) -- called from the `no_change` and `refinement` branches of `cmd_replan`,
    which apply corrected bytes to an ALREADY-APPROVED session in place, without
    requiring a fresh `approve`. Without this, those two branches update
    `state.plan_path` but leave `plan_snapshot_path`/`plan_snapshot_hash`/
    `approved_grants_sha256` pointed at the PRIOR approval's bytes, so
    `_stage_grant_entries` either hash-mismatches on a corrected snapshot that no
    longer matches the stale stamped hash, or silently keeps re-deriving grants from
    plan content the session no longer runs.

    The binding is not a re-derivation of every stage against the venue filesystem of
    this moment: that set would depend on files the user never approved. A session with
    stored entries gets `plan.refined_grant_entries` -- a stage whose grant inputs did
    not move keeps its approved entries, a moved one admits only the plan's own change,
    and venue drift is withheld and logged (`grant_drift_withheld`). A session bound
    before entries were stored gets entries only when `doc` still reproduces the hash it
    was approved under; otherwise its hash is left untouched (as `--renormalize` does),
    so dispatch keeps withholding what the live derivation no longer reproduces.

    `doc`, `data` and `digest` are the ONE read `cmd_replan` took of `args.plan`
    (`load_plan_with_digest`) -- passed through rather than re-read here, so this
    function can never bind a snapshot to bytes different from the ones `doc` was
    parsed from or `state.accepted_plan_digest` was stamped from. Belt and braces:
    refuses to rebind (raising, rather than silently keeping the stale snapshot)
    when `digest` does not match `state.accepted_plan_digest` -- the caller must
    always stamp that field from this SAME buffer first (both `cmd_replan`
    branches do, immediately before calling this)."""
    if digest != state.accepted_plan_digest:
        raise ValueError(
            "refusing to rebind the approved-grant snapshot: the plan bytes' "
            f"digest {digest!r} does not match state.accepted_plan_digest "
            f"{state.accepted_plan_digest!r} -- stamp accepted_plan_digest from "
            "this same buffer before refreshing the snapshot"
        )
    replaced = _approved_snapshot_doc(state)
    snap = _write_plan_snapshot(store, state, data, digest)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    stored = state.approved_grant_entries
    if stored is None:
        if (state.approved_grants_sha256
                and grants_sha256(doc) == state.approved_grants_sha256):
            state.approved_grant_entries = materialized_grant_entries(doc)
        return
    entries, withheld = refined_grant_entries(stored, replaced, doc)
    state.approved_grant_entries = entries
    state.approved_grants_sha256 = entries_grants_sha256(entries)
    if withheld:
        state.log("grant_drift_withheld", stages=withheld)


def _approved_snapshot_doc(state: SessionState):
    """The approved-plan snapshot as a plan, or None when it cannot be read."""
    snap_path = state.plan_snapshot_path
    if not snap_path or not Path(snap_path).exists():
        return None
    try:
        return _load_prior_plan(state, snap_path, strict=False)
    except (OSError, PlanError):
        return None


def _agent_fire_ack_refusal(
    state: SessionState, last: dict, decision: str,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an agent-authored fire-acknowledge: a refusal when it may not
    proceed, else None and the backstop crossing it would be (None below the backstop)."""
    if _ledgered_order_key(state) is None:
        return "the order has no user-approved version; the fire is the user's decision", None
    if last.get("scale") != effort.SCALE_REPLANS:
        return "an open spend/wall-clock fire is owed to the user, not the agent", None
    if decision != "revise":
        return f"decision {decision!r} on a fire is the user's; the agent may only revise", None
    def load_doc():
        try:
            return load_plan(state.plan_path, strict=False)
        except (OSError, PlanError):
            return None

    backstopped, crossing = _since_user_backstop_gate(
        state, load_doc, decision="fire_acknowledge", axis="replans_since_user_decision",
        backstop=Thresholds().effort_replan_backstop(), what="acknowledging an effort fire",
        cleared_by="a user approval, fire-acknowledgement or renegotiation")
    if backstopped is not None:
        return backstopped, None
    refused = _agent_self_decision_refusal(
        state, last.get("difficulty_id_at_fire"),
        event="the fire", consumed="an earlier fire", decision="fire")
    return (refused, None) if refused is not None else (None, crossing)


def _agent_renegotiation_refusal(
    state: SessionState, decision: str, doc,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an agent-authored renegotiation at the diagnosing-replan
    ceiling: a refusal when a `continue` may not proceed, else None and the backstop
    crossing it would be (None below the backstop)."""
    if decision != "continue":
        return f"{decision!r} changes the order's scope or ends it; that is the user's decision", None
    if doc is None or _ledgered_order_key(state) is None:
        return "the order has no user-approved version to continue within", None
    if state.renegotiation_ceiling_difficulty_id is None:
        diff = state.difficulty
        if _difficulty_id(diff) is None or not diff.complete():
            return ("no complete planning-difficulty record (declare, investigate, critique) "
                    "is open"), None
        return "no ceiling refusal is recorded; run the replan without a decision first", None
    backstopped, crossing = _since_user_backstop_gate(
        state, lambda: doc, decision="renegotiation_continue", axis="replans_since_user_decision",
        backstop=Thresholds().effort_replan_backstop(), what="continuing a renegotiation",
        cleared_by="a user approval, fire-acknowledgement or renegotiation")
    if backstopped is not None:
        return backstopped, None
    refused = _agent_self_decision_refusal(
        state, state.renegotiation_ceiling_difficulty_id, event="the ceiling event")
    if refused is not None:
        return refused, None
    # Past the backstop the gate has just held `doc` to the boundary; only below it is the
    # boundary still unjudged.
    if crossing is None:
        boundary = _autonomy_for(state, doc)
        if not boundary["eligible"]:
            return f"the plan is outside the approved boundary: {boundary['reason']}", None
    return None, crossing


_TRIGGER_EXIT_OVERRIDE = "override"


_TRIGGER_EXIT_CHANGED = "changed"


_TRIGGER_EXIT_ADMITTED = "approve is admitted on the recorded override"


_TRIGGER_EXIT_CLEARED = "approve is admitted on the review record that clears the gate"


_TRIGGER_EXIT_FRESH_PASS = (
    "approve re-checks the plan-review gate on the records carried onto the changed plan; "
    "a fresh counted PASS is owed for whatever they leave uncovered")


def _review_trigger_exit(state: SessionState, plan_path: str) -> str | None:
    """Which exit of a review-cost trigger a replan to `plan_path` would take, or None when
    no trigger is open: the same bytes the trigger fired on leave by the recorded override,
    any other bytes by a fresh review."""
    fire = state.review_trigger_fire
    if fire is None or state.node != Node.DIAGNOSING.value:
        return None
    if gates.plan_digest_equivalent(state, fire.get("plan_sha256"), _plan_file_sha256(plan_path)):
        return _TRIGGER_EXIT_OVERRIDE
    return _TRIGGER_EXIT_CHANGED


def _trigger_exit_text(trigger_exit: str, override: dict | None) -> str:
    """What `approve` rests on after the exit: the recorded agent override when one
    stands, else the review record that cleared the gate; the changed plan owes a fresh pass."""
    if trigger_exit == _TRIGGER_EXIT_CHANGED:
        return _TRIGGER_EXIT_FRESH_PASS
    return _TRIGGER_EXIT_ADMITTED if override else _TRIGGER_EXIT_CLEARED


def _drop_agent_overrides(state: SessionState) -> None:
    """Remove every agent-authored plan-review override record: the changed plan a trigger
    exit offers is owed a fresh review, which an override of the old bytes must not stand in for."""
    if state.plan_review is not None and _is_agent_override(state.plan_review):
        state.plan_review = None
    state.plan_stage_reviews = {
        scope: record for scope, record in state.plan_stage_reviews.items()
        if not _is_agent_override(record)}
    state.plan_pair_reviews = {
        pair: record for pair, record in state.plan_pair_reviews.items()
        if not _is_agent_override(record)}


def _kind_within_boundary(state: SessionState, kind: str, doc, *, ledgered: bool) -> str:
    """A refinement that moves the order digest, adds a resource beyond the user's
    approved set, or changes a command the engine cannot resolve to a resource is not a
    refinement: it re-enters the approval gate as substantive. `ledgered`: the order has
    a user-approved ledger record (`_ledgered_order_key`) or the plan declares a
    `parent_norm`; without either nothing is judged. A plan judged on the parent-norm basis
    re-enters the gate whenever its verdict is no longer eligible (a dropped parent check,
    an uncovered resource, an unresolved item the handoff does not carry)."""
    if kind != "refinement" or not ledgered:
        return kind
    verdict = _autonomy_for(state, doc)
    if verdict.get("basis") == gates.PARENT_NORM_BASIS:
        return kind if verdict["eligible"] else "substantive"
    if (verdict["order_changed"] or verdict["extra_resources"]
            or (verdict["unresolved_changed_commands"] and not _vouched_by_thinker_pass(verdict))):
        return "substantive"
    return kind


def _vouched_by_thinker_pass(verdict: dict) -> bool:
    """Whether the changed unresolved commands of `verdict` are all engine-executed and a
    thinker pass has vouched them: the whole-plan first verdict, or a stage-scoped pass at
    the scope the review delta reports as sufficient (each stage's own, when several
    stages carry a changed command)."""
    if verdict.get("thinker_pass") is not True or verdict.get("first_verdict_key") is None:
        return False
    return not any(c["needs_user"] for c in verdict["unresolved_changed_commands"])


def _replan_kind(state: SessionState, old, new) -> str:
    """The kind a replan from `old` to `new` applies as. The verify-identity relaxation
    is admitted only for a ledgered order; a plan that declares a `parent_norm` is judged
    by `_kind_within_boundary` too but gets no relaxation. `parent_norm` sits in the order
    place, which diffs as a refinement, so removing or changing the one the baseline
    declared is substantive here: the plan would otherwise leave the parent-norm basis
    (and its parent check) through an edit nobody judges."""
    from .plan import diff_plans
    ledgered = _ledgered_order_key(state) is not None
    old_order, order = old.meta.order, new.meta.order
    old_parent_norm = old_order.parent_norm if old_order is not None else None
    parent_norm = order.parent_norm if order is not None else None
    if old_parent_norm and old_parent_norm != parent_norm:
        return "substantive"
    parent_norm_plan = bool(parent_norm)
    kind = diff_plans(old, new, relax_verify_identity=ledgered)
    return _kind_within_boundary(state, kind, new, ledgered=ledgered or parent_norm_plan)


def _passed_dependents_closure(live_stages, new_stages, roots) -> set[int]:
    """`roots` plus every PASSED stage that transitively depends on one of them, along the
    dependency edges of either plan version."""
    edges: dict[int, set[int]] = {}
    for stage in (*live_stages, *new_stages):
        edges.setdefault(stage.index, set()).update(stage.depends_on)
    passed = {s.index for s in live_stages if s.outcome.status == StageStatus.PASSED.value}
    closure = set(roots)
    grew = True
    while grew:
        grew = False
        for index in passed - closure:
            if edges.get(index, set()) & closure:
                closure.add(index)
                grew = True
    return closure


def _reopened_stages(state: SessionState, new_stages, roots) -> set[int]:
    """The PASSED stages `replan --reopen roots` re-arms: the roots and the PASSED stages
    depending on them. A root naming no live stage is ignored."""
    known = set(roots) & {s.index for s in state.stages}
    if not known:
        return set()
    return {
        i for i in _passed_dependents_closure(state.stages, new_stages, known)
        if state.stage(i).outcome.status == StageStatus.PASSED.value
    }


def _frozen_stage_refusal(state: SessionState, tier, reopening: set[int]):
    """Refusal text and data for a replan that edits an operative or interface leaf of a
    PASSED stage the replan does not reopen; None when none does."""
    frozen = {}
    for stage in state.stages:
        if stage.outcome.status != StageStatus.PASSED.value or stage.index in reopening:
            continue
        change = tier.stages.get(stage.index)
        paths = [p for p, cls in change.moved if cls in ("O", "I")] if change else []
        if paths:
            frozen[stage.index] = paths
    if not frozen:
        return None
    named = "; ".join(f"stage {i} ({', '.join(p)})" for i, p in sorted(frozen.items()))
    flags = " ".join(f"--reopen {i}" for i in sorted(frozen))
    return (f"replan refused: it edits what an already PASSED stage was built from or "
            f"promises — {named}; nothing was applied. Re-run with {flags} to re-arm the "
            f"stage and every PASSED stage depending on it, or revert the edit",
            {"blockers": [f"stage {i} (PASSED): {', '.join(p)}" for i, p in sorted(frozen.items())],
             "frozen_stages": sorted(frozen)})


def _replan_pending_indices(state: SessionState, new, kind: str, tier=None,
                            reopened: frozenset[int] | set[int] = frozenset()) -> list[int]:
    """Indices of `new`'s stages this replan leaves to run: every stage but one whose
    PASSED verdict survives it. A refinement or no-op never re-arms a PASSED stage; a
    substantive replan keeps a PASSED stage's verdict only when `_verdict_survives` holds:
    its definition and its suppliers' interfaces are unchanged (`stage_norm.stage_carried`),
    or only verdict-preserving leaves moved. A stage in `reopened` is always left to run."""
    live = {s.index: s for s in state.stages}
    pending = []
    for ns in new.stages:
        prev = live.get(ns.index)
        passed = prev is not None and prev.outcome.status == StageStatus.PASSED.value
        if passed and ns.index not in reopened and (
            kind != "substantive" or _verdict_survives(state.stages, new.stages, ns.index, tier)
        ):
            continue
        pending.append(ns.index)
    return pending


def _count_replan(state: SessionState, change_class: str) -> None:
    """Count one replan of the task, on the cross-session total and on the since-user
    total the agent self-decision backstop reads — when it is operative. A no_change,
    bookkeeping or check-only replan changes nothing the work runs under, so it spends
    none of the budget those totals bound (the same split `effort.replan_count` makes)."""
    _settle_mandated_review(state, change_class == effort.OPERATIVE_CLASS)
    if change_class != effort.OPERATIVE_CLASS:
        return
    task_accumulator.add(
        state.task_id, "replan_count", 1, session_id=state.session_id, now=_utcnow(),
        mirror_axis="replans_since_user_decision",
    )


def _effort_fire_window(state: SessionState) -> list[int] | None:
    """The window stages of the spend/wall-clock fire whose difficulty is the open one,
    or None when the open difficulty was not opened by such a fire."""
    difficulty = state.difficulty
    if difficulty is None:
        return None
    for fire in reversed(state.effort_fires):
        if (fire.get("difficulty_uid") == difficulty.uid
                and fire.get("scale") in effort.RATIO_SCALES):
            return list(fire.get("window_stages") or [])
    return None


def _refresh_runtime_grant_titles(state: SessionState, stage_index: int, new_title: str) -> None:
    """A refinement/no_change replan keeps every stage at its existing index (only a
    substantive replan can renumber -- that case is `_rekey_runtime_grants`, run on
    the next approve), but MAY retitle a stage the same replan touches. A runtime
    grant's stamped `stage_title` must track that retitle in place, else a later
    substantive approve's re-key-by-title finds zero matches for the grant's now-stale
    title and drops it (round-4 F2)."""
    for entry in state.runtime_grants.get(str(stage_index), []):
        entry["stage_title"] = new_title


def _renormalize_replan(args, state, store: StateStore, runner: Runner | None) -> Directive:
    """The light path: the executor replaces his own SEQUENCE of operations.

    `Means.method` is the REQUIREMENT on the way of acting — the planner's and the
    customer's, moved only through the review and approval a replan re-arms. `Means.
    procedure` is the sequence proposed for meeting it, and it is the EXECUTOR's:
    reading the code routinely shows a better order, and making him buy that order at
    the price of a re-approval is what produces the two failures the field split exists
    to remove — an executor who follows a worse sequence because it is written down, or
    one who quietly rewrites what he is held to, neither visible in the diff as what it
    is.

    `--renormalize` is therefore a TYPED CLAIM, not a verdict of `diff_plans`: the
    executor says "the procedure is the only thing I changed", and the engine checks it
    (`gates.renormalization_blockers`) rather than inferring it. Keeping it off
    `diff_plans` leaves that function's three-word vocabulary and every caller intact,
    and makes the refusal message able to name the norm the claim turned out to touch.

    Three things this path deliberately does NOT do, each because it is not a replan:

    * It does not run `_apply_refined_stage_fields`. Only `means.procedure` is copied
      onto the live stages, so the claim the engine just verified stays true of the live
      state too — no recorded `criterion.observation`, no `outcome`, no status is
      disturbed, and the comparison stage 8 requires of a passed stage keeps standing.
    * It does not re-arm a FAILED stage or leave DIAGNOSING. Working a difficulty
      through is a separate obligation with its own record; re-sequencing inside a
      stage does not discharge it.
    * It logs `renormalize`, not `replan`, so `effort.replan_count` — which fires the
      divergence trigger at three — counts norm revisions and not an executor using the
      authority the plan gave him.

    It DOES re-stamp `accepted_plan_digest`: that field must name the bytes the session
    is executing, and after this call those are `args.plan`'s. An AcceptanceReview
    carrying per-requirement bindings stays current (a procedure is no deliverable of
    any requirement); one written before bindings existed goes stale on the digest
    move. The directive names the requirement ids that went stale, if any."""
    from .plan import load_plan as _load

    # Backfill a snapshot for a legacy (pre-snapshot) session BEFORE this path rewrites
    # plan_path, exactly as the no_change branch of cmd_replan does and for a sharper
    # reason: without it `old_path` falls back to plan_path, which after one
    # renormalization holds the RENORMALIZED bytes, and the walk-in-small-steps this
    # branch claims to prevent would be open on precisely the sessions that have no
    # snapshot. Best-effort (see _snapshot_approved_plan); a None leaves the prior
    # fallback, so nothing here can refuse a renormalization.
    if not (state.plan_snapshot_path and Path(state.plan_snapshot_path).exists()):
        backfilled = _snapshot_approved_plan(store, state)
        if backfilled:
            state.plan_snapshot_path, state.plan_snapshot_hash = backfilled
    old_path = _replan_baseline_path(state)
    # Lenient OLD / strict NEW, for the reason cmd_replan's own loads document: the
    # comparison baseline may be a snapshot frozen before a newer trunk tightened the
    # schema, and only the incoming plan is held to today's submission grade. Comparing
    # against the SNAPSHOT (not plan_path) is also what makes successive renormalizations
    # honest: each is measured against the bytes that were approved, so a norm edit
    # cannot be walked to in small steps.
    old = _load_prior_plan(state, old_path, strict=False)
    # Read args.plan's bytes ONCE and derive `new`, the digest stamped below and the
    # snapshot written below from that same buffer -- three separate re-reads of a
    # file the coordinator can edit concurrently used to leave `accepted_plan_digest`
    # and the plan-version snapshot each free to bind to different bytes than the
    # ones `new` was diffed and gated against.
    new, _new_plan_bytes, _new_plan_digest = load_plan_with_digest(args.plan)
    run = runner if runner is not None else advisor.subprocess_runner
    submission = _submission_problems(new, run, state.weight_class, state=state, baseline=old)
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "renormalization blocked: the corrected plan does not meet "
                         "submission requirements",
                         data={"problems": submission})
    refusals = gates.renormalization_blockers(old, new)
    _log_gate(state, "renormalization", refusals, passed=not refusals)
    if refusals:
        return Directive(False, state.node, "replan",
                         "not a renormalization: this edit reaches the norm, not just "
                         "the sequence of operations — drop --renormalize and replan it "
                         "through the review and approval it is owed",
                         data={"blockers": refusals})
    live_status = {s.index: s.outcome.status for s in state.stages}
    rtblock = gates.renorm_retention_blockers(
        state, new,
        [ns.index for ns in new.stages
         if live_status.get(ns.index) != StageStatus.PASSED.value],
        installing_plan_path=args.plan)
    _log_gate(state, "renorm_retention", rtblock, passed=not rtblock)
    if rtblock:
        return Directive(False, state.node, "replan",
                         "renormalization blocked: an open norm-edit obligation is no "
                         "longer carried by a stage that still runs",
                         data={"blockers": rtblock})
    state.pending_renorms = gates.open_renorms(state)
    changed: list[int] = []
    for ns in new.stages:
        try:
            cur = state.stage(ns.index)
        except KeyError:
            continue
        if cur.means.procedure != ns.means.procedure:
            changed.append(ns.index)
        cur.means.procedure = ns.means.procedure
    state.plan_path = args.plan
    state.accepted_plan_digest = _new_plan_digest
    # The approved snapshot deliberately stays on the APPROVED bytes (see above), so the
    # binding is never moved here: stored entries stay as approved. A session bound
    # before entries were stored gets them materialized, but only when `new` still
    # reproduces the approved hash -- a procedure that changes what the stage derives is
    # not covered by the approval the binding records, and the hash is never moved here.
    if (state.approved_grants_sha256 and state.approved_grant_entries is None
            and grants_sha256(new) == state.approved_grants_sha256):
        state.approved_grant_entries = materialized_grant_entries(new)
    # renormalize is one of the replan kinds every submitted version is
    # content-addressed for, despite logging a distinct event name (see the class
    # docstring above on why it's `renormalize`, not `replan`).
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest, plan_path=args.plan)
    _new_plan_sha256 = _new_plan_digest
    state.log("renormalize", stages=changed, plan=args.plan,
              prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        "renormalized: the procedure of "
        + (f"stage(s) {changed}" if changed else "no stage")
        + " was replaced; every norm the plan sets is unchanged",
        data={"stages": changed})


def _replan_cause(state: SessionState, explicit_reason: str | None) -> dict:
    """D9 history capture: why THIS replan happened. Preferred source is the active
    Difficulty record's critique — functional_ground/replanning_task are exactly the
    "why" the overcome-difficulty cycle already produces, so this reads them rather
    than asking the caller to retype them. Falls back to an optional free-text
    --reason for a bare, non-DIAGNOSING replan (a plain refinement/no_change with no
    difficulty behind it). Returns {} when neither is available — a replan with no
    stated cause is captured as such, never refused for lacking one."""
    critique = state.difficulty.critique if state.difficulty is not None else None
    if critique is not None:
        return {
            "cause_source": "difficulty",
            "functional_ground": critique.functional_ground,
            "replanning_task": critique.replanning_task,
        }
    reason = (explicit_reason or "").strip()
    if reason:
        return {"cause_source": "reason", "reason": reason}
    return {}


def _report_acceptance_staleness(d: Directive, args, store: StateStore) -> None:
    """Name, on an applied replan of every kind, the requirement ids whose recorded
    acceptance the plan it carries has invalidated (`acceptance_stale`, empty when none) --
    the resolution gate's own check on the state the replan just saved
    (`gates.acceptance_stale_requirements`), so the author learns at the edit what
    verify-final would otherwise refuse later. Silent when acceptance is inactive, no
    AcceptanceReview exists or the replan was refused."""
    if not d.ok:
        return
    state = store.load(args.session)
    if state is None or state.acceptance_review is None or not gates.acceptance_active(state):
        return
    stale = gates.acceptance_stale_requirements(state) or []
    d.data["acceptance_stale"] = stale
    if stale:
        d.detail += (f"; the recorded acceptance is stale for requirement id(s) {stale} — "
                     "re-run accept before resolution")


def _replan_spelling_advisories(plan_path: str) -> list[str]:
    """The mixed-spelling lint over the NEW plan, on every Directive `replan`
    returns — refused ones included, since the author is about to edit the plan
    anyway. A plan that does not load has nothing to lint; the refusal the
    replan already returned names why."""
    from .plan import PlanError, load_plan as _load

    try:
        doc = _load(plan_path, strict=False)
    except (OSError, PlanError):
        return []
    return grant_shadow.mixed_spelling_advisories(doc)


@dataclass
class _ReplanContext:
    """What the phases of `_cmd_replan` share: the command's inputs, then the values
    each gate phase derives for the ones after it."""
    state: SessionState
    store: StateStore
    args: Any
    runner: Runner | None
    crossed: list[dict] | None
    reopened: list[int] | None
    reopen_ignored: list[int] | None
    warnings: list[str] | None = None
    normalize_factor: str = ""
    normalize_level: Any = None
    agent_acked_difficulty: bool = False
    closing_waiver: str | None = None
    gate_state: Any = None
    carry_preview: Any = None
    carry_change: Any = None
    trigger_exit: str | None = None
    run: Any = None
    new: Any = None
    new_plan_bytes: Any = None
    new_plan_digest: str = ""
    auth_kind: str = ""
    tier: Any = None
    tier_number: int | None = None
    change_class: Any = None
    reopening: set = field(default_factory=set)
    auth_waived: bool = False
    renorm_pending: Any = None
    old: Any = None
    prev_plan_sha256: Any = None
    new_plan_sha256: str = ""
    echo_advice: list[str] = field(default_factory=list)
    diagnosing: bool = False
    replan_cause: dict = field(default_factory=dict)
    planless: bool = False
    continuation: Any = None
    continuation_base: Any = None


def _cmd_replan(args, *, store: StateStore, runner: Runner | None = None,
                crossed: list[dict] | None = None,
                reopened: list[int] | None = None,
                reopen_ignored: list[int] | None = None,
                warnings: list[str] | None = None) -> Directive:
    """`crossed`, when given, receives the backstop crossing a renegotiation applied;
    `reopened`, the indices of the PASSED stages an applied `--reopen` re-armed;
    `reopen_ignored`, the `--reopen` indices that named a stage that is not PASSED;
    `warnings`, the grounding judge's outage warnings for the submitted plan.

    Every phase but the last either refuses with a Directive or returns None to let the
    next one run, in the order the gates were written; the last applies the plan."""
    ctx = _ReplanContext(
        state=_require(store, args.session), store=store, args=args, runner=runner,
        crossed=crossed, reopened=reopened, reopen_ignored=reopen_ignored,
        warnings=warnings, planless=args.plan is None)
    if ctx.planless:
        return _replan_planless(ctx)
    refusal = _replan_entry_gates(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_renegotiation_gate(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_closure_gates(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_review_gate(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_submission_gates(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_norm_and_premise_gates(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_baseline_and_coverage_gates(ctx)
    if refusal is not None:
        return refusal
    _replan_record_acceptance(ctx)
    ctx.continuation, ctx.continuation_base = continuation_decision(ctx.state, ctx.new.stages)
    if not ctx.continuation.ok:
        ctx.auth_kind = "substantive"
    apply = {"no_change": _replan_apply_no_change,
             "refinement": _replan_apply_refinement}.get(ctx.auth_kind, _replan_apply_substantive)
    return apply(ctx)


def _replan_entry_gates(ctx: _ReplanContext) -> Directive | None:
    state, store, args = ctx.state, ctx.store, ctx.args

    # --normalize-factor lets a replan record a normalize event in the SAME call,
    # instead of requiring a separate `normalize` invocation first. Validated here
    # (cheap, touches no state) so a bad flag refuses before any gate runs below.
    # Deliberately does NOT mutate state.difficulty.normalization: this command
    # saves state at several points (the enumeration-bag fold, the stale-
    # disposition invalidation) that sit ahead of later refusals (the
    # plan_approval-plugin block, the critique-coverage gate), and a mutation
    # staged here would ride one of those saves onto disk as an orphan if a
    # later gate then refused the replan — persisted with no normalize event
    # ever logged for it, and invisible to a retry once the hint at the bottom
    # of this function stops asking for the now-apparently-already-set flag.
    # gates.normalization_blockers instead takes this pending factor as an
    # explicit argument at its call site below, so the gate is satisfied
    # without state.difficulty being touched; the record is set together with
    # the event at the single logging point past every refusal (see below).
    normalize_factor = (getattr(args, "normalize_factor", None) or "").strip()
    normalize_level = getattr(args, "normalize_level", None)
    if normalize_factor and getattr(args, "renormalize", False):
        return Directive(False, state.node, "replan",
                         "--normalize-factor and --renormalize are incompatible: a "
                         "renormalization is defined as touching no norm, so it has "
                         "nothing for --normalize-factor to record")
    if normalize_factor:
        _nf_error, normalize_factor = _normalize_record_fields(normalize_factor, normalize_level, None)
        if _nf_error:
            return Directive(False, state.node, "replan", _nf_error)
        # An incomplete DIAGNOSING cycle is left to gates.difficulty_blockers below
        # (same action, "declare") rather than repeated here with a second message.

    # precondition: an unacknowledged effort-divergence fire (whether it forced this
    # DIAGNOSING entry itself via the passing-diagnose path, or merely rode along on
    # a failing stage/final-check's Directive) must be explicitly decided via
    # `agentctl fire-acknowledge` before the plan may be re-sequenced — this is the
    # gap the FAILING branches of record_result/verify_final left open (they attach
    # the fire data but never force a decision on it). Checked FIRST, ahead of
    # difficulty_blockers, since it is orthogonal to the declare/investigate/
    # critique/normalize cycle that difficulty_blockers governs.
    _note_mandated_review(state)
    _fire_refusal = _judge_and_refuse(state, store, args, effort_segments.ACT_REPLAN, "replan")
    if _fire_refusal is not None:
        return _fire_refusal

    # precondition: a nested difficulty (difficulty-open) is a sub-cycle of the enclosing
    # one -- closed with difficulty-close, never swept away by the replan of its parent.
    sblock = gates.difficulty_stack_blockers(state)
    _log_gate(state, "difficulty_stack", sblock, passed=not sblock)
    if sblock:
        return Directive(False, state.node, "difficulty_close",
                         "replan blocked by an open nested difficulty",
                         data={"blockers": sblock})

    # precondition: inside the DIAGNOSING cycle, the difficulty record must be
    # complete before a plan may be re-normed (variant (b) — internal command
    # precondition, not a tool-hook gate). [] outside DIAGNOSING.
    dblock = gates.difficulty_blockers(state)
    _log_gate(state, "difficulty_blockers", dblock, passed=not dblock)
    if dblock:
        return Directive(False, state.node, "declare", "replan blocked by incomplete difficulty record",
                         data={"blockers": dblock})

    if not state.plan_path and not ctx.planless:
        return Directive(False, state.node, "submit_plan", "no current plan to replan against")
    ctx.normalize_factor, ctx.normalize_level = normalize_factor, normalize_level
    return None


def _replan_renegotiation_gate(ctx: _ReplanContext) -> Directive | None:
    state, store, args, crossed = ctx.state, ctx.store, ctx.args, ctx.crossed
    from .plan import load_plan as _load

    # diagnosing-replan renegotiation gate (GitHub #177): once this task's
    # cross-session replan_count reaches the ceiling
    # (`effort-replan-absolute`) while inside DIAGNOSING, a further replan is
    # refused until the order's customer has made an explicit renegotiation
    # decision. [] (and this whole block a no-op) outside DIAGNOSING and below
    # the ceiling — gates.diagnosing_replan_blockers itself returns [] there.
    # Placed AFTER effort_fire_blockers/difficulty_blockers (an unacknowledged
    # fire or an incomplete difficulty record must still be resolved through the
    # ordinary path first) and BEFORE normalization_blockers/failure_address_
    # blockers/plan_review_blockers/renormalize (those are properties of the
    # CORRECTED PLAN or of the sequence it changes; this gate is about whether
    # another replan should happen at all, a strictly prior question).
    cross_replan_count = int(
        task_accumulator.get(state.task_id).get("per_axis_totals", {}).get("replan_count", 0) or 0
    )
    rrblock = gates.diagnosing_replan_blockers(state, task_replan_count=cross_replan_count)
    _log_gate(state, "diagnosing_replan", rrblock, passed=not rrblock)
    if rrblock:
        decision = getattr(args, "renegotiation_decision", None)
        if not decision:
            state.renegotiation_ceiling_difficulty_id = _difficulty_id(state.difficulty)
            store.save(state)
            return Directive(
                False, state.node, "renegotiate", "replan blocked: " + rrblock[0],
                marker=DIRECTIVE_ESCALATE_TO_USER,
                data={"blockers": rrblock, "replan_count": cross_replan_count},
            )
        renegotiated_by = (getattr(args, "renegotiated_by", None) or "").strip()
        if not renegotiated_by:
            return Directive(
                False, state.node, "renegotiate",
                "--renegotiation-decision requires a non-empty --renegotiated-by",
                data={"blockers": rrblock},
            )
        renegotiation_note = (getattr(args, "renegotiation_note", None) or "").strip()
        if not renegotiation_note:
            return Directive(
                False, state.node, "renegotiate",
                "--renegotiation-decision requires a non-empty --renegotiation-note",
                data={"blockers": rrblock},
            )
        try:
            order_doc = None if ctx.planless else _load(args.plan, strict=False)
        except (OSError, PlanError):
            order_doc = None
        order = order_doc.meta.order if order_doc is not None else None
        crossing = None
        if renegotiated_by.casefold() == AGENT_ACTOR:
            refusal, crossing = _agent_renegotiation_refusal(state, decision, order_doc)
            if refusal is not None:
                return Directive(
                    False, state.node, "renegotiate",
                    f"renegotiation by the agent refused: {refusal}",
                    marker=DIRECTIVE_ESCALATE_TO_USER,
                    data={"blockers": rrblock, "reason": refusal},
                )
            state.agent_ack_difficulty_ids.append(_difficulty_id(state.difficulty))
        elif order is not None and order.customer_id and renegotiated_by != order.customer_id:
            return Directive(
                False, state.node, "renegotiate",
                f"renegotiation author {renegotiated_by!r} does not match order "
                f"customer_id {order.customer_id!r}; record it as the customer of "
                "record, or correct --renegotiated-by",
                data={"blockers": rrblock},
            )
        state.renegotiations.append({
            "decision": decision,
            "note": renegotiation_note,
            "by": renegotiated_by,
            "ts": _utcnow(),
            "task_replan_count_at_decision": cross_replan_count,
        })
        state.log("renegotiation", decision=decision, by=renegotiated_by)
        _record_backstop_crossing(state, crossing)
        _clear_since_user_decision(state, renegotiated_by)
        if decision == "abandon":
            # mirrors cmd_block's own bypass-transition idiom (and fire-acknowledge's
            # "abandon" decision) — parks reversibly via unblock, never RESOLVED, and
            # never touches args.plan.
            _park_node(state)
            state.log("block", reason=f"renegotiation abandoned: {renegotiation_note}")
            store.save(state)
            return Directive(
                True, state.node, "unblock",
                "session abandoned after DIAGNOSING-replan renegotiation; unblock to resume",
                marker="ESCALATE",
                data={"renegotiation": state.renegotiations[-1]},
            )
        # continue/rescope: fold this renegotiation's effect the same way task-reset
        # does (task_accumulator.reset is now a deliberate second caller — see its
        # docstring) and fall through to the rest of this command unchanged. This is
        # also the concrete fix for GitHub #201: effort.py's own REPLANS-scale
        # effective_deltas() reads this exact accumulator field against the same
        # static ceiling and never resets it itself, which is why an unbounded
        # renegotiation-free loop could re-fire on every subsequent replan; zeroing
        # it here means the closing replan starts the scale's next
        # budget from zero instead.
        task_accumulator.reset(state.task_id)
        state.review_rounds = 0
        state.review_release_difficulty_id = None
        # The reset above is already on disk, so the record of the decision that earned it
        # lands with it: a later gate below may still refuse this replan.
        store.save(state)
        if crossing is not None and crossed is not None:
            crossed.append(crossing)
    return None


def _replan_closure_gates(ctx: _ReplanContext) -> Directive | None:
    state, store, args, runner = ctx.state, ctx.store, ctx.args, ctx.runner
    normalize_factor = ctx.normalize_factor

    # The renormalization branch sits HERE deliberately: after difficulty_blockers (a
    # renormalization offers a whole plan, and offering one while the difficulty record
    # is still incomplete is how a difficulty gets re-plannned away rather than worked
    # through) and after the no-plan check it needs a baseline from, but BEFORE the two
    # CLOSURE preconditions below and the plan-review and plan_approval gates after them.
    # The closure preconditions are conditions of LEAVING the DIAGNOSING cycle, and this
    # path does not leave it: blocking a re-sequencing on them demanded a re-norming as
    # the price of an act that closes nothing, and — worse — a `--normalization-waiver`
    # passed with the flag would have been spent, and logged, on a call that never
    # discharged the difficulty. The gates below govern the NORM, which is the very thing
    # this path is refused for touching: making an executor re-arm a review to reorder his
    # own operations is the cost the field split exists to remove.
    refusal = _order_agreement_refusal(state, args.plan, "replan", "replan")
    if refusal is not None:
        return refusal
    if getattr(args, "renormalize", False):
        return _renormalize_replan(args, state, store, runner)

    # closure precondition: a difficulty exposed a norm-failure; closing it (leaving the
    # DIAGNOSING cycle) REQUIRES re-norming the reproducible factor. Mandatory-if-
    # reproducible; the one-off escape is an explicit --normalization-waiver <reason>.
    # [] outside the DIAGNOSING-closure path, so a non-difficulty replan is unaffected.
    # `pending_factor` is this call's OWN still-unmutated --normalize-factor: the gate
    # judges it directly rather than reading state.difficulty.normalization, which this
    # command has deliberately not written yet (see the comment above).
    agent_acked_difficulty = (
        state.difficulty is not None
        and _difficulty_id(state.difficulty) in state.agent_ack_difficulty_ids
    )
    if agent_acked_difficulty and getattr(args, "normalization_waiver", None) is not None:
        _log_gate(state, "normalization_waiver", ["agent-acknowledged fire"], passed=False)
        return Directive(
            False, state.node, "normalize",
            "replan refused: the agent-acknowledged fire's difficulty closes on its real "
            "normalization record; a --normalization-waiver is not accepted",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"blockers": ["normalization waiver on an agent-acknowledged fire"]},
        )
    wtblock = gates.normalization_waiver_blockers(state, getattr(args, "normalization_waiver", None))
    if wtblock:
        _log_gate(state, "normalization_waiver", wtblock, passed=False)
        return Directive(
            False, state.node, "normalize",
            "replan refused: the difficulty record names norm target(s) the closing plan "
            "must change; a --normalization-waiver is not accepted for it",
            data={"blockers": wtblock},
        )
    nblock = gates.normalization_blockers(state, pending_factor=normalize_factor or None)
    closing_waiver: str | None = None
    if nblock:
        waiver = getattr(args, "normalization_waiver", None)
        if waiver is None:
            _log_gate(state, "normalization_blockers", nblock, passed=False)
            return Directive(False, state.node, "normalize",
                             "replan blocked: difficulty closure requires re-norming",
                             data={"blockers": nblock})
        if not waiver.strip():
            _log_gate(state, "normalization_blockers", nblock, passed=False)
            return Directive(False, state.node, "normalize",
                             "normalization waiver reason must not be empty",
                             data={"blockers": nblock})
        # a conscious, recorded bypass for a genuinely one-off factor — never a bypass
        # of the difficulty-record completeness precondition checked above.
        state.log("normalization_waived", reason=waiver, blockers=list(nblock))
        _log_gate(state, "normalization_waiver", nblock, passed=True)
        closing_waiver = waiver
    else:
        _log_gate(state, "normalization_blockers", nblock, passed=True)

    # closure precondition (R2): the fault must be ROUTED — the critique's failure_address
    # must be recorded (resource/normative/not_applicable provision), never a bare
    # omission — before the difficulty may be closed. Mirrors normalization_blockers: an
    # INTERNAL precondition (absent from GUARDIANS), [] outside the DIAGNOSING-closure
    # path, so a non-difficulty replan is unaffected. Unlike the normalization gate there
    # is no waiver — the explicit escape is a legal not_applicable on the critique itself.
    fablock = gates.failure_address_blockers(state)
    _log_gate(state, "failure_address_blockers", fablock, passed=not fablock)
    if fablock:
        return Directive(False, state.node, "critique",
                         "replan blocked: the fault must be routed to the inadequate "
                         "provision (re-run critique with --failure-address)",
                         data={"blockers": fablock})
    ctx.agent_acked_difficulty, ctx.closing_waiver = agent_acked_difficulty, closing_waiver
    return None


def _replan_review_gate(ctx: _ReplanContext) -> Directive | None:
    state, store, args = ctx.state, ctx.store, ctx.args
    from .plan import load_plan as _load

    # plan-review gate: the corrected plan (args.plan) must carry a thinker review
    # with a passing/overridden verdict BOUND to it before it may be applied. Gates
    # EVERY replan kind (refinement and substantive alike, per the user decision),
    # inactive for non-substantive sessions. A genuine no-op replan (args.plan ==
    # the already-reviewed plan_path) passes on the existing review.
    # The records are judged as carried onto the corrected plan: a tier 0-1 edit owes no
    # review, a tier 2 edit owes the edited stages' own. Nothing is written until the
    # replan is applied.
    try:
        reopened = _reopened_stages(state, _load(args.plan, strict=False).stages,
                                    getattr(args, "reopen", None) or [])
    except (OSError, PlanError):
        reopened = set()
    gate_state, carry_preview, carry_change = _carry_records(
        store, state, args.plan, in_place=False, reopened=reopened)
    trigger_exit = _review_trigger_exit(state, args.plan)
    # A changed plan leaves a review-cost trigger by owing a fresh counted PASS at approve,
    # so the replan itself does not wait on a review of bytes nobody has reviewed yet.
    prblock = (
        [] if trigger_exit == _TRIGGER_EXIT_CHANGED
        else gates.plan_review_blockers(gate_state, args.plan))
    _log_gate(state, "plan_review", prblock, passed=not prblock)
    if prblock:
        # With the round-release valve active the blockers ARE the release message,
        # which says no further review is required — so the refusal must stop
        # prescribing one, or the engine would name as the cure the very act the
        # valve just retired, and the loop would have no exit. This is the path the
        # valve exists for: post-approval replan is where review cycles recur.
        round_release = _note_round_release(state, prblock, store)
        if round_release:
            message = (
                "replan blocked: the review-round budget is spent, so the decision is "
                "the coordinator's — see blockers"
            )
            replan_data = {"blockers": prblock, "plan_review_round_release": round_release}
        else:
            try:
                review_doc = _load(args.plan)
            except (OSError, PlanError):
                review_doc = None
            delta = gates.review_delta(gate_state, review_doc, args.plan)
            if carry_change is not None:
                delta["tier"] = carry_change.tier
                delta["stage_tiers"] = {i: c.tier for i, c in sorted(carry_change.stages.items())}
            if delta["record_scope_args"]:
                review_commands = [
                    "plan-review --target " + args.plan + " " + scope_arg
                    for scope_arg in delta["record_scope_args"]
                ]
            else:
                review_commands = ["plan-review --target " + args.plan]
            message = (
                "replan blocked: the corrected plan needs a thinker review "
                "(run: " + "; run: ".join(review_commands)
                + f"; render with `{delta['render_command']}`)"
            )
            replan_data = {"blockers": prblock, "plan_review_round_release": round_release,
                           "review_delta": delta}
            changed = continuation_decision(state, review_doc.stages)[0].check_changed if review_doc else []
            if changed:
                replan_data["check_changed"] = changed
                message += (f"; check_changed: stage(s) {changed} change the done_criterion or "
                            "verify_command of a live stage, so the review must judge that check")
        return Directive(False, state.node, "plan_review", message, data=replan_data)
    ctx.gate_state, ctx.carry_change, ctx.trigger_exit = gate_state, carry_change, trigger_exit
    ctx.carry_preview = carry_preview
    return None


def _replan_submission_gates(ctx: _ReplanContext) -> Directive | None:
    state, store, args, runner = ctx.state, ctx.store, ctx.args, ctx.runner
    reopen_ignored = ctx.reopen_ignored

    # Submission seam (b): the single NEW-side load and the check it feeds. Its placement
    # answers one ordering: before the plan_approval plugin block below, so a command that
    # refuses on submission grade has mutated no persisted state.
    # Before diff_plans further down, so all three diff outcomes are covered by one check —
    # a no_change replan re-materializes live stages from these bytes just as a refinement
    # does, so "unchanged" is no reason to let an unvalidated plan in.
    # Entry-point fallback — see cmd_submit_plan's identical comment. Bound here rather
    # than below the refusals because this seam's own judged refusal needs it; binding a
    # callable spends nothing, and the judge is reached only on a prefilter hit.
    run = runner if runner is not None else advisor.subprocess_runner
    # Read args.plan's bytes ONCE and derive `new`, the digest stamped below (seam c)
    # and the snapshot _refresh_approved_grant_snapshot writes from this SAME buffer —
    # `_load(args.plan)` used to be a separate read from the digest stamp and the
    # snapshot write, leaving a window for a concurrent edit to desynchronize them.
    new, _new_plan_bytes, _new_plan_digest = load_plan_with_digest(args.plan)
    baseline = _baseline_plan_for_tier(store, state)
    submission = _submission_problems(
        new, run, state.weight_class,
        _frozen_edge_stages(state, baseline, new),
        state=state, baseline=baseline, warnings=ctx.warnings)
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "replan blocked: the corrected plan does not meet submission "
                         "requirements",
                         data={"problems": submission})

    # replan-authorization gate: outside DIAGNOSING, a non-substantive edit
    # (refinement or no_change) to an ALREADY APPROVED plan must have been
    # presented to the user as a diff and proven delivered before it may be
    # applied — the write-side twin of plan_presentation_blockers (see that
    # gate's docstring on state.py's PlanPresentation). The kind fed to the
    # gate is computed from the SAME baseline this command's own diff (below)
    # uses, via _replan_baseline_path, so the kind the gate reasons about is
    # the kind that will actually be applied. Placed strictly after the
    # submission refusal above (a plan that does not meet submission grade is
    # not worth authorizing) and strictly before the plan_approval PLUGIN
    # block below; this command has still written nothing to disk at this point.
    auth_kind = _replan_kind(state, _load_prior_plan(state, _replan_baseline_path(state), strict=False), new)

    # A finished stage is frozen against the edits that change what it was built from or
    # promises (operative and interface leaves): a replan of ANY kind touching one is
    # refused unless it names the stage with --reopen, which re-arms it and the PASSED
    # stages depending on it. Refused before anything is written. Without a known old
    # side there is no tier, and the replan routes as it did before the tier existed.
    tier = None
    if baseline is not None:
        tier = change_tier(baseline, new)
    change_class = replan_class(tier)
    reopen_roots = set(getattr(args, "reopen", None) or [])
    unknown_reopen = sorted(reopen_roots - {s.index for s in state.stages})
    if unknown_reopen:
        return Directive(False, state.node, "replan",
                         f"replan refused: --reopen names no such stage: {unknown_reopen}")
    reopening = _reopened_stages(state, new.stages, reopen_roots)
    if reopen_ignored is not None:
        reopen_ignored.extend(sorted(i for i in reopen_roots if i not in reopening))
    if tier is not None:
        frozen = _frozen_stage_refusal(state, tier, reopening)
        _log_gate(state, "frozen_stage", [] if frozen is None else frozen[1]["blockers"],
                  passed=frozen is None)
        if frozen is not None:
            return Directive(False, state.node, "replan", frozen[0], data=frozen[1])

    tier_number = tier.tier if tier is not None else None
    unwaived_blockers = gates.replan_authorization_blockers(state, args.plan, diff_kind=auth_kind)
    auth_waived = gates.replan_authorization_waived_by_tier(tier_number) and bool(unwaived_blockers)
    arblock = [] if auth_waived else unwaived_blockers
    _log_gate(state, "replan_authorization", arblock, passed=not arblock)
    if arblock:
        return Directive(False, state.node, "present_plan",
                         "replan blocked: this plan edit has not been authorized by "
                         "the user",
                         data={"blockers": arblock})
    ctx.run, ctx.new, ctx.new_plan_bytes, ctx.new_plan_digest = run, new, _new_plan_bytes, _new_plan_digest
    ctx.auth_kind, ctx.auth_waived = auth_kind, auth_waived
    ctx.tier, ctx.tier_number, ctx.change_class, ctx.reopening = tier, tier_number, change_class, reopening
    return None


def _replan_norm_and_premise_gates(ctx: _ReplanContext) -> Directive | None:
    state, store, args = ctx.state, ctx.store, ctx.args
    gate_state, carry_change, new = ctx.gate_state, ctx.carry_change, ctx.new
    carried = bool(ctx.carry_preview and ctx.carry_preview["carried"])
    auth_kind, tier, reopening = ctx.auth_kind, ctx.tier, ctx.reopening

    # Norm-edit consequence: a replan closing a difficulty whose record names norm targets
    # must be the re-norming it named, and an obligation an earlier replan admitted must
    # survive this one. Before the plan_approval plugin block below, whose dispositions
    # save can persist state, so a refusal here leaves the state file as it was.
    renorm_pending = _replan_pending_indices(state, new, auth_kind, tier, reopening)
    neblock = gates.normative_edit_blockers(state, new, renorm_pending,
                                            installing_plan_path=args.plan)
    _log_gate(state, "normative_edit", neblock, passed=not neblock)
    rtblock = gates.renorm_retention_blockers(state, new, renorm_pending,
                                              installing_plan_path=args.plan)
    _log_gate(state, "renorm_retention", rtblock, passed=not rtblock)
    if neblock or rtblock:
        return Directive(False, state.node, "replan",
                         "replan blocked: the corrected plan does not carry out the norm "
                         "edit the difficulty named (stage `renorms` must list each norm "
                         "file, on a stage that still runs)",
                         data={"blockers": neblock + rtblock})

    # plan_approval PLUGIN gate: mirror cmd_approve's plugins.plugin_gate_blockers
    # composition so a refinement/no_change replan cannot rotate the plan bytes back
    # to VERIFYING while a premise-plugin blocker (undispositioned question, stale
    # per-stage rebind) goes unchecked — the 2026-07-09 attest-vs-execute hole.
    # Evaluate against the PROPOSED plan (args.plan): premise_blockers reads
    # state.plan_path, so swap it for the call and restore in finally. [] when no
    # plugin extends plan_approval, so the row still logs (passed, empty) and the
    # replan is byte-identical to before.
    _saved_plan_path = gate_state.plan_path
    try:
        gate_state.plan_path = args.plan
        pblock = plugins.plugin_gate_blockers(gate_state, "plan_approval")
    finally:
        gate_state.plan_path = _saved_plan_path
    # Invalidate dispositions whose cited stage fields moved in the proposed plan so
    # the mismatch is visible in question-list output even when this replan is blocked
    # by the gate (#123). Runs here — after the try-finally restored plan_path and
    # before the pblock return — so a blocked replan still surfaces stale notes on disk.
    # A carried view (`carried`: `gate_state` is a copy) already holds the carried premise
    # stamps; on the refused path the real state gets the same carry first, so the
    # invalidation below marks only the dispositions the edit really made stale. On the
    # applied path the carry is left to `_carry_records`, whose `carried` verdict it would
    # otherwise change. A copy holding only an order change's settled rows carries no stamp.
    _inv_bag = state.plugins.get("premise")
    if _inv_bag is not None and (not carried or pblock):
        _inv_changed = False
        if carried:
            _carry_baseline, _ = _review_baseline(store, state)
            if _carry_baseline is not None:
                _inv_changed = bool(
                    carry_forward.carry_premise(state, _carry_baseline, new, carry_change))
        _inv_stage_keys = {s.index: stage_norm_keys(s) for s in new.stages}
        _inv_meta_keys = plan_meta_element_keys(new)
        _inv_changed = premise.invalidate_stale_dispositions(
            _inv_bag, _inv_stage_keys, meta_keys=_inv_meta_keys
        ) or _inv_changed
        _inv_changed = (
            premise.invalidate_stale_order_dispositions(_inv_bag, _inv_stage_keys)
            or _inv_changed
        )
        if _inv_changed:
            store.save(state)
    _log_gate(state, "plan_approval_plugin", pblock, passed=not pblock)
    if pblock:
        # Escape counts are read against the PROPOSED plan's digest, not
        # state.plan_path's, since that is the plan version the blocker speaks for.
        return Directive(False, state.node, "close_questions",
                         "replan blocked: the corrected plan carries unresolved "
                         "plan_approval premises (dispose open questions / rebind "
                         "stale per-stage bindings against the new plan)",
                         data={"blockers": pblock,
                               # LEGACY field: the retired gate's escape history
                               "enumeration_escapes": _enumeration_escape_counts(
                                   state, new)})
    ctx.renorm_pending = renorm_pending
    return None


def _replan_baseline_and_coverage_gates(ctx: _ReplanContext) -> Directive | None:
    state, store, args = ctx.state, ctx.store, ctx.args
    from .plan import diff_plans
    new, tier_number, trigger_exit = ctx.new, ctx.tier_number, ctx.trigger_exit
    agent_acked_difficulty = ctx.agent_acked_difficulty
    _new_plan_bytes, _new_plan_digest = ctx.new_plan_bytes, ctx.new_plan_digest

    # #8: diff against the plan AS APPROVED (the immutable snapshot), not plan_path —
    # which the coordinator may have edited in place. Absent a snapshot (legacy
    # session, or an approve that predates the field) fall back to plan_path.
    old_path = _replan_baseline_path(state)
    # OLD side is a read-only comparison baseline: a snapshot frozen before a
    # newer trunk tightened the schema (free-text executors #7, or a later-required
    # substantive field like [stage.principle].derivation) must stay diffable — the
    # lenient load keeps the structural parse but skips every submission-grade
    # check. Only the NEW side — loaded strictly at seam (b) above — and submit-plan
    # are strict.
    old = _load_prior_plan(state, old_path, strict=False)
    # Content-address both sides of this replan -- the OLD baseline (read fresh;
    # `old_path` may be a legacy plan_path fallback never snapshotted before) and the
    # NEW plan, reusing the (data, digest) seam already read via `load_plan_with_digest`
    # rather than re-reading args.plan a second time. Computed once here, past every
    # refusal above, and used by whichever of the three kind branches below applies.
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest, plan_path=args.plan)
    _new_plan_sha256 = _new_plan_digest
    # coverage gate: inside the difficulty flow, the corrected plan must CARRY the
    # critique's similarities into conditions/invariants and CHANGE a means/method
    # for the declared differences. Empty split -> [] -> behaves exactly as before.
    if state.difficulty and state.difficulty.critique and trigger_exit != _TRIGGER_EXIT_OVERRIDE:
        cov = gates.replan_coverage_blockers(
            old, new, state.difficulty.critique, tier=tier_number,
            cost_tier_window=_effort_fire_window(state))
        _log_gate(state, "replan_coverage", cov, passed=not cov)
        if cov:
            waiver = getattr(args, "coverage_waiver", None)
            if agent_acked_difficulty and waiver is not None:
                _log_gate(state, "replan_coverage_waiver", ["agent-acknowledged fire"], passed=False)
                return Directive(
                    False, state.node, "declare",
                    "replan refused: the agent-acknowledged fire's corrected plan must pass the "
                    "critique coverage check; a --coverage-waiver is not accepted",
                    marker=DIRECTIVE_ESCALATE_TO_USER,
                    data={"coverage_blockers": cov, "blockers": ["coverage waiver on an agent-acknowledged fire"]},
                )
            if waiver is None:
                return Directive(False, state.node, "declare",
                                 "replan blocked: critique coverage (carry the critique into the plan, "
                                 "or pass --coverage-waiver '<reason>' with a reason it does not apply)",
                                 data={"coverage_blockers": cov})
            if not waiver.strip():
                return Directive(False, state.node, "declare",
                                 "coverage waiver reason must not be empty",
                                 data={"coverage_blockers": cov})
            # a conscious, recorded bypass — only the coverage gate, never the
            # difficulty-record completeness precondition checked above.
            state.log("replan_coverage_waived", reason=waiver, blockers=list(cov))
            _log_gate(state, "replan_coverage_waiver", cov, passed=True)

    # Past every refusal above, so this replan is going through -- the single point
    # where a --normalize-factor passed to this call both sets
    # state.difficulty.normalization (in DIAGNOSING, mirroring cmd_normalize's own
    # DIAGNOSING branch) and gets its event logged. Setting the record and logging
    # the event together here, rather than staging the record earlier, is what keeps
    # a refused-then-retried call from either double-counting the event or
    # persisting an orphaned record with no event to match it.
    if agent_acked_difficulty and trigger_exit is None and (
        diff_plans(old, new) == "no_change"
        or not any((d or "").strip() for d in state.difficulty.critique.differences_to_remove)
    ):
        return Directive(
            False, state.node, "replan",
            "replan refused: the agent-acknowledged fire's corrected plan must change the "
            "operative surface against a critique that names a difference to remove",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"blockers": ["corrected plan changes no operative surface, or no difference to remove"]},
        )
    ctx.old, ctx.prev_plan_sha256, ctx.new_plan_sha256 = old, _prev_plan_sha256, _new_plan_sha256
    return None


def _replan_record_acceptance(ctx: _ReplanContext) -> None:
    """Past every refusal: books the replan's side effects shared by all three apply
    branches and derives what they log."""
    state, store, args = ctx.state, ctx.store, ctx.args
    normalize_factor, normalize_level = ctx.normalize_factor, ctx.normalize_level
    new, tier_number = ctx.new, ctx.tier_number
    reopening, renorm_pending, auth_waived = ctx.reopening, ctx.renorm_pending, ctx.auth_waived
    _new_plan_digest, run, reopened = ctx.new_plan_digest, ctx.run, ctx.reopened

    if normalize_factor:
        if state.node == Node.DIAGNOSING.value:
            state.difficulty.normalization = Normalization(
                factor=normalize_factor, level=normalize_level, destination=None)
        _log_normalize_event(state, factor=normalize_factor, level=normalize_level,
                             destination=None, in_diagnosis=(state.node == Node.DIAGNOSING.value))

    if reopened is not None:
        reopened.extend(sorted(reopening))
    state.pending_renorms = gates.bound_renorms(state, new, renorm_pending)
    # The replan-loop counterpart of cmd_approve's reset: a replan that gets this far has
    # applied a corrected plan, so the rounds spent arguing about the previous one are
    # settled and the next loop starts from zero. Placed here — past every refusal of this
    # command and common to all three of its success branches — so a REFUSED replan never
    # silently refills the budget it was blocked by.
    #
    # The `--renormalize` early return above is deliberately NOT reset: that path changes
    # the sequence without touching the norm under review (it demands no review at all and
    # logs `renormalize` precisely so the effort trigger does not read it as a norm
    # revision), so the rounds already spent still belong to the same norm and stay
    # against it.
    #
    # Fold into the cross-session task accumulator (item B) before the reset, same
    # reasoning as cmd_approve's fold above.
    # A tier 0-2 edit does not start a new review loop: its records are carried onto the
    # new version (`carry_forward`), so the rounds, the counted digest and the passes
    # stay with them; every other edit settles the old loop and starts from zero.
    _, carry_report, _ = _carry_records(store, state, args.plan, in_place=True, reopened=reopening)
    carry_note = carry_forward.describe(carry_report)
    _log_settled_rows(state, args.plan, carry_report)
    if carry_report["carried"]:
        state.log("review_carried", plan=args.plan, tier=carry_report["tier"],
                  records=list(carry_report["records"]), owed=list(carry_report["owed"]),
                  held=list(carry_report["held"]))
    else:
        task_accumulator.add(
            state.task_id, "plan_review_rounds", state.plan_review_rounds,
            session_id=state.session_id, now=_utcnow(),
        )
        state.plan_review_rounds = 0
        state.plan_review_counted_digest = ""
        # Terminal-pass custody — same "current approval cycle" boundary as the
        # pair above: a replanned plan starts with no pass on record for any scope.
        state.plan_review_passes = {}
    task_accumulator.add(
        state.task_id, "code_review_rounds", state.code_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    # Reset alongside the pair above (item A) — same reasoning: rounds spent
    # code-reviewing the previous plan version are settled once a corrected plan lands.
    state.code_review_rounds = 0
    # Stamped HERE and not up at the seam: every refusal path of this command is now behind
    # us — the last of them being the critique-coverage gate just above — so like seam (a)
    # the digest only ever names bytes the session ACCEPTED. (Not an enumeration: this
    # command refuses in eight or so places, and a new one added below this line would
    # break the property no matter how the list above it reads.)
    # (The load of args.plan can also raise out of the command, but it raises AT seam (b)
    # itself — the seam IS that load and the check it feeds — so no placement inside this
    # range answers for it.)
    # Stamping at seam (b) itself is now positively WRONG, not merely fragile: the
    # enumeration block's `store.save` sits between that seam and the refusals above, so a
    # digest stamped up there can be persisted for a plan the pblock or coverage gate
    # then rejects — whenever that save runs at all. Placement, not the absence of an intervening save, is what carries the
    # invariant — and the leak would be a silently wrong digest, not a crash.
    # Stamped from the SAME buffer `new` was parsed from (seam (b) above), not a
    # fresh re-read of args.plan (the former `_stamp_accepted_plan_digest(state,
    # args.plan)`) — so this digest can never name bytes different from the ones
    # `new` was diffed and gated against.
    state.accepted_plan_digest = _new_plan_digest

    # Seam (b)'s advice channel, attached to whichever of this command's several success
    # Directives is returned — an echo never changes which one that is. Computed HERE, on
    # the same "every refusal is behind us" property the digest stamp just above relies on,
    # and not up at the seam it belongs to: the judge costs live `claude -p` calls per
    # flagged stage, so a replan blocked by submission or by critique coverage must not pay
    # for advice that is then discarded. Placement is about cost only — the advice is
    # warn-only and cannot influence any decision above it either way. (`run` is bound at
    # the seam above; only this CALL is deferred, which is where the cost is.)
    echo_advice = _submission_advice(new, run, state.weight_class, args.plan)
    if carry_note:
        echo_advice = echo_advice + [carry_note]
    if auth_waived:
        state.log("replan_authorization_waived", plan=args.plan, tier=tier_number)
        echo_advice = echo_advice + [
            f"replan authorization waived: a tier {tier_number} edit changes nothing the user "
            "approved or a reviewer judged, so no replan_diff presentation was required"]
    # Non-blocking hint naming --normalize-factor -- advisory only, never a gate (a
    # replan with nothing reproducible to re-norm is a completely ordinary replan).
    # Suppressed when this call itself recorded one (the `not normalize_factor`
    # check) and when the most recent `normalize`/`replan`/`submit_plan` event in
    # history is a `normalize` -- covering both a prior separate DIAGNOSING
    # `normalize` call (the gate's own required path) and a prior `normalize`
    # OUTSIDE DIAGNOSING (record-only, no gate at all): either way the hint would
    # otherwise falsely claim "no normalize event recorded" right after one was.
    _last_normalize_plan_event = next(
        (h for h in reversed(state.history)
         if h.get("event") in ("normalize", "replan", "submit_plan")), None)
    _normalize_recorded_since_last_plan_event = (
        _last_normalize_plan_event is not None
        and _last_normalize_plan_event.get("event") == "normalize")
    if not normalize_factor and not _normalize_recorded_since_last_plan_event:
        echo_advice = echo_advice + [
            "no normalize event recorded for this replan; pass --normalize-factor "
            "<reproducible cause> to record one in the same call"
        ]

    # if we are exiting the DIAGNOSING cycle (difficulty complete), the failed
    # stage is re-armed and we leave the cycle back to VERIFYING so next_stage can
    # retry it; the difficulty record is cleared so a later failure starts fresh.
    diagnosing = state.node == Node.DIAGNOSING.value

    # D9 capture: WHY this replan happened, read off the active Difficulty record
    # (already populated by declare/investigate/critique) BEFORE any branch below
    # clears it. Every branch below either sets state.difficulty = None (substantive,
    # unconditionally; no_change/refinement, when diagnosing) or leaves it — so this is
    # the last point at which the record is guaranteed still present. Falls back to an
    # optional --reason for a bare (non-DIAGNOSING) replan; never refuses when both are
    # absent, since this is a capture and not a gate.
    replan_cause = _replan_cause(state, getattr(args, "reason", None))

    # Effort-divergence spend refresh (call site 3): book against the OLD plan_path
    # BEFORE any branch below may rewrite state.plan_path — the opposite ordering from
    # each branch's own rederive() call, which must run AFTER that branch's stage-list
    # update. A cost row a just-spawned plan review wrote against the NEW path (args.plan)
    # is not lost by refreshing against the old path here — it is simply picked up by
    # the NEXT refresh against the new path, once a branch below rewrites plan_path.
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    _flush_order_effort(state, store=store)
    ctx.echo_advice, ctx.diagnosing, ctx.replan_cause = echo_advice, diagnosing, replan_cause


def _replan_apply_no_change(ctx: _ReplanContext) -> Directive:
    state, store, args, new = ctx.state, ctx.store, ctx.args, ctx.new
    reopening, diagnosing, echo_advice = ctx.reopening, ctx.diagnosing, ctx.echo_advice
    _new_plan_bytes, _new_plan_digest = ctx.new_plan_bytes, ctx.new_plan_digest
    closing_waiver, trigger_exit, replan_cause = ctx.closing_waiver, ctx.trigger_exit, ctx.replan_cause
    _prev_plan_sha256, _new_plan_sha256 = ctx.prev_plan_sha256, ctx.new_plan_sha256
    change_class = ctx.change_class

    # A legacy session with no approved-plan snapshot (plan_snapshot_path=None)
    # diffs plan_path against itself, so an in-place edit self-diffs to no_change
    # (issue #8, one branch deeper). Re-materialize each live stage's prose+verify
    # fields from the freshly-loaded plan BEFORE re-arming, or record-result runs
    # the STALE verify_command still held in state. Idempotent when the plan is
    # genuinely unchanged (copies identical values).
    for ns in new.stages:
        try:
            cur = state.stage(ns.index)
        except KeyError:
            continue
        _apply_refined_stage_fields(cur, ns)
        _refresh_runtime_grant_titles(state, ns.index, ns.title)
        if ns.index in reopening:
            cur.outcome.status = StageStatus.PENDING.value
    # final_check is meta-level (not per-stage), so it needs its own refresh
    # next to the stage loop above — a self-diffed no_change still means the
    # FILE changed relative to what was cached at submit-plan/last replan.
    state.final_check = new.meta.final_check
    _sync_venue_from_plan(state, new)
    # …and plan_path follows the bytes too, as it does on the refinement and
    # substantive branches. "no_change" names the DIFF, not the file: the stages,
    # final_check and venue above were all just re-materialized from `args.plan`, so
    # leaving plan_path on the previous file would leave the session executing one
    # path's content while every later fresh load (the premise gate, verify-final,
    # the next replan's baseline) reads a different path — and accepted_plan_digest,
    # stamped on `args.plan`, would name bytes plan_path does not point at.
    state.plan_path = args.plan
    # Unconditional refresh (not just a legacy backfill when no snapshot exists at
    # all): a no_change replan can still carry corrected bytes for stages this
    # diff kind doesn't cover (prose/verify only, per the diff), so the approved
    # snapshot and approved_grants_sha256 must track `new` every time, not only
    # the first time a session ever takes a snapshot.
    _refresh_approved_grant_snapshot(state, store, new, _new_plan_bytes, _new_plan_digest)
    if diagnosing:
        for s in state.stages:
            if s.outcome.status == StageStatus.FAILED.value:
                s.outcome.status = StageStatus.PENDING.value
        _archive_difficulty(state, closing_waiver)
        if not state.approval.passed:
            return _replan_to_plan_ready(
                state, store, new, echo_advice, trigger_exit=trigger_exit,
                kind="no_change", exited_diagnosing=True,
                plan=args.plan, prev_plan_sha256=_prev_plan_sha256,
                plan_sha256=_new_plan_sha256, change_class=change_class, **replan_cause)
        state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
        state.log("replan", kind="no_change", exited_diagnosing=True, plan=args.plan,
                  prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
                  change_class=change_class, **replan_cause)
        _count_replan(state, change_class)
        effort.rederive(state)  # re-derive AFTER logging so this in-flight replan is counted
        store.save(state)
        if state.ready_stages():
            return _with_advisories(Directive(
                True, state.node, "next_stage",
                "difficulty worked through; plan unchanged — retry the re-armed stage",
                data=_replan_class_data(state)), echo_advice)
        return _with_advisories(Directive(
            True, state.node, "continue", "difficulty worked through; resume execution",
            data=_replan_class_data(state)), echo_advice)
    effort.rederive(state)  # re-derive the estimate from the (possibly refined) stages
    store.save(state)
    return _with_advisories(Directive(
        True, state.node, "continue", "replan is a no-op; plan unchanged"), echo_advice)


def _replan_apply_refinement(ctx: _ReplanContext) -> Directive:
    state, store, args, new = ctx.state, ctx.store, ctx.args, ctx.new
    reopening, diagnosing, echo_advice = ctx.reopening, ctx.diagnosing, ctx.echo_advice
    _new_plan_bytes, _new_plan_digest = ctx.new_plan_bytes, ctx.new_plan_digest
    closing_waiver, trigger_exit, replan_cause = ctx.closing_waiver, ctx.trigger_exit, ctx.replan_cause
    _prev_plan_sha256, _new_plan_sha256 = ctx.prev_plan_sha256, ctx.new_plan_sha256
    change_class = ctx.change_class

    # apply prose refinements and re-arm any FAILED stage for another attempt
    for ns in new.stages:
        try:
            cur = state.stage(ns.index)
        except KeyError:
            continue
        # carry the corrected prose+means/method/conditions/invariants/verify into
        # state so a difficulty-driven refinement actually re-selects the means
        # (not just prose).
        _apply_refined_stage_fields(cur, ns)
        _refresh_runtime_grant_titles(state, ns.index, ns.title)
        if cur.outcome.status == StageStatus.FAILED.value or ns.index in reopening:
            cur.outcome.status = StageStatus.PENDING.value
    state.plan_path = args.plan
    # Same unconditional refresh as the no_change branch above: a refinement
    # touches stage prose/means/verify, which can shift a stage's DERIVED grants
    # even though grants_sha256 growth is already checked by diff_plans/
    # _grants_grew before this branch is ever reached -- the snapshot bytes and
    # approved_grants_sha256 must track `new`, not the plan this session most
    # recently APPROVED via a full `approve` call.
    _refresh_approved_grant_snapshot(state, store, new, _new_plan_bytes, _new_plan_digest)
    _sync_venue_from_plan(state, new)
    state.final_check = new.meta.final_check
    if diagnosing:
        _archive_difficulty(state, closing_waiver)
        if not state.approval.passed:
            return _replan_to_plan_ready(
                state, store, new, echo_advice, trigger_exit=trigger_exit,
                kind="refinement", exited_diagnosing=True,
                plan=args.plan, prev_plan_sha256=_prev_plan_sha256,
                plan_sha256=_new_plan_sha256, change_class=change_class, **replan_cause)
        state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
    state.log("replan", kind="refinement", exited_diagnosing=diagnosing, plan=args.plan,
              prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
              change_class=change_class, **replan_cause)
    _count_replan(state, change_class)
    effort.rederive(state)  # re-derive AFTER logging so this replan is counted
    store.save(state)
    if state.node == Node.VERIFYING.value and state.ready_stages():
        return _with_advisories(Directive(
            True, state.node, "next_stage", "refinement applied; retry the ready stage",
            data=_replan_class_data(state)), echo_advice)
    return _with_advisories(Directive(
        True, state.node, "continue", "refinement applied; resume execution",
        data=_replan_class_data(state)), echo_advice)


def _replan_apply_substantive(ctx: _ReplanContext) -> Directive:
    state, store, args, new = ctx.state, ctx.store, ctx.args, ctx.new
    reopening, echo_advice, tier = ctx.reopening, ctx.echo_advice, ctx.tier
    closing_waiver, trigger_exit, replan_cause = ctx.closing_waiver, ctx.trigger_exit, ctx.replan_cause
    _prev_plan_sha256, _new_plan_sha256 = ctx.prev_plan_sha256, ctx.new_plan_sha256
    change_class = ctx.change_class

    second_current = _second_current_stage_refusal(state, state.current_stage)
    if second_current is not None:
        return second_current
    # substantive: re-arm the plan-approval gate, reload stages, return to PLAN_READY.
    # #12: carry PASSED status forward for any stage whose FULL definition is
    # unchanged by the diff, so a substantive replan doesn't reset already-delivered
    # work to PENDING and force needless re-verification. Compare each new stage
    # against the LIVE stage (what actually ran) by the full-fidelity carry key; an
    # unchanged, previously-PASSED stage keeps its recorded Outcome intact.
    # Stage 6: build the re-attest stash FRESH alongside the carry-forward pass
    # above — same PASSED gating, a NARROWER key (method/control criterion/
    # expected result image/executor/done criterion, not the stage's whole
    # definition). This never accumulates across replans: each substantive
    # replan replaces state.reattest_stash wholesale, so a stage re-armed two
    # replans ago and never re-dispatched since simply has no entry — the only
    # cost of that gap is one unnecessary full dispatch, never an incorrect
    # PASS, and `dispatch --re-attest` treats a missing entry as condition-1
    # failure (no prior PASSED to re-attest against) rather than an error.
    live_by_index = {s.index: s for s in state.stages}
    reattest_stash: list[ReattestStash] = []
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if prev is None or prev.outcome.status != StageStatus.PASSED.value:
            continue
        matched = stage_reattest_digest(prev) == stage_reattest_digest(ns)
        reattest_stash.append(ReattestStash(
            stage_index=ns.index,
            operative_surface_matched=matched,
            prior_outcome=Outcome(
                status=prev.outcome.status,
                actual=prev.outcome.actual,
                fail_digests=list(prev.outcome.fail_digests),
                cost_usd=prev.outcome.cost_usd,
                duration_ms=prev.outcome.duration_ms,
                spawn_count=prev.outcome.spawn_count,
                delivered_head=prev.outcome.delivered_head,
                delivered_base=prev.outcome.delivered_base,
                landed_head=prev.outcome.landed_head,
                landed_base=prev.outcome.landed_base,
                landed_content_waiver=prev.outcome.landed_content_waiver,
            ),
            prior_control=prev.control,
            reattest_digest=stage_reattest_digest(ns),
        ))
    state.reattest_stash = reattest_stash
    declined_issues = gates.declined_issue_set(state)
    declined_titles = gates.declined_title_set(state)
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if prev is not None and ns.index not in reopening and _verdict_survives(
            state.stages, new.stages, ns.index, tier
        ):
            # A declined optional stage whose definition did not move stays declined; a
            # changed one goes back to PENDING for a fresh choice at the customer's
            # re-approval.
            if prev.outcome.status == StageStatus.PASSED.value or (
                is_skipped(prev) and ns.optional
            ):
                ns.outcome = prev.outcome
        # The customer's decline follows the stage's identity (its issue), not its index,
        # and outlives a reset to PENDING: `approve --by agent` refuses while a declined
        # stage is live, so only a customer approval can bring it back.
        if is_skipped(ns) or (
            not is_settled(ns) and gates.is_declined_stage(ns, declined_issues, declined_titles)
        ):
            ns.outcome.declined = True
    _store_continuation_base(state, ctx.continuation_base)
    _rematch_stage_uids(state, new.stages, ctx.continuation)
    state.stages = new.stages
    _sync_venue_from_plan(state, new)
    state.final_check = new.meta.final_check
    state.plan_path = args.plan
    state.plan_verified = True
    state.overall_done_criterion = new.meta.done_criterion or state.overall_done_criterion
    state.current_stage = None
    _archive_difficulty(state, closing_waiver)
    return _replan_to_plan_ready(
        state, store, new, echo_advice, trigger_exit=trigger_exit, kind="substantive", plan=args.plan,
        prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
        change_class=change_class, continuation=ctx.continuation, **replan_cause)


def _replan_planless(ctx: _ReplanContext) -> Directive:
    """The replan of a small-change session, which holds no plan to correct: it runs the
    gates that govern leaving the DIAGNOSING cycle (effort fire, nested difficulty, a
    complete difficulty record, the renegotiation ceiling, re-norming, the routed fault),
    then re-arms the failed stage and returns to VERIFYING. It edits no norm, so a
    difficulty record naming norm targets is refused: that fix is a plan's `renorms`."""
    state, store, args = ctx.state, ctx.store, ctx.args
    if state.weight_class != WeightClass.SMALL_CHANGE.value or state.plan_path:
        return Directive(False, state.node, "replan",
                         "replan needs --plan: only a small-change session that holds no plan "
                         "replans without one")
    node_refusal = gates.planless_replan_refusal(state)
    if node_refusal is not None:
        return Directive(False, state.node, "replan", node_refusal)
    if getattr(args, "renormalize", False):
        return Directive(False, state.node, "replan",
                         "--renormalize reorders a plan's procedure; a plan-less replan has no plan")
    refusal = _replan_entry_gates(ctx)
    if refusal is not None:
        return refusal
    refusal = _replan_renegotiation_gate(ctx)
    if refusal is not None:
        return refusal
    if state.difficulty is not None and state.difficulty.norm_targets:
        return Directive(False, state.node, "replan",
                         "a plan-less replan edits no norm, and the difficulty record names "
                         "norm target(s); a plan that lists them in `renorms` is the way out",
                         data={"blockers": [t.get("target", "") for t in state.difficulty.norm_targets]})
    refusal = _replan_closure_gates(ctx)
    if refusal is not None:
        return refusal

    if ctx.normalize_factor:
        state.difficulty.normalization = Normalization(
            factor=ctx.normalize_factor, level=ctx.normalize_level, destination=None)
        _log_normalize_event(state, factor=ctx.normalize_factor, level=ctx.normalize_level,
                             destination=None, in_diagnosis=True)
    replan_cause = _replan_cause(state, getattr(args, "reason", None))
    _flush_order_effort(state, store=store)
    for s in state.stages:
        if s.outcome.status == StageStatus.FAILED.value:
            s.outcome.status = StageStatus.PENDING.value
    _archive_difficulty(state, ctx.closing_waiver)
    state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
    state.current_stage = None
    state.log("replan", kind="planless", exited_diagnosing=True,
              change_class=effort.OPERATIVE_CLASS, **replan_cause)
    _count_replan(state, effort.OPERATIVE_CLASS)
    effort.rederive(state)
    store.save(state)
    return Directive(True, state.node, "next_stage",
                     "difficulty worked through; no plan to correct — retry the re-armed stage",
                     data=_replan_class_data(state))


def _replan_to_plan_ready(state: SessionState, store: StateStore, new, echo_advice: list[str],
                          *, trigger_exit: str | None, continuation=None, **log_fields) -> Directive:
    """Re-arm the plan-approval gate and park the session at PLAN_READY after a replan
    that needs the plan approved again, logging `log_fields` as the `replan` event. The
    approved plan picks the next stage afresh, so no stage stays current. `trigger_exit`
    is the review-cost trigger exit `_cmd_replan` already decided (None when no trigger is open)."""
    state.current_stage = None
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.node = Node.PLAN_READY.value
    if continuation is not None and continuation.check_changed:
        log_fields["check_changed"] = list(continuation.check_changed)
    if continuation is not None and not continuation.ok:
        log_fields["continuation_breaches"] = continuation.reasons()
    state.log("replan", **log_fields)
    _count_replan(state, log_fields["change_class"])
    effort.rederive(state)  # re-derive AFTER logging so this replan is counted
    _review_cycle_arrive(state, log_fields["plan_sha256"])
    if trigger_exit == _TRIGGER_EXIT_CHANGED:
        _drop_agent_overrides(state)
    if trigger_exit is not None:
        state.review_trigger_fire = None
    store.save(state)
    autonomy = _autonomy_directive_data(state, new)
    override = _agent_review_override(state)
    directive_data = {
        "autonomy": autonomy, **_replan_class_data(state),
        **({"agent_review_override": override} if override else {}),
        **({"review_trigger_exit": _trigger_exit_text(trigger_exit, override)}
           if trigger_exit is not None else {}),
    }
    if continuation is not None and not continuation.ok:
        directive_data["continuation_breaches"] = continuation.reasons()
    if continuation is not None and continuation.check_changed:
        directive_data["check_changed"] = list(continuation.check_changed)
    if autonomy["action"] == "self_approve" and (continuation is None or continuation.ok):
        return _with_advisories(Directive(
            True, state.node, "self_approve",
            _self_approve_detail(autonomy, replan=True),
            data=directive_data,
        ), echo_advice)
    return _with_advisories(Directive(
        True, state.node, "await_user_approval",
        f"{log_fields['kind']} replan; HARD GATE — re-approval required",
        marker="PLAN-READY", data=directive_data,
    ), echo_advice)


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    crossed: list[dict] = []
    reopened: list[int] = []
    reopen_ignored: list[int] = []
    replan_warnings: list[str] = []
    d = _cmd_replan(args, store=store, runner=runner, crossed=crossed, reopened=reopened,
                    reopen_ignored=reopen_ignored, warnings=replan_warnings)
    d.data.update(_crossing_data(crossed[0] if crossed else None))
    if reopened and d.ok:
        d.data["reopened_stages"] = reopened
        d.detail += f"; re-armed PASSED stage(s) {reopened} (--reopen)"
    if reopen_ignored and d.ok:
        d.data["reopen_ignored"] = reopen_ignored
        d.detail += (f"; --reopen {reopen_ignored} ignored: not PASSED, so there is no "
                     "verdict to re-arm")
    if args.plan is None:
        return d
    # Only on something to say: an unconditional setdefault would add an empty
    # `advisories` key to every replan Directive, which test_history_capture's
    # exact-equality pin reads as a changed contract.
    advisories = _replan_spelling_advisories(args.plan) + replan_warnings
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)
    _report_acceptance_staleness(d, args, store)
    return d


def cmd_fire_acknowledge(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The synchronous decision `gates.effort_fire_blockers` refuses dispatch/replan/
    submit_plan without: appends an "ack" onto the LAST entry of state.effort_fires
    (never deletes — the append-only audit trail is preserved) recording who decided
    and what. Three decisions:

    - "continue": accept the overrun and keep executing the current plan as-is. No
      node change — whatever node the fire's own diagnose transition already put the
      session in (always DIAGNOSING, since every record_fire call site forces that
      transition in the same call) is left untouched; the coordinator proceeds
      through the ordinary declare/investigate/critique/normalize/replan cycle next.
    - "abandon": the order no longer warrants continuing. Parks the session at
      BLOCKED — the same bypass-transition() idiom cmd_block itself uses — rather
      than RESOLVED: a mid-execution fire routinely fires with stages still
      PENDING, and check_invariants refuses RESOLVED unless every stage is
      PASSED, so RESOLVED would be a lie for exactly the sessions this decision
      exists to stop. cmd_unblock remains the (audited) way back in, same as an
      ordinary block.
    - "revise": the plan itself needs to change in response to the overrun. No node
      change needed for the same reason as "continue" (already DIAGNOSING); the
      difference is purely in the human decision recorded, informing what the
      coordinator does next.
    """
    state = _require(store, args.session)
    if not state.effort_fires:
        return Directive(False, state.node, "noop", "no effort-divergence fire recorded")
    last = gates.open_fire_record(state) or state.effort_fires[-1]
    if last.get("ack") is not None:
        return Directive(True, state.node, "noop", "fire already acknowledged", data={"fire": last})
    if last.get("disposition") == EFFORT_FIRE_FINISHED_WORK:
        return Directive(True, state.node, "noop",
                         "the last fire is a finished-work retier record: it opened no difficulty "
                         "and owes no acknowledgement", data={"fire": last})
    decision = args.decision
    if decision not in ("continue", "abandon", "revise"):
        return Directive(False, state.node, "noop", f"invalid --decision {decision!r}: "
                         "must be one of continue, abandon, revise")
    if not args.by or not args.by.strip():
        return Directive(False, state.node, "noop", "empty --by: must name who decided")
    by_agent = args.by.strip().casefold() == AGENT_ACTOR
    crossing = None
    if by_agent:
        refusal, crossing = _agent_fire_ack_refusal(state, last, decision)
        if refusal is not None:
            return Directive(
                False, state.node, "fire_acknowledge",
                f"fire-acknowledge by the agent refused: {refusal}",
                marker=DIRECTIVE_ESCALATE_TO_USER,
                data={"effort_fire": _effort_fire_escalation_data(state), "reason": refusal,
                      **(_fire_directive_figures(state) if last.get("arming_id") else {})},
            )
        state.agent_ack_difficulty_ids.append(_difficulty_id(state.difficulty))
        # The replans-scale fire rebased every scale's baseline; the spend / wall-clock
        # progress it swallowed is still owed to the user's own fire.
        replaced = last.get("replaced_baseline") or {}
        for scale in effort.SCALE_ORDER:
            if scale != effort.SCALE_REPLANS and scale in replaced:
                state.effort_baseline[scale] = replaced[scale]
    last["ack"] = {
        "by": args.by,
        "decision": decision,
        "ts": _utcnow(),
        "note": getattr(args, "note", None),
    }
    _acked_arming = effort_segments.find_arming(state.effort_segments, last.get("arming_id"))
    if _acked_arming is not None and _acked_arming["fire"]:
        _acked_arming["fire"]["ack"] = last["ack"]
    if not by_agent and decision != "abandon" and _acked_arming is not None and _acked_arming["closed_at"] is None:
        _renew_arming(state, store, args, _acked_arming, effort_segments.ACT_FIRE_ACK_BY_USER, args.by)
    state.log("fire_acknowledge", by=args.by, decision=decision)
    _record_backstop_crossing(state, crossing)
    _clear_since_user_decision(state, args.by)
    _clear_open_fires_if_customer(state, args.by)
    if decision == "abandon":
        _park_node(state)
        state.log("block", reason="user-abandoned-after-fire")
        store.save(state)
        return Directive(True, state.node, "unblock",
                         "session abandoned after unacknowledged effort-divergence fire; "
                         "unblock to resume",
                         marker="ESCALATE",
                         data={"fire": last})
    store.save(state)
    next_action = "declare" if state.difficulty is not None else "next_stage"
    return Directive(True, state.node, next_action,
                     f"fire acknowledged (decision={decision}); resuming",
                     data={"fire": last, **_crossing_data(crossing)})


def cmd_check_coverage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only pre-flight: would `replan --plan <new>` clear the critique coverage
    gate right now? Calls gates.replan_coverage_blockers directly — the same check
    cmd_replan runs — so a coverage miss surfaces BEFORE the coordinator spends a
    thinker plan-review on a corrected plan that would only bounce off replan's own
    gate afterward. Mirrors cmd_replan's OLD/NEW loading contract exactly (snapshot-
    else-plan_path, lenient OLD / strict NEW) but never saves, never logs a gate,
    never transitions state — a pre-flight that mutated what it inspects would
    corrupt the very state a later real replan diffs against."""
    state = _require(store, args.session)
    verdicts = _coverage_grounding_verdicts(args, state, store, runner)
    d = _check_coverage(args, state)
    if verdicts:
        d.detail += "\ngrounding verdicts (the judge on each stage replan would judge):\n" + "\n".join(verdicts)
        d.data["grounding_verdicts"] = verdicts
    return d


def _coverage_grounding_verdicts(args, state: SessionState, store: StateStore,
                                 runner: Runner | None) -> list[str]:
    """The grounding judge's verdict line for each stage of the incoming plan that replan would judge
    (a judge that cannot look says so on the line). Nothing when the plan does not load."""
    try:
        new = load_plan(args.new)
    except Exception:
        return []
    exempt = grounding_gate.exempt_stages(state, new, _baseline_plan_for_tier(store, state))
    return grounding_gate.coverage_verdict_lines(
        new, exempt, runner=runner, enabled=grounding_gate.judge_enabled(state.weight_class))


def _check_coverage(args, state: SessionState) -> Directive:
    if not (state.difficulty and state.difficulty.critique):
        return Directive(True, state.node, "inspect", "no active critique; nothing to cover")
    old_path = _replan_baseline_path(state)
    old = _load_prior_plan(state, old_path, strict=False)
    new = load_plan(args.new)
    blockers = gates.replan_coverage_blockers(
        old, new, state.difficulty.critique, tier=change_tier(old, new).tier,
        cost_tier_window=_effort_fire_window(state))
    if blockers:
        return Directive(False, state.node, "inspect",
                         "coverage blockers: " + "; ".join(blockers),
                         data={"coverage_blockers": blockers})
    return Directive(True, state.node, "inspect", "OK — coverage clear")


def cmd_effort_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only effort-divergence report: where does this session stand against the
    norm its approved plan declared, on each of the four scales?

    Exists because the trigger only ever fires INSIDE a command the coordinator chose
    to run (`record-result`, `verify-final`, or the `gates.effort_fire_blockers` refusal
    on `dispatch`/`submit-plan`/`replan`). A session that runs long WITHOUT reaching one
    of those — a stage the coordinator keeps working, a specialist that never returns —
    diverges unobserved, however far past the multiple it goes. This command is the
    observation that does not depend on such a call arriving, and it is what the
    UserPromptSubmit watch hook drives on every prompt.

    STRICTLY READ-ONLY, and `ok=True` on every path including a refusal-shaped answer:
    it never transitions, never seeds a difficulty, never appends to `state.effort_fires`
    and never calls `store.save`. A read command with a side effect would be a SECOND,
    hidden fire site — it would consume the one-fire-per-replan budget belt 2 keeps
    (`effort._replans_since_last_fire`) without anyone having diagnosed anything, and
    the real fire site would then fall silent. `effort.refresh_spend` does mutate the
    loaded `SessionState` in memory (that is how the spend accumulator is read at all);
    with no save, the on-disk state is untouched, which the test asserts on the bytes.

    Deliberately does NOT call `effort.rederive`: the fire sites compare against the
    STORED estimate, so re-deriving here would report a divergence against a comparand
    no gate uses — a watch that disagrees with the gate it watches is worse than none.
    The same rule is why every per-scale row is computed from `effort.effective_deltas`
    / `effective_ratios` with the same cross-session totals `divergence()` is handed:
    `at_or_past_threshold` (what the hook speaks on) and `would_fire` (what a fire site
    would act on) must be answers about one vector, not two."""
    state = store.load(args.session) if getattr(args, "session", None) else None
    if state is None:
        return Directive(True, "(none)", "start", "no session state; nothing to check",
                         data={"armed": False, "scales": []})
    if not effort.armed(state):
        return Directive(
            True, state.node, "inspect",
            "effort trigger not armed (no approved-plan baseline); no scale can diverge",
            data={"armed": False, "active": gates.effort_active(state), "scales": []},
        )
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    thr = Thresholds()
    multiple = thr.effort_divergence_multiple()
    # The SAME cross-session totals the fire sites pass, read once and used for both
    # halves of this report. Reporting the session-local vector while `would_fire` was
    # decided on the cross-session one is how a watch goes silent on exactly the case it
    # exists for: a resolved re-entry hands the fresh SessionState a replan count of 0
    # while the accumulator still holds the prior laps. See effort.effective_deltas.
    now = _effort_now(store, state)
    cross_totals = _effort_cross_totals(state, now, store=store)
    local = effort.deltas(state, now=now)
    delta = effort.effective_deltas(state, cross_session_totals=cross_totals, now=now)
    comparand = effort.comparands(state, thr)
    ratio = effort.effective_ratios(state, thr, cross_session_totals=cross_totals, now=now)
    scales = []
    for scale in effort.SCALE_ORDER:
        label, unit = effort.describe(scale)
        kind = "ratio" if scale in effort.RATIO_SCALES else "absolute"
        trigger = multiple if kind == "ratio" else 1.0
        observed = ratio[scale]
        record_only = scale in effort.RECORD_ONLY_SCALES
        scales.append({
            "scale": scale, "label": label, "unit": unit, "kind": kind,
            "actual": delta[scale], "comparand": comparand[scale], "ratio": observed,
            # Normalized "how far past its OWN line", so the four scales rank against
            # each other — the same footing effort.divergence puts them on.
            "past_own_trigger": (observed / trigger) if observed is not None else None,
            # RECORD_ONLY_SCALES never fire, so forced False here mirrors
            # effort.divergence's own skip of these scales — a watch that showed
            # at_or_past_threshold=True for a scale that can never diverge would read
            # as an alarm nothing acts on.
            "at_or_past_threshold": (not record_only) and observed is not None and observed >= trigger,
            # Whether the accumulator, not this session's own history, supplied the
            # number — so a reader of the line knows the count is the TASK's, not the
            # session's, without having to open the accumulator to find out.
            "cross_session": delta[scale] > local[scale],
            "record_only": record_only,
        })
    div = effort.divergence(state, thr, cross_session_totals=cross_totals, now=now)
    over = [s["scale"] for s in scales if s["at_or_past_threshold"]]
    # Separate from `over_threshold`: the true crossing state of a record-only scale,
    # reported for visibility but never folded into `over`/`would_fire` — those two
    # stay reserved for scales the divergence trigger can actually act on.
    record_only_over = [
        s["scale"] for s in scales
        if s["record_only"] and s["ratio"] is not None and s["ratio"] >= (
            multiple if s["kind"] == "ratio" else 1.0
        )
    ]
    detail = (
        f"effort divergence on {', '.join(over)}" if over
        else "no scale at or past its threshold"
    )
    return Directive(
        True, state.node, "inspect", detail,
        data={
            "armed": True,
            # Whether a fire site WOULD act on this: gates.effort_active is the same
            # predicate they consult, so a report that omitted it would read as an
            # alarm on a session where the trigger is switched off.
            "active": gates.effort_active(state),
            "over_threshold": over,
            "record_only_over_threshold": record_only_over,
            "would_fire": div.scale if div is not None else None,
            "framing": div.framing if div is not None else None,
            "scales": scales,
        },
    )


def register(sub, add):
    sp = add("replan"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default=None,
                    help="the corrected plan file; omitted only by a small-change session, "
                         "which holds no plan: it closes its difficulty and re-arms its "
                         "failed stage with no plan to correct")
    sp.add_argument("--reason", default=None,
                    help="free-text cause for this replan, captured for history only; used "
                         "only when no active difficulty record supplies one (a bare "
                         "refinement/no_change replan outside DIAGNOSING) — never required, "
                         "and its absence never refuses the command")
    sp.add_argument("--coverage-waiver", dest="coverage_waiver", default=None,
                    help="bypass a failing coverage gate with a recorded reason (refused if empty); "
                         "never bypasses the difficulty-record completeness precondition")
    sp.add_argument("--normalization-waiver", dest="normalization_waiver", default=None,
                    help="close a difficulty WITHOUT a re-norming record when the exposed factor "
                         "is genuinely one-off; a recorded reason (refused if empty)")
    sp.add_argument("--renormalize", action="store_true",
                    help="claim the corrected plan changes only stage `procedure` — the "
                         "SEQUENCE of operations, which is the executor's to replace; skips "
                         "the plan-review and plan_approval gates and is REFUSED the moment "
                         "the edit reaches a method, a criterion, a result image or the goal")
    sp.add_argument("--reopen", dest="reopen", action="append", type=int, default=None,
                    metavar="N",
                    help="re-arm PASSED stage N (repeatable) together with every PASSED stage "
                         "that depends on it, so the replan may edit what N was built from or "
                         "promises; without it such an edit to a PASSED stage is refused")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp.add_argument("--renegotiation-decision", dest="renegotiation_decision", default=None,
                    choices=["continue", "rescope", "abandon"],
                    help="clear the diagnosing_replan round-release ceiling (the "
                         "replan-count threshold out of DIAGNOSING): continue/rescope zero the cross-session "
                         "task accumulator and let this replan proceed; abandon parks the "
                         "session at BLOCKED without applying --plan. Requires "
                         "--renegotiated-by and --renegotiation-note")
    sp.add_argument("--renegotiated-by", dest="renegotiated_by", default=None,
                    help="who made the renegotiation decision; must match "
                         "[meta.order].customer_id when the plan declares one")
    sp.add_argument("--renegotiation-note", dest="renegotiation_note", default=None,
                    help="what the customer decided and why (refused if empty)")
    sp.add_argument("--normalize-factor", dest="normalize_factor", default=None,
                    help="record a normalize event in the SAME replan call, without a "
                         "separate `normalize` invocation -- the reproducible cause being "
                         "re-normed. Optional; its absence never refuses the replan (a "
                         "non-blocking hint names the flag when it's missing)")
    sp.add_argument("--normalize-level", dest="normalize_level", default=None,
                    choices=list(NORMALIZATION_LEVELS),
                    help="paired with --normalize-factor; recording level (payoff-gated), "
                         "omit for an in-head note below the leaf threshold")
    sp = add("fire-acknowledge"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", required=True, help="who decided — a name, not a narrative")
    sp.add_argument("--decision", required=True, choices=["continue", "abandon", "revise"],
                    help="continue: accept the overrun, keep executing; abandon: park the "
                         "session at BLOCKED (via blocked_from, same as cmd_block) with "
                         "reason 'user-abandoned-after-fire' — never RESOLVED, since a "
                         "mid-execution fire routinely leaves stages PENDING and "
                         "check_invariants refuses RESOLVED unless every stage PASSED; "
                         "revise: route to the ordinary DIAGNOSING replan cycle")
    sp.add_argument("--note", default=None)
    sp = add("check-coverage"); sp.add_argument("--session", required=True)
    sp.add_argument("--new", required=True,
                    help="corrected plan file to check against the active critique, "
                         "BEFORE spending a thinker plan-review on it")
    sp = add("effort-check"); sp.add_argument("--session", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="cost log file path (test override)")
