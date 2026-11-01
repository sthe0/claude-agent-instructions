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
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from lib import argv_text, engine_session_binding, hook_telemetry
from . import (
    gates,
    grants as _grants,
    order_approvals,
    plan_convert,
    plan_refs,
    plan_resources,
    plugins,
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
from .config import Thresholds
from .directive import Directive
from .dispatch import subprocess_runner
from .plan import load_plan, load_plan_with_digest, plan_meta_digest, plan_stage_digests
from .render import cmd_plan_grants, cmd_plan_render
from .state import canonical_normalization_destination, PARENT_DIFFICULTY_ENV, PermissionRequest
from .store import FileStateStore, StateStore, safe_session_id as _safe_session_id
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
    cmd_question_enumerate_worker,
    cmd_question_enumerate_escape,
    cmd_question_enumerate,
)
# Re-exported: read as agentctl.cli.<name> by 4 test files.
from .cli_questions import _bound_order_stage_key, _upsert_candidate, _apply_enumeration_result
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
    cmd_mandate_resume,
    cmd_mandate_retry,
    cmd_mandate_stop,
    cmd_mandate_status,
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

_ROOT = "<root>"  # sentinel subcommand key for the top-level parser's own options


# Every subcommand that takes --session. Spelled out rather than derived: a
# derivation would silently classify the next subcommand someone adds.
_SESSION_COMMANDS = (
    "start", "reset", "plugin-activate", "plugin-deactivate", "plugin-record",
    "ledger-add", "ledger-check", "ledger-candidate", "ledger-dispose",
    "ledger-enumerate", "question-raise", "question-research", "question-dispose",
    "question-rebind", "question-retire", "question-list", "question-check",
    "question-enumerate", "question-enumerate-worker", "question-enumerate-escape",
    "question-candidate-dispose",
    "order-raise", "order-dispose", "order-list", "classify", "plan",
    "plan-render", "plan-grants", "plan-resources", "plan-digest", "plan-externalize",
    "plan-seal", "plan-convert", "plan-convert-live", "submit-plan", "present-plan", "confirm-delivery", "plan-review",
    "plan-review-delta", "plan-review-walk", "plan-review-compose", "plan-review-pair-history",
    "risk-accept", "accept-protected-grant", "stage-review", "code-review", "accept", "approve",
    "partition", "partition-units", "next-stage", "dispatch", "resolve-permission", "stage-grants", "evidence-dir", "grant-stats",
    "record-result", "record-landing", "declare",
    "investigate", "critique", "normalize", "difficulty-open", "difficulty-close",
    "verify-final", "resolve", "reject",
    "replan", "fire-acknowledge", "check-coverage", "effort-check", "block", "unblock", "status", "children",
    "adopt-escalation", "drive", "close", "push-subplan", "pop-subplan", "task-reset",
    "mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry", "mandate-stop", "mandate-status",
    "workspace-register",
)


# (dest, subcommands that declare it)
_RESOLVE_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("goal", ("start", "reset")),
    ("done_criterion", ("start", "reset")),
    ("note", ("plugin-record", "confirm-delivery", "plan-review", "stage-review", "code-review",
              "accept", "question-enumerate-escape", "fire-acknowledge", "accept-protected-grant")),
    ("statement", ("ledger-add", "ledger-candidate")),
    ("source", ("ledger-add", "question-dispose")),
    ("premises", ("ledger-add",)),
    ("basis", ("ledger-add", "question-dispose", "risk-accept")),
    ("reason", ("ledger-dispose", "question-retire", "question-candidate-dispose",
                "order-dispose", "reject", "block", "task-reset", "replan")),
    ("element", ("order-raise",)),
    ("question", ("question-raise", "question-candidate-dispose")),
    ("attempted", ("question-research",)),
    ("answer", ("question-dispose",)),
    ("derivation", ("question-dispose",)),
    ("risk", ("question-dispose", "risk-accept")),
    ("confirm_still_valid", ("question-rebind",)),
    ("concerns", ("plan-review", "stage-review", "code-review")),
    ("customer_questions", ("plan-review",)),
    ("observation", ("stage-review", "record-result", "close")),
    ("actual", ("record-result", "declare", "close")),
    ("control", ("record-result", "close")),
    ("expected", ("declare",)),
    ("mismatch", ("declare",)),
    ("localized_expectation", ("investigate",)),
    ("localized_actual", ("investigate",)),
    ("hypotheses", ("investigate",)),
    ("functional_ground", ("critique",)),
    ("replanning_task", ("critique",)),
    ("invariants_to_preserve", ("critique",)),
    ("differences_to_remove", ("critique",)),
    ("factor", ("normalize",)),
    ("normalize_factor", ("replan",)),
    ("quality_note", ("resolve", "close")),
    ("landing_waiver", ("resolve", "close")),
    ("resolution_ask_unverifiable", ("resolve", "close")),
    ("coverage_waiver", ("replan",)),
    ("normalization_waiver", ("replan", "normalize")),
    ("renegotiation_note", ("replan",)),
    ("bypass_reason", ("accept",)),
    ("reopen_reason", ("reset",)),
    ("reopen_user_decision", ("reset",)),
    ("rejection_text", ("present-plan",)),
    ("accept_content_loss", ("record-landing",)),
)


# (dest, subcommands that declare it, why '@' means nothing here)
_DO_NOT_WRAP_ROWS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("state_root", (_ROOT,), "directory path the state store is rooted at"),
    ("session", _SESSION_COMMANDS, "session id — the slug state is keyed by"),
    ("task", ("start", "reset", "push-subplan", "task-reset"), "task slug, not a description"),
    ("child", ("adopt-escalation",), "session id of the linked child"),
    ("criterion_type", ("start", "reset"), "one of two fixed verification kinds"),
    ("plugin", ("plugin-activate", "plugin-deactivate", "plugin-record"), "plugin registry name"),
    ("phase", ("plugin-record",), "plugin phase name from a fixed vocabulary"),
    ("attachment", ("plugin-record",), "ticket attachment id or URL, not a file to read"),
    ("comment", ("plugin-record",), "ticket comment id or URL, not a file to read"),
    ("tracker_key", ("plugin-activate", "classify", "drive"), "tracker issue key (ABC-123)"),
    ("id", ("ledger-add", "ledger-candidate", "ledger-dispose", "question-raise",
            "question-research", "question-dispose", "question-rebind", "question-retire",
            "question-candidate-dispose", "order-raise", "order-dispose", "accept-protected-grant"),
     "claim / question / order-element / protected-action id"),
    ("claim", ("ledger-dispose",), "id of the grounding claim, not its text"),
    ("artifact", ("ledger-enumerate",), "path to the deliverable being cross-checked"),
    ("target", ("question-raise", "plan-review", "plan-review-walk", "plan-review-compose",
                "plan-review-pair-history"),
     "plan element address or plan file path"),
    ("pair", ("plan-review-pair-history",), "review pair id (`<base>-<service>`) or unit id (`unit:<node>`) — an id, not narrative"),
    ("control", ("question-raise",),
     "structured control address matched against controls.MATERIALITY_GRAMMARS — a "
     "grammar-bound name, never the prose --control of record-result/close"),
    ("plan", ("plan-render", "plan-grants", "plan-resources", "plan-digest", "plan-externalize",
              "plan-seal", "plan-convert", "plan-review-delta", "submit-plan", "replan", "drive",
              "push-subplan", "question-enumerate", "question-enumerate-worker",
              "question-enumerate-escape", "question-dispose",
              "question-rebind", "question-raise", "present-plan", "order-dispose"),
     "plan file path"),
    ("out", ("plan-convert",), "output directory path for the converted tree"),
    ("field", ("plan-externalize",), "name of a plan field — an identifier, not narrative"),
    ("meta_field", ("plan-externalize",), "name of a [meta] field — an identifier, not narrative"),
    ("order_field", ("plan-externalize",),
     "name of a [meta.order] field — an identifier, not narrative"),
    ("corpus", ("plan-resources",), "directory/ies to scan for *.toml plans — file paths, not narrative"),
    ("commands_file", ("plan-resources",), "path to a file of literal command lines — a file path"),
    ("dump_commands", ("plan-resources",), "output file path for the collected command list"),
    ("report_json", ("plan-resources",), "output file path for the JSON resolution report"),
    ("digest", ("question-enumerate-worker",),
     "plan content digest the launcher computed — the sidecar's key, passed down "
     "verbatim rather than a narrative"),
    ("stages", ("question-enumerate-worker",),
     "comma-separated stage indices the launcher narrowed the pass to"),
    ("new", ("check-coverage",), "corrected plan file path — the object under a coverage pre-check, not narrative"),
    ("rendering_file", ("present-plan",), "path to the rendered presentation"),
    ("mandate_id", ("mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry",
                    "mandate-stop", "mandate-status"),
     "mandate slug the state directory is keyed by"),
    ("by", ("confirm-delivery", "approve", "resolve", "fire-acknowledge", "resolve-permission",
            "task-reset", "mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry"),
     "who acted — a name, not a narrative (resolve-permission's reserved 'agent' value is an "
     "identity token like any other --by, not free text)"),
    # --decision is NOT listed here: argparse `choices=` already makes it a non-candidate
    # for the @<path> partition (test_argv_text_call_sites.py's _is_candidate excludes any
    # action with choices set), so classifying it would be a stale entry the moment it's added.
    ("escape_reason", ("confirm-delivery",),
     "one token from delivery.DELIVERY_ESCAPE_REASONS — the narrative half of "
     "the escape is --note, which is RESOLVE"),
    ("reviewer", ("plan-review", "stage-review", "code-review"), "reviewer name"),
    ("plan_digest", ("plan-review",), "sha256 the review binds to"),
    ("scope", ("plan-review", "risk-accept"), "'' or 'stage:<n>' or 'topo:<pair-or-unit>' — the review's binding, not narrative"),
    ("concern_ids", ("plan-review",),
     "explicit stable ids for --concern, positionally paired — ids, not narrative"),
    ("concern_id", ("risk-accept",), "the concern id this acceptance answers — an id, not narrative"),
    ("norm_targets", ("critique",),
     "norm-target address (`stage:<n>` or a repo path) the critique must edit — an address, not narrative"),
    ("code_ref", ("code-review", "record-result"), "commit / PR reference the verdict binds to"),
    ("head", ("record-landing",), "commit the landing pushed — a revision, not narrative"),
    ("base", ("record-landing",), "commit the landing was rebased onto — a revision, not narrative"),
    ("unit", ("partition", "partition-units"), "'|'-delimited partition-unit record"),
    ("author", ("accept", "risk-accept"), "acceptance author id — an identity token, not narrative"),
    ("renegotiated_by", ("replan",), "who made the renegotiation decision — a name, not a narrative"),
    ("verdict", ("accept",), "'|'-delimited requirement-verdict record"),
    ("budget", ("dispatch",), "budget tier name"),
    ("complexity", ("dispatch",), "complexity tier name"),
    ("cost_log", ("record-result", "resolve", "verify-final", "replan", "effort-check", "submit-plan"),
     "cost log file path (test override)"),
    ("quality_by", ("resolve", "close"), "how the quality rating was obtained — a fixed token"),
    ("confirmed_by", ("close",), "who confirmed — a name, not a narrative"),
    ("approved_by", ("drive",), "who approved — a name, not a narrative"),
    ("not_checked", ("code-review",),
     "short axis name(s) or 'none' — a fixed-vocabulary token, not review narrative"),
    ("rules", ("resolve-permission",),
     "Bash/Edit/etc rule strings — grant syntax tokens, not narrative"),
    ("add_dirs", ("resolve-permission",),
     "'PATH:MODE' tokens — grant syntax, not narrative"),
    ("regression_command", ("plan-review",),
     "shell command run verbatim via the runner (terminal-pass evidence) — an "
     "invocation string, not narrative prose for the @<path> convention"),
    ("path", ("workspace-register",), "filesystem path of the workspace — a path, not narrative"),
    ("ws_backend", ("workspace-register",), "workspace backend name from the registry — an identifier"),
    ("ws_name", ("workspace-register",), "workspace/mount name the backend teardown is keyed by — an identifier"),
)


_ARG_RESOLVE: frozenset[tuple[str, str]] = frozenset(
    (command, dest) for dest, commands in _RESOLVE_ROWS for command in commands
)


_ARG_FORWARD: frozenset[tuple[str, str]] = frozenset({
    # dispatch's own process never reads this narrative text — it only hands it
    # on to the spawned specialist's argv, which resolves it via the same
    # lib.argv_text convention on its own side (see spawn-specialist.py /
    # spawn-cursor-specialist.py's _WRAPPER_RESOLVE).
    ("dispatch", "constraints"),
})


_ARG_DO_NOT_WRAP: dict[tuple[str, str], str] = {
    (command, dest): reason
    for dest, commands, reason in _DO_NOT_WRAP_ROWS
    for command in commands
}


def resolve_arg_text(args: argparse.Namespace) -> None:
    """Apply the `@<path>` convention in place to this command's RESOLVE arguments.

    One pass over the parsed namespace before dispatch, so no cmd_* body sees an
    unresolved reference and none has to remember to call the helper itself.

    Deliberately not an argparse ``type=`` converter: a SystemExit raised inside a
    converter is attributed to argparse's own error path rather than to the
    convention, and an append action applies its converter per element — which
    cannot preserve the None-vs-empty-list distinction the helpers guarantee.
    """
    for command, dest in _ARG_RESOLVE:
        # A _ROOT entry belongs to the top-level parser, so it applies whatever
        # subcommand ran. Matching only args.command would leave a root narrative
        # option classified RESOLVE but never actually resolved — the silent
        # false-green this partition exists to make impossible.
        if command not in (args.command, _ROOT):
            continue
        value = getattr(args, dest)
        # An append dest arrives as a list once given and as None when never
        # given; both helpers pass None through unchanged.
        setattr(
            args,
            dest,
            argv_text.read_arg_text_list(value)
            if isinstance(value, list)
            else argv_text.read_arg_text(value),
        )


def _inject_default_session(argv: list[str], harness: str | None) -> list[str]:
    """Return a copy of ``argv`` with ``--session <harness>`` appended when the
    harness session id is known and the caller passed no --session of its own.

    hook-state-gate.py receives only the HARNESS conversation session_id
    (payload["session_id"] == $CLAUDE_CODE_SESSION_ID) and reads the state of the
    engine session bound to it (lib/engine_session_binding.py, written by a verb
    that saves state under an explicit --session), else of the harness id itself.
    An omitted --session must default to the harness id — not stay unset and
    fail the 30 required=True subcommands. Appending places the flag inside the
    subparser's argument region (--session is a subcommand option); it is a
    no-op when --session (either '--session X' or '--session=X') is already
    present, or when the harness id is empty/None."""
    if not harness:
        return list(argv)
    for tok in argv:
        if tok == "--session" or tok.startswith("--session="):
            return list(argv)
    return list(argv) + ["--session", harness]


class _BindingStore(FileStateStore):
    """The default store of a verb run by a harness session: saving the state of the
    session the verb was run on binds that session to the harness id, so the
    production-edit gate (which sees only the harness id) follows the session the
    coordinator drives. Query verbs save nothing and so never bind."""

    def __init__(self, harness: str, session: str):
        super().__init__(None)
        self._harness = harness
        self._session = _safe_session_id(session)

    def save(self, state) -> None:
        super().save(state)
        if _safe_session_id(state.session_id) != self._session:
            return
        try:
            engine_session_binding.record_drive(
                self._harness, state.session_id, dt.datetime.now(dt.timezone.utc))
        except Exception:
            pass  # an unwritten binding leaves the gate on the previously most recent one (the harness id if none)


def _is_followed_by_gate(harness: str, session: str) -> bool:
    # The binding store always lives under the agent home; with --state-root the verb's
    # state is not the gate's state either, so that case reports "not followed" as base did.
    try:
        return engine_session_binding.resolve(harness) == _safe_session_id(session)
    except Exception:
        return False


def _fire_plugins(args, store: StateStore, directive: Directive) -> None:
    """After a command runs, fire the matching plugin event on the (just-saved)
    state so active plugins can observe, gate, and auto-retire. Central wiring —
    the command bodies stay plugin-agnostic. Fires regardless of directive.ok
    (a blocked resolve must still surface its publish nudge). A plugin-less session
    skips entirely (no reload, no behavior change). Plugin faults never crash the
    engine."""
    event = plugins.event_for(args.command)
    if event is None:
        return
    session = getattr(args, "session", None)
    if not session:
        return
    try:
        state = store.load(session)
        if state is None or not plugins.active(state):
            return
        plugins.fire(event, state, directive)
        store.save(state)
    except Exception as exc:  # observability without aborting the directive
        directive.data.setdefault("plugin_errors", []).append(str(exc))


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
