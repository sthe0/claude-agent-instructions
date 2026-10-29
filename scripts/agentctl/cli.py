"""Subcommands: each maps one coordination step to a Directive.

This is the composition layer — it loads state (via the injected StateStore),
fires a machine transition, mutates state under the gate guardians, persists, and
returns a Directive. machine.py and classify.py stay pure; the side effects live
here behind two injectable seams (store, runner) so the whole CLASSIFIED..RESOLVED
cycle runs in tests with no filesystem-of-record and no `claude -p` spend.

Every command function has the signature cmd_x(args, *, store, runner) -> Directive.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from lib import argv_text, ask_evidence, config_root, engine_session_binding, hook_telemetry, kind_baselines, transcript_stops, transcript_turns, widening_targets, writer_pass

from . import advisor, carry_forward, check_cache, child_runs, continuations, controls, cost, delivery, delivery_channel, effort, enumerate_sidecar, exempt_paths, gates, grant_shadow, grants as _grants, ledger, mandate as mandate_rules, mandate_store, norm_handoff, order_approvals, permissions, plan_convert, plan_refs, plan_resources, plan_store, plugins, plugins_ledger, plugins_premise, premise, protected_actions, resources as _resources, runtime_host, solved_marker, task_accumulator, workspace_cleanup
from .checkrun import NEGATIVE_CONTROL_REFUSED_EXIT_CODES, format_observations, observe_stage_checks
from .classify import TRACKER_KEY_RE, Signals, classify
from . import config as agentctl_config
from .config import Thresholds
from .partition import render_section, render_units, verdict
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import (
    CHILD_EXHAUSTED,
    CHILD_INFRA_FAILURE,
    Runner,
    dispatch_stage,
    LABEL_DECORATION,
    permission_request_action,
    parse_marker,
    subprocess_runner,
)
from .machine import transition
from .plan import (
    CONDITION_MARKERS,
    META_PART,
    ConcernFormatError,
    PlanDoc,
    PlanError,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    acceptance_requirement_bindings,
    change_tier,
    changed_parts,
    check_venue_warnings,
    customer_question_defect,
    entries_grants_sha256,
    grants_sha256,
    load_plan,
    load_plan_with_digest,
    materialized_grant_entries,
    is_unit_id,
    order_digest,
    pair_binding,
    pair_content,
    pair_part_tokens,
    parse_concern,
    parse_pair,
    parse_unit,
    part_digest_map,
    plan_has_any_grants,
    plan_meta_digest,
    plan_meta_element_key,
    plan_interface_digests,
    plan_meta_element_keys,
    plan_stage_digests,
    refined_grant_entries,
    replan_class,
    review_ids,
    stage_norm_keys,
    stage_part,
    stage_question_key,
    stage_reattest_digest,
    unit_currency_hash,
    unit_node,
    verify_command_reachability_blockers,
    cost_tier_floor_advisories,
    final_check_duplicate_warnings,
    verify_command_scope_warnings,
    whole_suite_warnings,
    _derive_stage_grants,
    _venue_for,
)
from .render import (
    build_site_prompt,
    cmd_plan_grants,
    cmd_plan_render,
    plan_render_stage_arg_type,
    plan_summary_text,
    render_plan_grants,
    render_plan_md,
    render_stages_md,
)
from .submission import plan_size_blocks, plan_size_warnings, submission_advice, submission_violations
from .state import (
    _EXECUTION_NODES,
    _MAX_PLAN_STACK,
    Actor,
    AcceptanceBypass,
    AcceptanceReview,
    AGENT_ACTOR,
    AUTHORIZE_REPLAN_MARKER,
    CheckKind,
    CheckVenue,
    CodeReview,
    CONCERN_FIXED,
    CONCERN_OPEN,
    CONCERN_RECORDED,
    CONCERN_SUPERSEDED,
    ConcernRecord,
    CONTENT_WAIVER_ASK_MARKER,
    Critique,
    Criterion,
    CriterionType,
    Declaration,
    Difficulty,
    canonical_failure_address,
    canonical_normalization_destination,
    EFFORT_FIRE_FINISHED_WORK,
    FAILURE_ADDRESS_ALIASES,
    FAILURE_ADDRESS_VALUES,
    FinalCheck,
    Investigation,
    is_settled,
    is_skipped,
    JudgeBypass,
    LANDED_GIT_ERROR_EXIT,
    Partition,
    PartitionUnit,
    PARTITION_UNIT_MODES,
    GateRecord,
    Means,
    Node,
    Normalization,
    NORMALIZATION_DESTINATION_ALIASES,
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
    Outcome,
    PARENT_DIFFICULTY_ENV,
    parse_parent_difficulty,
    PermissionRequest,
    PLAN_PRESENTATION_KIND_ESSENCE,
    PLAN_PRESENTATION_KIND_FULL,
    PLAN_PRESENTATION_KIND_REPLAN_DIFF,
    PLAN_PRESENTATION_KINDS,
    PLAN_PRESENTATION_RENDERING_CAP_BYTES,
    PlanFrame,
    PlanPairReview,
    PlanPresentation,
    PlanReview,
    plan_review_concern_ids,
    plan_review_pair_scope,
    plan_review_scope_for_pair,
    plan_review_scope_for_stage,
    plan_review_scope_stage_index,
    ProtectedGrantAcceptance,
    ReattestStash,
    RequirementVerdict,
    RESOLUTION_ASK_MARKER,
    RiskAcceptance,
    Route,
    SessionState,
    SHOW_FULL_PLAN_MARKER,
    Stage,
    StageReview,
    StageStatus,
    Subject,
    WeightClass,
    landed_containment_script,
)
from .store import FileStateStore, StateStore, safe_session_id as _safe_session_id

from .cli_common import (
    REPO_ROOT,
    GATE_LOG,
    TASK_QUALITY_LOG,
    ESCALATION_LEDGER_ENV,
    _VALID_QUALITY_RATINGS,
    OBSERVATION_CONTRACT,
    _digest,
    _bash_rule_command_from_call,
    _plan_file_sha256,
    _plan_refs_problem,
    _plan_file_bytes,
    _observation_sha256,
    _normalize_reviewer_token,
    _record_stage_review,
    _record_code_review,
    _judge_bypassed_surface,
    _acceptance_bypass_surface,
    _record_bypass,
    _load_prior_plan,
    _write_plan_snapshot,
    _write_plan_version_snapshot,
    _read_plan_version_snapshot,
    _snapshot_approved_plan,
    _refresh_approved_grant_snapshot,
    _approved_snapshot_doc,
    _bind_approved_grants,
    _replan_baseline_path,
    _ORDER_EFFORT_SCALES,
    _ledgered_order_key,
    _effort_cross_totals,
    _order_actual,
    _flush_order_effort,
    _clear_open_fires_if_customer,
    _open_effort_fire,
    _record_effort_fire,
    _difficulty_id,
    NESTED_DIFFICULTY_ORIGINS,
    _open_difficulty,
    _difficulty_cap_directive,
    _archive_difficulty,
    _adopt_parent_difficulty,
    _adopt_norm_handoff,
    _loop_self_check,
    _agent_self_decision_refusal,
    _record_backstop_crossing,
    _crossing_data,
    _backstop_crossings,
    _disclose_backstop_crossings,
    _disclose_agent_plan_conversions,
    _plan_review_release_active,
    _sync_release_anchor,
    _agent_fire_ack_refusal,
    _self_approve_detail,
    _autonomy_for,
    _record_customer_stage_grants,
    _agent_review_override,
    _settle_override_decision,
    _TRIGGER_EXIT_FRESH_PASS,
    _drop_agent_overrides,
    _plan_review_override_refusal,
    _review_vouch,
    _record_first_thinker_verdict,
    _kind_within_boundary,
    _replan_kind,
    _baseline_plan_for_tier,
    _frozen_edge_stages,
    _review_baseline,
    _carry_records,
    _autonomy_directive_data,
    _CRITERION_ENGINE_WRITTEN_FIELDS,
    _apply_refined_stage_fields,
    _sync_venue_from_plan,
    _plan_venue_pair,
    _restore_current_stage,
    _stamp_accepted_plan_digest,
    _refresh_caches_from_plan_path,
    _log_gate,
    _instructions_head,
    _write_quality_row,
    _attach_advisories,
    _with_advisories,
    _submission_advice,
    _submission_problems,
    _run_check,
    _verify_command_result,
    _VENUE_IDENTITY_GIT_TIMEOUT_S,
    _VenueSnapshot,
    _GitTreeProbe,
    _venue_tree_identity,
    _check_definition_digest,
    _cached_check_hit,
    _resolve_or_refuse,
    _diagnose_venue_refusal,
    _diagnose_acceptance_rejection,
    _diagnose_effort_divergence,
    _effort_fire_escalation_data,
    _effort_fire_refusal,
    _freeze_delivered_head,
    _landed_spec_for_stage,
    _needs_delivered_head_freeze,
    _landed_check_result,
    _content_waiver_blocker,
    _disclose_landed_content_waivers,
    _is_recursion_refusal,
    _is_child_outside_project_refusal,
    _GRANT_WIDENING_EXIT,
    _HANDOFF_SHA_LINE_RE,
    _loop_detected_line,
    _is_grant_widening_refusal,
    _parse_rule_lines,
    _require,
    _count_review_round,
    _clear_since_user_decision,
    _replan_class_data,
    _effort_fire_window,
    _finished_work_fire,
    _finished_work_data,
    _disclose_finished_work,
    _park_node,
    _park_blocked,
    _MANDATE_NODE,
    _mandate_by_refusal,
    _mandate_view,
    _enumeration_escape_counts,
    _bound_stage_key,
    _bound_order_stage_key,
    _record_review_questions,
    _register_plan_assumptions,
    _record_plan_presentation,
    _PLAN_PRESENTATION_STAGE_ANCHOR_RE,
    _plan_presentation_skeleton,
    _present_plan_transcript,
    _deliver_presentation_via_channel,
    _note_round_release,
    _ledger_scope,
    _parse_review_concerns,
    _parse_review_questions,
    _question_data,
    _changed_parts,
    _plan_concern_severity,
    _stamp_concern_plan,
    _commit_concern_ledger,
    _log_concerns_downgraded,
    _cmd_plan_review_pair,
    TOPOLOGICAL_COMPOSITION_REVIEWER,
    _is_counted_review,
    _next_record_seq,
    _stamp_whole_plan_baseline,
    _count_plan_review_round,
    _review_cycle_arrive,
    _review_cycle_close,
    _count_review_act,
    _evaluate_review_trigger,
    _pair_ledger_groups,
    _ledger_concern,
    _prior_row_diff,
    _walk_target,
    _parse_verdicts,
    _skip_optional_problems,
    _stranded_dependency_problems,
    _mandate_basis_problems,
    _parse_partition_units,
    _continuation_worktree,
    _try_reattest,
    _EFFORT_BY_COST_TIER,
    _dispatch_project_settings_path,
    _parse_add_dir_spec,
    _parse_transcript_path,
    _parse_rule_line,
    _rule_line_to_call,
    _parse_resource_line,
    _parse_resource_spec,
    _self_grant_resources_for_rule,
    _stage_grant_entries,
    _stage_would_derive_grants,
    _refresh_runtime_grant_titles,
    _rekey_runtime_grants,
    _effective_stage_grants,
    _effective_stage_grants_for_self_grant,
    _consume_once_grants,
    _classify_transcript_denials,
    _diagnose_materialization_defect,
    _escalation_link,
    _child_norm_mismatch,
    _verify_child_escalation,
    _open_norm_mismatch,
    _repeat_norm_mismatch,
    _covers_resource,
    EvidenceDirError,
    evidence_dir_for,
    _cost_rows,
    _descendant_sessions,
    _refresh_spend,
    _credit_descendant_order_flush,
    _utcnow,
    _effort_now,
    _begin_child_run,
    _carry_hook_stamps,
    _end_child_run,
    _write_check_log,
    _evaluate_final_checks,
    _landing_gate,
    RESOLUTION_ASK_BLOCKER_PREFIX,
    RESOLUTION_ASK_RULE_HINT,
    _resolution_ask_gate,
    _require_diagnosing,
    _supersede_for_release,
    _session_plan_paths,
    _snapshot_norm_targets,
    _normalize_record_fields,
    _log_normalize_event,
    _record_normalization_waiver,
    _cmd_normalize_outside_diagnosing,
    _resume_action,
    _report_acceptance_staleness,
    _replan_spelling_advisories,
    _cmd_replan,
    _parent_link_summary,
    _load_or_none,
    _linked_children,
    _escalation_rows,
    _run_step,
    _ROOT,
    _SESSION_COMMANDS,
    _ARG_RESOLVE,
    _ARG_FORWARD,
    _ARG_DO_NOT_WRAP,
    resolve_arg_text,
    _inject_default_session,
    _BindingStore,
    _is_followed_by_gate,
    _fire_plugins,
)
from .cli_session import (
    cmd_start,
    cmd_reset,
    cmd_plugin_activate,
    cmd_plugin_deactivate,
    cmd_plugin_record,
    cmd_ledger_add,
    cmd_ledger_check,
    cmd_ledger_candidate,
    cmd_ledger_dispose,
    cmd_ledger_enumerate,
)
from .cli_questions import (
    cmd_question_raise,
    cmd_question_research,
    cmd_question_dispose,
    cmd_question_rebind,
    cmd_question_retire,
    cmd_question_list,
    cmd_order_raise,
    cmd_order_dispose,
    cmd_order_list,
    cmd_question_check,
    cmd_question_candidate_dispose,
    _upsert_candidate,
    _apply_enumeration_result,
    cmd_question_enumerate_worker,
    cmd_question_enumerate,
    cmd_question_enumerate_escape,
)
from . import cli_session, cli_questions



# --- the difficulty stack ---------------------------------------------------
# `state.difficulty` is the open (top) record; `state.difficulty_stack` holds its
# suspended ancestors. EVERY record is created by `_open_difficulty` -- the automatic
# sites (a failed result, a failed final check, a rejection, an effort divergence, a venue
# or materialization diagnosis), the lazy creation behind `declare`, and the explicit
# `difficulty-open` -- so the depth cap and the "never silently discard a declared record"
# rule have exactly one place to live.

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


# --- commands -------------------------------------------------------------

def cmd_task_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Explicit renegotiation: zero the cross-session task accumulator (item B)
    for `--task`. `cmd_reset` deliberately does NOT clear it, since the
    accumulator is task-scoped, not session-scoped: a fresh session re-armed
    on the same stuck task must inherit its prior friction, not silently
    forgive it (that would defeat the accumulator's entire purpose).
    Session-independent by design (no `--session`, no state load) — the
    accumulator lives outside any single session's state file — and requires
    `--reason` so this is never a casual one-flag habit; a user genuinely
    renegotiating a task's scope states why. A second, in-session path exists
    for the same explicit-renegotiation act: `cmd_replan`'s
    `--renegotiation-decision continue|rescope` (see
    `task_accumulator.reset`'s own docstring) — that path folds the reset into
    an already-required customer decision instead of a separate command."""
    by = (args.by or "").strip()
    if not by or by.casefold() == AGENT_ACTOR:
        return Directive(
            False, "(task-scoped)", "noop",
            f"task-reset is a user decision: --by must name the customer, not "
            f"{AGENT_ACTOR!r}",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"task": args.task, "by": by},
        )
    task_accumulator.reset(args.task)
    task_accumulator.clear_since_user_decision(args.task, now=_utcnow())
    return Directive(
        True, "(task-scoped)", "noop",
        f"cross-session task accumulator reset for task {args.task!r} by {by}: {args.reason}",
        data={"task": args.task, "reason": args.reason, "by": by},
    )


def cmd_mandate_grant(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Create a standing mandate: the user's one-time grant of bounded authority to the
    background debt cycle. Refused for `--by agent`, and for an id that already has a
    record (extend or resume that one instead)."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is not None:
        return Directive(
            False, _MANDATE_NODE, "noop",
            f"mandate {args.mandate_id!r} already exists; use mandate-extend or mandate-resume",
            data={"mandate": args.mandate_id},
        )
    defaults = mandate_rules.MandateLimits()
    limits = mandate_rules.MandateLimits(**{
        name: (getattr(args, name) if getattr(args, name) is not None else getattr(defaults, name))
        for name in mandate_rules.LIMIT_FIELDS
    })
    now = mandate_store.utcnow()
    mandate = mandate_rules.new_mandate(
        args.mandate_id, args.by.strip(), now, limits,
        term_days=args.days if args.days is not None else mandate_rules.DEFAULT_TERM_DAYS,
    )
    mandate_store.save_mandate(mandate)
    mandate_store.append_event(
        mandate.id, "granted", {"expires_at": mandate.expires_at}, by=mandate.granted_by, now=now,
    )
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r} granted by {mandate.granted_by} until {mandate.expires_at}",
        data=_mandate_view(mandate, now),
    )


def cmd_mandate_extend(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    mandate = mandate_store.load_mandate(args.mandate_id)
    if mandate is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    now = mandate_store.utcnow()
    mandate = mandate_rules.extended(mandate, now, args.days)
    mandate_store.save_mandate(mandate)
    mandate_store.append_event(
        mandate.id, "extended", {"expires_at": mandate.expires_at}, by=args.by.strip(), now=now,
    )
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r} extended to {mandate.expires_at}",
        data=_mandate_view(mandate, now),
    )


def cmd_mandate_resume(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Unpause a stopped mandate and close an open breaker -- a user decision, since the
    breaker opens exactly when the cycle's own judgement failed."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    detail = mandate_store.resume_cycle(args.mandate_id, by=args.by.strip(), runner=runner)
    now = mandate_store.utcnow()
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r} resumed by {args.by.strip()}",
        data={**_mandate_view(mandate_store.load_mandate(args.mandate_id), now), "resume": detail},
    )


def cmd_mandate_retry(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Let the cycle take an issue again after an item-local failure -- a user decision,
    since the cycle excludes a failed issue until the user says otherwise."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    mandate_store.allow_retry(args.mandate_id, args.issue, by=args.by.strip())
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r}: issue #{args.issue} may be taken again, allowed by {args.by.strip()}",
        data={"mandate": args.mandate_id, "issue": args.issue},
    )


def cmd_mandate_stop(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Pause the mandate, disable its timer and kill a running cycle. Open to the agent:
    stopping only ever narrows authority."""
    if not mandate_store.mandate_exists(args.mandate_id):
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    detail = mandate_store.stop_cycle(args.mandate_id, runner=runner)
    if detail["killed"]:
        outcome = f"killed cycle pid {detail['killed']['pid']}"
    elif detail["cycle_running"]:
        outcome = "a cycle holds the lock but its pid is unknown, nothing killed"
    else:
        outcome = "no running cycle"
    try:
        view = _mandate_view(mandate_store.load_mandate(args.mandate_id), mandate_store.utcnow())
    except (mandate_rules.MandateError, AttributeError):
        view = {"mandate": None, "record_unreadable": True}
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r} stopped; {outcome}",
        data={**view, "stop": detail},
    )


def cmd_mandate_status(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    mandate = mandate_store.load_mandate(args.mandate_id)
    if mandate is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    now = mandate_store.utcnow()
    view = _mandate_view(mandate, now)
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r}: " + ("may start a cycle" if view["may_start"] else "refused: " + ", ".join(view["refusals"])),
        data=view,
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


def cmd_plan_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
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
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to review: submit a plan first, or pass --target <plan.toml>",
        )
    scope = (getattr(args, "scope", None) or "").strip()
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
    if attested:
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
            + gates.grants_approval_blockers(state, state.plan_path)
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
    _bind_approved_grants(state, _approved_doc)
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
            view = plan_resources.compute_boundary_view(_approved_doc, order_key)
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


def cmd_dispatch(args, *, store: StateStore, runner: Runner | None = None,
                 perm_checker=None) -> Directive:
    state = _require(store, args.session)
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if efblock:
        return _effort_fire_refusal(state, "dispatch", efblock)
    try:
        host = runtime_host.require_bound_host(state)
    except runtime_host.HostAmbiguousError as exc:
        return Directive(False, state.node, "noop", str(exc))
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to dispatch")
    if not stage.is_spawn():
        return Directive(True, state.node, "execute_in_thread", f"stage {stage.index} is in-thread; no spawn")
    # Stage 6: an explicit, OPT-IN re-attest request. Absent --re-attest this
    # branch never runs — cmd_dispatch's behavior stays byte-identical to
    # before. On refusal _try_reattest has already logged the specific failing
    # condition; falling through below runs the existing, unmodified dispatch.
    if bool(getattr(args, "re_attest", False)):
        directive = _try_reattest(state, stage, store, runner)
        if directive is not None:
            return directive
    dry_run = bool(getattr(args, "dry_run", False))
    # Tier resolution order: explicit --budget flag > the stage's declared
    # Actor.cost_tier > the "medium" default — same precedence on the argparse
    # path (getattr(args, "budget", None)) and any in-process Namespace caller
    # that also omits --budget.
    tier = getattr(args, "budget", None) or stage.actor.cost_tier or "medium"
    # Same resolution order as --budget above: explicit --effort flag > a
    # cost_tier-derived default > "medium". spawn-specialist.py now hard-requires
    # --effort (no inherit-the-parent fallback there either), so dispatch_stage
    # must always hand it a value — never omit the flag and let the child's own
    # argparse refuse with "the following arguments are required: --effort".
    # There is no plan-declared per-stage reasoning-effort field (unlike
    # cost_tier on Actor), so the derived default reuses the same three-tier
    # cost_tier already on the stage rather than inventing a new plan field.
    effort_tier = getattr(args, "effort", None) or _EFFORT_BY_COST_TIER.get(
        stage.actor.cost_tier, "medium"
    )
    child_cwd = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    # Pre-launch: read the stage's effective grant set and hash the live
    # settings documents BEFORE the child runs. Grant coverage is captured
    # here (not after dispatch_stage returns) so a denial is classified
    # against what the stage was ACTUALLY spawned with, not against whatever
    # runtime_grants happens to hold once the child has already exited --
    # the two can differ if this session's grants are touched concurrently.
    # The settings hashes are the pre-image half of the settings_drift check
    # below: a spawned developer is untrusted to touch settings*.json, so any
    # difference after the child returns is itself the signal, independent of
    # whatever marker the child reports.
    # Refuse to dispatch outright when the stage has grants to lose -- DECLARED
    # (`stage.grants`) or DERIVED (`_stage_would_derive_grants`, trust-independent
    # so a derived-hash mismatch is caught too, not just a missing/unloadable
    # snapshot) -- but its approved-plan snapshot can no longer be read cleanly.
    # Spawning anyway would silently launch the child under whatever
    # `_effective_stage_grants` degrades to (declared entries alone, or nothing at
    # all) rather than the coverage this stage was actually approved with. A
    # stage with nothing to lose either way has a read error that is not fatal
    # here (`cmd_stage_grants` still surfaces it for inspection).
    _grant_read_error = _stage_grant_entries(state, stage.index)[3]
    _stage_declares_grants = bool(getattr(stage, "grants", None)) and not stage.grants.is_empty()
    if _grant_read_error and (
        _stage_declares_grants or _stage_would_derive_grants(state, stage.index)
    ):
        return Directive(
            False, state.node, "noop",
            f"dispatch refused: stage {stage.index} has grants to lose (declared "
            "and/or derived) but they could not be read from the approved-plan "
            f"snapshot ({_grant_read_error}) -- spawning now would silently drop "
            "that coverage instead of materializing it; a fresh `approve` "
            "re-snapshots the plan and rebinds approved_grants_sha256",
        )
    coverage = _effective_stage_grants(state, stage.index)
    _defects_before = len(state.materialization_defects)
    _misses_before = len(state.planning_misses)
    _settings_paths = widening_targets.enumerate_live_settings(child_cwd, state.repo_root)
    _settings_before = {p: _plan_file_sha256(p) for p in _settings_paths}
    # Resolved (not created) BEFORE the child spawns, so an unresolvable evidence
    # root refuses the dispatch before real spawn cost is spent. Only a
    # spawn:developer stage actually writes checkpoint evidence, so only that
    # kind is refused on failure; other kinds report the path best-effort and
    # proceed without it.
    try:
        evidence_dir = str(evidence_dir_for(state.session_id, stage.index))
    except EvidenceDirError as exc:
        if stage.spawn_kind() == "developer":
            return Directive(False, state.node, "noop", f"dispatch refused: {exc}")
        evidence_dir = None
    child_run = None if dry_run else _begin_child_run(store, state, stage)
    result = None
    try:
        result = dispatch_stage(
            stage, state.plan_path or "",
            runner=runner,
            budget=tier,
            complexity=getattr(args, "complexity", "medium"),
            effort=effort_tier,
            continue_worktree=_continuation_worktree(state, stage),
            constraints=getattr(args, "constraints", "") or "",
            dry_run=dry_run,
            # A hard-sandboxed spawned child can only write the tree it is
            # launched in, so pin its cwd to the plan's delivery venue rather
            # than relying on the dispatching process's ambient cwd. Always
            # "delivery" — dispatch has no verify_venue/venue of its own to read,
            # and delivery is where a spawned developer must write.
            cwd=child_cwd,
            runtime_host=host,
            project_settings=_dispatch_project_settings_path(state),
            session_id=state.session_id,
        )
    finally:
        if child_run is None:
            if not dry_run:
                _carry_hook_stamps(store, state)
        else:
            transcript = _parse_transcript_path(getattr(result, "stderr", "") or "")
            _end_child_run(store, state, child_run, Path(transcript).stem if transcript else None)
    if dry_run:
        # #10: a dry-run is a pure preview — no event log, no state save, no
        # marker routing. The echoed command is the whole result.
        return Directive(
            True, state.node, "preview",
            f"stage {stage.index} dry-run preview (no state change)",
            data={
                "returncode": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "evidence_dir": evidence_dir,
            },
        )
    state.log("dispatch", stage=stage.index, kind=stage.spawn_kind(), returncode=result.returncode)
    if result.returncode != 0 and _is_recursion_refusal(result):
        # spawn-specialist refused at the recursion cap or on the norm-loop rule
        # (both exit 3, told apart by the LOOP_DETECTED stdout line) — a structural
        # blocker, not a stage result. Park at BLOCKED and escalate; never report
        # success. This must win before marker routing (a refusal carries no valid
        # marker).
        loop_line = _loop_detected_line(result)
        _park_node(state)
        state.log(
            "dispatch_refused", stage=stage.index,
            reason="norm-loop" if loop_line else "recursion-cap",
        )
        store.save(state)
        data = {"returncode": result.returncode, "stderr": result.stderr}
        if loop_line:
            data["loop_detected"] = loop_line
            message = (
                f"stage {stage.index} spawn refused: its norm is already on the delegation "
                f"lineage (norm-loop) — escalate to the user: {loop_line}"
            )
        else:
            message = f"stage {stage.index} spawn refused: recursion cap reached — escalate to the user"
        return Directive(False, state.node, "escalate", message, marker="ESCALATE", data=data)

    if _is_child_outside_project_refusal(result):
        # The delivery venue loads no project settings while this session runs in a
        # project that registers hooks: a structural blocker fixed in the venue, so
        # there is no override here. Park and escalate before marker routing.
        _park_node(state)
        state.log("dispatch_refused", stage=stage.index, reason="child-outside-project")
        store.save(state)
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} spawn refused: its venue {child_cwd} is in no project while this "
            "session runs in one that registers hooks, so the child would run without them — "
            "escalate to the user; remedy: create .claude/settings.local.json ({} suffices) at the "
            "git root of that venue (the main checkout's root for a linked worktree)",
            marker="ESCALATE", data={"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
        )

    # The marker wins over the returncode: a specialist may exit 0 with CLARIFY, or
    # non-zero with a valid escalation marker. spawn-specialist.py has already parsed
    # and (if needed) MALFORMED-wrapped the marker onto stdout.
    marker, body = parse_marker(result.stdout)
    base = {
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        "evidence_dir": evidence_dir,
    }

    grant_widening = _is_grant_widening_refusal(result)
    # The direct form is the wrapper refusing before any child launched; the relayed
    # form is a child that DID run and handed the block back, so only it has a
    # post-launch footprint (settings drift, transcript denials, a spent once-grant).
    child_launched = not (grant_widening and result.returncode == _GRANT_WIDENING_EXIT)

    # Post-launch half of the settings_drift check: re-hash the SAME paths
    # enumerated before dispatch_stage ran. A spawned child is never granted
    # write access to a settings document, so any difference here is itself
    # the signal regardless of what marker the child reports -- recorded and
    # surfaced on every Directive this call returns, not only on a denial.
    _settings_after = {p: _plan_file_sha256(p) for p in _settings_paths}
    if _settings_after != _settings_before:
        drift_row = {
            "stage_index": stage.index,
            "changed": sorted(
                p for p in _settings_paths if _settings_before.get(p) != _settings_after.get(p)
            ),
            "ts": _utcnow(),
        }
        state.settings_drift.append(drift_row)
        base["settings_drift"] = drift_row

    # After every dispatched child returns (any marker, not just PERMISSION-REQUEST):
    # classify any permission-denial stop its transcript recorded against the stage's
    # PRE-LAUNCH effective grant set (`coverage`, captured before dispatch_stage ran
    # above). The PERMISSION-REQUEST branch below covers the same ground via its own
    # self-reported `Rule:`-line fallback for the case this transcript-based path
    # finds nothing (announcer-thread timeout, or discovery failed).
    _classify_transcript_denials(state, stage, coverage, _parse_transcript_path(result.stderr))
    if child_launched and marker not in (CHILD_INFRA_FAILURE, CHILD_EXHAUSTED):
        # A transient/resource marker means the child never meaningfully ran
        # against the once-scoped grant this launch was meant to spend — the
        # launch didn't count, so consuming a single-use grant here would
        # burn it for a re-dispatch that gets no benefit from having used it.
        _consume_once_grants(state, stage.index)

    if grant_widening:
        # A delegation that would widen authority beyond what the session was
        # handed is a request for authority, not a defect of the materialized
        # grants: park it for the user's decision. No materialization_defects row
        # for the refusal itself, no self_grant (the engine never widens on its
        # own say-so), no DIAGNOSING. `project`/`global` are not offered: they
        # cannot materialize the per-rule runtime grant the re-delegation counts.
        rules = _parse_rule_lines(result.stdout)
        sha_match = _HANDOFF_SHA_LINE_RE.search(result.stdout)
        action = (
            f"grant-widening refusal on stage {stage.index}: delegation holds "
            f"{len(rules)} grant(s) beyond the session's handoff: {', '.join(rules)}"
        )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=result.stdout
        )
        state.log("dispatch_refused", stage=stage.index, reason="grant-widening", rules=rules)
        store.save(state)
        return Directive(
            True, state.node, "ask_user_permission",
            f"stage {stage.index} delegation refused: it would widen grants beyond the "
            "authority this session was handed — the user decides",
            marker="PERMISSION-REQUEST",
            data={
                **base, "action": action,
                "options": ["once", "stage", "deny"],
                "grant_widening": {
                    "rules": rules,
                    "handoff_sha256": sha_match.group(1) if sha_match else None,
                },
            },
        )

    if marker == "COMPLETED":
        store.save(state)
        return Directive(
            True, state.node, "record_result",
            f"stage {stage.index} returned COMPLETED — diff delivery vs approved intent before recording",
            marker="COMPLETED", data={**base, "intent_diff_required": True},
        )
    if marker == "CLARIFY":
        store.save(state)
        return Directive(
            True, state.node, "answer_clarify",
            f"stage {stage.index} needs a clarification answered before it can continue",
            marker="CLARIFY",
            data={**base, "question": body, "continuation": continuations.clarify(body)},
        )
    if marker == "REPLAN":
        mismatch, unverified = _child_norm_mismatch(state, store, stage, result.stderr)
        if mismatch is not None:
            standing = gates.escalation_counterpart(
                state, mismatch["child_uid"], mismatch["child_session"])
            if standing is not None:
                return _repeat_norm_mismatch(state, store, stage, mismatch, base, standing, body)
            return _open_norm_mismatch(state, store, stage, mismatch, base)
        if unverified:
            state.log("norm_mismatch_unverified", stage=stage.index, reason=unverified)
        store.save(state)
        return Directive(
            False, state.node, "replan",
            f"stage {stage.index} proposes a plan-level revision",
            marker="REPLAN", data={**base, "reason": body},
        )
    if marker == "PLAN-READY":
        store.save(state)
        return Directive(
            True, state.node, "await_plan_approval",
            f"stage {stage.index} returned a fresh plan — HARD GATE, get explicit user approval",
            marker="PLAN-READY", data=base,
        )

    def _route_permission_request(action: str, extra: dict | None = None) -> Directive:
        data_base = {**base, **(extra or {})}
        # The permissions-cli check still runs for its own audit-row side effect, but
        # its answer no longer skips the ask: a global grant recorded there never
        # reaches the child's own --settings/--add-dir, so the old auto-continue just
        # re-spawned into the identical denial (removed per the plan's design).
        checker = perm_checker or permissions.check_permission
        checker(action)
        # The optional `Rule:` line is a SEPARATE line from the marker's own first
        # line, so it must be read off the full child stdout -- parse_marker's
        # `body` is only the text after "PERMISSION-REQUEST:" on that first line.
        rule_line = _parse_rule_line(result.stdout)
        call = _rule_line_to_call(rule_line) if rule_line else None
        # Finding N1: a transcript denial newly recorded THIS dispatch call may
        # belong to an entirely unrelated command (the stage's transcript can
        # carry several denials in one launch) -- counting "any new
        # materialization_defects row" as covering THIS request conflates the
        # two. When the request names its own `Rule:`, tie transcript_covered
        # to a new row matching that SAME (tool_name, tool_input) pair; only
        # absent a `Rule:` line (nothing to disambiguate against) fall back to
        # the old any-new-row signal.
        # Finding S9 (stage 9 live run, 2026-09-27): the self-reported `Rule:` line
        # can simply be WRONG -- the specialist misnames the command it actually ran
        # (e.g. reports the covered repo-relative spelling while the transcript shows
        # an uncovered worktree-absolute one it actually invoked). Trusting
        # `self_covered` whenever it agrees, without first checking whether the
        # transcript ALREADY classified this same call as an uncovered miss, let a
        # wrong self-report override the transcript's own record of what was denied.
        # The transcript is ground truth when it has an opinion at all: check BOTH
        # transcript outcomes (covered -> materialization_defects, uncovered ->
        # planning_misses[source="transcript"]) and let a transcript-sourced row —
        # of either kind — win over the self-report; the self-report drives
        # classification only when the transcript recorded nothing for this call.
        if call is not None:
            expected_text = call[1].get("command") or call[1].get("file_path")
            expected_digest = _digest(expected_text) if expected_text is not None else None
            transcript_covered = any(
                row.get("tool_name") == call[0] and row.get("tool_input_digest") == expected_digest
                for row in state.materialization_defects[_defects_before:]
            )
            transcript_uncovered = any(
                row.get("source") == "transcript"
                and row.get("tool_name") == call[0]
                and row.get("tool_input_digest") == expected_digest
                for row in state.planning_misses[_misses_before:]
            )
        else:
            transcript_covered = len(state.materialization_defects) > _defects_before
            transcript_uncovered = any(
                row.get("source") == "transcript"
                for row in state.planning_misses[_misses_before:]
            )
        self_covered = call is not None and _grants.grant_covers_call(coverage, call[0], call[1])
        if transcript_covered or (not transcript_uncovered and self_covered):
            if not transcript_covered:
                state.materialization_defects.append({
                    "stage_index": stage.index, "action": action,
                    "tool_name": call[0],
                    "tool_input_digest": _digest(json.dumps(call[1], sort_keys=True)),
                    "evidence": "self-reported", "ts": _utcnow(),
                })
            return _diagnose_materialization_defect(
                state, store, stage, action=action,
                evidence="transcript" if transcript_covered else "self-reported",
                data=extra,
            )
        # REQ5 (dispatch half): a Rule:-line request may resolve to a resource
        # already approved for this order, or already covered by the stage's
        # own effective grants under different-but-equivalent text -- self_grant
        # instead of parking on the user. An optional advisory `Resource:` line
        # is NEVER trusted to DECIDE this by itself -- self_grant is decided
        # ONLY from the engine's own resolution of the `Rule:` line; a
        # disagreeing `Resource:` line routes to the user with a reason naming
        # BOTH, rather than silently preferring either.
        resource_line = _parse_resource_line(result.stdout)
        reported_resource = _parse_resource_spec(resource_line) if resource_line else None
        self_grant_resources: list = []
        self_grant_reason: str | None = None
        if rule_line is not None:
            try:
                _grants.validate_rule(rule_line)
            except _grants.GrantValidationError as exc:
                self_grant_reason = f"refused rule grant: {exc}"
            else:
                self_grant_coverage = _effective_stage_grants_for_self_grant(state, stage.index)
                self_grant_resources, self_grant_reason = _self_grant_resources_for_rule(
                    state, self_grant_coverage, rule_line
                )
        resource_disagreement = None
        if (
            reported_resource is not None
            and self_grant_resources
            and not any(reported_resource == r for r in self_grant_resources)
        ):
            resource_disagreement = (
                f"specialist-reported Resource: {resource_line!r} disagrees with the "
                f"engine's own resolution of Rule: {rule_line!r} to "
                f"{[plan_resources._resource_to_dict(r) for r in self_grant_resources]!r} "
                "-- asking the user rather than trusting either alone"
            )
        if self_grant_resources and resource_disagreement is None:
            # The self_grant directive's own message tells the caller to run
            # `resolve-permission --by agent --scope stage --rule ...` next --
            # that command requires a pending `state.permission_request`
            # (checked first thing in cmd_resolve_permission), so it must be
            # parked here exactly as the ask_user_permission path below does,
            # or the directive's own instructions would fail when followed.
            state.permission_request = PermissionRequest(
                action=action, stage_index=stage.index, raw=action
            )
            state.log(
                "permission_self_grant", stage=stage.index, action=action, rule=rule_line,
                resources=[plan_resources._resource_to_dict(r) for r in self_grant_resources],
            )
            store.save(state)
            return Directive(
                True, state.node, "self_grant",
                f"stage {stage.index} permission request for {rule_line!r} resolves to "
                "resource(s) already approved for this order or already covered by the "
                "stage's own effective grants -- self-grant (`resolve-permission --by "
                f"agent --scope stage --rule {rule_line!r}`) and re-dispatch, no user ask needed",
                marker="PERMISSION-REQUEST",
                data={
                    **data_base, "action": action, "rule": rule_line,
                    "resources": [plan_resources._resource_to_dict(r) for r in self_grant_resources],
                },
            )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=action
        )
        # `_classify_transcript_denials` above may already have appended a
        # planning_misses row for this SAME denial (source="transcript",
        # asked_user=False) before we knew the marker would ask the user.
        # Promote that row in place rather than appending a second one for
        # the same event -- search only the rows THIS dispatch call added
        # (`_misses_before:`), most-recent first, since an earlier stage's
        # unrelated miss must never be touched. A single dispatch call can
        # legitimately add MULTIPLE unasked transcript rows (one per denial
        # stop in the transcript), so "most recent unasked" alone is not
        # enough to identify the SAME denial the self-report names --
        # require the row's own stored `command` to be prefixed by this
        # call's rule command (via `_bash_rule_command_from_call`); a
        # `None` call command (no Rule: line, or a non-Bash call) never
        # promotes, since it cannot be confirmed to be the same event, and
        # appending a second row is a cheaper mistake than silently
        # mutating the wrong denial's row.
        _call_command = _bash_rule_command_from_call(call)
        _promoted = None
        if _call_command is not None:
            for _row in reversed(state.planning_misses[_misses_before:]):
                _row_command = _row.get("command")
                if (
                    _row.get("source") == "transcript"
                    and not _row.get("asked_user")
                    and isinstance(_row_command, str)
                    and (
                        _row_command == _call_command
                        or _row_command.startswith(_call_command + " ")
                    )
                ):
                    _promoted = _row
                    break
        if _promoted is not None:
            _promoted["asked_user"] = True
            _promoted["action"] = action
        else:
            state.planning_misses.append({
                "stage_index": stage.index, "asked_user": True, "ts": _utcnow(),
                "tool_name": call[0] if call else None,
                "tool_input_digest": _digest(json.dumps(call[1], sort_keys=True)) if call else None,
                "action": action, "source": "permission-request",
            })
        state.log("permission_request", stage=stage.index, action=action)
        store.save(state)
        _ask_data = {**data_base, "action": action, "options": ["once", "stage", "project", "global", "deny"]}
        if rule_line is not None:
            _ask_data["reason_class"] = self_grant_reason
        if resource_disagreement is not None:
            _ask_data["resource_disagreement"] = resource_disagreement
        return Directive(
            True, state.node, "ask_user_permission",
            f"stage {stage.index} requests permission: {action}",
            marker="PERMISSION-REQUEST",
            # Finding S8: "stage" was materializable via --scope stage but
            # missing from this options list, so the root never saw it as an
            # offered choice.
            data=_ask_data,
        )
    if marker == "PERMISSION-REQUEST":
        return _route_permission_request(permission_request_action(result.stdout) or "")
    if marker == "INCOMPLETE":
        embedded = permission_request_action(result.stdout)
        if embedded is not None:
            return _route_permission_request(embedded, {"incomplete_summary": body})
        store.save(state)
        return Directive(
            False, state.node, "decide_incomplete",
            f"stage {stage.index} returned INCOMPLETE — re-spawn / ask / accept",
            marker="INCOMPLETE", data={**base, "reason": body},
        )
    if marker == CHILD_INFRA_FAILURE:
        # A transient condition about the RUN, never a judgement about the
        # output — named directive only, no automatic re-spawn: the
        # coordinator still spends the money on the retry.
        store.save(state)
        return Directive(
            False, state.node, "retry_dispatch",
            f"stage {stage.index} spawn never reached (or lost) the API — "
            "transient; recommend retrying the same dispatch",
            marker="CHILD_INFRA_FAILURE", data={**base, "reason": body},
        )
    if marker == CHILD_EXHAUSTED:
        # A resource condition about the RUN — the child was refused for size
        # before it could answer. Recommend a reduced brief or the re-attest
        # path (stage 6); again a directive only, no automatic re-spawn.
        store.save(state)
        return Directive(
            False, state.node, "reduce_brief_or_reattest",
            f"stage {stage.index} spawn was refused for size before it could "
            "answer — recommend a reduced brief or the re-attest path",
            marker="CHILD_EXHAUSTED", data={**base, "reason": body},
        )
    if marker == "LOOP_DETECTED":
        # The child reports its task re-frames an ancestor's: routed like ESCALATE
        # (park BLOCKED, the user decides), with the report carried on the directive.
        return _park_blocked(
            state, store, stage, marker, {**base, "loop_detected": f"LOOP_DETECTED: {body}".strip()},
        )
    if marker is None and result.returncode != 0:
        store.save(state)
        return Directive(
            False, state.node, "handle_spawn_failure",
            f"stage {stage.index} spawn failed (rc={result.returncode}) with no marker",
            data=base,
        )
    # ESCALATE / MALFORMED / marker-less success (rc==0, no marker) -> park BLOCKED.
    return _park_blocked(state, store, stage, marker, base)


def cmd_resolve_permission(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Resume a session parked on a PERMISSION-REQUEST once the manager has the
    user's decision. The user ask is cognitive; this only records the outcome,
    clears the parked request, and hands back the continuation to re-spawn with.

    `--scope stage` additionally MATERIALIZES `--rule`/`--add-dir` entries as
    RUNTIME grants on the active stage (state.runtime_grants), validated through
    `grants.validate_rule`/`grants.validate_add_dir` -- the same validator every
    declared/derived grant passes through, so a runtime grant is never trusted
    more than either. This is ADDITIVE to the pre-existing decision/continuation
    logic below, never a replacement for it: a `--scope stage` grant with a
    validation failure refuses the WHOLE call (nothing partially recorded) so the
    manager can correct the rule/add-dir spec and retry, rather than leaving a
    parked request half-resolved."""
    state = _require(store, args.session)
    req = state.permission_request
    if req is None:
        return Directive(False, state.node, "noop", "no pending permission request to resolve")
    scope = getattr(args, "scope", "once")
    # Finding S8: neither "project" nor "global" scope materializes anything
    # anywhere in this engine -- there is no persistence path for either, so
    # a --rule/--add-dir passed alongside them was silently dropped while
    # continuations.permission_granted told the child it had been "recorded".
    # Refuse the whole call instead of accepting and discarding.
    if scope in ("project", "global") and (getattr(args, "rules", None) or getattr(args, "add_dirs", None)):
        return Directive(
            False, state.node, "noop",
            f"--rule/--add-dir is not materialized for --scope {scope} (no persistence path exists); "
            "use --scope stage or --scope once, or omit --rule/--add-dir",
        )
    by = (getattr(args, "by", None) or "").strip()
    if by.casefold() == AGENT_ACTOR and args.decision == "granted":
        # REQ5: an agent-authored --by gets no benefit of human judgment -- it
        # must instead prove every --rule/--add-dir it wants materialized
        # resolves (via the SAME plan_resources.py functions cmd_approve's own
        # ledger-stamping uses -- one resolution story, never a second one
        # trusted less) to a typed resource already covered by a resource the
        # CUSTOMER approved for this plan's order (order_approvals.py's
        # ledger, keyed by plan.order_digest). Unresolved or uncovered refuses
        # the WHOLE call fail-closed, mirroring validate_rule's refusal below.
        if scope not in ("once", "stage"):
            return Directive(False, state.node, "noop",
                              f"agent self-grant requires --scope once or stage, got {scope!r}")
        if _open_effort_fire(state):
            return Directive(False, state.node, "noop",
                              f"agent self-grant refused: {gates.AUTONOMY_REASON_OPEN_FIRE}",
                              data={"reason_class": "open-effort-fire"})
        self_rules = getattr(args, "rules", None) or []
        self_add_dirs = getattr(args, "add_dirs", None) or []
        if not self_rules and not self_add_dirs:
            return Directive(False, state.node, "noop",
                              "agent self-grant requires --rule/--add-dir naming what to grant")
        if not state.plan_path:
            return Directive(False, state.node, "noop",
                              "agent self-grant requires an approved plan (state.plan_path is empty)")
        from .plan import PlanError, load_plan as _load
        try:
            self_doc = _load(state.plan_path)
        except (OSError, PlanError) as exc:
            return Directive(False, state.node, "noop", f"agent self-grant: cannot read plan: {exc}")
        self_venue = _venue_for(self_doc)
        # The SAME union `_self_grant_resources_for_rule` checks (REQ5/finding
        # #4): the order-approvals ledger alone is not the whole coverage
        # story -- a stage's own declared/derived [stage.grants] are already
        # user-approved (as part of the plan approval) and must self-grant
        # here too, or dispatch's self_grant directive (which DOES check this
        # union) would tell the caller to run a command that then refuses.
        self_approved = list(order_approvals.boundary_resources(order_digest(self_doc)))
        self_active_stage = state.active_stage()
        if self_active_stage is not None:
            self_coverage = _effective_stage_grants_for_self_grant(state, self_active_stage.index)
            for rule_grant in self_coverage.allow:
                eff = plan_resources.resolve_rule_grant(rule_grant.rule, self_venue)
                if eff.status == "resolved":
                    self_approved.extend(eff.resources)
            for add_dir in self_coverage.add_dirs:
                eff = plan_resources.resolve_add_dir_grant(add_dir.path, add_dir.mode)
                self_approved.extend(eff.resources)
        self_protected = _resources.protected_permission_surfaces(
            repo_root=state.repo_root, delivery_worktree=state.delivery_worktree,
            ledger_dir=str(order_approvals._root(None)),
        )
        self_to_check: list = []
        for rule in self_rules:
            self_to_check.append((rule, plan_resources.resolve_rule_grant(rule, self_venue)))
        for spec in self_add_dirs:
            parsed = _parse_add_dir_spec(spec)
            if parsed is None:
                return Directive(False, state.node, "noop",
                                  f"malformed --add-dir spec (want 'PATH:read|write'): {spec}")
            path, mode = parsed
            self_to_check.append((spec, plan_resources.resolve_add_dir_grant(path, mode)))
        for label, res in self_to_check:
            if res.status != "resolved" or not res.resources:
                # Mirrors _self_grant_resources_for_rule's identical check
                # (dispatch's own self_grant path) -- a "resolved" status
                # with an EMPTY resource set (a readonly rule that resolves
                # to nothing) must still refuse here, not vacuously pass the
                # `for resource in res.resources` loop below with zero
                # iterations and zero approvals actually checked.
                reason_class = res.reason_class or "contract-unresolved"
                detail = res.reason if res.status != "resolved" else "resolves to an empty resource set"
                return Directive(
                    False, state.node, "noop",
                    f"agent self-grant refused: {label!r} is unresolved ({reason_class}): {detail}",
                    data={"reason_class": reason_class},
                )
            for resource in res.resources:
                if not any(_covers_resource(appr, resource, self_protected) for appr in self_approved):
                    return Directive(
                        False, state.node, "noop",
                        f"agent self-grant refused: {label!r} resolves to a resource not "
                        f"covered by any resource the customer approved for this order",
                        data={"reason_class": "not-approved"},
                    )
    new_entries: list[dict] = []
    # `once` materializes a runtime grant exactly like `stage` does -- the
    # difference is lifetime, not whether a --rule/--add-dir gets recorded at
    # all: `_consume_once_grants` marks a `scope: "once"` entry consumed
    # after the next dispatch, while a `scope: "stage"` entry stays live for
    # the stage's remaining launches. Without this branch also firing for
    # "once", a --scope once resolution silently dropped its --rule/--add-dir
    # on the floor instead of granting a single re-launch past the denial.
    if args.decision == "granted" and scope in ("once", "stage"):
        stage = state.active_stage()
        if stage is None:
            return Directive(False, state.node, "noop",
                              "--scope stage/once requires an active stage to grant onto")
        for rule in (getattr(args, "rules", None) or []):
            try:
                _grants.validate_rule(rule)
            except _grants.GrantValidationError as exc:
                return Directive(False, state.node, "noop", f"refused rule grant: {exc}")
            new_entries.append({
                "rule": rule, "provenance": "runtime", "consumed": False,
                "stage_title": stage.title, "scope": scope, "granted_by": by,
            })
        for spec in (getattr(args, "add_dirs", None) or []):
            parsed = _parse_add_dir_spec(spec)
            if parsed is None:
                return Directive(False, state.node, "noop",
                                  f"malformed --add-dir spec (want 'PATH:read|write'): {spec}")
            path, mode = parsed
            try:
                _grants.validate_add_dir(path, mode)
            except _grants.GrantValidationError as exc:
                return Directive(False, state.node, "noop", f"refused add_dir grant: {exc}")
            new_entries.append({
                "path": path, "mode": mode, "provenance": "runtime", "consumed": False,
                "stage_title": stage.title, "scope": scope, "granted_by": by,
            })
    if args.decision == "granted":
        cont = continuations.permission_granted(req.action, scope)
        detail = f"permission granted for {req.action}; re-spawn the stage"
    else:
        cont = continuations.permission_denied(req.action)
        detail = f"permission denied for {req.action}; re-spawn with the fallback"
    if new_entries:
        key = str(state.active_stage().index)
        state.runtime_grants.setdefault(key, []).extend(new_entries)
        _record_customer_stage_grants(state, by, scope, new_entries)
    state.permission_request = None
    # `by` + the two env overrides that steer WHICH ledger/contract-table a
    # self-grant resolved against are recorded on every call, not only a
    # self-grant one, so the audit trail is uniform regardless of who --by
    # names -- a human-attributed resolution reads the same overrides.
    state.log(
        "resolve_permission", action=req.action, decision=args.decision, by=by,
        order_approvals_dir_override=os.environ.get("AGENTCTL_ORDER_APPROVALS_DIR"),
        tool_contracts_override=os.environ.get("AGENTCTL_TOOL_CONTRACTS"),
    )
    store.save(state)
    return Directive(
        True, state.node, "continue_spawn", detail,
        data={"action": req.action, "decision": args.decision, "continuation": cont,
              "runtime_grants_added": new_entries},
    )


def cmd_evidence_dir(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Create (idempotently) and report the durable per-stage evidence directory (see
    evidence_dir_for) where a developer spawn commits checkpoint evidence as it goes —
    see spawn-specialist.py's '## Checkpoints' prompt section and the write grant
    load_or_create_evidence_dir arranges for it. Purely path-derived from --session/
    --stage — no session-state lookup — since the directory must be creatable (and
    granted to a spawned child) before dispatch necessarily reflects the stage in
    progress."""
    try:
        path = evidence_dir_for(args.session, args.stage)
    except EvidenceDirError as exc:
        return Directive(False, "n/a", "noop", str(exc))
    path.mkdir(parents=True, exist_ok=True)
    return Directive(True, "n/a", "evidence_dir", str(path) + "\n", data={"evidence_dir": str(path)})


def cmd_workspace_register(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a workspace this task created so `resolve` may clean it up (see
    workspace_cleanup.py). Session-keyed, or --pending for a workspace made before any
    engine session exists (enter-task.sh): the engine binds a pending record at resolve
    time by the resolve cwd / the plan's delivery_worktree. Needs no session state, so it
    works before `classify`."""
    kind = getattr(args, "kind", None)
    try:
        if getattr(args, "pending", False):
            record = workspace_cleanup.register_pending(
                args.path, kind=kind, backend=args.ws_backend, name=args.ws_name)
            scope = "pending"
        elif args.session:
            record = workspace_cleanup.register(
                args.session, args.path, kind=kind, backend=args.ws_backend, name=args.ws_name)
            scope = "session"
        else:
            return Directive(False, "n/a", "noop",
                             "workspace-register needs --session (or CLAUDE_CODE_SESSION_ID) "
                             "or --pending")
    except (OSError, ValueError) as exc:
        return Directive(False, "n/a", "noop", str(exc))
    return Directive(True, "n/a", "workspace_registered", f"{record}\n",
                     data={"record": str(record), "scope": scope, "path": args.path})


def cmd_stage_grants(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Report the EFFECTIVE grant set for one stage — declared + derived (only when
    the live re-derivation still re-hashes to `approved_grants_sha256`, per
    grants.py's docstring) + unconsumed runtime grants — the read `dispatch_stage`
    (stage 2) is meant to consult before materializing a child's `--settings`/
    `--add-dir`. Declared/derived are read from the hash-verified
    `plan_snapshot_path`/`plan_snapshot_hash` snapshot, NEVER from live `plan_path`
    -- the snapshot is the frozen, human-approved bytes; a live plan_path may
    already carry an unreviewed in-place edit at plan-mutable PLAN_READY of the
    NEXT plan. `--stage` defaults to the session's active stage."""
    state = _require(store, args.session)
    stage_index = getattr(args, "stage", None)
    if stage_index is None:
        stage_index = state.current_stage
    if stage_index is None:
        return Directive(False, state.node, "noop", "no active stage and no --stage given")
    try:
        stage = state.stage(stage_index)
    except KeyError:
        return Directive(False, state.node, "noop", f"no stage with index {stage_index}")

    declared_entries, derived_entries, dropped, error, note = _stage_grant_entries(state, stage_index)
    runtime_entries = [
        e for e in state.runtime_grants.get(str(stage_index), [])
        if not e.get("consumed")
    ]
    # Flat, not {"declared": [...], "derived": [...], "runtime": [...]} -- every
    # entry already carries its own "provenance" (RuleGrant/AddDirGrant.to_dict,
    # and the runtime constructors in this file, all stamp it), so a second,
    # positional grouping on top just gives callers two disagreeing ways to ask
    # "what kind of grant is this" and invites them to drift apart.
    all_entries = declared_entries + derived_entries + runtime_entries

    data = {
        "stage": stage_index,
        "executor": stage.actor.executor,
        "grants": all_entries,
        "dropped": dropped,
        "error": error,
        "note": note,
    }
    if getattr(args, "json", False):
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        lines = [f"Stage {stage_index} ({stage.actor.executor}):"]
        for label, entries in (
            ("declared", declared_entries),
            ("derived", derived_entries),
            ("runtime", runtime_entries),
        ):
            if not entries:
                continue
            rendered = ", ".join(e.get("rule") or f"{e.get('path')}:{e.get('mode')}" for e in entries)
            lines.append(f"  {label}: {rendered}")
        if not all_entries:
            lines.append("  (no grants)")
        if error:
            lines.append(f"  ERROR: {error}")
        if note:
            lines.append(f"  NOTE: {note}")
        text = "\n".join(lines) + "\n"
    # A read error is reported, never swallowed into a quiet "no grants" -- ok stays
    # True (this is a report command, not a gate) but the error rides d.data/text so
    # a caller cannot mistake an unreadable snapshot for a plan that grants nothing.
    return Directive(True, state.node, "inspect", text, data=data)


def cmd_grant_stats(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Report the session's accumulated grant-classification ledgers:
    planning_misses (denials NOT covered by a stage's effective grant set --
    correctly asked the user), materialization_defects (denials the effective
    grant set DID cover, but the child's own --settings/--add-dir materialization
    failed to carry -- routes the stage to FAILED/DIAGNOSING, never a re-ask),
    settings_drift (a stage's live settings document changed underneath the
    spawn), and cwd_drift (a Bash denial that's only uncovered because the child
    ran it from a subdirectory of the stage venue -- rebasing it back to the
    venue would be covered). A pure read of state.planning_misses/
    materialization_defects/settings_drift/cwd_drift -- see state.py's schema-39
    field block for each row's shape."""
    state = _require(store, args.session)
    asked = sum(1 for m in state.planning_misses if m.get("asked_user"))
    unasked = len(state.planning_misses) - asked
    data = {
        "planning_misses": state.planning_misses,
        "materialization_defects": state.materialization_defects,
        "settings_drift": state.settings_drift,
        "cwd_drift": state.cwd_drift,
        "planning_miss_counts": {"asked": asked, "unasked": unasked},
    }
    if getattr(args, "json", False):
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        text = (
            f"planning_misses: {len(state.planning_misses)} (asked={asked}, unasked={unasked})\n"
            f"materialization_defects: {len(state.materialization_defects)}\n"
            f"settings_drift: {len(state.settings_drift)}\n"
            f"cwd_drift: {len(state.cwd_drift)}\n"
        )
    return Directive(True, state.node, "inspect", text, data=data)


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


def cmd_verify_final(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    # Idempotency guard (must precede the node-agnostic resolution gate below):
    # verify-final's body performs node transitions — `final` -> RESOLUTION on
    # success, `diagnose` -> DIAGNOSING on a refusal/failure — that are legal ONLY
    # from VERIFYING (machine.TRANSITIONS). The resolution gate checks stage
    # outcomes, not the node, so a re-invocation after a prior refusal/failure has
    # already routed the session to DIAGNOSING would re-enter and raise an uncaught
    # TransitionError. From any node other than VERIFYING, return a legible
    # directive instead of re-running the transitioning body.
    if state.node == Node.DIAGNOSING.value:
        return Directive(
            False, state.node, "declare",
            "a prior final-verification refusal/failure already routed this session "
            "into the difficulty cycle; complete overcome-difficulty (declare -> "
            "investigate -> critique -> replan — a replan, including a no_change one that "
            "counts toward no budget, re-arms verification) before re-running verify-final",
            marker="OVERCOME-DIFFICULTY",
        )
    if state.node == Node.RESOLUTION.value:
        return Directive(
            True, state.node, "resolve",
            "final verification already passed; the resolution gate is armed — "
            "run `resolve` once the user confirms",
        )
    if state.node != Node.VERIFYING.value:
        return Directive(
            False, state.node, "inspect",
            f"verify-final runs only from VERIFYING; node is {state.node}",
        )
    renorm_block = gates.pending_renorm_final_blockers(state)
    _log_gate(state, "renorm_final", renorm_block, passed=not renorm_block)
    if renorm_block:
        return Directive(False, state.node, "renorm",
                         "not ready for resolution: a normative difficulty's norm edit is "
                         "still owed", data={"blockers": renorm_block})
    orphan_block = gates.escalation_orphan_blockers(
        gates.escalation_orphans(state, _linked_children(store, state.session_id)))
    _log_gate(state, "escalation_orphans", orphan_block, passed=not orphan_block)
    if orphan_block:
        return Directive(False, state.node, "adopt_escalation",
                         "not ready for resolution: a linked child's escalation of a stage "
                         "norm is not adopted", data={"blockers": orphan_block})
    state.pending_renorms = gates.open_renorms(state)
    blockers = gates.blockers(state, "resolution")
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        failing_ids = gates.failing_acceptance_requirements(state)
        if failing_ids:
            return _diagnose_acceptance_rejection(state, store, failing_ids)
        return Directive(False, state.node, "fix_stages", "not ready for resolution", data={"blockers": blockers})
    # Effort-divergence spend refresh (call site 2) + divergence computation — fire
    # site 2. Hoisted here, ahead of the stage-verification loop below, so a venue
    # refusal or a failing final_check can attach this SAME live divergence via
    # _diagnose_venue_refusal / the failures branch, instead of only the clean-pass
    # path reaching it — mirrors record_result's failed branch, which attaches
    # divergence data at its own failure point rather than solely on success. A
    # separate cost-log read from the rollup below: refresh_spend needs every row by
    # plan_path (including engine-mandated review spawns no stage attributes), the
    # rollup needs only what record-result already stamped onto each Outcome.
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
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
    # Final-gate execution: see _evaluate_final_checks.
    failures, refusal_directive = _evaluate_final_checks(state, store, runner, div)
    if refusal_directive is not None:
        return refusal_directive
    if failures:
        # A failing final_check is a difficulty (actual result diverges from the
        # plan's declared image) exactly like a failed stage — route into the same
        # DIAGNOSING cycle (record_result's failed-stage path, above) rather than
        # stranding the session at VERIFYING with no reachable resolution: from
        # VERIFYING, declare/investigate/critique all refuse ("difficulty commands
        # run only in the DIAGNOSING cycle"), and only `reset --force` escaped.
        state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
        opened = _open_difficulty(state)
        data = {"failures": failures}
        if div is not None and gates.effort_active(state):
            # Already entering DIAGNOSING for the failure — attach the divergence
            # instead of re-transitioning or opening a second Difficulty, mirroring
            # record_result's failed branch (still honoring divergence()'s CALLER
            # OBLIGATION: record the fire so it doesn't re-trip).
            now = _utcnow()
            data["effort_divergence"] = _record_effort_fire(state, div, now=now, store=store)
        store.save(state)
        if opened is None:
            return _difficulty_cap_directive(
                state, "final verification command(s) failed", data)
        return Directive(
            False, state.node, "declare",
            "final verification command(s) failed; run overcome-difficulty — declare "
            "the divergence, then investigate, then critique; replan is blocked until "
            "the cycle is complete",
            data=data,
        )
    # Fire check on a clean pass (fire site 2) — the last point at which `diagnose`
    # is still a legal transition (a contracted plan can reach here with no further
    # record-result to fire from).
    if _finished_work_fire(state, div, store=store) is None and div is not None and gates.effort_active(state):
        now = _utcnow()
        fire = _record_effort_fire(state, div, now=now, store=store)
        return _diagnose_effort_divergence(state, store, div, fire)

    # Compute whole-plan cost rollup from already-attributed stage outcomes.
    # No second log read — record-result already stored the costs on each Outcome.
    rollup = cost.rollup_plan([], state.plan_path, state.stages)
    state.cost = rollup
    state.node = transition(state.node, "final")  # VERIFYING -> RESOLUTION
    state.resolution = GateRecord("resolution", armed=True, passed=False)
    state.log("verify_final", at=time.time())
    store.save(state)
    kind = "run the measurable check" if state.overall_criterion_type == CriterionType.MEASURABLE.value \
        else "ask the user to accept on review"
    data = {
        "cost": {
            "total_cost_usd": rollup.total_cost_usd,
            "total_duration_ms": rollup.total_duration_ms,
            "spawn_count": rollup.spawn_count,
            "attributed_stages": rollup.attributed_stages,
            "note": rollup.note,
        },
        "resolution_ask_marker": RESOLUTION_ASK_MARKER,
    }
    # Bypass visibility: verify-final never returns a clean bill while any acceptance
    # pass skipped a genuine judge verdict (kill switch / override) — the bypasses are
    # surfaced verbatim so the resolution decision is made with them in view, never
    # silently. A later passing review does not clear them.
    detail = f"all stages passed; resolution gate armed — {kind}"
    if gates.resolution_ask_gate_active(state):
        detail += (
            f"; the resolution AskUserQuestion that carries the 1-5 rating must embed the "
            f"literal marker {RESOLUTION_ASK_MARKER!r} in an option label or description "
            f"and be asked AFTER this verify-final — resolve refuses a rating without an "
            f"answered marked ask"
        )
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f"; WARNING: {len(bypasses)} acceptance judge bypass(es) recorded (see judge_bypassed)"
    acceptance_bypass = _acceptance_bypass_surface(state)
    if acceptance_bypass is not None:
        data["acceptance_bypass"] = acceptance_bypass
        detail += "; WARNING: acceptance recorded via bypass, not judge corroboration (see acceptance_bypass)"
    detail = _disclose_backstop_crossings(state, data, detail)
    detail = _disclose_finished_work(state, data, detail)
    detail = _disclose_agent_plan_conversions(state, data, detail)
    detail = _disclose_landed_content_waivers(state, data, detail)
    return Directive(True, state.node, "await_user_confirmation", detail, data=data)


def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    # plugin gates fold into resolve (not verify_final) so a plugin can let the
    # session reach RESOLUTION — where its publish-directive fires — yet still
    # block the final resolve until its sub-condition is met.
    blockers = (gates.blockers(state, "resolution") + plugins.plugin_gate_blockers(state, "resolution")
                + gates.pending_renorm_final_blockers(state))
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty confirmer: --by must name who confirmed resolution"]
    quality = getattr(args, "quality", None)
    if quality is None:
        blockers = blockers + [
            "missing --quality: resolve requires a 1-5 rating (agent-proposed from the "
            "rubric with an eye on this task's in-flight signals, confirmed or adjusted "
            "by the user inside the same resolution AskUserQuestion — see the "
            "quality-regression-investigation runbook)"
        ]
    elif quality not in _VALID_QUALITY_RATINGS:
        blockers = blockers + [f"invalid --quality {quality!r}: must be an integer 1-5"]
    landing_blockers, landing = _landing_gate(state, args, runner)
    blockers = blockers + landing_blockers
    ask_blockers, resolution_ask = _resolution_ask_gate(state, args, channel_ok=not blockers)
    blockers = blockers + ask_blockers
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        return Directive(False, state.node, "fix_stages", "cannot resolve", data={"blockers": blockers})
    state.resolution = GateRecord("resolution", armed=True, passed=True, by=args.by)
    state.pending_renorms = gates.open_renorms(state)
    state.node = transition(state.node, "resolve")  # RESOLUTION -> RESOLVED
    cost_surface: dict = {}
    if state.cost is not None:
        cost_surface = {
            "total_cost_usd": state.cost.total_cost_usd,
            "total_duration_ms": state.cost.total_duration_ms,
            "spawn_count": state.cost.spawn_count,
            "attributed_stages": state.cost.attributed_stages,
            "note": state.cost.note,
        }
        state.log("cost", **{k: v for k, v in cost_surface.items() if k != "note"})
    quality_by = getattr(args, "quality_by", None) or "user-confirmed"
    quality_note = getattr(args, "quality_note", None)
    state.log("resolve", by=args.by, quality=quality, quality_by=quality_by)
    state.log("landing_gate", waiver=landing["waiver"], waiver_source=landing["waiver_source"],
              override=landing["override"])
    if resolution_ask["override"] is not None or resolution_ask["unverifiable"] is not None:
        state.log("resolution_ask_gate", override=resolution_ask["override"],
                  unverifiable=resolution_ask["unverifiable"])
    store.save(state)
    # Session-end cleanup: drop any sidecar a background enumeration wrote for this
    # session, whether or not it was ever folded (e.g. an outstanding child from the
    # LAST replan before resolve, whose result nobody will ever read now).
    enumerate_sidecar.discard_all_for_session(state.session_id)
    tracker_key = getattr(state, "tracker_key", None)
    if not tracker_key and solved_marker.key_shape(state.task_id):
        tracker_key = state.task_id
    # Realized budget-tier labels for this task, for budget-calibration.py to group
    # spend by (kind x tier) and by task-type against. Joined by plan_path — the
    # same key attribute_stage already uses — rather than session_id, whose
    # semantics (the SPAWNING session's CLAUDE_CODE_SESSION_ID) are not guaranteed
    # to equal state.session_id. [] when no spawn ever ran (in-thread task) or
    # plan_path is unset; a spawn row with a missing/null tier is skipped.
    _rows = _cost_rows(args)
    budget_tiers = sorted({
        r["budget_tier"] for r in _rows
        if r.get("plan_path") == state.plan_path and r.get("budget_tier")
    })
    # Effort-divergence surface (the plan's named mitigation for the self-set-estimate
    # weakness): carries both vectors into the quality ledger so the thresholds — and
    # the estimate() formula itself — can be recalibrated against what they actually
    # caught, not just whether this one session happened to fire.
    _now = _effort_now(store, state)
    _effort_ratios = [v for v in effort.ratios(state, now=_now).values() if v is not None]
    quality_row = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "task_id": state.task_id,
        "tracker_key": tracker_key,
        "session": state.session_id,
        "quality": quality,
        "quality_by": quality_by,
        "quality_note": quality_note,
        "resolved_by": args.by,
        "instructions_head": _instructions_head(),
        "n_stages": len(state.stages),
        "n_failed_stage_results": sum(
            1 for h in state.history
            if h.get("event") == "record_result" and h.get("status") == "failed"
        ),
        "n_replans": sum(1 for h in state.history if h.get("event") == "replan"),
        "n_difficulty_records": sum(1 for h in state.history if h.get("event") == "declare"),
        # n_normalizations counts every "normalize" event regardless of in_diagnosis;
        # the outside-diagnosis variant is the subset explicitly flagged
        # in_diagnosis=False (a DIAGNOSING-closure normalize never sets that key at all,
        # so it is correctly excluded here without needing an in_diagnosis=True mirror).
        "n_normalizations": sum(1 for h in state.history if h.get("event") == "normalize"),
        "n_normalizations_outside_diagnosis": sum(
            1 for h in state.history
            if h.get("event") == "normalize" and h.get("in_diagnosis") is False
        ),
        "n_renormalize_replans": sum(1 for h in state.history if h.get("event") == "renormalize"),
        "spawn_count": cost_surface.get("spawn_count", 0),
        "total_cost_usd": cost_surface.get("total_cost_usd"),
        "weight_class": state.weight_class,
        "deliverable_kind": state.deliverable_kind or None,
        "route": state.route,
        "budget_tiers": budget_tiers,
        "effort_estimate": state.effort_estimate,
        "effort_actual": effort.deltas(state, now=_now),
        "effort_ratio_max": max(_effort_ratios) if _effort_ratios else None,
        "effort_fires": state.effort_fires,
        "effort_interactions": state.user_prompt_count,
        "planning_misses_asked": sum(
            1 for m in state.planning_misses if m.get("asked_user")
        ),
        "planning_misses_unasked": sum(
            1 for m in state.planning_misses if not m.get("asked_user")
        ),
        "materialization_defects": len(state.materialization_defects),
        "landing_waiver": landing["waiver"],
        "landing_waiver_source": landing["waiver_source"],
        "landing_gate_override": landing["override"],
        "resolution_ask_gate_override": resolution_ask["override"],
        "resolution_ask_unverifiable": resolution_ask["unverifiable"],
    }
    _write_quality_row(quality_row)
    # Whether to stamp is fully decidable from observed state (resolved + a known
    # tracker key) — a rule, not a judgement — so it runs unconditionally here rather
    # than being left for the coordinator to remember. Belt-and-suspenders around
    # solved_marker.stamp's own fail-open contract: resolution has already happened
    # and must never be undone by a marker failure.
    try:
        marker_status = solved_marker.stamp(tracker_key)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        marker_status = {"channel": None, "key": tracker_key, "stamped": False,
                          "skipped_reason": str(exc)}
    # Same shape as the marker stamp: resolution is already saved, so a cleanup fault
    # is reported, never allowed to undo it. Runs last of the post-resolve effects that
    # act on the working directory, because it may remove the very directory the engine
    # was invoked from.
    try:
        cleanup_report = workspace_cleanup.run(
            state.session_id, cwd=workspace_cleanup.current_dir(),
            delivery_worktree=state.delivery_worktree)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        cleanup_report = {"enabled": True, "dry_run": False, "removed": [],
                          "would_remove": [], "kept": [], "error": str(exc)}
    detail = "task resolved"
    data = {"cost": cost_surface, "quality": quality_row, "solved_marker": marker_status,
            "workspace_cleanup": cleanup_report}
    # Bypass visibility: the resolution summary surfaces every acceptance judge bypass
    # verbatim, so a resolved task's record shows which acceptance passes were unjudged
    # (kill switch) or overridden — never hidden behind a clean COMPLETED.
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f" (with {len(bypasses)} acceptance judge bypass(es); see judge_bypassed)"
    acceptance_bypass = _acceptance_bypass_surface(state)
    if acceptance_bypass is not None:
        data["acceptance_bypass"] = acceptance_bypass
        detail += " (acceptance recorded via bypass; see acceptance_bypass)"
    detail = _disclose_backstop_crossings(state, data, detail)
    detail = _disclose_finished_work(state, data, detail)
    detail = _disclose_agent_plan_conversions(state, data, detail)
    detail = _disclose_landed_content_waivers(state, data, detail)
    if landing["override"] is not None:
        data["landing_gate_override"] = landing["override"]
        detail += (f" (landing gate overridden by AGENTCTL_LANDING_GATE={landing['override']}; "
                   "see landing_gate_override)")
    if resolution_ask["override"] is not None:
        data["resolution_ask_gate_override"] = resolution_ask["override"]
        detail += (f" (resolution-ask gate overridden by AGENTCTL_RESOLUTION_ASK_GATE="
                   f"{resolution_ask['override']}; see resolution_ask_gate_override)")
    if resolution_ask["unverifiable"] is not None:
        data["resolution_ask_unverifiable"] = resolution_ask["unverifiable"]
        detail += " (resolution ask unverifiable, escape used; see resolution_ask_unverifiable)"
    # The crossings are now disclosed and accepted: drop exactly those entries from the task's
    # record, so a reopened task or a later task sharing the id does not disclose them again.
    # Fail-open like the effects above: a fault here is reported, never allowed to lose the
    # disclosure; the entries then stay on record and are disclosed once more.
    try:
        task_accumulator.discard_crossings(
            state.task_id, data["agent_backstop_crossings"], now=_utcnow())
        data["agent_backstop_crossings_cleared"] = True
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        data["agent_backstop_crossings_cleared"] = False
        data["agent_backstop_crossings_clear_error"] = str(exc)
        detail += (" (the task's backstop crossing record could not be cleared: "
                   f"{exc}; see agent_backstop_crossings_clear_error)")
    return Directive(True, state.node, "done", detail, marker="COMPLETED", data=data)


def cmd_reject(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The resolution gate's negative exit (#14): the user rejects the delivery as
    not matching intent. RESOLUTION previously exited ONLY via resolve, so a rejected
    delivery had no engine-tracked edge and stranded the session at the gate.

    reject re-opens the difficulty cycle: it seeds the difficulty record with the
    user's rejection reason AND marks the named stage(s) FAILED — default the final
    stage — so the subsequent replan always has concrete rework to route (a reject is
    never a structural no-op). It then hands off to overcome-difficulty exactly like
    a stage failure: declare -> investigate -> critique -> replan."""
    state = _require(store, args.session)
    if state.node != Node.RESOLUTION.value:
        return Directive(
            False, state.node, "noop",
            f"reject runs only at the resolution gate (node=RESOLUTION); node={state.node}",
        )
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        return Directive(
            False, state.node, "noop",
            "reject requires a non-empty --reason (the intent mismatch the user named)",
        )
    raw = getattr(args, "stage", None) or []
    if raw:
        targets: list[Stage] = []
        for idx in raw:
            try:
                target = state.stage(int(idx))
            except KeyError:
                return Directive(
                    False, state.node, "noop",
                    f"reject --stage {idx} does not exist in the plan",
                )
            if is_skipped(target):
                return Directive(
                    False, state.node, "noop",
                    f"reject --stage {idx} names a SKIPPED stage; a declined optional stage "
                    "is never re-opened (R8: SKIPPED is settled and is never dispatched)",
                )
            targets.append(target)
    else:
        live = [s for s in state.stages if not is_skipped(s)]
        if not live:
            return Directive(False, state.node, "noop", "reject has no stages to re-open")
        targets = [max(live, key=lambda s: s.index)]  # default: the final non-declined stage
    state.node = transition(state.node, "reject")  # RESOLUTION -> DIAGNOSING
    for s in targets:
        s.outcome.status = StageStatus.FAILED.value
    state.current_stage = None
    # Seed the difficulty record with the rejection so the reason is durably
    # captured; the coordinator refines it through declare -> investigate -> critique
    # (which the difficulty_blockers gate still requires complete before replan).
    opened = _open_difficulty(state, declaration=Declaration(
        expected=state.overall_done_criterion or "delivery matches the user's approved intent",
        actual=reason,
        mismatch="user rejected the delivery at the resolution gate (delivered != approved intent)",
    ))
    idxs = [s.index for s in targets]
    rejecter = (getattr(args, "by", None) or "").strip()
    state.log("reject", reason=reason, stages=idxs, **({"by": rejecter} if rejecter else {}))
    store.save(state)
    if opened is None:
        return _difficulty_cap_directive(
            state, f"delivery rejected: {reason}; stage(s) {idxs} re-opened as FAILED",
            {"rejected_stages": idxs, "reason": reason})
    return Directive(
        False, state.node, "declare",
        f"delivery rejected: {reason}; stage(s) {idxs} re-opened as FAILED. Work the "
        "difficulty (declare -> investigate -> critique), then replan.",
        marker="OVERCOME-DIFFICULTY",
        data={"rejected_stages": idxs, "reason": reason},
    )


# --- overcome-difficulty sub-spine: declare -> investigate -> critique --------
# Each command fills one section of the active Difficulty record in order. The
# engine enforces the ORDERING and that each section's artifact exists; the
# CONTENT (what the divergence is, the >=2 hypotheses, the functional ground) is
# the cognition the overcome-difficulty skill supplies.

def cmd_declare(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if _plan_review_release_active(state):
        _sync_release_anchor(state)
        _supersede_for_release(state)
    state.difficulty.declaration = Declaration(
        expected=args.expected, actual=args.actual, mismatch=args.mismatch
    )
    state.log("declare", expected=args.expected, actual=args.actual, mismatch=args.mismatch)
    store.save(state)
    return Directive(True, state.node, "investigate",
                     "declaration recorded; localize the divergence next (investigate)")


def cmd_investigate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if state.difficulty.declaration is None:
        return Directive(False, state.node, "declare",
                         "investigate is out of order: declare the divergence first")
    state.difficulty.investigation = Investigation(
        localized_expectation=args.localized_expectation,
        localized_actual=args.localized_actual,
        hypotheses=list(getattr(args, "hypotheses", None) or []),
    )
    state.log("investigate")
    store.save(state)
    return Directive(True, state.node, "critique",
                     "investigation recorded; state the functional ground + replanning task (critique)")


def cmd_critique(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if state.difficulty.declaration is None or state.difficulty.investigation is None:
        return Directive(False, state.node, "declare",
                         "critique is out of order: declaration and investigation must come first")
    # getattr-with-default: in-process Namespace callers (test_replan.py builds one by
    # hand with only the two required fields) must keep working — an absent field is None.
    failure_address = canonical_failure_address(getattr(args, "failure_address", None))
    # Reject a bogus value HERE (mirroring cmd_normalize's --level check) so an in-process
    # caller bypassing the argparse `choices` is caught before the Critique is built — this
    # (with argparse) is the ONLY validation of the value set. The closure gate
    # (failure_address_blockers) checks only non-None, so a bogus value must never reach a
    # persisted record; that also grandfathers a legacy OLD-value record on load. None is
    # allowed at critique time — the routing may be recorded now or left for a later replan,
    # where the closure gate demands it be present.
    if failure_address is not None and failure_address not in FAILURE_ADDRESS_VALUES:
        return Directive(False, state.node, "critique",
                         f"--failure-address must be one of {list(FAILURE_ADDRESS_VALUES)} "
                         f"or omitted, got {failure_address!r}")
    norm_targets = list(getattr(args, "norm_targets", None) or [])
    if failure_address == "normative" and not norm_targets:
        return Directive(False, state.node, "critique",
                         "a normative critique must name the norm that failed: pass "
                         "--norm-target stage:<n> or the path of the norm file "
                         "(repeatable), the edit the closing replan has to make")
    if norm_targets and failure_address != "normative":
        return Directive(False, state.node, "critique",
                         "--norm-target names the norm of a normative fault; it is "
                         f"refused with --failure-address {failure_address!r}")
    snapshots, refusal = _snapshot_norm_targets(state, norm_targets)
    if refusal:
        return Directive(False, state.node, "critique", refusal)
    inherited = [t for t in state.difficulty.norm_targets if t.get("source", "critique") != "critique"]
    held = {t.get("target") for t in inherited}
    # A re-critique naming a stage again keeps the baseline of the first snapshot: the
    # bar is the norm as it stood when the fault was first routed to it.
    first_baselines = {t.get("target"): t.get("baseline")
                       for t in state.difficulty.norm_targets
                       if t.get("kind") == "stage" and t.get("source", "critique") == "critique"}
    for t in snapshots:
        if t["kind"] == "stage" and t["target"] in first_baselines:
            t["baseline"] = first_baselines[t["target"]]
    state.difficulty.norm_targets = (
        [t for t in snapshots if t["target"] not in held] + inherited)
    state.difficulty.escalated_to = _escalation_link(state.difficulty, state.parent_link)
    state.difficulty.critique = Critique(
        functional_ground=args.functional_ground,
        replanning_task=args.replanning_task,
        invariants_to_preserve=list(getattr(args, "invariants_to_preserve", None) or []),
        differences_to_remove=list(getattr(args, "differences_to_remove", None) or []),
        failure_address=failure_address,
    )
    state.log(
        "critique",
        functional_ground=state.difficulty.critique.functional_ground,
        replanning_task=state.difficulty.critique.replanning_task,
        invariants_to_preserve=state.difficulty.critique.invariants_to_preserve,
        differences_to_remove=state.difficulty.critique.differences_to_remove,
        failure_address=state.difficulty.critique.failure_address,
    )
    store.save(state)
    # Consult (never fire) the same gate cmd_replan enforces: the record now has all
    # three sections, but the gate also shape-checks them (>=2 distinct hypotheses,
    # non-placeholder declaration fields). Announcing "replan unblocked" without
    # reading the gate drifts the moment either side changes shape.
    blockers = gates.difficulty_blockers(state)
    if blockers:
        action = "investigate" if any("investigation" in b for b in blockers) else "declare"
        return Directive(False, state.node, action, "; ".join(blockers), data={"blockers": blockers})
    if any(t.get("kind") == gates.NORM_TARGET_KIND_PARENT
           for t in state.difficulty.norm_targets):
        return Directive(
            True, state.node, "return_replan",
            "the fault is in the norm of the parent stage this session serves, which this "
            "session cannot edit: end the session with `REPLAN:` naming the mismatch -- the "
            "parent's dispatch verifies this record and opens its own difficulty; do not "
            "replan or normalize here",
            marker="REPLAN",
            data={"norm_target": gates.NORM_TARGET_PARENT,
                  "parent_session": (state.parent_link or {}).get("parent_session"),
                  "stage_index": (state.parent_link or {}).get("stage_index")},
        )
    if state.node != Node.DIAGNOSING.value:
        d = Directive(True, state.node, "normalize",
                      "reflection record complete; record the re-norming (normalize --factor) "
                      "-- an agent-authored review override is then admitted once, with "
                      "this record")
    else:
        d = Directive(True, state.node, "replan",
                      "difficulty cycle complete; replan is now unblocked")
    inv = state.difficulty.investigation
    decl = state.difficulty.declaration
    _attach_advisories(d, "hypothesis_distinctness", {
        "hypotheses": inv.hypotheses if inv else [],
        "declaration": {"expected": decl.expected, "actual": decl.actual, "mismatch": decl.mismatch}
        if decl else {},
    }, runner, weight_class=state.weight_class,
       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    return d


def cmd_normalize(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Phase 4 (closure): record the renorming act. Mandatory at DIAGNOSING closure —
    a reproducible factor left un-normed re-fails, so replan is blocked (see
    gates.normalization_blockers) until this records the factor, or the user takes the
    explicit --normalization-waiver escape for a genuinely one-off factor. The LEVEL
    (note/leaf/principle) is payoff-gated and may be omitted, and so may the DESTINATION —
    the functional place the act lands on. The two are ORTHOGONAL: `--level` says how
    generally the record is written down, `--destination` says what is being repaired, and
    every destination is recordable at every level.

    Also callable OUTSIDE DIAGNOSING (any node from CLASSIFIED up to, but not
    including, RESOLVED) as a plain record-only act -- a normalization worth writing
    down does not always arrive framed as a difficulty closure. That branch touches no
    gate and `state.difficulty` only under a plan-review release (the reflection exit's
    record takes the re-norming), and logs the SAME `normalize` event shape
    with `in_diagnosis=False` so every reader of the DIAGNOSING event still recognizes
    it. Dispatch happens FIRST, before any of this function's own validation, so a
    DIAGNOSING call reaches byte-identical code below."""
    state = _require(store, args.session)
    if state.node != Node.DIAGNOSING.value:
        return _cmd_normalize_outside_diagnosing(args, state, store)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    d = state.difficulty
    if d.declaration is None or d.investigation is None or d.critique is None:
        return Directive(False, state.node, "declare",
                         "normalize is out of order: declaration, investigation, and "
                         "critique must come first")
    waiver = getattr(args, "normalization_waiver", None)
    if waiver is not None:
        return _record_normalization_waiver(args, state, store, waiver)
    error, factor = _normalize_record_fields(
        getattr(args, "factor", None), getattr(args, "level", None), getattr(args, "destination", None))
    if error:
        return Directive(False, state.node, "normalize", error)
    level = getattr(args, "level", None)
    destination = canonical_normalization_destination(getattr(args, "destination", None))
    d.normalization = Normalization(factor=factor, level=level, destination=destination)
    _log_normalize_event(state, factor=factor, level=level, destination=destination, in_diagnosis=True)
    store.save(state)
    return Directive(True, state.node, "replan",
                     "renorming recorded; replan is now unblocked")


def cmd_difficulty_open(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Open a NESTED difficulty under the one being worked: the current record is
    suspended on `state.difficulty_stack` (never overwritten, never discarded) and a fresh
    one, one level deeper, becomes the open record the declare/investigate/critique/
    normalize commands act on. At the depth cap (`max-recursion-depth`) nothing is
    recorded and the user decides -- the state is left exactly as it was."""
    state = _require(store, args.session)
    origin = getattr(args, "origin", None) or "stage"
    if origin not in NESTED_DIFFICULTY_ORIGINS:
        return Directive(False, state.node, "noop",
                         f"difficulty-open --origin must be one of "
                         f"{list(NESTED_DIFFICULTY_ORIGINS)}, got {origin!r}")
    bad = _require_diagnosing(state)
    if bad:
        return bad
    parent = state.difficulty
    opened = _open_difficulty(state, origin, nested=True)
    if opened is None:
        return _difficulty_cap_directive(state, "a nested difficulty cannot be opened")
    state.log("difficulty_open", uid=opened.uid, parent_uid=opened.parent_uid,
              level=opened.level, origin=origin, suspended=parent.uid)
    store.save(state)
    return Directive(
        True, state.node, "declare",
        f"nested difficulty {opened.uid} opened at level {opened.level} under "
        f"{parent.uid}; work it through declare -> investigate -> critique -> normalize, "
        f"then difficulty-close",
        data={"uid": opened.uid, "parent_uid": opened.parent_uid, "level": opened.level,
              "origin": origin},
    )


def cmd_difficulty_close(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Close the open NESTED difficulty and resume its parent. The record must be as
    complete as one closing through `replan` (`gates.difficulty_close_blockers`). The
    norm targets it named -- including ones whose file or stage was already edited -- move
    to the parent, tagged with the closed record's uid, so the parent's closing replan
    still has to deliver every norm edit; the closed record joins the parent's `children`.
    The root difficulty has no parent to resume and closes with `replan`."""
    state = _require(store, args.session)
    blockers = gates.difficulty_close_blockers(state)
    _log_gate(state, "difficulty_close", blockers, passed=not blockers)
    if blockers:
        d = state.difficulty
        if state.node != Node.DIAGNOSING.value:
            action = "noop"
        elif not state.difficulty_stack:
            action = "replan"
        elif d is None or not d.complete():
            action = _resume_action(d) if d else "declare"
        elif d.critique.failure_address is None:
            action = "critique"
        else:
            action = "normalize"
        return Directive(False, state.node, action,
                         "difficulty-close refused: " + "; ".join(blockers),
                         data={"blockers": blockers})
    closed = state.difficulty
    parent = state.difficulty_stack.pop()
    held = {t.get("target"): t for t in parent.norm_targets}
    moved: list[str] = []
    for t in closed.norm_targets:
        entry = dict(t)
        entry["source"] = closed.uid
        if entry.get("target") in held:
            # Both records named the target: the closing child's entry (baseline
            # included) replaces the parent's -- the child's is the later snapshot, and
            # keeping the parent's stale digest would hold the file to an edit the
            # child's own cycle has already accounted for.
            held[entry["target"]].update(entry)
        else:
            parent.norm_targets.append(entry)
        moved.append(entry.get("target", ""))
    # A parent-kind target moved onto `parent` is the child's escalation, now carried by
    # the record the child session will return REPLAN from: it needs the typed link too.
    parent.escalated_to = _escalation_link(parent, state.parent_link)
    parent.children.append(closed)
    state.difficulty = parent
    state.log("difficulty_close", uid=closed.uid, parent_uid=parent.uid, level=closed.level,
              moved_norm_targets=moved)
    store.save(state)
    return Directive(
        True, state.node, _resume_action(parent),
        f"nested difficulty {closed.uid} closed; {parent.uid} (level {parent.level}) is the "
        f"open difficulty again" + (f"; norm targets moved to it: {moved}" if moved else ""),
        data={"closed_uid": closed.uid, "parent_uid": parent.uid, "level": parent.level,
              "moved_norm_targets": moved},
    )


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    crossed: list[dict] = []
    reopened: list[int] = []
    reopen_ignored: list[int] = []
    d = _cmd_replan(args, store=store, runner=runner, crossed=crossed, reopened=reopened,
                    reopen_ignored=reopen_ignored)
    d.data.update(_crossing_data(crossed[0] if crossed else None))
    if reopened and d.ok:
        d.data["reopened_stages"] = reopened
        d.detail += f"; re-armed PASSED stage(s) {reopened} (--reopen)"
    if reopen_ignored and d.ok:
        d.data["reopen_ignored"] = reopen_ignored
        d.detail += (f"; --reopen {reopen_ignored} ignored: not PASSED, so there is no "
                     "verdict to re-arm")
    # Only on something to say: an unconditional setdefault would add an empty
    # `advisories` key to every replan Directive, which test_history_capture's
    # exact-equality pin reads as a changed contract.
    advisories = _replan_spelling_advisories(args.plan)
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
    last = state.effort_fires[-1]
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
                data={"effort_fire": _effort_fire_escalation_data(state), "reason": refusal},
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


# --- sub-plan stack: push_subplan / pop_subplan --------------------------------

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


# --- spine orchestrators: collapse the deterministic ceremony into one call -----
# `drive` (opening) and `close` (closing) are THIN orchestrators: they sequence the
# existing cmd_* functions and branch on the Directives those return. They add no
# Node, no machine edge, and no gate of their own — every state mutation is performed
# by a delegated cmd_*, so the engine's invariants hold by construction. Their ONE
# rule beyond sequencing: never auto-cross a human gate. `drive` stops at PLAN_READY
# unless given --approved-by; `close` stops at the resolution gate unless given
# --confirmed-by, and it surfaces resolution blockers (core + plugin-phase) by
# delegating to cmd_resolve, which already aggregates and refuses an empty --by.

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


COMMANDS = {
    "start": cmd_start,
    "reset": cmd_reset,
    "plugin-activate": cmd_plugin_activate,
    "plugin-deactivate": cmd_plugin_deactivate,
    "plugin-record": cmd_plugin_record,
    "ledger-add": cmd_ledger_add,
    "ledger-check": cmd_ledger_check,
    "ledger-candidate": cmd_ledger_candidate,
    "ledger-dispose": cmd_ledger_dispose,
    "ledger-enumerate": cmd_ledger_enumerate,
    "question-raise": cmd_question_raise,
    "question-research": cmd_question_research,
    "question-dispose": cmd_question_dispose,
    "question-rebind": cmd_question_rebind,
    "question-retire": cmd_question_retire,
    "question-list": cmd_question_list,
    "question-check": cmd_question_check,
    "question-enumerate": cmd_question_enumerate,
    "question-enumerate-worker": cmd_question_enumerate_worker,
    "question-enumerate-escape": cmd_question_enumerate_escape,
    "question-candidate-dispose": cmd_question_candidate_dispose,
    "order-raise": cmd_order_raise,
    "order-dispose": cmd_order_dispose,
    "order-list": cmd_order_list,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "plan-render": cmd_plan_render,
    "plan-grants": cmd_plan_grants,
    "plan-resources": plan_resources.cmd_plan_resources,
    "plan-digest": plan_refs.cmd_plan_digest,
    "plan-externalize": plan_refs.cmd_plan_externalize,
    "plan-seal": plan_refs.cmd_plan_seal,
    "plan-convert": plan_convert.cmd_plan_convert,
    "plan-convert-live": plan_convert.cmd_plan_convert_live,
    "submit-plan": cmd_submit_plan,
    "present-plan": cmd_present_plan,
    "confirm-delivery": cmd_confirm_delivery,
    "plan-review": cmd_plan_review,
    "plan-review-delta": cmd_plan_review_delta,
    "plan-review-walk": cmd_plan_review_walk,
    "plan-review-compose": cmd_plan_review_compose,
    "plan-review-pair-history": cmd_plan_review_pair_history,
    "risk-accept": cmd_risk_accept,
    "accept-protected-grant": cmd_accept_protected_grant,
    "stage-review": cmd_stage_review,
    "code-review": cmd_code_review,
    "accept": cmd_accept,
    "approve": cmd_approve,
    "partition": cmd_partition,
    "partition-units": cmd_partition_units,
    "next-stage": cmd_next_stage,
    "dispatch": cmd_dispatch,
    "resolve-permission": cmd_resolve_permission,
    "stage-grants": cmd_stage_grants,
    "evidence-dir": cmd_evidence_dir,
    "workspace-register": cmd_workspace_register,
    "grant-stats": cmd_grant_stats,
    "record-result": cmd_record_result,
    "record-landing": cmd_record_landing,
    "declare": cmd_declare,
    "investigate": cmd_investigate,
    "critique": cmd_critique,
    "normalize": cmd_normalize,
    "difficulty-open": cmd_difficulty_open,
    "difficulty-close": cmd_difficulty_close,
    "verify-final": cmd_verify_final,
    "resolve": cmd_resolve,
    "reject": cmd_reject,
    "replan": cmd_replan,
    "fire-acknowledge": cmd_fire_acknowledge,
    "check-coverage": cmd_check_coverage,
    "effort-check": cmd_effort_check,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "status": cmd_status,
    "children": cmd_children,
    "adopt-escalation": cmd_adopt_escalation,
    "drive": cmd_drive,
    "close": cmd_close,
    "push-subplan": cmd_push_subplan,
    "pop-subplan": cmd_pop_subplan,
    "task-reset": cmd_task_reset,
    "mandate-grant": cmd_mandate_grant,
    "mandate-extend": cmd_mandate_extend,
    "mandate-resume": cmd_mandate_resume,
    "mandate-retry": cmd_mandate_retry,
    "mandate-stop": cmd_mandate_stop,
    "mandate-status": cmd_mandate_status,
}


# --- Argument partition for the `@<path>` convention ---------------------------
#
# Every argument the parser below declares belongs to exactly one of the three
# structures here. scripts/tests/test_argv_text_call_sites.py walks the parser
# itself — the root parser and every subparser — and goes RED on an argument that
# is in none of them, in two of them, or named here but no longer declared.
#
# OWNERSHIP RULE: the change that INTRODUCES an argument registers it here, in
# that same change. The table is exhaustive only as long as it stays exhaustive,
# and nobody re-derives it retroactively.
#
#   _ARG_RESOLVE      narrative free text this process CONSUMES. Resolved through
#                     lib.argv_text before dispatch, so the value may be written
#                     '@<path>' once the text outgrows a single argv string
#                     (Linux MAX_ARG_STRLEN = 131072 bytes).
#   _ARG_FORWARD      narrative free text this process only HANDS ON to a child,
#                     which resolves it at its own boundary. Resolving it here
#                     would inline the text straight back into the child's argv —
#                     the very ceiling the convention exists to stay under.
#   _ARG_DO_NOT_WRAP  identifiers, slugs, digests, file paths, enum-like tokens.
#                     Never prose, so a leading '@' carries no meaning; each entry
#                     states why in one line.

def build_parser(allow_abbrev: bool = True) -> argparse.ArgumentParser:
    """The agentctl parser. `allow_abbrev=False` is the exact-spelling form the own
    channel (scripts/agentctl-own.py) runs: argparse does not pass `allow_abbrev` on to
    a subparser, so it is set on the top parser and on every verb's own."""
    p = argparse.ArgumentParser(
        prog="agentctl", description="deterministic coordination state machine",
        allow_abbrev=allow_abbrev,
    )
    p.add_argument("--state-root", help="override state directory (tests/inspection)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, **kw):
        return sub.add_parser(name, allow_abbrev=allow_abbrev, **kw)

    cli_session.register(sub, add)
    cli_questions.register(sub, add)
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
    sp.add_argument("--scope", default=None,
                    help="'stage:<n>' to bind this review to one stage instead of the "
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
    sp = add("dispatch"); sp.add_argument("--session", required=True)
    # None (not "medium") so cmd_dispatch can tell "flag omitted" apart from "flag
    # explicitly set to medium" and fall through to the stage's declared cost_tier
    # before the "medium" default.
    sp.add_argument("--budget", default=None); sp.add_argument("--complexity", default="medium")
    # None (not e.g. "medium"), same reason as --budget above: lets cmd_dispatch
    # tell "omitted" apart from "explicitly medium" and fall through to the
    # cost_tier-derived default (_EFFORT_BY_COST_TIER) before "medium". Choices
    # mirror spawn-specialist.py's own --effort (the child this argv reaches).
    sp.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default=None,
                    help="claude -p reasoning-effort level for the dispatched child. Optional: "
                    "defaults from the stage's cost_tier (small->low, medium->medium, "
                    "large->high) rather than requiring the caller to classify twice.")
    sp.add_argument("--constraints", default="",
                    help="clarification for the spawned specialist that bounds HOW it does "
                         "the already-approved stage — never a scope or done-criterion change; "
                         "long text as '@<path>' (forwarded, resolved by the specialist itself)")
    sp.add_argument("--dry-run", action="store_true")
    # Stage 6, OPT-IN: re-enter a stage recorded PASSED and re-armed by a
    # substantive replan without paying for a full (re-)spawn, when the replan
    # left the stage's operative surface untouched AND its own control passes
    # NOW. Absent, behavior is byte-identical to today — refusal is the default,
    # never an automatic route.
    sp.add_argument("--re-attest", action="store_true")
    sp = add("resolve-permission"); sp.add_argument("--session", required=True)
    sp.add_argument("--decision", choices=["granted", "denied"], required=True)
    sp.add_argument("--by", default=None,
                    help="who is resolving this request; the reserved identity 'agent' "
                         "(AGENT_ACTOR) attempts a self-grant against the order-approvals "
                         "ledger (REQ5) instead of recording a human decision -- refused "
                         "unless every --rule/--add-dir resolves to a typed resource ALREADY "
                         "covered by a resource the customer approved for this plan's order")
    sp.add_argument("--scope", choices=["once", "project", "global", "stage"], default="once",
                    help="'stage' additionally materializes --rule/--add-dir as a RUNTIME "
                         "grant on the active stage (state.runtime_grants), validated through "
                         "grants.py exactly like a declared grant -- so the next dispatch of "
                         "this stage carries it into the child's --settings/--add-dir")
    sp.add_argument("--rule", dest="rules", action="append", default=None,
                    help="a Bash/Edit/etc rule string to grant (repeatable); meaningful with "
                         "--scope stage or once (refused with project/global, which have no "
                         "materialization path), validated via grants.validate_rule before storage")
    sp.add_argument("--add-dir", dest="add_dirs", action="append", default=None,
                    help="'PATH:MODE' (mode: read|write) to grant (repeatable); meaningful with "
                         "--scope stage or once (refused with project/global, which have no "
                         "materialization path), validated via grants.validate_add_dir before storage")
    sp = add("stage-grants"); sp.add_argument("--session", required=True)
    sp.add_argument("--stage", type=int, default=None,
                    help="stage index to report (defaults to the session's active stage)")
    sp.add_argument("--json", action="store_true")
    sp = add("evidence-dir"); sp.add_argument("--session", required=True)
    sp.add_argument("--stage", type=int, required=True,
                    help="stage index the evidence directory is scoped to")
    sp = add("workspace-register"); sp.add_argument("path")
    sp.add_argument("--kind", choices=workspace_cleanup.KINDS, default=None,
                    help="workspace kind; inferred from --backend when omitted "
                         "(git or none -> git-worktree, any other backend -> mount)")
    sp.add_argument("--backend", dest="ws_backend", default=None,
                    help="workspace backend name (registry.sh), used for mount teardown")
    sp.add_argument("--name", dest="ws_name", default=None,
                    help="mount name the backend teardown is keyed by")
    sp.add_argument("--pending", action="store_true",
                    help="no engine session exists yet (enter-task.sh): record a pending "
                         "binding the engine resolves at `resolve` time by the resolve cwd")
    # Optional: the harness-session auto-injection appends it; --pending ignores it.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("grant-stats"); sp.add_argument("--session", required=True)
    sp.add_argument("--json", action="store_true")
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
    sp = add("declare"); sp.add_argument("--session", required=True)
    sp.add_argument("--expected", required=True); sp.add_argument("--actual", required=True)
    sp.add_argument("--mismatch", required=True)
    sp = add("investigate"); sp.add_argument("--session", required=True)
    sp.add_argument("--localized-expectation", dest="localized_expectation", required=True)
    sp.add_argument("--localized-actual", dest="localized_actual", required=True)
    sp.add_argument("--hypothesis", dest="hypotheses", action="append", default=None,
                    help="a candidate hypothesis (repeatable; >=2 required for a complete record)")
    sp = add("critique"); sp.add_argument("--session", required=True)
    sp.add_argument("--functional-ground", dest="functional_ground", required=True)
    sp.add_argument("--replanning-task", dest="replanning_task", required=True)
    sp.add_argument("--invariant-to-preserve", dest="invariants_to_preserve",
                    action="append", default=None,
                    help="a similarity the corrected plan must PRESERVE as a condition/"
                         "invariant (repeatable); the engine verifies coverage on replan")
    sp.add_argument("--difference-to-remove", dest="differences_to_remove",
                    action="append", default=None,
                    help="a difference whose removal requires a CHANGED means/method "
                         "(repeatable); the engine verifies a means/method changed on replan")
    sp.add_argument("--failure-address", dest="failure_address", default=None,
                    choices=list(FAILURE_ADDRESS_VALUES) + list(FAILURE_ADDRESS_ALIASES),
                    metavar="{" + ",".join(FAILURE_ADDRESS_VALUES) + "}",
                    help="route the fault to the inadequate provision (R2): resource "
                         "(resource provision — material/tool) | normative "
                         "(normative provision — norm/method) | not_applicable (routing "
                         "does not apply); the original Russian spellings are accepted as aliases; "
                         "the closure gate demands it on a difficulty-closing replan")
    sp.add_argument("--norm-target", dest="norm_targets", action="append", default=None,
                    help="the norm a normative fault names, repeatable and required with "
                         "--failure-address normative: `stage:<n>` (a stage of the current "
                         "plan) or the absolute path of an existing norm file (a linked "
                         "worktree of the instructions repo, a memory leaf, or a file under "
                         "the agent home's skills/ or plans/); the closing replan must "
                         "change it. `parent` (alone, in a session linked to a parent "
                         "stage) names that stage's norm instead: the session hands the "
                         "fault back with REPLAN and the parent's dispatch verifies the "
                         "record")
    sp = add("normalize"); sp.add_argument("--session", required=True)
    sp.add_argument("--factor", default=None,
                    help="the reproducible cause the difficulty exposed, being re-normed "
                         "(the ACT is mandatory at closure); required unless "
                         "--normalization-waiver is given")
    sp.add_argument("--normalization-waiver", dest="normalization_waiver", default=None,
                    help="record the open difficulty as exposing a genuinely one-off factor "
                         "instead of a re-norming; a reason, stored on the record and what "
                         "lets a nested difficulty close without a factor (refused if empty, "
                         "alongside --factor, and for a record that names norm targets)")
    sp.add_argument("--level", dest="level", default=None, choices=list(NORMALIZATION_LEVELS),
                    help="recording level (payoff-gated by rediscovery-threshold-min); omit "
                         "for an in-head note below the leaf threshold")
    sp.add_argument("--destination", dest="destination", default=None,
                    choices=list(NORMALIZATION_DESTINATIONS) + list(NORMALIZATION_DESTINATION_ALIASES),
                    metavar="{" + ",".join(NORMALIZATION_DESTINATIONS) + "}",
                    help="the functional place the renorming lands on — material | tool "
                         "| norm | method | knowledge (the original Russian spellings are "
                         "accepted as aliases); ORTHOGONAL to --level (any destination "
                         "at any level), omit when the place is not being recorded")
    sp = add("difficulty-open"); sp.add_argument("--session", required=True)
    sp.add_argument("--origin", dest="origin", default="stage",
                    choices=list(NESTED_DIFFICULTY_ORIGINS),
                    help="why the nested difficulty exists: it arose while replanning "
                         "(replan), while re-norming (normalize), or from the stage's "
                         "own work (stage)")
    sp = add("difficulty-close"); sp.add_argument("--session", required=True)
    sp = add("verify-final"); sp.add_argument("--session", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp = add("resolve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--quality", type=int, choices=list(_VALID_QUALITY_RATINGS), default=None,
                    help="1-5 rating, agent-proposed and user-confirmed/adjusted in the "
                         "resolution AskUserQuestion; refused if absent")
    sp.add_argument("--quality-by", dest="quality_by", default="user-confirmed",
                    help="'user-confirmed' (default), 'user-adjusted', or 'user-other' "
                         "(free-text answer)")
    sp.add_argument("--quality-note", dest="quality_note", default=None)
    sp.add_argument("--landing-waiver", dest="landing_waiver", default=None,
                    help="reason this task has no landing to assert; accepted only when the "
                         "plan has no delivery_worktree (a git delivery venue declares its "
                         "waiver in the plan)")
    sp.add_argument("--resolution-ask-unverifiable", dest="resolution_ask_unverifiable",
                    default=None,
                    help="why the session transcript is unlocatable or unreadable, so the "
                         "answered [resolution-ask] AskUserQuestion cannot be verified; "
                         "refused whenever the transcript is readable")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG); "
                         "read to derive the realized budget_tiers for the quality row")
    sp = add("reject"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", default=None, type=str.strip,
                    help="who rejected the delivery — a name, not a narrative; recorded on the "
                         "reject row (a `mandate:<id>` rejection does not clear the since-user counters)")
    sp.add_argument("--reason", required=True,
                    help="the intent mismatch the user named when rejecting the delivery "
                         "(seeds the difficulty record)")
    sp.add_argument("--stage", dest="stage", action="append", default=None, type=int,
                    help="plan stage index to re-open as FAILED (repeatable; "
                         "defaults to the final stage so a reject is never a no-op)")
    sp = add("replan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
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

    sp = add("task-reset", help="explicit renegotiation: zero the cross-session "
             "task accumulator (item B) for --task — never called from `reset`")
    # Session-independent by design (cmd_task_reset's own docstring), but
    # _inject_default_session unconditionally appends --session <harness> when
    # $CLAUDE_CODE_SESSION_ID is set (i.e. every real invocation inside a
    # Claude Code session) — mirror plan-render's suppressed-absorb pattern so
    # that injection doesn't crash argparse with "unrecognized arguments".
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp.add_argument("--task", required=True, help="task_id whose accumulator to zero")
    sp.add_argument("--reason", required=True,
                    help="why this task's accumulated cross-session friction is being forgiven")
    sp.add_argument("--by", required=True,
                    help="the customer authorizing the reset; the coordinator is refused")

    def add_mandate(name: str, help_: str, *, by: bool = False) -> argparse.ArgumentParser:
        sub = add(name, help=help_)
        sub.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
        sub.add_argument("--id", dest="mandate_id", default=mandate_rules.DEFAULT_MANDATE_ID,
                         help="mandate slug (default: %(default)s)")
        if by:
            sub.add_argument("--by", required=True,
                             help="the customer authorizing this; the coordinator is refused")
        return sub

    sp = add_mandate("mandate-grant", "create the standing mandate for the background debt cycle", by=True)
    sp.add_argument("--days", type=float, default=None, help="term length in days")
    for limit, unit in (
        ("daily_usd", "USD"), ("weekly_usd", "USD"), ("item_usd_cap", "USD"),
        ("item_minutes_cap", "minutes"), ("cycle_minutes_cap", "minutes"),
        ("veto_window_hours", "hours"),
    ):
        sp.add_argument(f"--{limit.replace('_', '-')}", dest=limit, type=float, default=None,
                        help=f"override the default ({unit})")
    sp.add_argument("--max-items", dest="max_items", type=int, default=None,
                    help="items taken per cycle")
    sp = add_mandate("mandate-extend", "push the mandate's expiry out", by=True)
    sp.add_argument("--days", type=float, required=True, help="days to add past the later of now and expiry")
    add_mandate("mandate-resume", "unpause the mandate and close its breaker", by=True)
    sp = add_mandate("mandate-retry", "let the cycle take an issue again after an item-local failure", by=True)
    sp.add_argument("--issue", type=int, required=True, help="the issue number to allow again")
    add_mandate("mandate-stop", "pause the mandate, disable its timer, kill a running cycle")
    add_mandate("mandate-status", "show the mandate, its spend windows and whether a cycle may start")
    return p


def main(argv: list[str] | None = None, *, allow_abbrev: bool = True) -> int:
    harness = os.environ.get("CLAUDE_CODE_SESSION_ID")
    raw = _inject_default_session(
        list(sys.argv[1:] if argv is None else argv), harness
    )
    args = build_parser(allow_abbrev).parse_args(raw)
    resolve_arg_text(args)
    session = getattr(args, "session", None)
    binds = bool(harness and session and not args.state_root)
    store = (
        _BindingStore(harness, session) if binds
        else FileStateStore(Path(args.state_root) if args.state_root else None)
    )
    fn = COMMANDS[args.command]
    try:
        directive = fn(args, store=store)
    except Exception as exc:  # surface as a failed directive, not a traceback
        directive = Directive(False, "(error)", "error", str(exc))
    else:
        _fire_plugins(args, store, directive)
    if harness and session and session != harness and not _is_followed_by_gate(harness, session):
        print(
            f"agentctl: warning: --session {session!r} differs from "
            f"CLAUDE_CODE_SESSION_ID {harness!r} and is not the session this harness "
            f"session last drove with a state-changing command, so the production-edit "
            f"gate does not follow it: an Edit from the harness session is decided by "
            f"another session until a state-changing command drives {session!r}.",
            file=sys.stderr,
        )
    print(json.dumps(directive.to_dict(), ensure_ascii=False, indent=2))
    return 0 if directive.ok else 1


if __name__ == "__main__":
    sys.exit(main())
