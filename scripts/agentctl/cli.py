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
import json
import os
import sys
import tempfile
from pathlib import Path
# Re-exported: read as agentctl.cli.<name> by tests.
from lib import hook_telemetry
from . import (
    gates,
    grants as _grants,
    order_approvals,
    plan_convert,
    plan_refs,
    plan_resources,
    cli_session,
    cli_questions,
    cli_order,
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
# Re-exported: read as agentctl.cli.<name> by tests.
from .config import Thresholds
from .directive import Directive
from .dispatch import subprocess_runner
from .plan import load_plan, load_plan_with_digest, plan_meta_digest, plan_stage_digests
from .render import cmd_plan_grants, cmd_plan_render
from .state import canonical_normalization_destination, PARENT_DIFFICULTY_ENV, PermissionRequest
from .store import FileStateStore
# Re-exported: read as agentctl.cli.<name> by scripts/agentctl/plan_convert.py, scripts/mandate_cycle/driver.py, scripts/spawn-specialist.py, 43 test files.
from .cli_common import (
    REPO_ROOT,
    _observation_sha256,
    _record_stage_review,
    _write_plan_version_snapshot,
    _read_plan_version_snapshot,
    _ledgered_order_key,
    _effort_cross_totals,
    _flush_order_effort,
    _difficulty_id,
    _archive_difficulty,
    _adopt_norm_handoff,
    _plan_review_release_active,
    handoff_vcs_resources,
    _handoff_held,
    _autonomy_for,
    _agent_review_override,
    _baseline_plan_for_tier,
    _frozen_edge_stages,
    _grounding_exempt_stages,
    _grounding_inputs,
    _generalizations_block,
    _generalizations_missing,
    _grounding_problems,
    _CRITERION_ENGINE_WRITTEN_FIELDS,
    _apply_refined_stage_fields,
    _log_gate,
    _submission_problems,
    _VENUE_IDENTITY_GIT_TIMEOUT_S,
    _VenueSnapshot,
    _venue_tree_identity,
    _landed_check_result,
    _require,
    _present_plan_transcript,
    _review_cycle_arrive,
    EvidenceDirError,
    _descendant_sessions,
    _refresh_spend,
    RESOLUTION_ASK_BLOCKER_PREFIX,
    _ARG_DO_NOT_WRAP,
    _ARG_FORWARD,
    _ARG_RESOLVE,
    _ROOT,
    _SESSION_COMMANDS,
    resolve_arg_text,
)
from .cli_common import _BindingStore, _fire_plugins, _inject_default_session, _is_followed_by_gate
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
    cmd_question_enumerate_worker,
    cmd_question_enumerate_escape,
    cmd_question_enumerate,
)
# Re-exported: read as agentctl.cli.<name> by 4 test files.
from .cli_questions import _bound_order_stage_key, _upsert_candidate, _apply_enumeration_result
from .cli_order import cmd_order_submit, cmd_order_agree, cmd_order_challenge_dismiss
from .cli_plan import (
    cmd_classify,
    cmd_plan,
    cmd_submit_plan,
    cmd_present_plan,
    cmd_plan_generalizations,
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
# Re-exported: read as agentctl.cli.<name> by scripts/review-cost-calibrate.py, 7 test files.
from .cli_review import (
    ESCALATION_LEDGER_ENV,
    _changed_parts,
    TOPOLOGICAL_COMPOSITION_REVIEWER,
    _is_counted_review,
    _count_plan_review_round,
    _count_review_act,
)
from .cli_approval import cmd_approve, cmd_partition, cmd_partition_units, cmd_next_stage
# Re-exported: read as agentctl.cli.<name> by 7 test files.
from .cli_approval import (
    _stamp_accepted_plan_digest,
    _refresh_caches_from_plan_path,
    _review_cycle_close,
    _parse_partition_units,
    _rekey_runtime_grants,
)
from .cli_dispatch import (
    cmd_dispatch,
    cmd_resolve_permission,
    cmd_evidence_dir,
    cmd_workspace_register,
    cmd_stage_grants,
    cmd_grant_stats,
)
# Re-exported: read as agentctl.cli.<name> by scripts/spawn-specialist.py, 11 test files.
from .cli_dispatch import (
    _is_recursion_refusal,
    _is_child_outside_project_refusal,
    _parse_rule_lines,
    _refusal_vcs_resources,
    _continuation_worktree,
    _parse_transcript_path,
    _parse_rule_line,
    _self_grant_resources_for_rule,
    stage_branch_resources,
    _stage_grant_entries,
    _stage_would_derive_grants,
    _effective_stage_grants,
    _effective_stage_grants_for_self_grant,
    _classify_transcript_denials,
    _covers_resource,
    evidence_dir_for,
)
from .cli_results import cmd_record_landing, cmd_record_result
# Re-exported: read as agentctl.cli.<name> by 3 test files.
from .cli_results import _freeze_delivered_head, _needs_delivered_head_freeze
from .cli_difficulty import (
    cmd_declare,
    cmd_investigate,
    cmd_critique,
    cmd_normalize,
    cmd_difficulty_open,
    cmd_difficulty_close,
)
# Re-exported: read as agentctl.cli.<name> by 1 test file.
from .cli_difficulty import _session_plan_paths
from .cli_final import cmd_verify_final, cmd_resolve, cmd_reject
# Re-exported: read as agentctl.cli.<name> by 3 test files.
from .cli_final import (
    _backstop_crossings,
    _disclose_backstop_crossings,
    _GitTreeProbe,
    RESOLUTION_ASK_RULE_HINT,
)
from .cli_replan import cmd_replan, cmd_fire_acknowledge, cmd_check_coverage, cmd_effort_check
# Re-exported: read as agentctl.cli.<name> by 8 test files.
from .cli_replan import (
    _refresh_approved_grant_snapshot,
    _approved_snapshot_doc,
    _TRIGGER_EXIT_FRESH_PASS,
    _drop_agent_overrides,
    _kind_within_boundary,
    _replan_kind,
    _refresh_runtime_grant_titles,
)
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
    cmd_mandate_limits,
    cmd_mandate_resume,
    cmd_mandate_retry,
    cmd_mandate_stop,
    cmd_mandate_status,
)


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
    "order-submit": cmd_order_submit,
    "order-agree": cmd_order_agree,
    "order-challenge-dismiss": cmd_order_challenge_dismiss,
    "order-list": cmd_order_list,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "plan-render": cmd_plan_render,
    "plan-grants": cmd_plan_grants,
    "plan-generalizations": cmd_plan_generalizations,
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
    "mandate-limits": cmd_mandate_limits,
    "mandate-resume": cmd_mandate_resume,
    "mandate-retry": cmd_mandate_retry,
    "mandate-stop": cmd_mandate_stop,
    "mandate-status": cmd_mandate_status,
}


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
    cli_order.register(sub, add)
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
