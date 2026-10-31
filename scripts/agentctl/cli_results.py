"""Stage-result and landing recording subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import os
import time
from . import advisor, cost, effort, gates, runtime_host
from .checkrun import NEGATIVE_CONTROL_REFUSED_EXIT_CODES
from .config import Thresholds
from .directive import Directive
from .dispatch import Runner, subprocess_runner
from .machine import transition
from .state import (
    CheckKind,
    CONTENT_WAIVER_ASK_MARKER,
    CriterionType,
    JudgeBypass,
    Node,
    StageReview,
    StageStatus,
    WeightClass,
)
from .store import StateStore
from .cli_common import (
    OBSERVATION_CONTRACT,
    _digest,
    _observation_sha256,
    _record_stage_review,
    _record_bypass,
    _effort_cross_totals,
    _flush_order_effort,
    _record_effort_fire,
    _open_difficulty,
    _difficulty_cap_directive,
    _attach_advisories,
    _run_check,
    _verify_command_result,
    _VenueSnapshot,
    _venue_tree_identity,
    _check_definition_digest,
    _cached_check_hit,
    _resolve_or_refuse,
    _diagnose_effort_divergence,
    _freeze_delivered_head,
    _landed_spec_for_stage,
    _needs_delivered_head_freeze,
    _landed_check_result,
    _content_waiver_blocker,
    _require,
    _finished_work_fire,
    _finished_work_data,
    _cost_rows,
    _refresh_spend,
    _utcnow,
    _effort_now,
    _write_check_log,
)


def cmd_record_landing(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the landing of stage `--stage`: `--head` is the trunk tip the landing
    pushed, `--base` the trunk tip the delivery was rebased onto. The git landed check
    then proves the delivery from that range (SessionState.render_landed_command) before
    its frozen-head path. Every call resets the content waiver to `--accept-content-loss`
    (or None) and logs a stamped `record_landing` event; a refusal changes nothing."""
    state = _require(store, args.session)

    def refuse(reason: str) -> Directive:
        return Directive(False, state.node, "noop", f"record-landing refused: {reason}")

    try:
        stage = state.stage(args.stage)
    except KeyError:
        return refuse(f"stage {args.stage} does not exist")
    spec = _landed_spec_for_stage(state, args.stage)
    if spec is None or spec.provider != "git":
        return refuse(f"no git landed check names stage {args.stage} as its delivered_stage")
    if not state.repo_root:
        return refuse("[meta].repo_root is not set")
    reason = getattr(args, "accept_content_loss", None)
    if reason is not None and not reason.strip():
        return refuse("--accept-content-loss needs a non-empty reason")
    run = runner or subprocess_runner
    resolved = {}
    for name, sha in (("head", args.head), ("base", args.base)):
        result = run(["git", "-C", state.repo_root, "rev-parse", "--verify", "--quiet",
                      f"{sha}^{{commit}}"])
        if result.returncode != 0 or not result.stdout.strip():
            return refuse(f"--{name} {sha!r} is not a commit in {state.repo_root}")
        resolved[name] = result.stdout.strip()
    head, base = resolved["head"], resolved["base"]
    ancestry = run(["git", "-C", state.repo_root, "merge-base", "--is-ancestor", base, head])
    if head == base or ancestry.returncode != 0:
        return refuse(f"--base {base} is not a proper ancestor of --head {head}")
    if reason is not None:
        blocker = _content_waiver_blocker(state, args.stage)
        if blocker:
            return refuse(blocker)
        reason = reason.strip()
    stage.outcome.landed_head = head
    stage.outcome.landed_base = base
    stage.outcome.landed_content_waiver = reason
    state.log("record_landing", stage=args.stage, head=head, base=base,
              content_waiver=reason, at=time.time())
    store.save(state)
    detail = f"stage {args.stage}: landing {base[:12]}..{head[:12]} recorded"
    if reason:
        detail += f"; the content fact is waived by the user: {reason}"
    return Directive(True, state.node, "continue", detail,
                     data={"stage": args.stage, "landed_head": head, "landed_base": base,
                           "landed_content_waiver": reason})


def cmd_record_result(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to record")
    # A replan re-arms the stage as PENDING at VERIFYING; recording then would log an
    # attempt and only fail at the EXECUTING -> VERIFYING transition, with no way out named.
    if state.node != Node.EXECUTING.value or stage.outcome.status != StageStatus.ACTIVE.value:
        return Directive(
            False, state.node, "next_stage",
            f"stage {stage.index} is {stage.outcome.status} at node={state.node}; "
            "run `agentctl next-stage` first, then record-result",
        )
    # Before any mutation: a refused PASS leaves the state file exactly as it was, so
    # the obligation is read from the files' bytes on the retry as on this call.
    if args.status == "passed":
        renorm_block = gates.pending_renorm_stage_blockers(state, stage.index)
        if renorm_block:
            return Directive(
                False, state.node, "renorm",
                f"stage {stage.index} cannot pass: it is the re-norming stage of a "
                "normative difficulty and the norm file is unchanged",
                data={"blockers": renorm_block})
    actual = args.actual or ""
    stage.outcome.actual = actual
    passed = args.status == "passed"
    # Set when the mechanical check below fails for a reason worth naming to a
    # later overcome-difficulty pass without scanning state.history — currently
    # only "control_not_discriminating" (R2's negative control matched the
    # stage's expected_exit on a known-bad input).
    failure_kind: str | None = None

    # Unconditional attempt log: every record-result call for this stage
    # counts and logs, including one a later gate blocks before any verification
    # runs — a diagnosing session scanning history must see every attempt made,
    # not just the ones that got past the first gate.
    stage.outcome.record_attempts = (stage.outcome.record_attempts or 0) + 1
    state.log(
        "record_result_attempt", stage=stage.index,
        attempt=stage.outcome.record_attempts, status=args.status,
    )

    # General control-criterion attestation: optional on any stage, but required
    # non-empty for spawn:developer + passed (review is the control criterion of a
    # developer-actor stage; reviewer ⊂ controller, developer ⊂ executor).
    control = getattr(args, "control", None) or None
    if control:
        stage.control = control
    if passed and stage.needs_control() and not stage.has_control():
        store.save(state)
        return Directive(
            False, state.node, "attest_control",
            f"stage {stage.index} is a spawn:developer stage; the control criterion of a "
            "developer-produced result is review — supply it via: "
            "record-result --control '<how the code was reviewed>'",
        )

    # Code-review gate: recording a PASSED spawn:developer stage additionally requires
    # a bound passing (or user-overridden) CodeReview when the gate is active
    # (substantive session / AGENTCTL_CODE_REVIEW=1) — the SUBSTANTIVE-only structured
    # upgrade layered after the free-text control floor above (Op-Q5: the two stay
    # independent, so a knob-off/non-substantive stage's behaviour is unaffected).
    # Unlike the acceptance path there is no automated judge here — the verdict is
    # always code-reviewer/human-authored via `agentctl code-review`; this fold-in only
    # checks the pure gates.code_review_blockers, which reads solely the recorded
    # CodeReview. Placed BEFORE verify_command execution + the node transition so a
    # blocked pass never advances past EXECUTING.
    code_ref = getattr(args, "code_ref", None) or None
    if passed and stage.needs_control() and gates.code_review_active(state):
        crb = gates.code_review_blockers(
            state, stage, expected_code_sha256=_digest(code_ref) if code_ref else None,
        )
        if crb:
            store.save(state)
            return Directive(
                False, state.node, "spawn_code_review",
                f"stage {stage.index} spawn:developer pass blocked by the code-review gate — "
                "spawn the `code-reviewer` specialization to review this stage's diff, then "
                "record with `agentctl code-review --session <sid> --verdict pass|revise|override "
                "--reviewer code-reviewer [--code-ref <rev>]`, then re-run record-result",
                data={"blockers": crb},
            )

    # Observation gate: recording a PASSED stage requires a non-empty observation that
    # differs (normalized) from the expected image — CONTROL comparing the actual RESULT
    # against the stage's own goal, not just a program's exit code. Originally scoped to
    # acceptance_review stages only; broadened (Defect 2) to every stage of a SUBSTANTIVE
    # session, because "the result was checked against the goal" is a claim every stage
    # makes, not a criterion-type-specific one — a measurable stage's exit-code check
    # (further below) proves the PROGRAM ran clean, never that anyone looked at what it
    # produced. Non-substantive sessions (chat/small-change) keep the pre-Defect-2
    # behaviour: only acceptance_review stages pay this cost. An echoed target ("I saw
    # the expected result") is no observation at all.
    observation = getattr(args, "observation", None) or ""
    is_acceptance_review = stage.criterion.criterion_type == CriterionType.ACCEPTANCE_REVIEW.value
    requires_observation = is_acceptance_review or (
        state.weight_class == WeightClass.SUBSTANTIVE.value
    )
    if passed and requires_observation:
        norm_obs = gates._normalize_string(observation)
        norm_img = gates._normalize_string(stage.subject.result)
        reason = (
            "is acceptance_review" if is_acceptance_review
            else "is a substantive-session stage (Defect 2: control compares result "
                 "with goal at every stage)"
        )
        if not norm_obs:
            store.save(state)
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} {reason}; pass requires recording an observation — "
                f"{OBSERVATION_CONTRACT} "
                "(supply: record-result --observation '<what you observed>')",
            )
        if norm_obs == norm_img:
            store.save(state)
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} {reason}; pass requires recording an observation, "
                "not echoing the target — "
                f"{OBSERVATION_CONTRACT} "
                "(supply: record-result --observation '<what you observed>')",
            )
        # bind the observation to the stage now so the judge gate's sha recompute
        # (run below, AFTER the mechanical check) sees it.
        stage.criterion.observation = observation

    # Delivered-head freeze: stamp what commit this stage delivered BEFORE any
    # verification below dispatches — a landed check self-referencing this same
    # stage must find its own frozen head already present (see
    # _freeze_delivered_head). Only when some landed check in the plan actually
    # names this stage as its delivered_stage (_needs_delivered_head_freeze) —
    # a plan with no landed check makes no extra runner call, unchanged from
    # before schema 23.
    if _needs_delivered_head_freeze(state, stage.index):
        _freeze_delivered_head(state, stage, runner)

    # Machine-executed verification: for a measurable stage carrying a verify_command
    # (or a `kind = "landed"` check), the engine runs it and OVERRIDES a 'passed'
    # claim the command contradicts. A contradicted pass becomes a real failure
    # (digest + DIAGNOSING), so "report honestly" is an invariant for the
    # measurable subset, not a discipline. This runs BEFORE the cheap judge
    # below — a check the command itself contradicts must never spend a judge call.
    if passed:
        crit = stage.criterion
        if crit.criterion_type == CriterionType.MEASURABLE.value and crit.verify_kind == CheckKind.LANDED.value:
            ok, refusal, result = _landed_check_result(state, crit.landed, runner)
            if refusal:
                store.save(state)
                return Directive(
                    False, state.node, "fix_venue",
                    f"stage {stage.index} landed check refused: {refusal}",
                )
            if not ok:
                passed = False
                note = (
                    f"landed check: delivered commit not (yet) contained in "
                    f"{crit.landed.target!r} (or {crit.landed.remote}/{crit.landed.target})"
                )
                actual = (actual + "\n" + note) if actual else note
                stage.outcome.actual = actual
        else:
            cwd = None
            has_check = crit.verify_command and crit.criterion_type == CriterionType.MEASURABLE.value
            if has_check:
                cwd, refusal = _resolve_or_refuse(state, crit.verify_venue)
                if refusal:
                    store.save(state)
                    return Directive(
                        False, state.node, "fix_venue",
                        f"stage {stage.index} verify_command refused: {refusal}",
                    )
            # Green-check cache: a repeated record-result call whose venue tree
            # hasn't moved AND whose check definition hasn't changed since the last
            # fully-green run (the positive command AND, if declared, its
            # negative_control) skips re-running BOTH commands and goes straight to
            # the judge below. checked_tree_ok is only ever set True once the WHOLE
            # check has cleared -- see state.py's Outcome docstring -- so a cache hit
            # never needs to re-prove the control discriminates: the tree it
            # discriminated against hasn't moved, under the same definition it
            # discriminated with. A red or non-discriminating result is never
            # cached, so a genuinely broken check or control is always re-run
            # rather than trusted to still be broken. Only computed when there is
            # an actual check to cache (measurable + verify_command) — an
            # acceptance_review stage's verify_command, if present, is never
            # machine-run, so identity must never be probed for it either. The
            # tree half assumes the check reads only the venue tree (see
            # _venue_tree_identity) — a check that also reads files outside it
            # (the plan TOML, proposals.json, ...) can be served stale even though
            # its own venue is unchanged. The definition half
            # (_check_definition_digest) is what catches an edited verify_command
            # or negative_control on an otherwise-unmoved tree (R2 review B) — the
            # plan-refresh path (_apply_refined_stage_fields) resets a PASSED
            # stage's own status on any carry-key change, but never touched these
            # two fields, so an unchanged tree + an edited control used to replay
            # a stale cache hit straight past the (now different) control.
            venue_snapshot = _VenueSnapshot()
            tree_identity = _venue_tree_identity(cwd, runner, venue_snapshot) if has_check else None
            identity = (
                f"{tree_identity}:{_check_definition_digest(crit)}"
                if tree_identity is not None else None
            )
            cache_hit = _cached_check_hit(stage, identity)
            if cache_hit:
                ok, result = True, None
            else:
                ok, result = _verify_command_result(stage, runner, cwd=cwd)
            if not ok:
                passed = False
                note = (
                    f"verify_command exit {result.returncode} != expected "
                    f"{stage.criterion.expected_exit}: {stage.criterion.verify_command}"
                )
                if result.stdout or result.stderr:
                    note += _write_check_log(
                        state.session_id, f"stage-{int(stage.index)}", "verify_command",
                        crit.verify_command, cwd or os.getcwd(), result.returncode,
                        result.stdout, result.stderr,
                    )
                actual = (actual + "\n" + note) if actual else note
                stage.outcome.actual = actual
                if identity is not None:
                    stage.outcome.checked_tree_identity = identity
                    stage.outcome.checked_tree_sha = venue_snapshot.tree
                    stage.outcome.checked_tree_ok = False
            elif not cache_hit and has_check and crit.negative_control:
                # The positive check just went green -- now prove it CAN go red.
                # Fed the known-bad input, a matching exit code means the check
                # never discriminated bad state from good in the first place, so
                # the stage does not pass even though the positive check did.
                neg_matched, neg_result = _run_check(
                    crit.negative_control, crit.expected_exit, runner, cwd=cwd,
                )
                if neg_result.returncode in NEGATIVE_CONTROL_REFUSED_EXIT_CODES:
                    # The control command itself never ran (typo, missing binary) —
                    # a fact about the exit code alone, not a judgment on whether the
                    # bad input was caught. Refuse like an unresolvable venue: never
                    # cache, never spend a verdict either way, tell the author to fix
                    # the command text.
                    store.save(state)
                    return Directive(
                        False, state.node, "fix_control",
                        f"stage {stage.index} negative_control refused: exit "
                        f"{neg_result.returncode} (command not found or not "
                        f"executable): {crit.negative_control}",
                    )
                if neg_matched:
                    passed = False
                    note = (
                        f"negative_control exit {neg_result.returncode} == expected "
                        f"{crit.expected_exit} (did not fail on bad input): "
                        f"{crit.negative_control}"
                    )
                    actual = (actual + "\n" + note) if actual else note
                    stage.outcome.actual = actual
                    failure_kind = "control_not_discriminating"
                    state.log(
                        "control_not_discriminating",
                        stage=stage.index, negative_control=crit.negative_control,
                    )
                    if identity is not None:
                        stage.outcome.checked_tree_identity = identity
                        stage.outcome.checked_tree_sha = venue_snapshot.tree
                        stage.outcome.checked_tree_ok = False
                else:
                    state.log("negative_control_discriminates", stage=stage.index)
                    if identity is not None:
                        stage.outcome.checked_tree_identity = identity
                        stage.outcome.checked_tree_sha = venue_snapshot.tree
                        stage.outcome.checked_tree_ok = True
            elif not cache_hit:
                # Neither branch above fired: no negative_control to run (a
                # waiver, or no check at all). A cache hit falls through this
                # whole chain untouched -- the tree and the check definition
                # already cleared on a prior call, so there is nothing left to
                # do before the judge below.
                if has_check and crit.negative_control_waiver:
                    state.log(
                        "negative_control_waived",
                        stage=stage.index, reason=crit.negative_control_waiver,
                    )
                if identity is not None:
                    stage.outcome.checked_tree_identity = identity
                    stage.outcome.checked_tree_sha = venue_snapshot.tree
                    stage.outcome.checked_tree_ok = True

    # Cheap-judge COGNITION + PURE gate (runs AFTER the mechanical check above, so
    # a check the command itself contradicts never spends a judge call).
    # When the acceptance-review gate is active (substantive session /
    # AGENTCTL_STAGE_REVIEW=1), run the fail-open haiku judge over the observation,
    # record its verdict as a StageReview bound to the observation bytes, then block
    # the pass on gates.acceptance_review_blockers (which reads that record AND any
    # fail_open JudgeBypass bound to the same observation). A judge call that itself
    # fails (disabled/errored/timed out) now records a `fail_open` JudgeBypass and
    # the pass proceeds — fail-open in effect, not just in name.
    if passed and requires_observation:
        if gates.stage_review_active(state):
            judge_runner = runner if runner is not None else advisor.subprocess_runner
            verdict, judge_reason = advisor.acceptance_judge(
                observation, stage.subject.result, judge_runner, enabled=True,
                timeout=advisor._ACCEPTANCE_JUDGE_TIMEOUT_S)
            obs_sha = _observation_sha256(observation)
            if verdict is not None:
                state.log(
                    "acceptance_judge_verdict", stage=stage.index, verdict=verdict,
                    reason=judge_reason, observation_sha256=obs_sha,
                )
                _record_stage_review(
                    state,
                    StageReview(
                        stage_index=stage.index, verdict=verdict,
                        reviewer=advisor.JUDGE_REVIEWER, note=judge_reason,
                        observation_sha256=obs_sha,
                    ),
                    from_judge=True,
                )
            else:
                # Fail-open: the judge CALL ITSELF failed (disabled/errored/timed out).
                # Record it as a bypass bound to THIS observation so the pure gate below
                # waves the pass through instead of stalling it indefinitely — a judge
                # outage is not evidence against the observation. Log the judge's own
                # reason so a session review can tell an outage apart from a genuine
                # revise even if the caller only looks at the recorded bypass. Skip the
                # recording entirely when a standing revise already binds to this exact
                # observation: the gate already refuses to let such a bypass override
                # that revise (gates.standing_revise_bound_to), so recording one here
                # would only add a misleading entry to any surface that lists bypasses
                # without also checking for the standing review.
                state.log("acceptance_judge_fail_open", stage=stage.index, reason=judge_reason)
                if not gates.standing_revise_bound_to(state, stage.index, obs_sha):
                    _record_bypass(state, JudgeBypass(
                        stage_index=stage.index, kind="fail_open", reviewer="",
                        note=judge_reason, observation_sha256=obs_sha))
            ab = gates.acceptance_review_blockers(state, stage)
            if ab:
                store.save(state)
                detail = f"stage {stage.index} acceptance pass blocked by the judge gate"
                data = {"blockers": ab}
                if verdict is None:
                    detail += f" (judge call failed: {judge_reason})"
                    data["judge_reason"] = judge_reason
                return Directive(
                    False, state.node, "attest_observation",
                    detail,
                    data=data,
                )
            # Cleared: if it cleared via an override, that is a bypass of a genuine
            # passing verdict — record it visibly (never cleared by a later review).
            rev = gates._stage_review_for(state, stage.index)
            if rev is not None and rev.verdict == gates._STAGE_REVIEW_OVERRIDE:
                _record_bypass(state, JudgeBypass(
                    stage_index=stage.index, kind="override",
                    reviewer=rev.reviewer, note=rev.note))
        elif (os.environ.get("AGENTCTL_STAGE_REVIEW") == "0"
              and state.weight_class == WeightClass.SUBSTANTIVE.value):
            # The gate WOULD apply to this substantive session but the kill switch
            # disabled it: the acceptance pass proceeds WITHOUT a judge verdict — record
            # the bypass so verify-final/resolve surface that this pass was unjudged.
            _record_bypass(state, JudgeBypass(
                stage_index=stage.index, kind="killswitch", reviewer="",
                note="AGENTCTL_STAGE_REVIEW=0"))

    # Read the cost log unconditionally — not just for is_spawn() stages — because
    # effort.refresh_spend (below) sums by plan_path alone and needs to see the
    # engine-mandated review spawns too, which attribute_stage's stage_index filter
    # would otherwise hide from this read.
    _rows = _cost_rows(args)
    # Attribute cost for spawn stages from the cost log. In-thread stages leave
    # None — cost splitting per in-thread stage is out of scope for this attribution.
    if stage.is_spawn():
        _attr = cost.attribute_stage(_rows, state.plan_path, stage.index)
        stage.outcome.cost_usd = _attr["cost_usd"]
        stage.outcome.duration_ms = _attr["duration_ms"]
        stage.outcome.spawn_count = _attr["spawn_count"]

    # Effort-divergence spend refresh (call site 1) + fire check. Accounting runs
    # unconditionally — only ACTING on a fire is gated by gates.effort_active — see
    # effort.py's module docstring and gates.effort_active's docstring.
    _refresh_spend(state, store, _rows, state.plan_path)
    _flush_order_effort(state, store=store)
    _now = _effort_now(store, state)
    div = effort.divergence(state, cross_session_totals=_effort_cross_totals(state, _now, store=store), now=_now)
    if effort.armed(state):
        _cross = _effort_cross_totals(state, _now, store=store)
        _ro_delta = effort.effective_deltas(state, cross_session_totals=_cross, now=_now)
        _ro_comparand = effort.comparands(state, Thresholds())
        for _ro_scale in effort.RECORD_ONLY_SCALES:
            if _ro_comparand[_ro_scale] > 0 and _ro_delta[_ro_scale] >= _ro_comparand[_ro_scale]:
                effort.record_crossing(
                    state, _ro_scale, actual=_ro_delta[_ro_scale],
                    estimate=_ro_comparand[_ro_scale], now=_utcnow(),
                )

    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING

    if passed:
        stage.outcome.status = StageStatus.PASSED.value
        if observation:
            stage.criterion.observation = observation
        state.current_stage = None
        state.pending_renorms = gates.open_renorms(state)
        state.log("record_result", stage=stage.index, status="passed")
        finished_fire = _finished_work_fire(state, div, store=store)
        if finished_fire is None and div is not None and gates.effort_active(state):
            now = _utcnow()
            fire = _record_effort_fire(state, div, now=now, store=store)
            return _diagnose_effort_divergence(state, store, div, fire)
        store.save(state)
        if state.all_stages_settled():
            d = Directive(True, state.node, "verify_final", f"stage {stage.index} passed; all stages passed",
                          data=_finished_work_data(state))
        else:
            d = Directive(True, state.node, "next_stage", f"stage {stage.index} passed; more stages ready")
        # Warn-only advisory: kept ONLY for the non-gated acceptance path. When the
        # judge gate is active it already paid a cheap judge over this same observation,
        # so re-running the sonnet advisory here would pay for the judgement twice; the
        # advisory survives as the fallback cognition when the gate is off (kill switch).
        if (stage.criterion.criterion_type == CriterionType.ACCEPTANCE_REVIEW.value
                and not gates.stage_review_active(state)):
            _attach_advisories(d, "acceptance_observation",
                               {"expected": stage.subject.result, "observation": observation},
                               runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
        return d

    # failed: loop guard — same stage failing twice on the same actual digest -> escalate
    dig = _digest(actual)
    repeat = dig in stage.outcome.fail_digests
    stage.outcome.fail_digests.append(dig)
    stage.outcome.status = StageStatus.FAILED.value
    state.log("record_result", stage=stage.index, status="failed", repeat=repeat)
    if repeat:
        store.save(state)
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} failed twice with same result digest; stop retrying",
            marker="ESCALATE",
            data={"failure_kind": failure_kind} if failure_kind else {},
        )
    # enter the overcome-difficulty sub-spine: a fresh Difficulty record must be
    # worked through (declare -> investigate -> critique) before replan is allowed.
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    opened = _open_difficulty(state)
    data = {"failure_kind": failure_kind} if failure_kind else {}
    if div is not None and gates.effort_active(state):
        # Already entering DIAGNOSING for the stage failure — attach the divergence
        # instead of re-transitioning or opening a second Difficulty, but still honor
        # divergence()'s CALLER OBLIGATION (record the fire so it doesn't re-trip).
        now = _utcnow()
        data["effort_divergence"] = _record_effort_fire(state, div, now=now, store=store)
    store.save(state)
    if opened is None:
        return _difficulty_cap_directive(state, f"stage {stage.index} failed", data)
    return Directive(
        False, state.node, "declare",
        f"stage {stage.index} failed; run overcome-difficulty — declare the divergence, "
        "then investigate, then critique; replan is blocked until the cycle is complete",
        marker="OVERCOME-DIFFICULTY",
        data=data,
    )


def register(sub, add):
    sp = add("record-landing"); sp.add_argument("--session", required=True)
    sp.add_argument("--stage", type=int, required=True,
                    help="delivered_stage of the git landed check the landing proves")
    sp.add_argument("--head", required=True, help="trunk commit the landing pushed")
    sp.add_argument("--base", required=True,
                    help="trunk commit the delivery was rebased onto (a proper ancestor of --head)")
    sp.add_argument("--accept-content-loss", dest="accept_content_loss", default=None,
                    help="user-confirmed reason to skip the content fact; needs an "
                         f"AskUserQuestion, asked after the latest record-landing of any stage, "
                         f"whose chosen option carries {CONTENT_WAIVER_ASK_MARKER!r}")
    sp = add("record-result"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], required=True)
    sp.add_argument("--actual", default="")
    sp.add_argument("--control", default=None,
                    help="control-criterion attestation (required for spawn:developer stages "
                         "when recording passed; accepted on any stage)")
    sp.add_argument("--observation", default="",
                    help=f"{OBSERVATION_CONTRACT} (required when recording passed on an "
                         "acceptance_review stage, or on any stage of a substantive session)")
    sp.add_argument("--code-ref", dest="code_ref", default=None,
                    help="for spawn:developer stages: the reviewed-code revision/digest, to "
                         "cross-check against the bound CodeReview's --code-ref (drift -> stale)")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
