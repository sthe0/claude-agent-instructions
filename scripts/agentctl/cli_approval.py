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
    mandate_store,
)
from .partition import render_section, render_units, verdict
from .directive import Directive
from .dispatch import Runner
from .machine import transition
from .plan import (
    PlanError,
    load_plan_with_digest,
    materialized_grant_entries,
    order_digest,
    entries_grants_sha256,
)
from .state import (
    AGENT_ACTOR,
    is_skipped,
    Partition,
    GateRecord,
    Node,
    StageStatus,
    is_settled,
    PartitionUnit,
    PARTITION_UNIT_MODES,
    SessionState,
    Stage,
)
from .store import StateStore
from . import effort_segments
import hashlib
import json
import sys
from pathlib import Path
from .cli_common import (
    _judge_and_refuse,
    _settle_agent_skipped_stages,
    _judge_order_agreement,
    REPO_ROOT,
    _write_plan_snapshot,
    _snapshot_approved_plan,
    _ORDER_EFFORT_SCALES,
    _ledgered_order_key,
    _order_actual,
    _flush_order_effort,
    _autonomy_for,
    _baseline_plan_for_tier,
    _frozen_edge_stages,
    _verdict_survives,
    _apply_refined_stage_fields,
    _sync_venue_from_plan,
    _log_gate,
    _with_advisories,
    _submission_advice,
    _submission_problems,
    _adopt_order_identities,
    _carried_live_stage_blockers,
    _ensure_stage_arming,
    _note_user_approved_tiers,
    _require,
    _second_current_stage_refusal,
    _clear_since_user_decision,
    _enumeration_escape_counts,
    _note_round_release,
    _evaluate_review_trigger,
    _utcnow,
    _effort_now,
)


def _bind_approved_grants(state: SessionState, doc, entries: dict | None = None) -> None:
    """Bind the effective grant set of `doc` to the session at approval: the entries
    materialized once, and the hash of those entries. Dispatch reads the stored entries
    (`_stage_grant_entries`) after re-checking them against the hash; re-deriving them
    there made the grant set depend on the engine code and venue filesystem of the
    moment (#338). None/None when `doc` did not load, so a prior approval's binding is
    never carried over to unrelated bytes."""
    if doc is None:
        state.approved_grants_sha256 = None
        state.approved_grant_entries = None
        return
    if entries is None:
        entries = materialized_grant_entries(doc)
    state.approved_grant_entries = entries
    state.approved_grants_sha256 = entries_grants_sha256(entries)


def _stamp_accepted_plan_digest(state: SessionState, plan_path: str) -> None:
    """Record the sha256 of the ACCEPTED plan bytes on the session.

    Called from the three submission seams' commands and nowhere else, and only past every
    refusal path of the command that stamps — at approve that includes the plan_approval
    gate itself, which sits BELOW the seam — so state.accepted_plan_digest always names
    bytes the session actually took. Best-effort like its neighbours: an unreadable file
    leaves the previous digest in place rather than raising out of a command that has
    already decided to accept."""
    try:
        state.accepted_plan_digest = hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()
    except OSError:
        return


def _refresh_caches_from_plan_path(
    state: SessionState,
    *,
    runner: Runner | None = None,
    advice: list[str] | None = None,
    baseline=None,
) -> list[str]:
    """SUBMISSION SEAM (c). Re-load state.plan_path, validate it at submission grade, and
    — only if it is clean — refresh state.final_check plus each live stage's
    prose/criterion fields from those bytes. Returns the violation list; [] == refreshed.

    The seam's non-refusing channel is an OUT-PARAMETER rather than a second return value:
    the violation list is what this function's caller branches on, and advice must never
    become part of that decision. Pass a list to collect it; pass nothing (every caller
    that has no Directive to hang it on) and the seam behaves exactly as before.

    The seam is here rather than in `cmd_approve` directly because this is already the one
    place that re-reads plan_path at approve time, and a second read would let the two
    disagree. Nothing is mutated when the plan is dirty: the caller refuses with a
    Directive and the session keeps the state it had, so a rejected approve is not also a
    half-applied edit. The refusal must NOT be a raised PlanError — approve is where the
    plan_approval gate is armed, and an exception escaping there strands the session at
    PLAN_READY with no edge back.

    Approve snapshots and hashes plan_path, but the plan-review cycle answers a
    REVISE verdict by editing plan_path IN PLACE at PLAN_READY (deliberately
    plan-mutable — see hook-state-gate.py's PLAN_MUTABLE_NODES), so the copy
    cached at submit-plan can drift from the file approve is about to attest.
    Without this refresh, plan_snapshot_hash matches the edited bytes while
    dispatch/verify-final keep running the stale pre-review cache — the gate
    attests to a plan it never actually executes.

    Mutates each live stage IN PLACE via `_apply_refined_stage_fields`, which
    never touches `outcome` — an unchanged stage's Outcome therefore survives
    with no extra logic. A stage that is not carried (`stage_norm.stage_carried`: its
    full definition changed, or a direct supplier's interface did) is a different
    case: its recorded PASSED outcome no longer attests to the stage's current
    criterion or inputs, so it is reset to PENDING for re-verification. Carry is
    decided for every stage BEFORE `_apply_refined_stage_fields` mutates any —
    comparing after would compare a stage against itself post-copy, which always
    matches and would let a genuinely stale PASSED outcome survive unnoticed.

    An absent plan_path still returns [] — "there is nothing to refresh" is not a
    submission violation, and the plan_approval gate already refuses a session with no
    plan artifact. A plan_path that is set but no longer LOADS is different: it is a
    session whose approve is about to attest to bytes nobody can read, so it is reported
    as a violation rather than swallowed. The old silent return let approve pass on the
    stale pre-edit cache — the very "attests to a plan it never actually executes" failure
    this function's own docstring names."""
    if not state.plan_path:
        return []
    from .plan import PlanError, load_plan as _load
    try:
        refreshed = _load(state.plan_path)
    except (OSError, PlanError) as exc:
        return [f"cannot load the plan at {state.plan_path!r}: {exc}"]
    # `baseline` is the last approved plan: a finished stage's unchanged edge is frozen
    # against it, exactly as at replan, so approve judges the same exemption replan did.
    violations = _submission_problems(refreshed, runner, state.weight_class,
                                      _frozen_edge_stages(state, baseline, refreshed))
    if violations:
        return violations
    if advice is not None:
        advice.extend(_submission_advice(refreshed, runner, state.weight_class, state.plan_path))
    # Decided for every stage BEFORE any is mutated: a consumer's verdict depends on its
    # suppliers' pre-edit interfaces.
    carried = {rs.index: _verdict_survives(state.stages, refreshed.stages, rs.index)
               for rs in refreshed.stages}
    declined_issues = gates.declined_issue_set(state)
    declined_titles = gates.declined_title_set(state)
    rebuilt: list[Stage] = []
    for rs in refreshed.stages:
        try:
            cur = state.stage(rs.index)
        except KeyError:
            # An in-place edit may renumber or add a stage; the file is the plan, so the
            # session's stage list is rebuilt from it and a stage the file no longer has
            # leaves with it (an orphan SKIPPED stage would carry a decline for work that
            # is no longer there).
            rebuilt.append(rs)
            continue
        unchanged = carried[rs.index]
        _apply_refined_stage_fields(cur, rs)
        # One rule shared with the substantive-replan carry: a changed stage loses its
        # settled status, a declined one included, and is offered again for a fresh choice.
        if not unchanged and cur.outcome.status in (
            StageStatus.PASSED.value, StageStatus.SKIPPED.value,
        ):
            cur.outcome.status = StageStatus.PENDING.value
        rebuilt.append(cur)
    for s in rebuilt:
        s.outcome.declined = is_skipped(s) or (
            not is_settled(s) and gates.is_declined_stage(s, declined_issues, declined_titles))
    state.stages = rebuilt
    state.final_check = refreshed.meta.final_check
    _sync_venue_from_plan(state, refreshed)
    # The digest is NOT stamped here. A clean submission is not yet an accepted plan at this
    # seam: `cmd_approve` still has the plan_approval gate to compose, and a blocked approve
    # must not leave the session naming bytes it refused. The stamp is the caller's, placed
    # past that refusal.
    return []


def _review_cycle_close(state: SessionState) -> None:
    """Approval ends the pre-approval review cycle, its streaks and the fire's bookkeeping."""
    state.review_cycle_open = False
    state.review_cycle_sha = ""
    state.review_cycle_acts = 0
    state.review_cycle_minutes_anchor = None
    state.review_stage_streaks = {}
    state.review_trigger_spent = {}
    state.review_trigger_fire = None


def _skip_optional_problems(
    indices: list[int], doc: "PlanDoc | None", state: SessionState, *, by_agent: bool,
) -> list[str]:
    """Why `approve --skip-optional <indices>` cannot be honoured; [] when it can."""
    if by_agent:
        return ["--skip-optional is the customer's decision and cannot be made with --by agent"]
    if doc is None:
        return ["--skip-optional needs a loadable plan at plan_path"]
    by_index = {s.index: s for s in doc.stages}
    problems: list[str] = []
    for index in indices:
        stage = by_index.get(index)
        if stage is None:
            problems.append(f"stage {index} does not exist")
        elif not stage.optional:
            problems.append(f"stage {index} is not optional (only `optional = true` stages can be declined)")
    if problems:
        return problems
    return _stranded_dependency_problems(doc, state, set(indices))


def _stranded_dependency_problems(
    doc: "PlanDoc", state: SessionState, newly_declined: set[int],
) -> list[str]:
    """Stages that stay live but depend on a declined stage and so could never become ready.

    Declined = `newly_declined` plus every stage the session already holds SKIPPED. Run on
    every approve that has any declined stage, not only on `--skip-optional`: a replan can
    carry a SKIPPED stage forward and add a live optional stage that depends on it."""
    declined = newly_declined | {s.index for s in state.stages if is_skipped(s)}
    problems: list[str] = []
    for stage in doc.stages:
        stranded = sorted(d for d in stage.depends_on if d in declined)
        if stage.index not in declined and stranded:
            problems.append(
                f"stage {stage.index} depends on declined stage(s) {stranded} — decline it too "
                f"or keep them"
            )
    return problems


def _mandate_basis_problems(plan_path, plan_data: bytes | None, approver: str) -> list[str]:
    """Why the `mandate:<id>` approver may not approve the plan at `plan_path`, empty when it may.

    The approval is the mandate's, for one item its running cycle took: `plan_data` -- the bytes
    `cmd_approve` read once and snapshots -- is the item norm re-rendered from the inputs beside
    the plan, the cycle lock is held, the cycle's start event and the item's taken event name this
    plan's issue, directory and worktree, the worktree lies under the cycle's worktree root, the
    cycle's repo is the repo this engine runs from, and the mandate's gate is open. Never raises."""
    inputs_file, item_event, cycle_event = "norm.inputs.json", "item-taken", "cycle-start"
    mandate_id = approver[len(mandate_rules.mandate_approver("")):]
    mandate = mandate_store.load_mandate(mandate_id) if mandate_id else None
    if mandate is None:
        return [f"no mandate {mandate_id!r}"]
    plan = Path(plan_path or "")
    if plan_data is None:
        return [f"the plan and {inputs_file} cannot be read beside it"]
    try:
        plan_text = plan_data.decode("utf-8")
        inputs = json.loads((plan.parent / inputs_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"the plan and {inputs_file} cannot be read beside it"]
    if not isinstance(inputs, dict):
        return [f"{inputs_file} is not an object"]
    held, _pid = mandate_store.probe_cycle(mandate_id)
    if not held:
        return ["no cycle holds the mandate's lock"]
    events = mandate_store.read_events(mandate_id)
    starts = [e for e in events if e.get("event") == cycle_event]
    if not starts:
        return ["the cycle recorded no start"]
    start = starts[-1]
    detail = start.get("detail") or {}
    problems: list[str] = []
    cycle_id = inputs.get("cycle_id")
    if detail.get("cycle_id") != cycle_id:
        problems.append("the inputs name a cycle that is not the running one")
    if not detail.get("repo") or inputs.get("repo") != detail.get("repo"):
        problems.append("the inputs' repo is not the cycle's repo")
    elif os.path.realpath(detail["repo"]) != os.path.realpath(REPO_ROOT):
        problems.append("the cycle's repo is not the repo this engine runs from")
    root = detail.get("worktree_root")
    worktree = inputs.get("worktree")
    try:
        under_root = bool(root) and isinstance(worktree, str) and os.path.commonpath(
            [os.path.normpath(root), os.path.normpath(worktree)]) == os.path.normpath(root) \
            and os.path.normpath(worktree) != os.path.normpath(root)
    except ValueError:
        under_root = False
    if not under_root:
        problems.append("the inputs' worktree is not under the cycle's worktree root")
    taken = [
        e for e in events
        if e.get("event") == item_event
        and (e.get("detail") or {}).get("cycle_id") == cycle_id
        and (e.get("detail") or {}).get("issue") == inputs.get("issue")
        and (e.get("detail") or {}).get("item_dir") == str(plan.parent)
        and (e.get("detail") or {}).get("worktree") == worktree
    ]
    if not taken:
        problems.append("the cycle recorded no item taken for this issue, directory and worktree")
    now = mandate_store.utcnow()
    started_at = mandate_rules.parse_ts(start.get("ts"))
    elapsed = max(0.0, (now - started_at).total_seconds() / 60.0) if started_at else 0.0
    gate_result = mandate_rules.gate(mandate, now, mandate_store.spend_now(mandate_id, now), elapsed)
    return problems + mandate_rules.mandate_approval_problems(plan_text, inputs, mandate, gate_result)


def _parse_partition_units(
    raw_units: list[str], known_indices: set[int]
) -> tuple[list[PartitionUnit], list[str]]:
    """Parse repeatable ``--unit '<mode>|<stages csv>|<title>[|<ref>]'`` specs into
    typed PartitionUnit objects, validating against the loaded plan. Returns
    ``(units, errors)`` — a non-empty ``errors`` list means the caller must reject
    with a failing Directive and record nothing.

    Validation: mode ∈ PARTITION_UNIT_MODES; a non-empty title; ≥1 integer stage
    index; every stage index exists in ``known_indices``; stage sets pairwise
    disjoint across units (a stage belongs to at most one delivery unit — stages
    left uncovered stay on the default single-PR path). ``ref`` is optional and
    org-neutral (tracker key / issue URL / child session id, assigned at
    materialization)."""
    units: list[PartitionUnit] = []
    errors: list[str] = []
    seen_stages: dict[int, int] = {}  # stage index -> owning unit (1-based)
    for pos, spec in enumerate(raw_units, start=1):
        parts = spec.split("|")
        if len(parts) < 3:
            errors.append(
                f"unit {pos}: expected '<mode>|<stages csv>|<title>[|<ref>]', got {spec!r}"
            )
            continue
        mode = parts[0].strip()
        stages_field = parts[1].strip()
        title = parts[2].strip()
        ref = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else None
        if mode not in PARTITION_UNIT_MODES:
            errors.append(
                f"unit {pos}: unknown mode {mode!r} (expected one of {', '.join(PARTITION_UNIT_MODES)})"
            )
        if not title:
            errors.append(f"unit {pos}: empty title")
        stages: list[int] = []
        for tok in [t.strip() for t in stages_field.split(",") if t.strip()]:
            try:
                stages.append(int(tok))
            except ValueError:
                errors.append(f"unit {pos}: non-integer stage index {tok!r}")
        if not stages:
            errors.append(f"unit {pos}: no stage indices given")
        for s in stages:
            if s not in known_indices:
                errors.append(f"unit {pos}: stage index {s} does not exist in the plan")
            elif s in seen_stages:
                errors.append(
                    f"unit {pos}: stage index {s} already assigned to unit {seen_stages[s]} "
                    "(units must be disjoint)"
                )
            else:
                seen_stages[s] = pos
        units.append(PartitionUnit(title=title, stages=stages, mode=mode, ref=ref))
    return units, errors


def _rekey_runtime_grants(state: SessionState, doc) -> None:
    """A runtime grant is keyed by stage INDEX but was granted for a stage IDENTIFIED
    by its title at grant time -- a replan can renumber stages (insert/remove before
    it) without changing what the grant was actually about. Called on every approve
    (fresh snapshot, fresh indices): for each stage index currently holding runtime
    grants, re-key each entry to whichever new-snapshot stage has the SAME title it
    was stamped with. Zero or several matches (title now shared or gone) drops the
    entry with a stderr note rather than guessing."""
    title_to_indices: dict[str, list[int]] = {}
    for s in doc.stages:
        title_to_indices.setdefault(s.title, []).append(s.index)
    rekeyed: dict[str, list[dict]] = {}
    for old_key, entries in state.runtime_grants.items():
        for entry in entries:
            title = entry.get("stage_title")
            matches = title_to_indices.get(title, []) if title else []
            if len(matches) != 1:
                print(
                    f"stage-grants: dropping runtime grant stamped for stage title "
                    f"{title!r} (was stage {old_key}) -- {len(matches)} stages now "
                    "match that title",
                    file=sys.stderr,
                )
                continue
            new_key = str(matches[0])
            rekeyed.setdefault(new_key, []).append(entry)
    state.runtime_grants = rekeyed


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
    agreement_blockers = _judge_order_agreement(state, _approved_doc)
    blockers = (
        gates.blockers(state, "plan_approval")
        + agreement_blockers
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
    _second_current = _second_current_stage_refusal(state, state.current_stage)
    if _second_current is not None:
        return _second_current
    _identity_order = (
        order_digest(_approved_doc)
        if _approved_doc is not None and _approved_doc.meta.order is not None else None
    )
    if by_agent:
        _fire_refusal = _judge_and_refuse(
            state, store, args, effort_segments.ACT_APPROVE_BY_AGENT, "approve --by agent")
        if _fire_refusal is not None:
            return _fire_refusal
        blockers = blockers + _carried_live_stage_blockers(state, _identity_order)
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
    if by_agent:
        _settle_agent_skipped_stages(state)
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
    _adopt_order_identities(state, _identity_order, _utcnow())
    if not by_agent:
        _note_user_approved_tiers(state)
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
    second_current = _second_current_stage_refusal(state, stage.index)
    if second_current is not None:
        return second_current
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
    _ensure_stage_arming(state, stage, _utcnow(), "next_stage")
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
