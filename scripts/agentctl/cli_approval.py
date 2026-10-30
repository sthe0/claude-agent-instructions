"""Approval, partition and stage-advance subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import datetime as dt
import os
from . import (
    advisor,
    effort,
    gates,
    mandate as mandate_rules,
    order_approvals,
    plan_resources,
    plugins,
    resources as _resources,
    task_accumulator,
)
from .partition import render_section, render_units, verdict
from .directive import Directive
from .dispatch import Runner
from .machine import transition
from .plan import PlanError, load_plan_with_digest, materialized_grant_entries, order_digest
from .state import AGENT_ACTOR, is_skipped, Partition, GateRecord, Node, StageStatus
from .store import StateStore
from .cli_common import (
    _write_plan_snapshot,
    _snapshot_approved_plan,
    _bind_approved_grants,
    _ORDER_EFFORT_SCALES,
    _ledgered_order_key,
    _order_actual,
    _flush_order_effort,
    _autonomy_for,
    _baseline_plan_for_tier,
    _stamp_accepted_plan_digest,
    _refresh_caches_from_plan_path,
    _log_gate,
    _with_advisories,
    _require,
    _clear_since_user_decision,
    _enumeration_escape_counts,
    _note_round_release,
    _review_cycle_close,
    _evaluate_review_trigger,
    _skip_optional_problems,
    _stranded_dependency_problems,
    _mandate_basis_problems,
    _parse_partition_units,
    _rekey_runtime_grants,
    _utcnow,
    _effort_now,
)


def cmd_approve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    # plan_presentation_blockers is fail-open on the RECEIPT side (mirrors
    # plan_review_blockers) but fail-CLOSED on the DELIVERY side: approval — the
    # irreversible act — cannot be recorded without proof the plan actually
    # reached the user, even if no live-turn ask ever fired. Admissible only
    # because confirm-delivery is a reachable, audit-logged escape (gates.py's
    # plan_presentation_blockers docstring has the full justification).
    state = _require(store, args.session)
    # Node guard: approve is only legal from PLAN_READY. Return a legible directive for
    # other nodes so the caller gets an actionable message instead of an uncaught
    # TransitionError bubbling from the transition table (#339).
    _POST_APPROVAL_NODES: frozenset[str] = frozenset({
        Node.APPROVED.value, Node.PARTITIONED.value, Node.EXECUTING.value,
        Node.VERIFYING.value, Node.RESOLUTION.value, Node.RESOLVED.value,
        Node.DIAGNOSING.value,
    })
    if state.node in _POST_APPROVAL_NODES:
        return Directive(
            True, state.node, "no_action",
            f"plan is already approved (node={state.node}); nothing needs approving here",
        )
    if state.node != Node.PLAN_READY.value:
        return Directive(
            False, state.node, "submit_plan",
            f"cannot approve at node={state.node}; submit a plan first",
        )
    # Submission seam (c), BEFORE _log_gate: the coordinator may have edited plan_path in
    # place at plan-mutable PLAN_READY, so the bytes approve is about to attest to have
    # never been through submission validation. Refusing here — as a fix_plan Directive,
    # never a raised PlanError — keeps the gate row out of the log entirely rather than
    # writing a failed plan_approval for a plan that was never really put to the gate; the
    # coordinator fixes the file and re-runs approve from the same node.
    #
    # The whole REFRESH, not only its refusal, now precedes the gate, and that widening is
    # the contract rather than an accident of placement: every plan_approval blocker below
    # — core, plugin, review, presentation — is evaluated against the POST-refresh session,
    # so an in-place PLAN_READY edit can flip a blocker's verdict within a single approve
    # call. That is the intent (the gate must judge the bytes it is about to attest to, not
    # the pre-edit cache); a blocker that must instead see the cache as submitted has no
    # place on this gate. `test_plan_approval_blockers_see_the_refreshed_state` asserts it.
    echo_advice: list[str] = []
    # Entry-point fallback — see cmd_submit_plan's identical comment. cmd_approve is the
    # entry point (_refresh_caches_from_plan_path is a private single-caller helper), so
    # the runner is resolved here and threaded down.
    run = runner if runner is not None else advisor.subprocess_runner
    submission = _refresh_caches_from_plan_path(
        state, runner=run, advice=echo_advice,
        baseline=_baseline_plan_for_tier(store, state))
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "cannot approve: the plan at plan_path does not meet submission "
                         "requirements (edit it and re-run approve)",
                         data={"problems": submission})
    # Read plan_path's bytes ONCE and derive `_approved_doc`, the digest stamped
    # below (seam (c)) and the snapshot written near the end of this function from
    # that SAME buffer -- three separate reads of the same file (`load_plan`, then
    # `_stamp_accepted_plan_digest`'s own read, then `_snapshot_approved_plan`'s own
    # read) used to leave a window for a concurrent edit to desynchronize the
    # digest, the snapshot and the doc this approval actually validated.
    _approved_doc = None
    _approved_data: bytes | None = None
    _approved_digest: str | None = None
    if state.plan_path:
        try:
            _approved_doc, _approved_data, _approved_digest = load_plan_with_digest(state.plan_path)
        except (OSError, PlanError):
            _approved_doc = None
    _approved_entries = materialized_grant_entries(_approved_doc) if _approved_doc is not None else None
    skip_optional = sorted(set(getattr(args, "skip_optional", None) or []))
    by_agent = bool(args.by and args.by.strip().casefold() == AGENT_ACTOR)
    if skip_optional:
        skip_problems = _skip_optional_problems(
            skip_optional, _approved_doc, state, by_agent=by_agent)
        if skip_problems:
            return Directive(False, state.node, "fix_plan",
                             "cannot approve: --skip-optional names stages that cannot be "
                             "declined (fix the indices and re-run approve)",
                             data={"problems": skip_problems})
    elif _approved_doc is not None and any(is_skipped(s) for s in state.stages):
        stranded_problems = _stranded_dependency_problems(_approved_doc, state, set())
        if stranded_problems:
            return Directive(False, state.node, "fix_plan",
                             "cannot approve: a live stage depends on a declined (SKIPPED) "
                             "stage and would never become ready (edit the plan and re-run "
                             "approve)",
                             data={"problems": stranded_problems})
    mandate_basis = (args.by or "").strip() \
        if (args.by or "").strip().startswith(mandate_rules.MANDATE_BASIS + ":") else ""
    review_blockers = [] if mandate_basis else gates.plan_review_blockers(state, state.plan_path)
    blockers = (
        gates.blockers(state, "plan_approval")
        + plugins.plugin_gate_blockers(state, "plan_approval")
        + review_blockers
        + gates.protected_grant_blockers(state, state.plan_path)
    )
    if not by_agent and not mandate_basis:
        blockers = (
            blockers
            + gates.plan_presentation_blockers(state, state.plan_path)
            + gates.grants_approval_blockers(state, state.plan_path, entries=_approved_entries)
        )
    agent_autonomy: dict | None = None
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty approver: --by must name who approved"]
    elif mandate_basis:
        blockers = blockers + [
            f"--by {args.by!r} refused: {problem}"
            for problem in _mandate_basis_problems(state.plan_path, _approved_data, mandate_basis)
        ]
    elif by_agent:
        # A stage the customer declined and that has since changed, been renumbered or
        # re-added (or stopped being optional) is live again; the resource boundary below
        # cannot see that, and `--skip-optional` is refused for the agent, so its approval
        # would dispatch work the customer turned down. Two lines of defence: the decline
        # keyed by the stage's issue, and the marker the stage itself carries. Only a
        # customer approval re-offers the choice.
        revived = sorted(
            {s.index for s in state.stages if s.outcome.declined and not is_skipped(s)}
            | set(gates.declined_live_optional(
                state.stages, gates.declined_issue_set(state), gates.declined_title_set(state)))
        )
        if revived:
            blockers = blockers + [
                f"--by {args.by!r} refused: stage(s) {revived} were declined by the customer "
                "and have changed since -- the customer must choose again (approve by the "
                "customer, with --skip-optional to keep them declined)"
            ]
        # The coordinator approves only inside the boundary the user approved for this
        # order (a ledger entry and an eligible verdict on the CURRENT bytes); it never
        # stamps the ledger, so its approval is never a later boundary.
        # A plan that declares a parent_norm is judged on that basis and needs no ledger.
        _declared_order = _approved_doc.meta.order if _approved_doc is not None else None
        _parent_norm_plan = bool(_declared_order is not None and _declared_order.parent_norm)
        if _approved_doc is not None and (
                _parent_norm_plan or _ledgered_order_key(state) is not None):
            agent_autonomy = _autonomy_for(state, _approved_doc)
        if agent_autonomy is None:
            blockers = blockers + [
                f"--by {args.by!r}: {gates.AUTONOMY_REASON_NO_VERSION} -- approval must be "
                "attributed to the customer"
            ]
        elif not agent_autonomy["eligible"]:
            blockers = blockers + [
                f"--by {args.by!r} refused: outside the approved boundary -- "
                + "; ".join(agent_autonomy.get("reason_texts") or agent_autonomy["reasons"])
            ]
    _log_gate(state, "plan_approval", blockers, passed=not blockers)
    if blockers:
        fired = _evaluate_review_trigger(state, store, review_blockers)
        if fired is not None:
            return fired
        # `enumeration_escapes` is LEGACY: the history of the retired enumeration gate,
        # reported only from a bag an earlier engine wrote.
        round_release = _note_round_release(state, review_blockers, store)
        return _with_advisories(
            Directive(False, state.node, "fix_plan", "cannot approve", data={
                "blockers": blockers,
                "enumeration_escapes": _enumeration_escape_counts(state, _approved_doc),
                "plan_review_round_release": round_release,
            }),
            echo_advice)
    # Seam (c)'s stamp, past BOTH of this command's refusals — the submission check above and
    # the plan_approval gate. The refresh helper that owns the seam cannot stamp it: it runs
    # before the gate by contract, so a blocked approve would leave the session carrying a
    # digest for bytes it did not approve.
    if _approved_digest is not None:
        # From the SAME buffer _approved_doc was parsed from, not a fresh re-read
        # of state.plan_path (the former `_stamp_accepted_plan_digest(state,
        # state.plan_path)`).
        state.accepted_plan_digest = _approved_digest
    else:
        # The single-buffer read above failed (OSError/PlanError) -- fall back to
        # the legacy best-effort re-read rather than leaving the digest stale.
        _stamp_accepted_plan_digest(state, state.plan_path)
    # Before arm: the effort estimate the window is armed against must not price work the
    # user just declined.
    for skipped_index in skip_optional:
        skipped = state.stage(skipped_index)
        skipped.outcome.status = StageStatus.SKIPPED.value
        skipped.outcome.declined = True
        skipped.outcome.actual = "declined at approval (optional stage)"
    for s in state.stages:
        if not is_skipped(s):
            s.outcome.declined = False
    previous = (
        order_approvals.latest_user_approved_record(order_digest(_approved_doc))
        if _approved_doc is not None and _approved_doc.meta.order is not None else None
    )
    if not by_agent and not mandate_basis:
        # The customer's own choice, made again: a stage left live is no longer declined; the
        # issues of stages no longer in the plan stay, so re-adding one is still the
        # customer's call. "Stay" includes the declines this session never saw: they are
        # read from the order's last approval record, or the same act would keep a decline
        # in the session that made it and erase it in any other.
        kept = {s.backlog_issue for s in state.stages if is_skipped(s) and s.backlog_issue}
        offered = {
            s.backlog_issue for s in state.stages
            if s.optional and s.backlog_issue and not is_skipped(s)
        }
        state.declined_issues = sorted(
            (set(state.declined_issues) | set(order_approvals.declined_issues_of(previous)) | kept)
            - offered)
    if skip_optional:
        state.log("skip_optional", by=args.by, stages=skip_optional)
    effort.arm(state, now=_effort_now(store, state))  # opens the effort-divergence window — see effort.py's ARMED-ONLY
    # Fold this session's review-round counts into the cross-session task accumulator
    # (item B) BEFORE the reset-to-0 below — approval is the reset point, so this is
    # the last moment these session-local counts are readable.
    task_accumulator.add(
        state.task_id, "plan_review_rounds", state.plan_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    task_accumulator.add(
        state.task_id, "code_review_rounds", state.code_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by=args.by)
    _review_cycle_close(state)
    state.plan_review_rounds = 0
    state.review_release_difficulty_id = None
    # Cleared with the counter, not merely alongside it: the marker is what makes the
    # NEXT post-approval review count as round 1. Left carrying the approved plan's
    # digest, a first replan-time review of that same unedited plan would be read as
    # "already counted" and skipped.
    state.plan_review_counted_digest = ""
    # Reset alongside plan_review_rounds (item A) — approval starts a fresh execution
    # against the newly-approved plan, so friction spent reviewing code under the
    # PRIOR plan version should not count against this one.
    state.code_review_rounds = 0
    # Bind the approved grant set: stage-grants only returns DERIVED grants when a
    # live re-derivation still hashes to this value, so a materialization-layer
    # change after approval can never silently widen what a dispatched stage
    # receives without a fresh approve. None (rather than a stale prior digest) on
    # a plan that failed to load or grants nothing, matching grants_approval_
    # blockers' own fail-open reading of the same two conditions.
    _bind_approved_grants(state, _approved_doc, _approved_entries)
    if _approved_doc is not None:
        _rekey_runtime_grants(state, _approved_doc)
    # Terminal-pass custody: a fresh approval cycle starts with no pass on
    # record for any scope — the same "current approval cycle" boundary
    # plan_review_rounds resets at, immediately above.
    state.plan_review_passes = {}
    state.node = transition(state.node, "approve")
    # Write from the SAME buffer already read above when available, so the
    # snapshot can never describe bytes other than the ones `_approved_doc` was
    # parsed from and `accepted_plan_digest` was just stamped from; only a session
    # whose single-buffer read failed falls back to `_snapshot_approved_plan`'s
    # own best-effort re-read.
    if _approved_data is not None and _approved_digest is not None:
        snap = _write_plan_snapshot(store, state, _approved_data, _approved_digest)
    else:
        snap = _snapshot_approved_plan(store, state)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    if agent_autonomy is not None:
        parent_norm_basis = agent_autonomy.get("basis") == gates.PARENT_NORM_BASIS
        ledger_key = None if parent_norm_basis else _ledgered_order_key(state)
        approved = order_approvals.latest_user_approved_record(ledger_key) if ledger_key else None
        frozen = (approved or {}).get("effort_estimate") or {}
        if frozen:
            state.order_effort_frozen = {scale: float(value) for scale, value in frozen.items()}
            effort.rederive(state)
        state.log(
            "approve", by=args.by,
            **({"basis": gates.PARENT_NORM_BASIS} if parent_norm_basis else {}),
            extra_resources=[e["name"] for e in agent_autonomy["extra_resources"]],
            unresolved_changed_commands=[
                c["source"] for c in agent_autonomy["unresolved_changed_commands"]],
            active_overrides=[
                f"{name}={os.environ[name]}"
                for name in ("AGENTCTL_ORDER_APPROVALS_DIR", "AGENTCTL_TOOL_CONTRACTS")
                if os.environ.get(name)
            ],
        )
    elif mandate_basis:
        state.log("approve", by=args.by, basis=mandate_rules.MANDATE_BASIS)
    else:
        state.log("approve", by=args.by)
    if not mandate_basis:
        _clear_since_user_decision(state, args.by)
    # REQ4: stamp the order-approvals ledger for the CUSTOMER only -- never
    # AGENT_ACTOR (refused above, before this point is ever reached) and
    # never a non-customer --by (a reviewer, a delegate) whose approval does
    # not carry the customer's own authority to pre-grant resources. Gated on
    # a non-empty customer_id: an order-less plan (customer_id == "") can
    # never casefold-match any --by, so this is a no-op for every plan
    # authored before [meta.order] existed.
    if _approved_doc is not None:
        order = _approved_doc.meta.order
        customer_id = order.customer_id if order is not None else ""
        if (customer_id and not mandate_basis
                and args.by.strip().casefold() == customer_id.casefold()):
            order_key = order_digest(_approved_doc)
            view = plan_resources.compute_boundary_view(
                _approved_doc, order_key,
                branch_resources={
                    int(idx): [_resources.resource_from_dict(d)
                               for d in entry["branch"]["resources"]]
                    for idx, entry in (_approved_entries or {}).items()
                })
            all_resources = view.resources
            all_unresolved = [item["identity"] for item in view.unresolved]
            all_stage_effects: list = []
            for sr in plan_resources.compute_plan_resources(_approved_doc):
                all_stage_effects.extend(e.to_dict() for e in sr.stage_effects)
            armed_estimate = {
                scale: float((state.effort_estimate or {}).get(scale) or 0.0)
                for scale in effort.RATIO_SCALES
            }
            order_approvals.record_approval(
                order_key,
                plan_sha256=_approved_digest or "",
                resources=all_resources,
                unresolved_identities=all_unresolved,
                stage_effects=all_stage_effects,
                by=args.by,
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
                effort_estimate=armed_estimate,
                declined_issues=sorted(state.declined_issues),
                declined_stages=gates.declined_stage_entries(
                    state, order_approvals.declined_stages_of(previous)),
            )
            state.order_effort_frozen = armed_estimate
            actual_now = _order_actual(state, store=store)
            state.order_effort_flushed = {
                scale: actual_now[scale] for scale, _ in _ORDER_EFFORT_SCALES
            }
            state.order_effort_base = {}
        else:
            _flush_order_effort(state, store=store)
    store.save(state)
    return _with_advisories(Directive(
        True, state.node, "partition",
        "approved; assess partition (M1–M4) before execution",
    ), echo_advice)


def cmd_partition(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node != Node.APPROVED.value:
        return Directive(
            False, state.node, "noop",
            f"partition runs after approval, before execution; node={state.node} is not APPROVED",
        )
    known = {s.index for s in state.stages}
    units, unit_errors = _parse_partition_units(getattr(args, "unit", None) or [], known)
    if unit_errors:
        return Directive(
            False, state.node, "fix_units",
            "invalid partition units — nothing recorded", data={"errors": unit_errors},
        )
    m1 = bool(getattr(args, "m1", False))
    m2 = bool(getattr(args, "m2", False))
    m3 = bool(getattr(args, "m3", False))
    m4 = bool(getattr(args, "m4", False))
    m3_severe = bool(getattr(args, "m3_severe", False))
    m4_severe = bool(getattr(args, "m4_severe", False))
    v = verdict(m1, m2, m3, m4, m3_severe, m4_severe)
    state.partition = Partition(
        m1=m1, m2=m2, m3=m3, m4=m4, m3_severe=m3_severe, m4_severe=m4_severe,
        verdict=v, units=units,
    )
    stage_depends = {s.index: s.depends_on for s in state.stages}
    section = render_section(m1, m2, m3, m4, m3_severe, m4_severe, v,
                            units=units, stage_depends=stage_depends)
    state.node = transition(state.node, "partition")
    state.log("partition", verdict=v, m1=m1, m2=m2, m3=m3, m4=m4, units=len(units))
    store.save(state)
    action = "surface_partition" if v in ("recommended", "possible") else "next_stage"
    detail = (
        f"partition verdict: {v}; surface to the user before implementation"
        if v in ("recommended", "possible")
        else f"partition verdict: {v}; ship as one PR — advance to first stage"
    )
    return Directive(
        True, state.node, action, detail,
        data={"verdict": v, "section": section},
    )


def cmd_partition_units(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-record) the per-unit delivery routing AFTER the verdict is
    surfaced — the user's structure decision (subtickets vs several PRs vs one)
    arrives once they have seen the M1–M4 verdict. Allowed only at PARTITIONED (just
    after `partition`) or EXECUTING (mid-flight structure change); replaces the whole
    units list with the parsed `--unit` specs and leaves the verdict + node
    untouched.

    Re-recording at EXECUTING replaces the list WITHOUT validating against
    already-PASSED stages — a documented limitation, not a check."""
    state = _require(store, args.session)
    if state.node not in (Node.PARTITIONED.value, Node.EXECUTING.value):
        return Directive(
            False, state.node, "noop",
            "partition-units runs after the partition verdict is surfaced; "
            f"node={state.node} is neither PARTITIONED nor EXECUTING",
        )
    if state.partition is None:
        return Directive(
            False, state.node, "partition",
            "no partition assessment recorded yet — run `partition` first",
        )
    known = {s.index for s in state.stages}
    units, unit_errors = _parse_partition_units(getattr(args, "unit", None) or [], known)
    if unit_errors:
        return Directive(
            False, state.node, "fix_units",
            "invalid partition units — the recorded list is unchanged",
            data={"errors": unit_errors},
        )
    state.partition.units = units
    stage_depends = {s.index: s.depends_on for s in state.stages}
    block = render_units(units, stage_depends)
    state.log("partition_units", units=len(units))
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"recorded {len(units)} delivery unit(s)",
        data={"units_block": block},
    )


def cmd_next_stage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    ready = state.ready_stages()
    if not ready:
        # #17: a substantive replan whose stage carry-keys were unchanged (e.g. a
        # control-criterion-only change) carries every PASSED outcome forward, so a
        # re-approval lands the session at PARTITIONED with no PENDING stage left to
        # start — the intended next step (final verification) had no edge from here.
        # Fire it only under the exact guard that makes VERIFYING the faithful node
        # (every stage already PASSED); a genuine dependency problem (a non-ready,
        # non-PASSED stage) must NOT be silently finalized, so it keeps the prior
        # non-ok directive below.
        if state.node == Node.PARTITIONED.value and state.all_stages_settled():
            state.node = transition(state.node, "finalize_partitioned")
            state.log("finalize_partitioned")
            store.save(state)
            return Directive(
                True, state.node, "verify_final",
                "all stages already passed (a replan preserved them); advanced to "
                "final verification — run verify-final",
            )
        return Directive(False, state.node, "verify_final", "no ready stages; run verify-final if all passed")
    stage = ready[0]
    # pick the entry edge into EXECUTING from the current node
    if state.node == Node.PARTITIONED.value:
        event = "execute_approved"
    elif state.node == Node.ROUTED.value:
        event = "execute_small"
    elif state.node == Node.VERIFYING.value:
        event = "next_stage"
    elif state.node == Node.EXECUTING.value and state.active_stage() is None:
        # cmd_pop_subplan restores the parent at EXECUTING with no ACTIVE stage
        # (the originating stage was marked PASSED directly, bypassing the
        # record_result "verify" edge) and its own Directive tells the caller
        # to "run next-stage to continue" — so this node must be a legal entry
        # here too, not just VERIFYING. No edge to cross: already EXECUTING.
        event = None
    else:
        return Directive(False, state.node, "blocked", f"cannot start a stage from node={state.node}")
    if event is not None:
        state.node = transition(state.node, event)
    stage.outcome.status = StageStatus.ACTIVE.value
    state.current_stage = stage.index
    state.log("next_stage", stage=stage.index, executor=stage.actor.executor)
    store.save(state)
    action = "dispatch" if stage.is_spawn() else "execute_in_thread"
    if action == "dispatch":
        # #13: the directive must be unambiguous that `agentctl dispatch` IS the
        # spawn (synchronous, blocking) — a generic detail invited coordinators
        # to spawn manually and then feed dispatch a second, duplicate spawn.
        detail = (
            f"stage {stage.index} active: {stage.title} — spawning "
            f"{stage.actor.executor} now via agentctl dispatch (synchronous, "
            "blocking); do NOT spawn manually with spawn-specialist.py or claude -p"
        )
    else:
        detail = f"stage {stage.index} active: {stage.title}"
    return Directive(
        True, state.node, action, detail,
        data={"stage": stage.index, "executor": stage.actor.executor,
              "expected_result_image": stage.subject.result},
    )


def register(sub, add):
    sp = add("approve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--skip-optional", dest="skip_optional", type=int, action="append",
                    default=[], metavar="INDEX",
                    help="decline an `optional = true` stage at approval (repeatable); the "
                         "stage is marked SKIPPED, never dispatched, and counts as settled")
    _UNIT_HELP = ("delivery unit as '<mode>|<stages csv>|<title>[|<ref>]' "
                  "(mode: inline|spawn|subtask); repeatable")
    sp = add("partition"); sp.add_argument("--session", required=True)
    sp.add_argument("--m1", action="store_true"); sp.add_argument("--m2", action="store_true")
    sp.add_argument("--m3", action="store_true"); sp.add_argument("--m4", action="store_true")
    sp.add_argument("--m3-severe", dest="m3_severe", action="store_true")
    sp.add_argument("--m4-severe", dest="m4_severe", action="store_true")
    sp.add_argument("--unit", dest="unit", action="append", default=None, help=_UNIT_HELP)
    sp = add("partition-units"); sp.add_argument("--session", required=True)
    sp.add_argument("--unit", dest="unit", action="append", default=None, help=_UNIT_HELP)
    sp = add("next-stage"); sp.add_argument("--session", required=True)
