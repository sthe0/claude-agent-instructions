"""Plan authoring subcommands: classify, plan rendering and conversion, submit and present.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path
from lib import config_root, writer_pass
from . import (
    advisor,
    carry_forward,
    delivery,
    gates,
    grant_shadow,
    plugins,
    plugins_premise,
    runtime_host,
    solved_marker,
)
from .checkrun import format_observations, observe_stage_checks
from .classify import TRACKER_KEY_RE, Signals, classify
from .config import Thresholds
from .directive import Directive
from .dispatch import Runner
from .machine import transition
from .plan import (
    PlanError,
    changed_parts,
    check_venue_warnings,
    grants_sha256,
    load_plan,
    load_plan_with_digest,
    plan_has_any_grants,
    verify_command_reachability_blockers,
    cost_tier_floor_advisories,
    final_check_duplicate_warnings,
    verify_command_scope_warnings,
    whole_suite_warnings,
)
from .render import plan_render_stage_arg_type, plan_summary_text, render_plan_grants
from .state import (
    Actor,
    AUTHORIZE_REPLAN_MARKER,
    Criterion,
    GateRecord,
    Means,
    Node,
    PLAN_PRESENTATION_KIND_ESSENCE,
    PLAN_PRESENTATION_KIND_FULL,
    PLAN_PRESENTATION_KIND_REPLAN_DIFF,
    PLAN_PRESENTATION_KINDS,
    PLAN_PRESENTATION_RENDERING_CAP_BYTES,
    PlanPresentation,
    PlanReview,
    plan_review_scope_stage_index,
    SHOW_FULL_PLAN_MARKER,
    Stage,
    Subject,
    WeightClass,
)
from .store import StateStore
from .plan import dropped_grants_report
from .cli_common import (
    _plan_file_sha256,
    _write_plan_version_snapshot,
    _flush_order_effort,
    _adopt_parent_difficulty,
    _adopt_norm_handoff,
    _loop_self_check,
    _self_approve_detail,
    _agent_review_override,
    _review_baseline,
    _carry_records,
    _autonomy_directive_data,
    _sync_venue_from_plan,
    _log_gate,
    _attach_advisories,
    _submission_advice,
    _submission_problems,
    _effort_fire_refusal,
    _require,
    _register_plan_assumptions,
    _record_plan_presentation,
    _PLAN_PRESENTATION_STAGE_ANCHOR_RE,
    _plan_presentation_skeleton,
    _present_plan_transcript,
    _deliver_presentation_via_channel,
    _review_cycle_arrive,
    _evaluate_review_trigger,
    _cost_rows,
    _refresh_spend,
)


def cmd_classify(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    try:
        runtime_host.bind_runtime_host(state, getattr(args, "host", None), require=True)
    except (runtime_host.HostAmbiguousError, runtime_host.HostConflictError) as exc:
        return Directive(False, state.node, "noop", str(exc))
    _adopt_parent_difficulty(state)
    _adopt_norm_handoff(state, store)
    thr = Thresholds()
    sig = Signals(
        is_chat=bool(getattr(args, "chat", False)),
        changed_lines=int(getattr(args, "changed_lines", 0) or 0),
        cohesive=not bool(getattr(args, "not_cohesive", False)),
        wall_clock_min=int(getattr(args, "wall_clock_min", 0) or 0),
        tracker_key=getattr(args, "tracker_key", None),
        architectural=bool(getattr(args, "architectural", False)),
        external_effect=bool(getattr(args, "external_effect", False)),
        new_dependency=bool(getattr(args, "new_dependency", False)),
        public_api_change=bool(getattr(args, "public_api_change", False)),
    )
    result = classify(sig, thr)
    state.weight_class = result.weight_class
    state.route = result.route
    if sig.tracker_key and TRACKER_KEY_RE.match(sig.tracker_key):
        state.tracker_key = sig.tracker_key
    elif sig.tracker_key and solved_marker.key_shape(sig.tracker_key) == "github":
        # a fully-qualified github ref (owner/repo#N) is not TRACKER_KEY_RE-shaped and so
        # does not force SUBSTANTIVE, but it must still reach cmd_resolve's marker stamp.
        state.tracker_key = sig.tracker_key
    state.deliverable_kind = getattr(args, "deliverable_kind", "") or ""
    state.node = transition(state.node, "classify")
    state.log("classify", weight_class=result.weight_class, route=result.route, reasons=result.reasons)

    if result.weight_class == WeightClass.SMALL_CHANGE.value:
        # carve-out: no plan-approval gate; auto-pass so ROUTED->EXECUTING is legal
        state.approval = GateRecord("plan_approval", armed=True, passed=True, by="small-change-carve-out")
        state.stages = [
            Stage(
                index=1,
                title=state.goal or "small change",
                subject=Subject(
                    material=state.goal or "target",
                    result=state.goal or "change applied",
                ),
                means=Means(means="Edit tool", method="apply the small change in-thread"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=state.overall_criterion_type,
                    done_criterion=state.overall_done_criterion or "change applied and self-checked",
                ),
            )
        ]
        action, detail = "execute_in_thread", "small change: execute in-thread, then record-result"
    elif result.weight_class == WeightClass.CHAT.value:
        action, detail = "answer_in_thread", "chat: answer directly; terminal at ROUTED"
    else:
        action, detail = "plan", "substantive: route to planner, then submit-plan"

    if result.weight_class == WeightClass.SUBSTANTIVE.value:
        plugins.auto_activate_for(state)

    store.save(state)
    d = Directive(True, state.node, action, detail, data={"reasons": result.reasons})
    loop_self_check = _loop_self_check(state, thr)
    if loop_self_check is not None:
        d.data["loop_self_check"] = loop_self_check
        d.detail += (
            f" | loop self-check (depth {loop_self_check['depth']} >= "
            f"{loop_self_check['threshold']}): if your task re-frames any ancestor task in "
            "data.loop_self_check.ancestors, end with `LOOP_DETECTED:` naming it instead "
            "of recursing further"
        )
    _attach_advisories(d, "weight_classification",
                       {"goal": state.goal, "weight_class": state.weight_class, "route": state.route},
                       runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    return d


def cmd_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    state.node = transition(state.node, "plan")
    state.log("plan")
    store.save(state)
    return Directive(True, state.node, "await_plan", "planner working; submit-plan when ready")


def cmd_submit_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if efblock:
        return _effort_fire_refusal(state, "submit_plan", efblock)
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    _flush_order_effort(state, store=store)
    plan_path = args.plan
    # #15: a resubmission — the coordinator revised the plan at PLAN_READY (after a
    # thinker `revise` verdict, or the user's own pre-approval edit) and re-runs
    # submit-plan without a reset --force. Distinguished by the source node; drives
    # the `revise_plan` edge (PLAN_READY -> PLAN_READY) instead of `submit_plan`.
    resubmitting = state.node == Node.PLAN_READY.value
    if not plan_path.endswith(".toml"):
        state.log("submit_plan", plan=plan_path, verified=False)
        store.save(state)
        return Directive(
            False, state.node, "fix_plan",
            "plans are TOML-only — rewrite as TOML with typed stages",
            data={"problems": ["plans are TOML-only; rewrite as TOML with typed stages"]},
        )
    # Read plan_path's bytes ONCE and derive doc, the digest stamped below (seam (a))
    # and the plan-version snapshot below from that same buffer, so a concurrent edit
    # between separate reads can't desynchronize accepted_plan_digest from the bytes
    # that were actually parsed and verified.
    doc, _submitted_plan_bytes, _submitted_plan_digest = load_plan_with_digest(plan_path)
    _prior_baseline = (*_review_baseline(store, state), state.plan_path) if resubmitting else None
    state.stages = doc.stages
    _sync_venue_from_plan(state, doc)
    state.final_check = doc.meta.final_check
    if not state.goal:
        state.goal = doc.meta.goal
    if not state.overall_done_criterion:
        state.overall_done_criterion = doc.meta.done_criterion
    state.plan_verified = True
    # Submission seam (a): the first entry of these bytes into the session. The session's
    # weight class is passed in so this seam and the reachability gate immediately below
    # arm on the same condition — before, the seam keyed on the plan's own [meta] and the
    # gate on the session's, so a substantive session submitting a plan that simply omits
    # `weight_class` cleared the seam and not the gate.
    # Entry-point fallback (the run=runner-if-not-None-else-advisor.subprocess_runner idiom
    # used at the other advisor call sites): production's cmd_submit_plan is always invoked
    # with runner=None, so without this the judge is unreachable outside tests regardless of
    # advisor.resolve_enabled — which stays the actual kill switch, unaffected by this line.
    # Resolved HERE rather than beside the advice channel below because the seam's own
    # judged refusal (a `conditions` that merely restates depends_on) is part of `problems`,
    # and a refusal cannot be computed after the return that acts on it. The binding itself
    # costs nothing; only a prefilter hit spends a judge call.
    run = runner if runner is not None else advisor.subprocess_runner
    problems: list[str] = _submission_problems(doc, run, state.weight_class)
    if state.weight_class == WeightClass.SUBSTANTIVE.value:
        # Two-directional control: the scope lint (advisory, below) keeps a
        # control from being false-RED; this BLOCKS a control that can never
        # go honestly GREEN because it names a path no stage produces and
        # that does not exist. No legitimate instance -> a blocker, not a warn.
        # The base a path's existence is judged against is the plan's declared absolute
        # venue — repo_root first, as before, delivery_worktree when that is the only
        # one declared. A plan declaring neither is refused above for it
        # (`plan.venue_declaration_blockers`); the process cwd is never the base.
        problems.extend(
            verify_command_reachability_blockers(
                doc.stages, doc.meta.final_check,
                next((v for v in (doc.meta.repo_root, doc.meta.delivery_worktree)
                      if v and os.path.isabs(v)), None),
            )
        )
    if problems:
        state.plan_verified = False

    state.plan_path = plan_path
    if not state.plan_verified:
        # Stay at PLANNING — do NOT transition or arm the gate. Advancing to
        # PLAN_READY on a failed structure check strands the session there with
        # an armed gate and no recovery edge back (every retry bounced; had to
        # be unstuck by hand via `reset --force`). The agent fixes the plan and
        # re-runs submit-plan in place from PLANNING.
        state.log("submit_plan", plan=plan_path, verified=False)
        store.save(state)
        return Directive(False, state.node, "fix_plan", "plan failed verification", data={"problems": problems})

    # Past the refusal, so these bytes were ACCEPTED — which is the only thing
    # accepted_plan_digest ever records.
    state.accepted_plan_digest = _submitted_plan_digest
    # Content-address these accepted bytes as a plan version, independent of the
    # approved-plan snapshot below (that one is taken only at `approve`) -- so the
    # `submit_plan` history event's plan_sha256 always resolves to a stored file, even
    # for a submission never subsequently approved.
    _write_plan_version_snapshot(store, state, _submitted_plan_bytes, _submitted_plan_digest,
                                 plan_path=plan_path)
    _submitted_plan_sha256 = _submitted_plan_digest
    state.node = transition(state.node, "revise_plan" if resubmitting else "submit_plan")
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    _carry_note = ""
    if resubmitting:
        # The counter (read by gates.plan_review_round_release_active, reset by
        # cmd_approve) advances per resubmission made while a review record STANDS —
        # redrafts of a plan nobody reviewed are not rounds. Read before the staleness
        # clear below, which ends the round. An edit whose records carry onto the new
        # version (`carry_forward`) is not a round: no review is owed again for it.
        _, _submit_carry, _ = _carry_records(
            store, state, plan_path, in_place=True, baseline=_prior_baseline)
        if _submit_carry["carried"]:
            _carry_note = carry_forward.describe(_submit_carry)
            state.log("review_carried", plan=plan_path, tier=_submit_carry["tier"],
                      records=list(_submit_carry["records"]), owed=list(_submit_carry["owed"]),
                      held=list(_submit_carry["held"]))
        elif state.plan_review is not None or state.plan_stage_reviews:
            state.plan_review_rounds += 1
        # The plan changed, so any recorded thinker review that no longer covers
        # the resubmitted bytes must clear so the plan-review gate re-arms for
        # them. "No longer covers" is decided per review record via the SAME
        # plan.changed_parts a review's own recorded digests feed the coverage
        # gate with — a review recorded before this field existed (empty
        # reviewed_meta_digest/reviewed_stage_keys) compares as "everything
        # moved" against ANY doc, reproducing the old unconditional clear for
        # every legacy record without a special case.
        def _still_covers(pr: PlanReview) -> bool:
            meta_moved, moved = changed_parts(
                doc, {"meta": pr.reviewed_meta_digest, "stages": pr.reviewed_stage_keys})
            if meta_moved:
                return False
            idx = plan_review_scope_stage_index(pr.scope)
            return True if idx is None else idx not in moved

        if state.plan_review is not None and not _still_covers(state.plan_review):
            state.plan_review = None
        state.plan_stage_reviews = {
            scope: pr for scope, pr in state.plan_stage_reviews.items() if _still_covers(pr)
        }
    _register_plan_assumptions(state, doc, plan_path)
    state.plan_submitted_ts = time.time()
    state.log("submit_plan", plan=plan_path, verified=True, revised=resubmitting,
              plan_sha256=_submitted_plan_sha256)
    if not _review_cycle_arrive(state, _submitted_plan_sha256) and resubmitting:
        fired = _evaluate_review_trigger(
            state, store, gates.plan_review_blockers(state, plan_path))
        if fired is not None:
            return fired
    store.save(state)
    autonomy = _autonomy_directive_data(state, doc)
    override = _agent_review_override(state)
    directive_data = {"autonomy": autonomy, **({"agent_review_override": override} if override else {})}
    if autonomy["action"] == "self_approve":
        d = Directive(
            True, state.node, "self_approve",
            _self_approve_detail(autonomy, replan=False),
            data=directive_data,
        )
    else:
        d = Directive(
            True, state.node, "await_user_approval",
            "plan ready; HARD GATE — get explicit user approval before approve",
            marker="PLAN-READY", data=directive_data,
        )
    if _carry_note:
        d.data.setdefault("advisories", []).append(_carry_note)
    _attach_advisories(d, "plan_completeness",
                       {"plan_summary": plan_summary_text(doc), "stage_count": len(state.stages),
                        "titles": [s.title for s in state.stages]},
                       runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    # Deterministic scope lint (experience leaf 2026-06-29) — always runs,
    # independent of the optional LLM advisor above; warn-only, never blocks.
    d.data.setdefault("advisories", []).extend(
        verify_command_scope_warnings(doc.stages, doc.meta.final_check)
    )
    d.data["advisories"].extend(whole_suite_warnings(doc.stages, doc.meta.final_check))
    # Duplicate final_check lint (issue #141) — warn when a final_check command
    # is byte-identical (whitespace-normalized) to a stage verify_command; that
    # pair makes verify-final run the command twice with no intervening state
    # change, doubling resolution wall-clock for zero added coverage.
    d.data.setdefault("advisories", []).extend(
        final_check_duplicate_warnings(doc.stages, doc.meta.final_check)
    )
    # Deterministic check-venue lint (#45) — same warn-only channel; fires
    # only when [meta] delivery_worktree names a venue distinct from repo_root.
    # Two triggers (see check_venue_warnings): a check whose `cd` target
    # contradicts its declared venue, and (schema 24) a bare "delivery"-venue
    # stage in a plan that asserts landing, which will refuse at verify-final.
    d.data.setdefault("advisories", []).extend(
        check_venue_warnings(doc.stages, doc.meta.final_check, doc.meta.repo_root, doc.meta.delivery_worktree)
    )
    d.data.setdefault("advisories", []).extend(grant_shadow.mixed_spelling_advisories(doc))
    dropped = dropped_grants_report(doc)
    if dropped:
        d.data["dropped_grants"] = dropped
        d.data["advisories"].append(
            "dropped grants (derived entries refused by the validator or DR-P; advisory):\n"
            + "\n".join(f"- stage {x['stage']}: {x['entry']} — {x['reason']}" for x in dropped)
        )
    d.data.setdefault("advisories", []).extend(cost_tier_floor_advisories(doc.stages))
    # Submission seam (a)'s advice channel: a stage whose expected_result_image merely
    # restates its own check. Warn-only at all three seams — see submission.submission_advice.
    # Rides the same `run` resolved at the seam above (see its comment for why the fallback
    # exists at all). The `plan_completeness` advisory above is DELIBERATELY left on the raw
    # `runner`, i.e. inert in production, so the two adjacent advisor call sites in this
    # function behave oppositely on purpose: giving it the same fallback would add a second
    # live `claude -p` to every submit, which no stage has sized or measured. Read "is the
    # advisor reachable?" per call site, not by generalizing from either one.
    d.data.setdefault("advisories", []).extend(
        _submission_advice(doc, run, state.weight_class, plan_path)
    )
    # Predsubmit check-run observation (C.2) — actually RUNS each stage's
    # verify_command in its declared venue, warn-only, same advisories channel.
    # Substantive-only, mirroring the reachability-blocker gate above: a
    # non-substantive plan carries no verify_command discipline through this
    # cycle at all, so there is nothing here for it to observe.
    if state.weight_class == WeightClass.SUBSTANTIVE.value:
        d.data.setdefault("advisories", []).extend(
            format_observations(
                observe_stage_checks(doc.stages, state.resolve_check_venue, runner)
            )
        )
    if gates.plan_presentation_active(state):
        # A NUDGE, not the enforcement — the hash-bound gate in gates.
        # plan_presentation_blockers (checked at `approve`) is what actually
        # makes presentation non-skippable. d.ok/d.node/marker stay untouched
        # so a coordinator/test relying on the PLAN-READY contract sees no
        # change; this only adds an advisory string a well-behaved caller reads.
        d.data.setdefault("advisories", []).append(
            "run `present-plan --kind essence` (and `--kind full` if the plan "
            "needs stage-by-stage detail) before requesting approval — "
            "`approve` will refuse without a bound presentation + delivery proof"
        )
    return d


def cmd_present_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Stamp a PlanPresentation receipt: proof the coordinator rendered the plan
    for the user, bound to the exact plan version (plan_sha256) and the exact
    rendered bytes (rendering_sha256). Records the ARTIFACT and its bindings
    only — never a judgement of whether the rendering is a FAITHFUL or GOOD
    summary of the plan (that remains perception, same charter as PlanReview/
    StageReview). Orthogonal to the node graph: never calls transition() and
    never changes state.node — a plan may be presented at any point after it
    exists, independent of where the state machine currently sits.

    `--emit-skeleton` stamps nothing; it only hands back the anchor scaffold a
    `full` rendering must preserve, so the coordinator does not have to
    hand-enumerate stages itself.
    """
    state = _require(store, args.session)
    if not state.plan_path:
        return Directive(False, state.node, "noop", "no plan to present: submit a plan first")

    if getattr(args, "emit_skeleton", False):
        try:
            doc = load_plan(state.plan_path)
        except Exception as exc:
            return Directive(False, state.node, "noop", f"cannot load plan: {exc}")
        skeleton = _plan_presentation_skeleton(doc.stages)
        return Directive(
            True, state.node, "continue",
            "skeleton emitted; fill in each [stage N] section, then present-plan "
            "--kind full --rendering-file <path> with the completed rendering",
            data={"skeleton": skeleton, "stage_count": len(doc.stages)},
        )

    kind = args.kind
    if kind not in PLAN_PRESENTATION_KINDS:
        return Directive(
            False, state.node, "noop",
            f"unknown presentation kind {kind!r}; expected one of {PLAN_PRESENTATION_KINDS}",
        )
    explicit_plan = getattr(args, "plan", None)
    if explicit_plan and kind != PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # --plan is a degree of freedom only replan_diff needs (the proposed
        # plan is a different file than state.plan_path): widening it to
        # essence/full would let a receipt be stamped for a file the session
        # is not executing, which plan_presentation_blockers never checks for.
        return Directive(
            False, state.node, "noop",
            f"--plan is only accepted with --kind {PLAN_PRESENTATION_KIND_REPLAN_DIFF!r}; "
            f"essence/full always present state.plan_path ({state.plan_path!r})",
        )
    target = explicit_plan or state.plan_path
    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # essence is the receipt an approval ask is assembled from — gate it on
        # the same plan_review_blockers precondition as approve/replan, so a
        # thinker review must exist BEFORE that receipt can be stamped, not only
        # before the terminal approve. `full` is the detailed on-request view,
        # not the approval trigger, so it stays ungated.
        prblock = gates.plan_review_blockers(state, target)
        _log_gate(state, "plan_review", prblock, passed=not prblock)
        if prblock:
            return Directive(
                False, state.node, "noop", "cannot present essence",
                data={"blockers": prblock},
            )
    elif kind == PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # A proposed diff must itself have cleared thinker review before it can
        # be shown as the authorization prompt — the same precondition essence
        # pays, over the PROPOSED bytes rather than state.plan_path.
        prblock = gates.plan_review_blockers(state, target)
        _log_gate(state, "plan_review", prblock, passed=not prblock)
        if prblock:
            return Directive(
                False, state.node, "noop", "cannot present replan_diff",
                data={"blockers": prblock},
            )
    rendering_file = getattr(args, "rendering_file", None)
    if not rendering_file:
        return Directive(False, state.node, "noop", "--rendering-file is required")
    try:
        raw = Path(rendering_file).read_bytes()
    except OSError as exc:
        return Directive(False, state.node, "noop", f"cannot read rendering file: {exc}")
    if len(raw) > PLAN_PRESENTATION_RENDERING_CAP_BYTES:
        return Directive(
            False, state.node, "noop",
            f"rendering is {len(raw)} bytes, over the "
            f"{PLAN_PRESENTATION_RENDERING_CAP_BYTES}-byte cap — trim it, never "
            "truncate silently; the recorded receipt must match what the user saw",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Directive(False, state.node, "noop", "rendering file must be UTF-8 text")

    if kind == PLAN_PRESENTATION_KIND_FULL:
        # Completeness is MECHANIZABLE (every stage anchor present) — enforce it.
        # Whether the prose under each anchor is a faithful summary is
        # perception, not checked here; `essence` has no analogous check at all
        # (free-form by design — a stage-enumerated essence would just be `full`).
        try:
            doc = load_plan(target)
        except Exception as exc:
            return Directive(False, state.node, "noop", f"cannot load plan: {exc}")
        expected = {s.index for s in doc.stages}
        found = {int(m) for m in _PLAN_PRESENTATION_STAGE_ANCHOR_RE.findall(text)}
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"missing stage anchors: {missing}")
            if extra:
                problems.append(f"unknown stage anchors: {extra}")
            return Directive(
                False, state.node, "noop",
                "full rendering is incomplete; stamping nothing — " + "; ".join(problems),
                data={"missing": missing, "extra": extra},
            )
        # Same containment discipline as the anchor check just above, over a
        # DIFFERENT engine-generated block: a plan that grants anything must
        # show the reader exactly what, in the SAME rendering the approval ask
        # is assembled from (never a later, easy-to-skip detail view).
        if plan_has_any_grants(doc):
            full_grants_block = render_plan_grants(doc, fmt="full")
            if full_grants_block.strip() not in text:
                return Directive(
                    False, state.node, "noop",
                    "full rendering omits the grants block; stamping nothing — "
                    "paste this block into it verbatim and re-run",
                    data={"grants_block": full_grants_block},
                )

    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # The scope-coverage block must be IN the essence — checked the same
        # mechanical way the `full` branch above checks stage anchors (containment
        # of engine-generated lines, never a read of the essence's own prose).
        # A COURTESY, not the authority: plugins_premise.premise_blockers re-checks
        # it at `approve`. It lives here because the essence is emitted as a turn's
        # FINAL text message, so discovering the omission at `approve` would cost
        # the whole present -> timer -> ask cycle — hence: stamp NOTHING, hand the
        # block back verbatim to paste. Silent when the premise plugin is not armed
        # (no order bag exists, and the gate half is equally silent) or when plan
        # presentation is inactive.
        bag = state.plugins.get("premise")
        if bag is not None and gates.plan_presentation_active(state):
            block = plugins_premise.coverage_block(state, bag)
            missing = plugins_premise.coverage_block_missing_lines(block, text)
            if missing:
                return Directive(
                    False, state.node, "noop",
                    "essence rendering omits the scope-coverage block; stamping "
                    "nothing — paste these lines into it verbatim and re-run: "
                    + "; ".join(missing),
                    data={"coverage_block": block, "missing_lines": missing},
                )
        # Same containment discipline, this time over the COMPACT grants
        # projection — the essence is the approval ask's raw material, so a
        # plan that grants anything must show the reader what, right here,
        # not only in the on-request `full`/`plan-grants` detail view.
        try:
            _grants_doc = load_plan(target)
        except (OSError, PlanError):
            _grants_doc = None  # surfaced already by the coverage/anchor checks above
        if _grants_doc is not None and plan_has_any_grants(_grants_doc):
            compact_grants_block = render_plan_grants(_grants_doc, fmt="compact")
            if compact_grants_block.strip() not in text:
                return Directive(
                    False, state.node, "noop",
                    "essence rendering omits the grants block; stamping nothing — "
                    "paste this block into it verbatim and re-run",
                    data={"grants_block": compact_grants_block},
                )

        # Refuse to stamp a receipt for a plan `approve` would refuse on premise
        # grounds — reverses the #60 "present, then discover at approve"
        # ordering (incident 2026-09-28: an essence was stamped with 12 raised
        # candidates, approve then refused). Runs after the coverage/grants
        # containment checks above so their data payloads survive on a refusal,
        # and excludes the essence-coverage half (include_essence_coverage=
        # False): that half compares this receipt against itself, which is
        # circular before it exists — approve's own gating stays unfiltered.
        if bag is not None and gates.plan_presentation_active(state):
            try:
                _premise_blk = plugins_premise.premise_blockers(
                    state, bag, include_essence_coverage=False
                )
            except (OSError, PlanError):
                _premise_blk = []  # surfaced already by the coverage/grants checks above
            if _premise_blk:
                return Directive(
                    False, state.node, "noop",
                    "cannot present essence: premise blockers stand — dispose "
                    "every open question and undispositioned order element, "
                    "same routes `approve` prints — stamping nothing",
                    data={"blockers": _premise_blk},
                )

    # grants_sha256 over whatever doc this branch already loaded (full/essence);
    # replan_diff loads its own, since neither branch above populated one for it.
    # A load failure here leaves the digest None rather than raising — the same
    # fail-closed posture PlanPresentation.grants_sha256 documents, and consistent
    # with essence's own tolerance of a failed `_grants_doc` load above.
    if kind == PLAN_PRESENTATION_KIND_FULL:
        _sha_doc = doc
    elif kind == PLAN_PRESENTATION_KIND_ESSENCE:
        _sha_doc = _grants_doc
    else:
        try:
            _sha_doc = load_plan(target)
        except (OSError, PlanError):
            _sha_doc = None
    presented_grants_sha256 = grants_sha256(_sha_doc) if _sha_doc is not None else None

    # Writer-pass gate: the rendering's bytes must be bound to a tech-writer
    # witness in this session's transcript — the published-text hook's table
    # (hook-published-text-writer-gate.py `_decide_text`) minus its content-rule
    # judge, which checks ticket-text publication rules, not a plan rendering.
    # Last, so a rendering refused anyway costs no writer pass.
    writer_gate: dict
    if os.environ.get(writer_pass.OVERRIDE_ENV) == "0":
        writer_gate = {"outcome": "override"}
    else:
        transcript_path = _present_plan_transcript(state.session_id)
        if transcript_path is None:
            writer_gate = {"outcome": "no_transcript"}
        else:
            binding = writer_pass.bind(text, transcript_path)
            writer_gate = {
                "outcome": "bound", "strength": binding.strength,
                "evidence": binding.evidence, "transcript": str(transcript_path),
            }
            if binding.strength == writer_pass.UNREADABLE:
                writer_gate["outcome"] = "unreadable"
            elif binding.strength in writer_pass.REFUSING_STRENGTHS:
                return Directive(
                    False, state.node, "noop",
                    f"rendering is not bound to a tech-writer witness in {transcript_path}: "
                    f"{binding.strength} — run the tech-writer pass on these exact bytes "
                    "(Skill tech-writer, or spawn-specialist.py --kind tech-writer) and "
                    "rewrite the whole rendering file with Write after it — an Edit fragment "
                    f"never binds the whole body; {writer_pass.OVERRIDE_ENV}=0 overrides",
                    data={"writer_gate": {**writer_gate, "outcome": "refused"}},
                )
            elif binding.strength not in (writer_pass.WRITER_OUTPUT, writer_pass.POST_WITNESS):
                writer_gate["outcome"] = "allowed"
    state.log("present_plan_writer_gate", outcome=writer_gate["outcome"], kind=kind)

    def _with_writer_gate(directive: Directive) -> Directive:
        directive.data["writer_gate"] = writer_gate
        if writer_gate["outcome"] == "override":
            directive.detail = (
                f"writer gate OVERRIDDEN ({writer_pass.OVERRIDE_ENV}=0) — name this to the "
                f"user in the presenting turn. {directive.detail}"
            )
        return directive

    presentation = PlanPresentation(
        plan_path=target,
        kind=kind,
        plan_sha256=_plan_file_sha256(target),
        rendering_sha256=hashlib.sha256(raw).hexdigest(),
        rendering_text=text,
        presented_ts=time.time(),
        grants_sha256=presented_grants_sha256,
    )
    _record_plan_presentation(state, presentation)
    # D9 capture: what the user said when sending a PRIOR presentation of this plan
    # back (a correction or rejection) — the one thing present_plan's own receipt
    # never had anywhere to record, since the text belongs to the user's reply, not
    # to the rendering being stamped now. Optional and free-text; its absence (the
    # ordinary case — a first presentation, or a clean approval) is not a refusal.
    state.log("present_plan", plan=target, kind=kind,
              rendering_sha256=presentation.rendering_sha256,
              rejection_text=getattr(args, "rejection_text", None))
    _deliver_presentation_via_channel(state, presentation, raw)
    store.save(state)

    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # essence is the only kind that feeds an approval ask (full is
        # on-request detail, skeleton stamps nothing), so it alone gets the
        # full arm-timer/final-text/ask-next-turn choreography — spelled out
        # here, at the point the coordinator is guaranteed to read it, rather
        # than left to forgettable prose in a memory leaf it may never have
        # loaded (ask-user-question-split-turn.md).
        next_steps = [
            "arm a `sleep 2` background timer now (atomic with deferring the ask)",
            "emit THIS exact rendering as the turn's FINAL text message — zero "
            "tool calls after it",
            "next turn, open directly with the approval AskUserQuestion (zero "
            "preceding text) carrying an option whose label or description "
            f"embeds the literal marker {SHOW_FULL_PLAN_MARKER!r}",
        ]
        detail = (
            "presentation receipt recorded (kind=essence). Next: "
            + " Then, ".join(f"({i}) {step}" for i, step in enumerate(next_steps, 1))
        )
        data = {
            "rendering_sha256": presentation.rendering_sha256,
            "plan_sha256": presentation.plan_sha256,
            "show_full_plan_marker": SHOW_FULL_PLAN_MARKER,
            "next_steps": next_steps,
        }
        override = _agent_review_override(state)
        if override is not None:
            data["agent_review_override"] = override
        return _with_writer_gate(Directive(True, state.node, "continue", detail, data=data))

    if kind == PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # Mirrors the essence choreography above exactly, substituting the
        # replan-authorization marker for the approval one — this is the
        # rendering a non-substantive replan's diff-authorization ask is
        # assembled from, gated by gates.replan_authorization_blockers.
        next_steps = [
            "arm a `sleep 2` background timer now (atomic with deferring the ask)",
            "emit THIS exact rendering as the turn's FINAL text message — zero "
            "tool calls after it",
            "next turn, open directly with the replan-authorization "
            "AskUserQuestion (zero preceding text) carrying an option whose "
            f"label or description embeds the literal marker {AUTHORIZE_REPLAN_MARKER!r}",
        ]
        detail = (
            "presentation receipt recorded (kind=replan_diff). Next: "
            + " Then, ".join(f"({i}) {step}" for i, step in enumerate(next_steps, 1))
        )
        data = {
            "rendering_sha256": presentation.rendering_sha256,
            "plan_sha256": presentation.plan_sha256,
            "authorize_replan_marker": AUTHORIZE_REPLAN_MARKER,
            "next_steps": next_steps,
        }
        return _with_writer_gate(Directive(True, state.node, "continue", detail, data=data))

    return _with_writer_gate(Directive(
        True, state.node, "continue",
        f"presentation receipt recorded (kind={kind}); emit this exact rendering "
        "as the turn's FINAL text message so the delivery hook can verify it "
        "actually reached the user",
        data={"rendering_sha256": presentation.rendering_sha256, "plan_sha256": presentation.plan_sha256},
    ))


def cmd_confirm_delivery(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The HUMAN escape from the delivery-stamp half of the plan-presentation
    gate — used when the delivery hook is disabled, uninstalled, or otherwise
    cannot fire (see gates.plan_presentation_blockers's fail-CLOSED
    justification: without a reachable escape, a dead hook would brick every
    substantive session at PLAN_READY forever — the bypass-trainer shape a
    fail-closed gate must never produce).

    NOT a security boundary: anyone who can run this can already run
    `approve --by <name>` directly, exactly as with plan-review overrides — it
    exists for AUDIT-TRAIL hygiene (a named actor and a stated reason for the
    manual override), never to gate WHO may approve. cmd_present_plan and the
    delivery hook must never call this themselves; it is reachable only as a
    human-initiated command, enforced by rejecting `--by hook` outright.

    `--kind` (default essence) selects WHICH presentation receipt this stamp
    binds to — essence/full back the plan-approval gate
    (gates.plan_presentation_blockers), replan_diff backs the replan-
    authorization gate (gates.replan_authorization_blockers). Without this,
    the replan_diff gate would have no reachable escape at all: the same
    disabled/uninstalled-hook brick this command exists to prevent for
    approval would apply to every non-substantive replan on such a machine.
    """
    state = _require(store, args.session)
    kind = getattr(args, "kind", None) or PLAN_PRESENTATION_KIND_ESSENCE
    if kind not in PLAN_PRESENTATION_KINDS:
        return Directive(
            False, state.node, "noop",
            f"unknown presentation kind {kind!r}; expected one of {PLAN_PRESENTATION_KINDS}",
        )
    receipt = gates._plan_presentation_for(state, kind)
    if receipt is None:
        return Directive(
            False, state.node, "noop",
            f"no {kind} presentation receipt exists yet — run present-plan "
            f"--kind {kind} before confirm-delivery has anything to bind to",
        )
    by = (getattr(args, "by", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    escape_reason = (getattr(args, "escape_reason", "") or "").strip()
    missing = []
    if not by:
        missing.append("--by")
    if not note:
        missing.append("--note")
    if not escape_reason:
        missing.append("--escape-reason")
    if missing:
        return Directive(
            False, state.node, "noop",
            f"confirm-delivery requires {' and '.join(missing)} (a named actor, "
            "a typed reason from a closed set, and a stated explanation for the "
            "manual override)",
        )
    if escape_reason not in delivery.DELIVERY_ESCAPE_REASONS:
        # Refused in the body rather than by argparse `choices=`: the enum is
        # also enforced when a caller builds the namespace directly, and the
        # refusal joins the aggregate above instead of exiting the process.
        return Directive(
            False, state.node, "noop",
            f"--escape-reason {escape_reason!r} is not one of "
            f"{', '.join(delivery.DELIVERY_ESCAPE_REASONS)} — an untyped reason "
            "is exactly what makes escapes uncountable",
        )
    if by.lower() in (delivery.SOURCE_HOOK, delivery.SOURCE_CHANNEL):
        return Directive(
            False, state.node, "noop",
            f"--by {by.lower()} is reserved for the automated delivery source; a human "
            "override must name an actual person",
        )
    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    if state_file is None:
        return Directive(
            False, state.node, "noop",
            "cannot resolve this session's state file — nowhere to write the "
            "delivery stamp sidecar",
        )
    stamp = delivery.DeliveryStamp(
        plan_path=receipt.plan_path,
        plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256,
        verified_ts=time.time(),
        source=delivery.SOURCE_OVERRIDE,
        by=by,
        note=note,
        escape_reason=escape_reason,
    )
    delivery.write_stamp(state_file, stamp)
    state.log("confirm_delivery", by=by, note=note, escape_reason=escape_reason, kind=kind)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"delivery override recorded by {by!r}; the {kind} presentation's gate "
        "is now satisfied for this receipt",
    )


def register(sub, add):
    sp = add("classify"); sp.add_argument("--session", required=True)
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
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); "
                         "required (or ambient-detectable) at classify — sticky thereafter")

    sp = add("plan"); sp.add_argument("--session", required=True)
    sp = add("plan-render"); sp.add_argument("--plan", required=True,
        help="TOML plan to render to a markdown prose view on demand (a projection, "
             "never written to disk — the TOML is the single source of truth)")
    sp.add_argument("--stage", type=plan_render_stage_arg_type, default=None,
        help="render only this stage index, or a comma-separated list of stage "
             "indices (e.g. '3,5'), as a brief projection instead of the whole "
             "plan (the spawn prompt's per-dispatch projection, or a reviewer's "
             "scoped delta)")
    # Accept (and ignore) --session so the harness-session auto-injection
    # (_inject_default_session) is a no-op here: rendering is a pure, session-free
    # read of the plan file, unlike every other verb which drives session state.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-grants"); sp.add_argument("--plan", required=True,
        help="TOML plan whose per-stage grant set (declared + derived) to render "
             "on demand (a projection, never written to disk)")
    sp.add_argument("--format", choices=["compact", "full", "json"], default="compact")
    # Session-free read, same reason as plan-render's suppressed --session above.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-resources"); sp.add_argument("--plan", required=False, default=None,
        help="TOML plan whose per-stage typed resources (declared + derived rules, "
             "add_dirs, spawn/landed shape) to resolve on demand -- plan mode")
    sp.add_argument("--format", choices=["compact", "full", "json"], default="compact")
    sp.add_argument("--corpus", nargs="+", default=None,
        help="corpus mode: directory/ies to scan for *.toml plans, resolving every "
             "declared+derived Bash rule found through tool_contracts.resolve_command")
    sp.add_argument("--commands-file", default=None,
        help="corpus mode: a file of literal command lines (one per line, '#'-comments "
             "skipped) to resolve directly, in addition to any --corpus scan")
    sp.add_argument("--dump-commands", default=None,
        help="corpus mode: write every collected command line to this file")
    sp.add_argument("--report-json", default=None,
        help="corpus mode: write the full resolution report (JSON) to this file")
    sp.add_argument("--require-no-unknown-program", action="store_true", default=False,
        help="corpus mode: fail (non-ok Directive) if any command resolves to "
             "reason_class=unknown-program")
    # Session-free read, same reason as plan-render's suppressed --session above.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-digest"); sp.add_argument("--plan", required=True,
        help="TOML plan whose root digest, verified references and per-stage closure "
             "digests to report (read-only)")
    sp.add_argument("--stage", type=int, default=None,
        help="report the closure digest of this stage only")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-externalize"); sp.add_argument("--plan", required=True,
        help="TOML plan holding the field to move into its own hash-pinned file")
    sp.add_argument("--stage", type=int, default=None,
        help="stage index of the field named by --field")
    sp.add_argument("--field", default=None, help="string field of the stage to externalize")
    sp.add_argument("--meta-field", dest="meta_field", default=None,
        help="string field of [meta] to externalize")
    sp.add_argument("--order-field", dest="order_field", default=None,
        help="string field of [meta.order] to externalize")
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-seal"); sp.add_argument("--plan", required=True,
        help="TOML plan whose reference hashes to recompute bottom-up after an edit")
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-convert"); sp.add_argument("--plan", required=True,
        help="single-file TOML plan to convert into a hypertext tree (read-only)")
    sp.add_argument("--out", required=True,
        help="directory that receives the tree; must not exist or must be empty")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-convert-live"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", required=True, choices=["agent", "user"],
        help="who converts; recorded in the conversion link and disclosed")
    sp.add_argument("--revert", action="store_true",
        help="restore the plan bytes recorded before the last conversion (the parts stay on disk)")
    sp = add("submit-plan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp = add("present-plan"); sp.add_argument("--session", required=True)
    sp.add_argument("--kind", choices=list(PLAN_PRESENTATION_KINDS), default=PLAN_PRESENTATION_KIND_ESSENCE,
                    help="essence = free-form summary, no completeness check; "
                         "full = every [stage N] anchor required, stage-enumerated; "
                         "replan_diff = proposed-diff rendering for a non-substantive "
                         "replan against a candidate plan file (see --plan)")
    sp.add_argument("--plan", default=None,
                    help="candidate plan file the presentation is stamped against; "
                         "only legal with --kind replan_diff (defaults to the session's "
                         "current plan_path there too), refused for essence/full which "
                         "always target the session's own plan")
    sp.add_argument("--rendering-file", dest="rendering_file", default=None,
                    help="file containing the exact bytes shown to the user")
    sp.add_argument("--emit-skeleton", dest="emit_skeleton", action="store_true",
                    help="print the [stage N] anchor scaffold for a `full` rendering; "
                         "stamps nothing")
    sp.add_argument("--rejection-text", dest="rejection_text", default=None,
                    help="what the user said when sending a PRIOR presentation of this "
                         "plan back (a correction or rejection), captured for history "
                         "only; omit on a first presentation or a clean approval — never "
                         "required")
    sp = add("confirm-delivery"); sp.add_argument("--session", required=True)
    sp.add_argument("--kind", choices=list(PLAN_PRESENTATION_KINDS), default=PLAN_PRESENTATION_KIND_ESSENCE,
                    help="which presentation's delivery this override confirms — must "
                         "match the --kind used at present-plan time")
    sp.add_argument("--by", required=True,
                    help="the human who confirms delivery (must not be 'hook')")
    sp.add_argument("--note", required=True,
                    help="why the automated delivery hook could not verify this itself")
    sp.add_argument("--escape-reason", dest="escape_reason", required=True,
                    help="which of " + "/".join(delivery.DELIVERY_ESCAPE_REASONS) +
                         " this escape is — the countable half; --note stays the "
                         "explicable half")
