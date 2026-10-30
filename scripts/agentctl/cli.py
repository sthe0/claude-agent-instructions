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
    dropped_grants_report,
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
    stage_branch_resources,
    _refusal_vcs_resources,
    _REFUSAL_VCS_RESOURCE_RE,
    handoff_vcs_resources,
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
    _handoff_held,
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
    cmd_question_enumerate_escape,
    cmd_question_enumerate,
)
from .cli_plan import (
    cmd_classify,
    cmd_plan,
    cmd_submit_plan,
    cmd_present_plan,
    cmd_confirm_delivery,
)
from .cli_review import (
    cmd_plan_review,
    cmd_accept_protected_grant,
    cmd_risk_accept,
    cmd_plan_review_delta,
    cmd_plan_review_pair_history,
    cmd_plan_review_walk,
    cmd_plan_review_compose,
    cmd_stage_review,
    cmd_code_review,
    cmd_accept,
)
from .cli_approval import cmd_approve, cmd_partition, cmd_partition_units, cmd_next_stage
from .cli_dispatch import (
    cmd_dispatch,
    cmd_resolve_permission,
    cmd_evidence_dir,
    cmd_workspace_register,
    cmd_stage_grants,
    cmd_grant_stats,
)
from .cli_results import cmd_record_landing, cmd_record_result
from .cli_difficulty import (
    cmd_declare,
    cmd_investigate,
    cmd_critique,
    cmd_normalize,
    cmd_difficulty_open,
    cmd_difficulty_close,
)
from . import (
    cli_session,
    cli_questions,
    cli_plan,
    cli_review,
    cli_approval,
    cli_dispatch,
    cli_results,
    cli_difficulty,
)









# --- the difficulty stack ---------------------------------------------------
# `state.difficulty` is the open (top) record; `state.difficulty_stack` holds its
# suspended ancestors. EVERY record is created by `_open_difficulty` -- the automatic
# sites (a failed result, a failed final check, a rejection, an effort divergence, a venue
# or materialization diagnosis), the lazy creation behind `declare`, and the explicit
# `difficulty-open` -- so the depth cap and the "never silently discard a declared record"
# rule have exactly one place to live.

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
    cli_plan.register(sub, add)
    cli_review.register(sub, add)
    cli_approval.register(sub, add)
    cli_dispatch.register(sub, add)
    cli_results.register(sub, add)
    cli_difficulty.register(sub, add)
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
