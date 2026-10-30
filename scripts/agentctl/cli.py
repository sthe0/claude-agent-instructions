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
    _refusal_vcs_resources,
    handoff_vcs_resources,
    _REFUSAL_VCS_RESOURCE_RE,
    stage_branch_resources,
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
    cmd_question_enumerate,
    cmd_question_enumerate_escape,
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
from . import cli_session, cli_questions, cli_plan, cli_review, cli_approval






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
        rules += [
            f"vcs_ref {r.op} {r.remote}:{r.ref}" for r in _refusal_vcs_resources(result.stdout)
        ]
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
                f"{[_resources.resource_to_dict(r) for r in self_grant_resources]!r} "
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
                resources=[_resources.resource_to_dict(r) for r in self_grant_resources],
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
                    "resources": [_resources.resource_to_dict(r) for r in self_grant_resources],
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
            self_approved.extend(self_coverage.resources)
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
        if by.casefold() != AGENT_ACTOR:
            for resource in _refusal_vcs_resources(req.raw):
                new_entries.append({
                    "resource": _resources.resource_to_dict(resource), "provenance": "runtime",
                    "consumed": False, "stage_title": stage.title, "scope": scope,
                    "granted_by": by,
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

    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    resource_set = plan_resources.resource_set_from_entries(
        all_entries, stage, venue, stage_branch_resources(state, stage_index)).to_dict()
    data = {
        "stage": stage_index,
        "executor": stage.actor.executor,
        "grants": all_entries,
        "resources": resource_set["resources"],
        "rule_patterns": resource_set["rule_patterns"],
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
    cli_plan.register(sub, add)
    cli_review.register(sub, add)
    cli_approval.register(sub, add)
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
