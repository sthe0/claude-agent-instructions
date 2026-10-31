"""Session control subcommands: block, status, children, drive, close and the sub-plan stack.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from . import effort, gates
from .directive import Directive
from .dispatch import Runner
from .machine import transition
from .plan import PlanError, load_plan
from .state import (
    _EXECUTION_NODES,
    AGENT_ACTOR,
    CriterionType,
    is_skipped,
    GateRecord,
    Node,
    PlanFrame,
    StageStatus,
    WeightClass,
)
from .store import StateStore
from .cli_common import (
    _VALID_QUALITY_RATINGS,
    OBSERVATION_CONTRACT,
    _ledgered_order_key,
    _flush_order_effort,
    _archive_difficulty,
    _autonomy_directive_data,
    _sync_venue_from_plan,
    _plan_venue_pair,
    _restore_current_stage,
    _require,
    _replan_class_data,
    _finished_work_data,
    _park_node,
    _enumeration_escape_counts,
    _verify_child_escalation,
    _open_norm_mismatch,
    RESOLUTION_ASK_BLOCKER_PREFIX,
    _parent_link_summary,
    _load_or_none,
    _linked_children,
    _escalation_rows,
    _run_step,
)
from .cli_plan import cmd_classify, cmd_plan, cmd_submit_plan
from .cli_approval import cmd_approve, cmd_partition, cmd_next_stage
from .cli_results import cmd_record_result
from .cli_final import cmd_verify_final, cmd_resolve


def cmd_block(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node in (Node.RESOLVED.value, Node.BLOCKED.value):
        return Directive(False, state.node, "noop", f"cannot block from node={state.node}")
    _park_node(state)
    state.log("block", reason=getattr(args, "reason", None))
    store.save(state)
    return Directive(True, state.node, "unblock", "blocked; resolve the blocker then unblock", marker="ESCALATE")


def cmd_unblock(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node != Node.BLOCKED.value or not state.blocked_from:
        return Directive(False, state.node, "noop", "not blocked")
    state.node = state.blocked_from
    state.blocked_from = None
    state.log("unblock")
    store.save(state)
    return Directive(True, state.node, "continue", "unblocked; resume from prior node")


def cmd_status(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = store.load(args.session) if getattr(args, "session", None) else None
    if state is None:
        return Directive(True, "(none)", "start", "no session state; run start")
    parent = (state.parent_link or {}).get("parent_session")
    escalations = _escalation_rows(state, parent, _load_or_none(store, parent)) if parent else []
    return Directive(
        True, state.node, "inspect",
        f"{state.task_id}: {state.weight_class}/{state.route}"
        + "".join(f"; escalation {e['child_uid']}={e['status']}" for e in escalations),
        data={
            "escalations": escalations,
            "weight_class": state.weight_class,
            "route": state.route,
            "current_stage": state.current_stage,
            "stages": [{"index": s.index, "status": s.outcome.status, "title": s.title} for s in state.stages],
            "approval_passed": state.approval.passed,
            "resolution_passed": state.resolution.passed,
            # LEGACY field (the retired enumeration gate's escape history). None when
            # the premise plugin is not armed; `this_plan` None when no plan is
            # submitted — see _enumeration_escape_counts on why neither is reported
            # as a zero.
            "enumeration_escapes": _enumeration_escape_counts(state),
            "parent_link": _parent_link_summary(state.parent_link),
            **_replan_class_data(state),
            "effort_retiers": list(state.effort_retiers),
            "effort_finished_work": _finished_work_data(state).get("effort_finished_work", []),
        },
    )


def cmd_adopt_escalation(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Open this session's counterpart of a linked child's parent-norm escalation that its
    dispatch never saw (the child's REPLAN reached no dispatch, or reached one that could
    not verify it). The decision rests on the child's state record, read and never written,
    by the same checks as the dispatch route (`_verify_child_escalation`); a record that
    does not verify is refused with the reason. An escalation this session already holds a
    difficulty for is not opened a second time."""
    state = _require(store, args.session)
    if state.node not in (Node.EXECUTING.value, Node.VERIFYING.value):
        return Directive(False, state.node, "noop",
                         f"adopt-escalation runs from EXECUTING or VERIFYING; node is {state.node}")
    child_id = args.child
    child = _load_or_none(store, child_id) if child_id != state.session_id else None
    if child is None or not child.parent_link:
        return Directive(False, state.node, "noop",
                         f"adopt-escalation refused: {child_id} is not a readable session "
                         "linked to a parent stage")
    stage_index = child.parent_link.get("stage_index")
    try:
        stage = state.stage(stage_index)
    except KeyError:
        return Directive(False, state.node, "noop",
                         f"adopt-escalation refused: child {child_id} is linked to stage "
                         f"{stage_index!r}, which this session's plan does not hold")
    if state.node == Node.EXECUTING.value and stage.index != state.current_stage:
        return Directive(False, state.node, "noop",
                         f"adopt-escalation refused: child {child_id} is linked to stage "
                         f"{stage.index}, but the active stage is {state.current_stage}; failing "
                         "the earlier stage from EXECUTING would strand the active one -- "
                         "adopt it from VERIFYING")
    mismatch, reason = _verify_child_escalation(state, stage, child_id, child)
    if mismatch is None:
        return Directive(False, state.node, "noop", f"adopt-escalation refused: {reason}")
    standing = gates.escalation_counterpart(state, mismatch["child_uid"], child_id)
    if standing is not None:
        return Directive(True, state.node, "noop",
                         f"child {child_id}'s difficulty {mismatch['child_uid']} is already "
                         f"adopted by a {standing} difficulty of this session")
    return _open_norm_mismatch(state, store, stage, mismatch, {}, via="adopt-escalation")


def cmd_children(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The sessions the engine linked as children of `--session`: every stored session
    whose `parent_link.parent_session` names it. Read-only -- a state file that cannot be
    loaded is skipped, never repaired or rewritten."""
    parent = args.session
    parent_state = _load_or_none(store, parent)
    children: list[dict] = []
    for child in _linked_children(store, parent):
        children.append({
            "session": child.session_id,
            "stage_index": child.parent_link.get("stage_index"),
            "node": child.node,
            "recursion_depth": child.recursion_depth,
            "difficulty_level": child.difficulty.level if child.difficulty is not None else None,
            "resolved": child.node == Node.RESOLVED.value,
            "escalations": _escalation_rows(child, parent, parent_state),
        })
    data = {"parent_session": parent, "children": children}
    if getattr(args, "json", False):
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        lines = [
            f"{c['session']} stage={c['stage_index']} node={c['node']} depth={c['recursion_depth']} "
            f"difficulty={c['difficulty_level'] if c['difficulty_level'] is not None else 'none'}"
            f"{' resolved' if c['resolved'] else ''}"
            + "".join(f" escalation {e['child_uid']}={e['status']}" for e in c["escalations"])
            for c in children
        ]
        text = "\n".join(lines) if lines else f"no children linked to {parent}"
    return Directive(True, "(none)", "inspect", text, data=data)


def cmd_push_subplan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Start a service sub-plan: snapshot the parent into plan_stack, then reset
    the live state to a fresh child CLASSIFIED cycle. The child runs its normal
    classify->...->resolve spine; pop-subplan restores the parent on resolution."""
    state = _require(store, args.session)
    if state.node != Node.EXECUTING.value:
        return Directive(
            False, state.node, "noop",
            f"push-subplan requires node=EXECUTING; current node={state.node}",
        )
    originating = int(getattr(args, "originating_stage", None) or state.current_stage or 0)
    if not originating:
        return Directive(False, state.node, "noop", "cannot determine originating stage; pass --originating-stage")
    try:
        declined_origin = is_skipped(state.stage(originating))
    except KeyError:
        declined_origin = False
    if declined_origin:
        return Directive(
            False, state.node, "noop",
            f"push-subplan: originating stage {originating} is SKIPPED; a declined optional "
            "stage is never run, so it cannot spawn a sub-plan",
        )
    child_plan = args.plan
    child_task = getattr(args, "task", None) or f"sub:{Path(child_plan).stem}"

    # Snapshot the venue pair the PARENT plan file declares right now, at push time —
    # the comparand cmd_pop_subplan's venue-substitution guard checks the file against
    # later. A read failure here (unreadable/malformed parent) leaves the pair
    # uncaptured, which is exactly the signal that tells pop "nothing to compare;
    # re-derive as always".
    parent_pair = _plan_venue_pair(state.plan_path)

    # The child's plan may name another order: book what the parent has spent so far to the
    # parent's order window before the live plan changes, so only the child's own spend and
    # minutes reach the child's.
    _flush_order_effort(state, store=store)

    frame = PlanFrame(
        plan_path=state.plan_path,
        node=state.node,
        task_id=state.task_id,
        goal=state.goal,
        overall_done_criterion=state.overall_done_criterion,
        overall_criterion_type=state.overall_criterion_type,
        weight_class=state.weight_class,
        route=state.route,
        repo_root=state.repo_root,
        delivery_worktree=state.delivery_worktree,
        final_check=list(state.final_check),
        partition=state.partition,
        approval=state.approval,
        resolution=state.resolution,
        stages=list(state.stages),
        current_stage=state.current_stage,
        originating_stage=originating,
        effort_estimate=state.effort_estimate,
        effort_baseline=state.effort_baseline,
        effort_actuals=dict(state.effort_actuals),
        effort_fires=list(state.effort_fires),
        effort_spend_seen=dict(state.effort_spend_seen),
        plan_review_rounds=state.plan_review_rounds,
        plan_review_counted_digest=state.plan_review_counted_digest,
        code_review_rounds=state.code_review_rounds,
        landing_waiver=state.landing_waiver,
        parent_repo_root=parent_pair[0] if parent_pair is not None else "",
        parent_delivery_worktree=parent_pair[1] if parent_pair is not None else "",
        parent_venue_captured=parent_pair is not None,
        plugins=dict(state.plugins),
        plugins_archive=dict(state.plugins_archive),
        plan_snapshot_path=state.plan_snapshot_path,
        plan_snapshot_hash=state.plan_snapshot_hash,
        approved_grants_sha256=state.approved_grants_sha256,
        approved_grant_entries=copy.deepcopy(state.approved_grant_entries),
        accepted_plan_digest=state.accepted_plan_digest,
        plan_binding_captured=True,
        runtime_grants=copy.deepcopy(state.runtime_grants),
    )
    state.plan_stack.append(frame)
    # The approval-binding fields copied into the frame stay live on purpose: the order key
    # (`_replan_baseline_path`) and the pre-approval gates read the parent's snapshot until the
    # child's own approve rebinds them.
    # Reset to a fresh child cycle — the child re-classifies and plans normally.
    state.node = transition(Node.EXECUTING.value, "push_subplan")  # EXECUTING -> CLASSIFIED
    state.task_id = child_task
    state.plan_path = child_plan
    state.plan_verified = False
    state.goal = ""
    state.overall_done_criterion = ""
    state.overall_criterion_type = CriterionType.MEASURABLE.value
    state.weight_class = None
    state.route = None
    # Deliberately NOT _sync_venue_from_plan: the child's venue is unknown until
    # its own submit_plan reads it, and the null window cannot be observed —
    # this same reset empties state.stages and moves EXECUTING -> CLASSIFIED, so
    # no stage exists to dispatch until submit_plan refills both fields. The
    # window is closed by the state machine, not by a convention that would
    # silently stop holding if dispatch ever became reachable from CLASSIFIED.
    state.repo_root = None
    state.delivery_worktree = None
    state.landing_waiver = None
    state.final_check = []
    state.partition = None
    state.approval = GateRecord("plan_approval")
    state.resolution = GateRecord("resolution")
    state.stages = []
    state.current_stage = None
    _archive_difficulty(state)
    state.permission_request = None
    state.blocked_from = None
    # Effort-divergence custody (schema 25, effort.py's SUB-PLAN CUSTODY): the frame above
    # already snapshotted the parent's five fields, so the child starts an unarmed window
    # of its own — user_prompt_count is deliberately left alone, it lives outside effort_actuals.
    state.effort_estimate = None
    state.effort_baseline = None
    state.effort_actuals = {}
    state.effort_fires = []
    state.effort_spend_seen = {}
    # Review-round custody (schema 30), on the same reasoning as the effort block above:
    # the frame holds the parent's pair, so the child argues about its own plan on its own
    # budget and cannot spend — or be charged for — the parent's rounds.
    state.plan_review_rounds = 0
    state.plan_review_counted_digest = ""
    # Code-review round custody (item A, schema 33) — same reasoning, same frame.
    state.code_review_rounds = 0
    # Plugin custody (schema 35), same reasoning as the review-round block above: the
    # frame holds the parent's plugins/plugins_archive, so the child's own classify
    # auto-activates every plugin fresh, uncontaminated by parent bag content or a
    # parent-left archive-suppression entry.
    state.plugins = {}
    state.plugins_archive = {}
    # Runtime grants are keyed by the live plan's stage index: the child starts with none and
    # pop puts the parent's back.
    state.runtime_grants = {}
    state.log("push_subplan", child_plan=child_plan, originating_stage=originating, depth=len(state.plan_stack))
    store.save(state)
    return Directive(
        True, state.node, "classify",
        f"sub-plan pushed (depth={len(state.plan_stack)}); child at CLASSIFIED — run classify next",
        data={"child_plan": child_plan, "originating_stage": originating,
              "parent_task": frame.task_id, "stack_depth": len(state.plan_stack)},
    )


def cmd_pop_subplan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Restore the parent after a service sub-plan resolves. Requires node=RESOLVED;
    the RESOLVED-frm structural guarantee enforces 'no auto-pop across an unresolved
    child' — check_invariants already mandates resolution.passed at RESOLVED."""
    state = _require(store, args.session)
    if not state.plan_stack:
        return Directive(False, state.node, "noop", "plan_stack is empty; nothing to pop")
    if state.node != Node.RESOLVED.value:
        return Directive(
            False, state.node, "noop",
            f"pop-subplan requires node=RESOLVED (child must fully resolve first); current node={state.node}",
        )
    child_task_id = state.task_id
    # The mirror of push's flush: the child's plan may name another order, so what it spent
    # since its last flush is booked to its own window before the parent's plan is restored.
    _flush_order_effort(state, store=store)
    new_node = transition(state.node, "pop_subplan")  # RESOLVED -> EXECUTING
    frame = state.plan_stack.pop()
    # Restore all parent plan-level fields from the frame.
    state.plan_path = frame.plan_path
    state.task_id = frame.task_id
    state.goal = frame.goal
    state.overall_done_criterion = frame.overall_done_criterion
    state.overall_criterion_type = frame.overall_criterion_type
    state.weight_class = frame.weight_class
    state.route = frame.route
    state.repo_root = frame.repo_root
    state.delivery_worktree = frame.delivery_worktree
    state.landing_waiver = frame.landing_waiver
    state.final_check = frame.final_check
    state.partition = frame.partition
    state.approval = frame.approval
    state.resolution = frame.resolution
    state.stages = frame.stages
    # Effort-divergence custody (schema 25, effort.py's SUB-PLAN CUSTODY): estimate,
    # baseline, fires and spend_seen are restored straight from the frame, but
    # effort_actuals is ADDED — push zeroed it, so state.effort_actuals here is pure
    # child consumption and belongs on top of the parent's, not in place of it.
    state.effort_actuals = effort.merge_actuals(frame.effort_actuals, state.effort_actuals)
    state.effort_estimate = frame.effort_estimate
    state.effort_baseline = frame.effort_baseline
    state.effort_fires = frame.effort_fires
    state.effort_spend_seen = frame.effort_spend_seen
    # Review-round custody (schema 30). Restored, NOT merged like effort_actuals: rounds
    # are argument about a particular plan, and the child's rounds were spent arguing
    # about the child's plan, which no longer exists once the parent resumes.
    state.plan_review_rounds = frame.plan_review_rounds
    state.plan_review_counted_digest = frame.plan_review_counted_digest
    # Code-review round custody (item A, schema 33) — same restore-not-merge reasoning.
    state.code_review_rounds = frame.code_review_rounds
    # Plugin custody (schema 35) — restored, NOT merged, same reasoning: a plugin bag
    # argues about a particular plan, and the child's bags belonged to the child's plan,
    # which no longer exists once the parent resumes.
    state.plugins = frame.plugins
    state.plugins_archive = frame.plugins_archive
    if frame.plan_binding_captured:
        # Approval-binding custody (schema 56): the child's own approval rebound these to the
        # child's plan; the parent resumes with the binding it was approved under.
        state.plan_snapshot_path = frame.plan_snapshot_path
        state.plan_snapshot_hash = frame.plan_snapshot_hash
        state.approved_grants_sha256 = frame.approved_grants_sha256
        state.approved_grant_entries = frame.approved_grant_entries
        state.accepted_plan_digest = frame.accepted_plan_digest
        if frame.runtime_grants is not None:
            state.runtime_grants = frame.runtime_grants
    state.node = new_node
    # The parent PLAN FILE is authoritative for the venue, so re-derive it here
    # rather than trust the frame: a frame captured after the value was already
    # lost would keep it lost. The frame fields stay as the fallback the helper
    # leaves in place when that file cannot be read.
    #
    # BUT: re-deriving unconditionally trusts the file even when it moved out from
    # under the pushed child — the one other post-approval route from an edited plan
    # FILE to live state. So re-derive only when there is nothing to compare against
    # (a legacy frame, or the parent was unreadable at push); when the parent's venue
    # pair was captured, compare it against a fresh read now and keep the frame's
    # (already-restored, two lines up) venue if either field moved. A missing venue
    # kept is a lesser-of-two-evils choice, not a clean stop: it is never refused, it
    # just runs in whatever cwd is ambient, which is safer than silently adopting a
    # venue nobody approved at plan_approval time.
    venue_source = "plan-file"
    if frame.parent_venue_captured:
        current_pair = _plan_venue_pair(state.plan_path)
        venue_substituted = (
            current_pair is not None
            and current_pair != (frame.parent_repo_root, frame.parent_delivery_worktree)
        )
        if venue_substituted:
            venue_source = "frame (parent plan venue changed while pushed)"
        else:
            _sync_venue_from_plan(state)
    else:
        _sync_venue_from_plan(state)
    # Mark the originating stage as satisfied, THEN derive the active-stage
    # pointer from stage status — order is load-bearing: deriving first would
    # re-point at a stage this same call is about to mark PASSED.
    try:
        orig = state.stage(frame.originating_stage)
        if not is_skipped(orig):
            orig.outcome.status = StageStatus.PASSED.value
            orig.control = f"satisfied by sub-plan {child_task_id}"
    except KeyError:
        pass
    _restore_current_stage(state)
    state.log("pop_subplan", child_task_id=child_task_id, originating_stage=frame.originating_stage,
              depth=len(state.plan_stack), venue_source=venue_source)
    store.save(state)
    return Directive(
        True, state.node, "next_stage",
        f"sub-plan {child_task_id!r} resolved; parent restored at EXECUTING; "
        f"stage {frame.originating_stage} satisfied — run next-stage to continue "
        f"(venue: {venue_source})",
        data={"originating_stage": frame.originating_stage, "child_task_id": child_task_id,
              "stack_depth": len(state.plan_stack), "venue_source": venue_source},
    )


def cmd_drive(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Walk the OPENING spine from the session's current node, firing only legal
    forward edges, and STOP at the plan-approval gate (PLAN_READY) by default.
    Cross it only when given --approved-by <who>, threaded into cmd_approve --by:
    the flag does not make approval implicit — it is the human token authorizing the
    wrapper to collapse the post-approval ceremony (approve -> partition -> next-stage
    -> EXECUTING) into the same call. Idempotent: re-running at/after EXECUTING is a
    no-op that reports the node."""
    state = _require(store, args.session)
    trace: list = []
    node = state.node

    # idempotency / guard: opening spine is done (or the session is parked elsewhere)
    if node in _EXECUTION_NODES:
        return Directive(True, node, "noop",
                         f"drive: session already at {node}; opening spine complete",
                         data={"trace": trace})
    if node in (Node.BLOCKED.value, Node.DIAGNOSING.value):
        return Directive(False, node, "noop",
                         f"drive: session at {node}; resolve that before driving",
                         data={"trace": trace})

    # --- classify (at CLASSIFIED) ---
    if node == Node.CLASSIFIED.value:
        d = _run_step(cmd_classify, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(d.ok, d.node, d.action, f"drive: classify failed: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node

    state = _require(store, args.session)
    wc = state.weight_class

    # --- route on weight class (at ROUTED) ---
    if node == Node.ROUTED.value:
        if wc == WeightClass.CHAT.value:
            return Directive(True, node, "answer_in_thread",
                             "drive: chat — answer in-thread (terminal at ROUTED)",
                             data={"trace": trace})
        if wc == WeightClass.SMALL_CHANGE.value:
            d = _run_step(cmd_next_stage, args, store=store, runner=runner, trace=trace)
            return Directive(d.ok, d.node, d.action,
                             f"drive: small change to EXECUTING — {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        # substantive: plan -> submit_plan
        d = _run_step(cmd_plan, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(d.ok, d.node, d.action, f"drive: plan failed: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node

    # --- submit plan (at PLANNING) ---
    if node == Node.PLANNING.value:
        if not getattr(args, "plan", None):
            return Directive(False, node, "fix_plan",
                             "drive: at PLANNING but no --plan provided",
                             data={"trace": trace})
        d = _run_step(cmd_submit_plan, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action,
                             f"drive: plan failed verification: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- the plan-approval GATE-STOP (at PLAN_READY) ---
    if node == Node.PLAN_READY.value:
        approver = getattr(args, "approved_by", None)
        autonomy = None
        if not (approver and approver.strip()) and state.plan_path:
            try:
                drive_doc = load_plan(state.plan_path)
            except (OSError, PlanError):
                drive_doc = None
            if drive_doc is not None and _ledgered_order_key(state) is not None:
                autonomy = _autonomy_directive_data(state, drive_doc)
                if autonomy["action"] == "self_approve":
                    approver = AGENT_ACTOR
        if not (approver and approver.strip()):
            return Directive(True, node, "await_user_approval",
                             "drive: plan ready — HARD GATE; get explicit user approval, then "
                             "re-run drive with --approved-by <who>",
                             marker="PLAN-READY",
                             data={"trace": trace, **({"autonomy": autonomy} if autonomy else {})})
        ap = argparse.Namespace(session=args.session, by=approver)
        d = _run_step(cmd_approve, ap, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"drive: approve failed: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- partition (at APPROVED) ---
    if node == Node.APPROVED.value:
        pa = argparse.Namespace(
            session=args.session,
            m1=getattr(args, "m1", False), m2=getattr(args, "m2", False),
            m3=getattr(args, "m3", False), m4=getattr(args, "m4", False),
            m3_severe=getattr(args, "m3_severe", False),
            m4_severe=getattr(args, "m4_severe", False),
        )
        d = _run_step(cmd_partition, pa, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"drive: partition failed: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node
        if d.action == "surface_partition":
            # a split is suggested — STOP for the user; do not auto-advance (not a gate,
            # but the M1–M4 verdict is cognition the wrapper must not paper over)
            return Directive(True, node, "surface_partition",
                             f"drive: {d.detail}",
                             data={**d.data, "trace": trace})

    # --- enter the first stage (at PARTITIONED) ---
    if node == Node.PARTITIONED.value:
        d = _run_step(cmd_next_stage, args, store=store, runner=runner, trace=trace)
        return Directive(d.ok, d.node, d.action,
                         f"drive: first stage active — {d.detail}",
                         marker=d.marker, data={**d.data, "trace": trace})

    return Directive(True, node, "inspect", f"drive: stopped at {node}", data={"trace": trace})


def cmd_close(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Walk the CLOSING spine and STOP at the resolution gate. record-result for the
    active stage (only with an explicit --status; a failed result routes to DIAGNOSING
    and is surfaced, never swallowed) -> verify-final (when all stages passed) -> a
    read-only PROBE of cmd_resolve. With no --confirmed-by the probe leaves the session
    at RESOLUTION and close reports what still blocks resolve (core + experience-plugin-
    phase blockers); with --confirmed-by it resolves. Idempotent at RESOLVED.

    Note: plugin OBSERVER nudges (PluginDirectives, e.g. the experience plugin's
    record_experience nudge) are not emitted through this path — they require the
    main()/_fire_plugins wiring. Resolution gates still hold, because cmd_resolve reads
    plugin_gate_blockers directly; only the advisory nudge directives are silent here."""
    state = _require(store, args.session)
    trace: list = []
    node = state.node

    if node == Node.RESOLVED.value:
        return Directive(True, node, "noop", "close: already RESOLVED", data={"trace": trace})
    if node not in (Node.EXECUTING.value, Node.VERIFYING.value, Node.RESOLUTION.value):
        return Directive(False, node, "noop",
                         f"close: nothing to close yet (node={node}); drive to EXECUTING first",
                         data={"trace": trace})

    # --- record the active stage's result (only with an explicit status) ---
    if node == Node.EXECUTING.value:
        status = getattr(args, "status", None)
        if not status:
            return Directive(False, node, "record_result",
                             "close: stage is EXECUTING — supply --status passed|failed "
                             "(and --control for a spawn:developer stage)",
                             data={"trace": trace})
        rr = argparse.Namespace(
            session=args.session, status=status,
            actual=getattr(args, "actual", "") or "",
            control=getattr(args, "control", None),
            observation=getattr(args, "observation", "") or "",
        )
        d = _run_step(cmd_record_result, rr, store=store, runner=runner, trace=trace)
        if not d.ok:
            # failed result -> DIAGNOSING (overcome-difficulty), or attest_control needed
            return Directive(False, d.node, d.action, f"close: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node
        if d.action == "next_stage":
            return Directive(True, node, "next_stage",
                             "close: stage recorded; more stages remain — execute them, then "
                             "close again (close does not auto-run remaining stages)",
                             data={"trace": trace})

    # --- final verification (at VERIFYING, all stages passed) ---
    if node == Node.VERIFYING.value:
        d = _run_step(cmd_verify_final, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"close: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- resolution GATE-STOP: probe cmd_resolve (constraints 1 + 3) ---
    if node == Node.RESOLUTION.value:
        confirmer = getattr(args, "confirmed_by", None)
        rs = argparse.Namespace(session=args.session, by=(confirmer or ""),
                                quality=getattr(args, "quality", None),
                                quality_by=getattr(args, "quality_by", None),
                                quality_note=getattr(args, "quality_note", None),
                                landing_waiver=getattr(args, "landing_waiver", None),
                                resolution_ask_unverifiable=getattr(
                                    args, "resolution_ask_unverifiable", None))
        d = _run_step(cmd_resolve, rs, store=store, runner=runner, trace=trace)
        if d.ok:
            return Directive(True, d.node, d.action, "close: task resolved",
                             marker=d.marker, data={"trace": trace})
        # blocked: separate the gate-stop sentinels (confirmer + rating, both
        # supplied by the confirmed re-run itself; without a confirmer the resolution
        # ask cannot have happened yet either) from real blockers
        blockers = d.data.get("blockers", [])
        gate_stop = ("empty confirmer", "missing --quality")
        real = [b for b in blockers
                if not any(s in b for s in gate_stop)
                and not (not confirmer and b.startswith(RESOLUTION_ASK_BLOCKER_PREFIX))]
        if real:
            detail = ("close: confirmer given but resolution still blocked"
                      if confirmer and confirmer.strip() else "close: resolution blocked")
            return Directive(False, node, "fix_stages", detail,
                             data={"blockers": real, "trace": trace})
        return Directive(True, node, "await_user_confirmation",
                         "close: ready to resolve — get explicit user confirmation, then "
                         "re-run close with --confirmed-by <who> --quality <1-5>",
                         data={"trace": trace})

    return Directive(True, node, "inspect", f"close: stopped at {node}", data={"trace": trace})


def register(sub, add):
    sp = add("block"); sp.add_argument("--session", required=True); sp.add_argument("--reason", default="")
    sp = add("unblock"); sp.add_argument("--session", required=True)
    sp = add("children"); sp.add_argument("--session", required=True)
    sp.add_argument("--json", action="store_true")
    sp = add("adopt-escalation"); sp.add_argument("--session", required=True)
    sp.add_argument("--child", required=True,
                    help="session id of the linked child whose parent-norm escalation to adopt")
    sp = add("status"); sp.add_argument("--session", required=False)

    # drive: opening-spine orchestrator — union of classify signals + --plan +
    # --approved-by (the gate-cross token) + the M1–M4 partition markers.
    sp = add("drive"); sp.add_argument("--session", required=True)
    sp.add_argument("--chat", action="store_true")
    sp.add_argument("--changed-lines", dest="changed_lines", type=int, default=0,
                    help="hand-written production lines of the change (tests, generated files and "
                         "memory leaves excluded); measure with scripts/inline-measure.py")
    sp.add_argument("--not-cohesive", dest="not_cohesive", action="store_true",
                    help="the change is not one cohesive change (a module and its tests)")
    sp.add_argument("--wall-clock-min", dest="wall_clock_min", type=int, default=0)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp.add_argument("--architectural", action="store_true",
                    help="a design decision (more than one reasonable solution) or an "
                         "architectural change")
    sp.add_argument("--external-effect", dest="external_effect", action="store_true")
    sp.add_argument("--new-dependency", dest="new_dependency", action="store_true")
    sp.add_argument("--public-api-change", dest="public_api_change", action="store_true")
    sp.add_argument("--deliverable-kind", dest="deliverable_kind", default="",
                    choices=["", "reasoning", "code", "ops", "mixed"],
                    help="what kind of artifact this task produces; 'reasoning'/'mixed' "
                         "arms the claim-provenance ledger plugin on a SUBSTANTIVE session")
    sp.add_argument("--plan", default=None)
    sp.add_argument("--approved-by", dest="approved_by", default=None,
                    help="human token authorizing the wrapper to cross the plan-approval "
                         "gate; pass ONLY after a real user-approval round")
    sp.add_argument("--m1", action="store_true"); sp.add_argument("--m2", action="store_true")
    sp.add_argument("--m3", action="store_true"); sp.add_argument("--m4", action="store_true")
    sp.add_argument("--m3-severe", dest="m3_severe", action="store_true")
    sp.add_argument("--m4-severe", dest="m4_severe", action="store_true")

    # close: closing-spine orchestrator — record-result inputs + --confirmed-by
    # (the resolution-gate-cross token).
    sp = add("close"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], default=None)
    sp.add_argument("--actual", default="")
    sp.add_argument("--control", default=None)
    sp.add_argument("--observation", default="",
                    help=f"{OBSERVATION_CONTRACT} (threaded to record-result; see "
                         "record-result --observation)")
    sp.add_argument("--confirmed-by", dest="confirmed_by", default=None,
                    help="human token authorizing the wrapper to cross the resolution "
                         "gate; pass ONLY after explicit user confirmation")
    sp.add_argument("--quality", type=int, choices=list(_VALID_QUALITY_RATINGS), default=None,
                    help="1-5 rating threaded to resolve (see resolve --quality)")
    sp.add_argument("--quality-by", dest="quality_by", default="user-confirmed")
    sp.add_argument("--quality-note", dest="quality_note", default=None)
    sp.add_argument("--landing-waiver", dest="landing_waiver", default=None,
                    help="threaded to resolve (see resolve --landing-waiver)")
    sp.add_argument("--resolution-ask-unverifiable", dest="resolution_ask_unverifiable",
                    default=None,
                    help="threaded to resolve (see resolve --resolution-ask-unverifiable)")

    sp = add("push-subplan"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True, help="path to the child service sub-plan TOML")
    sp.add_argument("--task", default=None, help="task_id for the child (defaults to sub:<plan-stem>)")
    sp.add_argument("--originating-stage", dest="originating_stage", type=int, default=None,
                    help="parent stage whose missing element the sub-plan supplies "
                         "(defaults to state.current_stage)")

    sp = add("pop-subplan"); sp.add_argument("--session", required=True)
