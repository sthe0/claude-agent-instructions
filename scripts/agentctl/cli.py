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
    cmd_question_enumerate,
    cmd_question_enumerate_escape,
    cmd_question_enumerate_worker,
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
from .cli_final import cmd_verify_final, cmd_resolve, cmd_reject
from .cli_replan import cmd_replan, cmd_fire_acknowledge, cmd_check_coverage, cmd_effort_check
from .cli_control import (
    cmd_block,
    cmd_unblock,
    cmd_status,
    cmd_adopt_escalation,
    cmd_children,
    cmd_push_subplan,
    cmd_pop_subplan,
    cmd_drive,
    cmd_close,
)
from .cli_mandate import (
    cmd_task_reset,
    cmd_mandate_grant,
    cmd_mandate_extend,
    cmd_mandate_resume,
    cmd_mandate_retry,
    cmd_mandate_stop,
    cmd_mandate_status,
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
    cli_final,
    cli_replan,
    cli_control,
    cli_mandate,
)













# --- the difficulty stack ---------------------------------------------------
# `state.difficulty` is the open (top) record; `state.difficulty_stack` holds its
# suspended ancestors. EVERY record is created by `_open_difficulty` -- the automatic
# sites (a failed result, a failed final check, a rejection, an effort divergence, a venue
# or materialization diagnosis), the lazy creation behind `declare`, and the explicit
# `difficulty-open` -- so the depth cap and the "never silently discard a declared record"
# rule have exactly one place to live.

# --- commands -------------------------------------------------------------

# --- overcome-difficulty sub-spine: declare -> investigate -> critique --------
# Each command fills one section of the active Difficulty record in order. The
# engine enforces the ORDERING and that each section's artifact exists; the
# CONTENT (what the divergence is, the >=2 hypotheses, the functional ground) is
# the cognition the overcome-difficulty skill supplies.

# --- sub-plan stack: push_subplan / pop_subplan --------------------------------

# --- spine orchestrators: collapse the deterministic ceremony into one call -----
# `drive` (opening) and `close` (closing) are THIN orchestrators: they sequence the
# existing cmd_* functions and branch on the Directives those return. They add no
# Node, no machine edge, and no gate of their own — every state mutation is performed
# by a delegated cmd_*, so the engine's invariants hold by construction. Their ONE
# rule beyond sequencing: never auto-cross a human gate. `drive` stops at PLAN_READY
# unless given --approved-by; `close` stops at the resolution gate unless given
# --confirmed-by, and it surfaces resolution blockers (core + plugin-phase) by
# delegating to cmd_resolve, which already aggregates and refuses an empty --by.

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
    cli_final.register(sub, add)
    cli_replan.register(sub, add)
    cli_control.register(sub, add)
    cli_mandate.register(sub, add)
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
