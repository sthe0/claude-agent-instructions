#!/usr/bin/env python3
"""Mutation catalogue for the standing mandate: proves its tests discriminate.

Difficulty removed: a bound on delegated authority whose test no wrong implementation can turn
red is decoration. Each catalogue entry is one wrong version of a production line, applied by an
exact textual substitution to a scratch copy of scripts/, plus the test ids that must fail on it.

  python3 scripts/tests/mandate_mutation_control.py --mutant NAME   apply one mutant, run its tests
  python3 scripts/tests/mandate_mutation_control.py --control        run every listed test, unmutated
  python3 scripts/tests/mandate_mutation_control.py --list

Exit codes of --mutant:
  1  killed: the anchor matched exactly once and every listed id was collected once and
     FAILED in its call phase
  0  survived: every listed id was collected once, none errored or skipped, but at least one passed
  3  the anchor did not match exactly once (the production line moved)
  4  collection / import error in the child pytest
  5  a listed id was collected zero or several times, errored, or was skipped
  6  unhandled exception in this script
  2  usage (argparse)
Exit codes of --control: 0 all listed ids green on the unmutated copy, 7 otherwise.

The child pytest runs only the listed ids, serially, with MANDATE_MUTATION_CHILD=1 so the
in-suite catalogue test does not recurse.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
RULES = "agentctl/mandate.py"
STORE = "agentctl/mandate_store.py"
WIDENING = "lib/widening_targets.py"
T_RULES = "tests/test_mandate_rules.py"
T_CLI = "tests/test_mandate_cli.py"
DRIVER = "mandate_cycle/driver.py"
NOTIFIERS = "mandate_cycle/notifiers.py"
T_CYCLE = "tests/test_mandate_cycle.py"
T_BASIS = "tests/test_agentctl_mandate_basis.py"
T_NOTIFIER = "tests/test_mandate_notifier.py"
STATE = "agentctl/state.py"
PLAN = "agentctl/plan.py"
CLI = "agentctl/cli.py"
GATES = "agentctl/gates.py"
T_OPTIONAL = "tests/test_optional_stages.py"
INLINE = "agentctl/inline_class.py"
CLASSIFY = "agentctl/classify.py"
T_INLINE = "tests/test_inline_class.py"
T_INLINE_MEASURE = "tests/test_inline_measure.py"
T_CLASSIFY = "tests/test_classify.py"

KILLED, SURVIVED, ANCHOR_MISS, COLLECT_ERROR, BAD_ID, CRASH = 1, 0, 3, 4, 5, 6
CONTROL_RED = 7


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    ids: "tuple[str, ...]"


def _ids(module: str, *names: str) -> "tuple[str, ...]":
    return tuple(f"{module}::{name}" for name in names)


CATALOGUE: "dict[str, Mutant]" = {
    "constitution-classifier": Mutant(
        RULES,
        "return ConstitutionVerdict(bool(offending), tuple(offending))",
        "return ConstitutionVerdict(False, tuple(offending))",
        _ids(
            T_RULES,
            "test_the_mandate_machinery_is_on_the_default_list",
            "test_a_rename_out_of_a_listed_path_is_rejected_on_its_old_side",
            "test_settings_json_is_rejected_anywhere_whatever_the_list",
        ),
    ),
    "constitution-rename-old-side": Mutant(
        RULES, "for raw in _diff_paths(line):", "for raw in _diff_paths(line)[-1:]:",
        _ids(
            T_RULES,
            "test_a_rename_out_of_a_listed_path_is_rejected_on_its_old_side",
            "test_a_copy_from_a_listed_path_is_rejected",
        ),
    ),
    "veto-label": Mutant(
        RULES, "if veto in names:", "if False:",
        _ids(
            T_RULES,
            "test_a_veto_label_beats_a_user_set_auto_ok",
            "test_a_veto_label_beats_a_cycle_set_auto_ok_past_its_window",
        ),
    ),
    "previous-digest-window": Mutant(
        RULES,
        "if now < delivered_at + timedelta(hours=veto_window_hours):",
        "if now < delivered_at - timedelta(hours=veto_window_hours):",
        _ids(
            T_RULES,
            "test_a_cycle_set_auto_ok_waits_out_the_veto_window_after_the_digest",
            "test_the_veto_window_is_the_mandates_not_a_constant",
            "test_the_latest_cycle_label_row_picks_which_digest_applies",
        ),
    ),
    "severity-high-user-only": Mutant(
        RULES, "if SEVERITY_HIGH_LABEL in names:", "if False:",
        _ids(T_RULES, "test_a_cycle_set_auto_ok_on_severity_high_is_never_eligible"),
    ),
    "file-notifier-excluded": Mutant(
        RULES, 'and delivery.get("notifier") not in (None, "", "file")', 'and delivery.get("notifier") not in (None, "")',
        _ids(T_RULES, "test_a_file_notifier_digest_does_not_count_as_delivered"),
    ),
    "expiry": Mutant(
        RULES, "if is_expired(mandate, now):", "if False:",
        _ids(
            T_RULES,
            "test_gate_refuses_after_expiry",
            "test_gate_treats_the_expiry_instant_as_expired",
            "test_gate_reports_every_reason_at_once",
        ),
    ),
    "breaker-open": Mutant(
        RULES, "if mandate.breaker_open:", "if False:",
        _ids(
            T_RULES,
            "test_gate_refuses_with_the_breaker_open",
            "test_gate_reports_every_reason_at_once",
        ),
    ),
    "daily-budget": Mutant(
        RULES, "if spend.day_usd >= mandate.daily_usd:", "if False:",
        _ids(T_RULES, "test_gate_refuses_at_the_daily_budget", "test_gate_reports_every_reason_at_once")
        + _ids(T_CLI, "test_status_refuses_once_the_daily_budget_is_spent"),
    ),
    "weekly-budget": Mutant(
        RULES, "if spend.week_usd >= mandate.weekly_usd:", "if False:",
        _ids(T_RULES, "test_gate_refuses_at_the_weekly_budget", "test_gate_reports_every_reason_at_once"),
    ),
    "triage-filter": Mutant(
        RULES, 'and not barred.intersection(issue.get("labels") or ())', "and True",
        _ids(T_RULES, "test_triage_sees_only_unlabelled_owner_authored_domain_issues_oldest_first"),
    ),
    "user-authority-verbs": Mutant(
        WIDENING,
        '"plugin-record", "mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry",',
        '"plugin-record",',
        _ids(T_CLI, "test_grant_extend_resume_and_retry_are_user_authority_verbs"),
    ),
    "cycle-minutes-cap": Mutant(
        RULES, "if cycle_elapsed_minutes >= mandate.cycle_minutes_cap:", "if False:",
        _ids(T_RULES, "test_gate_refuses_at_the_cycle_minutes_cap", "test_gate_reports_every_reason_at_once"),
    ),
    "any-open-pr-excluded": Mutant(
        RULES, "if number in pr_refs:", "if False:",
        _ids(T_RULES, "test_an_issue_an_open_pr_references_is_not_eligible"),
    ),
    "owner-authored-only": Mutant(
        RULES, 'if issue.get("author") != owner:', "if False:",
        _ids(T_RULES, "test_issue_by_another_author_is_not_eligible"),
    ),
    "stop-idle-no-kill": Mutant(
        STORE, "if held and pid:", "if True:",
        _ids(
            T_CLI,
            "test_stop_pauses_and_disables_the_timer_without_killing_an_idle_mandate",
            "test_stop_does_not_kill_for_a_stale_pid_left_in_an_unheld_lock_file",
        ),
    ),
    "baseline-relative-tests": Mutant(
        DRIVER,
        "        baseline.passed, baseline.seen, result.passed, result.seen, rules.diff_path_groups(verdict.diff_lines),\n"
        "    )\n"
        "    verdict.excused, verdict.excused_count, verdict.failing = excused[:DIGEST_EXCUSED_IDS], len(excused), list(failing)\n"
        "    green = True\n"
        "    if failing:\n"
        "        rerun = suite_run(sh, suite, worktree, item_dir / \"rerun.xml\", timeout_s, files=rerun_targets(failing, worktree))\n"
        "        green = rules.passes_after_rerun(baseline.passed,",
        "        baseline.seen, baseline.seen, result.passed, result.seen, rules.diff_path_groups(verdict.diff_lines),\n"
        "    )\n"
        "    verdict.excused, verdict.excused_count, verdict.failing = excused[:DIGEST_EXCUSED_IDS], len(excused), list(failing)\n"
        "    green = True\n"
        "    if failing:\n"
        "        rerun = suite_run(sh, suite, worktree, item_dir / \"rerun.xml\", timeout_s, files=rerun_targets(failing, worktree))\n"
        "        green = rules.passes_after_rerun(baseline.seen,",
        _ids(T_CYCLE, "test_a_test_failing_on_the_baseline_does_not_count_against_the_item"),
    ),
    "no-trunk-push": Mutant(
        DRIVER,
        "if not branch.startswith(BRANCH_PREFIX) or branch == TRUNK:",
        "if False:",
        _ids(T_CYCLE, "test_push_refspec_pushes_the_given_commit_to_a_mandate_branch_only"),
    ),
    "push-checked-sha": Mutant(
        DRIVER,
        'return f"{sha}:refs/heads/{branch}"',
        'return f"HEAD:refs/heads/{branch}"',
        _ids(
            T_CYCLE,
            "test_push_refspec_pushes_the_given_commit_to_a_mandate_branch_only",
            "test_an_eligible_issue_becomes_a_pull_request_through_every_gate",
        ),
    ),
    "push-sha-shape": Mutant(
        DRIVER,
        "if not SHA_RE.fullmatch(sha):",
        "if False:",
        _ids(T_CYCLE, "test_push_refspec_pushes_the_given_commit_to_a_mandate_branch_only"),
    ),
    "head-pinned-before-review": Mutant(
        DRIVER,
        "if run.judged_tree and item_tree(self.sh, worktree, checked)[0] != run.judged_tree:",
        "if False:",
        _ids(T_CYCLE, "test_a_commit_made_while_the_suite_runs_is_never_pushed"),
    ),
    "worktree-rechecked-before-push": Mutant(
        DRIVER,
        "if self.worktree_state(worktree) != pinned:",
        "if False:",
        _ids(
            T_CYCLE,
            "test_a_commit_made_during_the_review_is_never_pushed",
            "test_a_file_changed_during_the_review_is_never_pushed",
        ),
    ),
    "scope-heartbeat": Mutant(
        DRIVER,
        "scopes.heartbeat(self.session_id, time.time(), self.scopes_dir, pid=os.getpid())",
        "None",
        _ids(T_CYCLE, "test_the_scope_record_is_heartbeated_while_an_item_works"),
    ),
    "spawn-bound-by-cycle-cap": Mutant(
        DRIVER,
        "if cycle_left < item_left:",
        "if False:",
        _ids(T_CYCLE, "test_a_spawn_killed_at_the_cycle_bound_is_charged_as_a_cycle_minutes_overrun"),
    ),
    "item-limit-event": Mutant(
        DRIVER,
        'cycle.note_limit(gate.reasons, "item", number)',
        "None",
        _ids(
            T_CYCLE,
            "test_the_cycle_wall_clock_cap_stops_the_next_item_with_a_limit_event",
            "test_the_weekly_budget_stops_the_next_item_with_a_limit_event",
        ),
    ),
    "item-row-defaults": Mutant(
        DRIVER,
        '"gates": {"constitution": NOT_RUN, "org_neutral": NOT_RUN, "lint": NOT_RUN},',
        '"gates": {},',
        _ids(
            T_CYCLE,
            "test_an_overrun_row_stops_at_the_spawn_that_overran",
            "test_a_limit_row_names_the_reasons_and_no_branch",
        ),
    ),
    "digest-kill-switch": Mutant(
        DRIVER,
        "Kill switch: `agentctl mandate-stop`.",
        "Kill switch: none.",
        _ids(T_CYCLE, "test_the_digest_carries_every_item_the_owner_needs_to_audit_a_cycle"),
    ),
    "digest-limits": Mutant(
        DRIVER,
        'lines += section("Limits hit", limits)',
        "pass",
        _ids(T_CYCLE, "test_the_digest_carries_every_item_the_owner_needs_to_audit_a_cycle"),
    ),
    "digest-skipped-comments": Mutant(
        DRIVER,
        "self.skipped.append({\"issue\": number, \"reason\": problem})",
        "None",
        _ids(T_CYCLE, "test_the_digest_lists_the_labels_applied_and_the_comments_the_neutral_check_held_back"),
    ),
    "digest-delivery-eligibility": Mutant(
        DRIVER,
        "ok=delivery.ok, notifier=delivery.notifier, delivered_at=cycle.now())",
        'ok=delivery.ok, notifier="plugin", delivered_at=cycle.now())',
        _ids(T_CYCLE, "test_a_file_digest_never_makes_a_cycle_set_label_eligible"),
    ),
    "notifier-precedence": Mutant(
        NOTIFIERS,
        "return plugins[0] if plugins else file_notifier(digests_dir, cycle_id)",
        "return file_notifier(digests_dir, cycle_id)",
        _ids(
            T_NOTIFIER,
            "test_a_loadable_plugin_is_preferred_over_the_file_notifier",
            "test_notify_test_reports_a_plugin_and_that_it_counts",
        ),
    ),
    "overrun-not-breaker": Mutant(
        DRIVER,
        "elif rules.opens_breaker(outcome, kind):",
        "elif True:",
        _ids(
            T_CYCLE,
            "test_an_overrun_does_not_open_the_breaker_and_the_cycle_continues",
            "test_a_killed_spawn_with_no_cost_row_is_charged_the_whole_item_cap",
        ),
    ),
    "item-local-not-breaker": Mutant(
        RULES,
        'return outcome != "failed" or failure_kind not in ITEM_FAILURE_KINDS',
        "return True",
        _ids(T_RULES, "test_an_item_local_failure_kind_does_not_open_the_breaker")
        + _ids(T_CYCLE, "test_an_item_local_failure_is_recorded_and_the_cycle_continues"),
    ),
    "unknown-kind-opens-breaker": Mutant(
        RULES,
        'return outcome != "failed" or failure_kind not in ITEM_FAILURE_KINDS',
        'return outcome == "failed" and failure_kind in CYCLE_FAILURE_KINDS',
        _ids(T_RULES, "test_an_unknown_failure_kind_opens_the_breaker"),
    ),
    "local-failure-streak": Mutant(
        DRIVER,
        "if len(run) >= rules.LOCAL_FAILURE_STREAK:",
        "if False:",
        _ids(T_CYCLE, "test_three_consecutive_item_failures_open_the_breaker"),
    ),
    "streak-since-granted": Mutant(
        DRIVER,
        "        if stamps:\n",
        "        if False:\n",
        _ids(T_CYCLE, "test_failures_before_the_resume_do_not_count_toward_the_streak"),
    ),
    "failed-before": Mutant(
        RULES,
        "if number in failed_issues:",
        "if False:",
        _ids(T_RULES, "test_a_failed_issue_is_not_eligible_again")
        + _ids(T_CYCLE, "test_the_cycle_does_not_retake_a_failed_issue"),
    ),
    "failed-issues-passed": Mutant(
        DRIVER,
        "failed_issues=failed,",
        "failed_issues=frozenset(),",
        _ids(T_CYCLE, "test_the_cycle_does_not_retake_a_failed_issue"),
    ),
    "retry-allowed-clears": Mutant(
        RULES,
        'and allowed.get(issue, floor) <= (parse_ts(row.get("ts")) or floor)',
        "and True",
        _ids(T_RULES, "test_a_retry_allowed_after_the_failure_makes_it_eligible"),
    ),
    "single-instance-lock": Mutant(
        DRIVER,
        "with store.cycle_lock(cfg.mandate_id):",
        'with __import__("contextlib").nullcontext():',
        _ids(T_CYCLE, "test_a_second_cycle_while_one_holds_the_lock_exits_busy_and_records_nothing"),
    ),
    "triage-label-logged": Mutant(
        DRIVER,
        'store.append_label_row(self.mid, number, label, self.id, by="cycle", reason=reason, now=self.now())',
        "None",
        _ids(
            T_CYCLE,
            "test_triage_logs_the_label_row_before_the_label_is_applied",
            "test_a_cycle_set_label_becomes_eligible_only_after_a_delivered_digest_and_the_veto_window",
        ),
    ),
    "no-issue-create": Mutant(
        DRIVER,
        'argv = ["gh", "issue", "comment", str(number), "--body-file", str(body)]',
        'argv = ["gh", "issue", "create", str(number), "--body-file", str(body)]',
        _ids(T_CYCLE, "test_no_gh_issue_create_is_invoked_across_triage_decline_and_pull_request"),
    ),
    "no-user-authority-calls": Mutant(
        DRIVER,
        'store.open_breaker(self.mid, reason, by="cycle", now=self.now())',
        'store.resume_cycle(self.mid, by="cycle")',
        _ids(T_CYCLE, "test_no_cycle_source_references_a_user_authority_transition[driver]"),
    ),
    "mandate-state-tamper": Mutant(
        DRIVER,
        "return store.state_fingerprint(self.mid) != before",
        "return False",
        _ids(T_CYCLE, "test_a_spawn_that_touches_the_mandate_state_fails_the_item_and_opens_the_breaker"),
    ),
    "triage-org-neutral": Mutant(
        DRIVER,
        "if code == 0:",
        "if True:",
        _ids(
            T_CYCLE,
            "test_a_triage_comment_is_not_posted_unless_the_org_neutral_check_is_clean[1]",
            "test_a_triage_comment_is_not_posted_unless_the_org_neutral_check_is_clean[2]",
        ),
    ),
    "skipped-is-settled": Mutant(
        STATE,
        "_SETTLED_STATUSES = frozenset({StageStatus.PASSED.value, StageStatus.SKIPPED.value})",
        "_SETTLED_STATUSES = frozenset({StageStatus.PASSED.value})",
        _ids(
            T_OPTIONAL,
            "test_the_settled_helper_is_exactly_passed_or_skipped",
            "test_a_skipped_stage_resolves_the_plan",
            "test_resolution_gate_counts_a_skipped_stage_as_settled",
            "test_resolved_invariant_accepts_skipped_and_refuses_pending",
        ),
    ),
    "optional-not-depended": Mutant(
        PLAN,
        "bad = sorted(d for d in s.depends_on if d in optional_ids)",
        "bad = []",
        _ids(T_OPTIONAL, "test_loader_rejects[a required stage depending on an optional one]"),
    ),
    "optional-max-two": Mutant(
        PLAN,
        "if len(optional) > MAX_OPTIONAL_STAGES:",
        "if len(optional) > MAX_OPTIONAL_STAGES + 1:",
        _ids(T_OPTIONAL, "test_loader_rejects[three optional stages]"),
    ),
    "optional-coverage-rule": Mutant(
        PLAN,
        "    if optional:\n        out.extend(optional_rest_violations(doc))",
        "    if False:\n        out.extend(optional_rest_violations(doc))",
        _ids(
            T_OPTIONAL,
            "test_loader_rejects[a requirement covered only by an optional stage]",
            "test_loader_rejects[a requirement whose only landed assertion is on an optional stage]",
            "test_loader_rejects[a final_check resting on an optional stage]",
        ),
    ),
    "reject-spares-skipped": Mutant(
        CLI,
        "        live = [s for s in state.stages if not is_skipped(s)]\n",
        "        live = list(state.stages)\n",
        _ids(T_OPTIONAL, "test_reject_without_a_stage_never_reopens_a_declined_stage"),
    ),
    "reject-names-skipped-refused": Mutant(
        CLI,
        "            if is_skipped(target):\n                return Directive(",
        "            if False:\n                return Directive(",
        _ids(T_OPTIONAL, "test_reject_refuses_to_name_a_declined_stage"),
    ),
    "push-refuses-skipped-origin": Mutant(
        CLI,
        "    if declined_origin:\n        return Directive(",
        "    if False:\n        return Directive(",
        _ids(T_OPTIONAL, "test_push_subplan_refuses_a_declined_originating_stage"),
    ),
    "agent-cannot-revive-declined": Mutant(
        CLI,
        "        if revived:\n            blockers = blockers + [",
        "        if False:\n            blockers = blockers + [",
        _ids(
            T_OPTIONAL,
            "test_the_agent_cannot_approve_a_declined_stage_back_in[changed-user-keeps-declined]",
            "test_the_agent_cannot_approve_a_declined_stage_back_in[changed-user-takes-it]",
            "test_the_agent_cannot_approve_a_declined_stage_back_in[made-required-user-takes-it]",
            "test_the_agent_cannot_approve_a_declined_stage_back_in[edited-in-place-user-keeps-declined]",
        ),
    ),
    "declined-follows-identity": Mutant(
        GATES,
        "    if stage.optional:\n        return stage.backlog_issue in declined_issues\n"
        "    return stage.title in declined_titles\n",
        "    return False\n",
        _ids(
            T_OPTIONAL,
            "test_a_renumbering_replan_keeps_the_decline_on_the_stage_by_its_issue",
            "test_the_customer_choosing_the_renumbered_stage_clears_the_recorded_decline",
            "test_dropping_then_readding_a_declined_stage_across_two_replans_stays_declined",
            "test_an_in_place_renumbering_at_plan_ready_leaves_no_orphan_skipped_stage",
            "test_an_insertion_before_the_declined_stage_keeps_the_marker_on_the_right_stage",
            "test_declined_live_optional_is_keyed_by_issue_and_ignores_skipped_stages",
        ),
    ),
    "declined-in-ledger": Mutant(
        GATES,
        '    out["declined_live_optional"] = declined_live_optional(\n'
        "        stages, _oa.declined_issues_of(last), _oa.declined_titles_of(last))\n",
        '    out["declined_live_optional"] = []\n',
        _ids(
            T_OPTIONAL,
            "test_a_new_session_cannot_self_approve_the_stage_the_customer_declined[keeps-it-declined]",
            "test_a_new_session_cannot_self_approve_the_stage_the_customer_declined[takes-it]",
            "test_a_reset_session_on_the_same_order_cannot_self_approve_the_declined_stage",
            "test_the_boundary_reads_the_decline_from_the_record_by_issue",
        ),
    ),
    "declined-required-in-ledger": Mutant(
        GATES,
        "        stages, _oa.declined_issues_of(last), _oa.declined_titles_of(last))\n",
        "        stages, _oa.declined_issues_of(last), ())\n",
        _ids(
            T_OPTIONAL,
            "test_a_new_session_cannot_self_approve_a_declined_stage_that_became_required",
            "test_a_reset_session_cannot_self_approve_a_declined_stage_that_became_required",
        ),
    ),
    "declined-title-half": Mutant(
        GATES,
        "    return stage.title in declined_titles\n",
        "    return False\n",
        _ids(
            T_OPTIONAL,
            "test_the_agent_cannot_approve_a_declined_stage_back_in[made-required-user-takes-it]",
            "test_a_declined_stage_is_matched_by_issue_while_optional_and_by_title_once_required",
        ),
    ),
    "declined-seeded-from-ledger": Mutant(
        CLI,
        " | set(order_approvals.declined_issues_of(previous))",
        "",
        _ids(
            T_OPTIONAL,
            "test_a_customer_approval_of_a_plan_without_the_stage_keeps_its_decline_across_sessions"
            "[as-optional-new-session]",
            "test_a_customer_approval_of_a_plan_without_the_stage_keeps_its_decline_across_sessions"
            "[as-required-new-session]",
            "test_a_customer_approval_of_a_plan_without_the_stage_keeps_its_decline_across_sessions"
            "[as-optional-after-reset]",
            "test_a_customer_approval_of_a_plan_without_the_stage_keeps_its_decline_across_sessions"
            "[as-required-after-reset]",
        ),
    ),
    "worktree-canonical-ids": Mutant(
        DRIVER,
        "::{case.get('name', '')}\", roots)",
        "::{case.get('name', '')}\", ())",
        _ids(T_CYCLE, "test_a_test_id_that_embeds_the_worktree_path_is_the_same_test_on_the_item"),
    ),
    "vanished-test-excused": Mutant(
        RULES,
        "        if ident in item_seen or index is None or index in dirty:\n",
        "        if True:\n",
        _ids(
            T_RULES,
            "test_vanished_id_in_a_touched_module_is_excused",
            "test_a_renamed_module_old_path_excuses_its_ids",
            "test_the_recorded_364_junit_excuses_exactly_the_removed_id",
        ) + _ids(T_CYCLE, "test_a_test_removed_by_the_item_does_not_fail_the_gate_and_is_reported"),
    ),
    "vanished-test-dirty-module": Mutant(
        RULES,
        " or index in dirty:\n",
        ":\n",
        _ids(
            T_RULES,
            "test_a_new_failure_in_the_touched_module_voids_the_excuse",
            "test_a_new_failure_in_the_renamed_to_module_voids_the_excuse",
        ) + _ids(T_CYCLE, "test_a_collection_error_in_the_touched_module_fails_the_gate"),
    ),
    "vanished-test-rename-group": Mutant(
        RULES,
        "test_module_of(ident, groups) for ident in new_bad",
        "test_module_of(ident, [g[:1] for g in groups]) for ident in new_bad",
        _ids(T_RULES, "test_a_new_failure_in_the_renamed_to_module_voids_the_excuse"),
    ),
    "rerun-verdict-ignores-excused": Mutant(
        DRIVER,
        "rerun.passed, excused)\n",
        "rerun.passed)\n",
        _ids(T_CYCLE, "test_an_excused_id_is_not_held_against_the_item_after_the_rerun"),
    ),
    "refusal-notice-sent": Mutant(
        DRIVER,
        "_notify_refusal(cycle, gate, mandate)",
        "None",
        _ids(T_CYCLE, "test_a_refused_start_sends_a_notice_through_the_digest_notifier"),
    ),
    "refusal-notice-throttle": Mutant(
        RULES,
        "    wanted = sorted(reasons)\n",
        "    return True\n    wanted = sorted(reasons)\n",
        _ids(T_RULES, "test_refusal_notice_due_once_per_day_per_reason_set")
        + _ids(T_CYCLE, "test_a_second_refusal_the_same_day_is_suppressed"),
    ),
    "refusal-notice-file-only-suppresses": Mutant(
        RULES,
        "            and digest_counts_as_delivered(detail)\n",
        "",
        _ids(T_RULES, "test_a_file_only_notice_does_not_suppress"),
    ),
    "refusal-notice-not-a-digest": Mutant(
        DRIVER,
        "    cycle.event(rules.REFUSED_NOTICE_EVENT, detail)\n",
        "    cycle.event(rules.REFUSED_NOTICE_EVENT, detail)\n"
        "    store.record_digest(mid, cycle.id, ok=delivery.ok, notifier=delivery.notifier, delivered_at=now)\n",
        _ids(T_CYCLE, "test_a_refusal_notice_is_not_recorded_as_a_digest"),
    ),
    "blocked-since-breaker-trip": Mutant(
        RULES,
        '    if "breaker-open" in reasons:',
        "    if False:",
        _ids(T_RULES, "test_blocked_since_is_the_trailing_refused_run_or_the_breaker_trip"),
    ),
    "unblock-command-per-reason": Mutant(
        RULES,
        '        if reason in ("breaker-open", "paused"):',
        '        if reason in ("paused",):',
        _ids(T_RULES, "test_unblock_commands_per_reason"),
    ),
    "expiry-warning": Mutant(
        DRIVER,
        "        if left <= timedelta(days=rules.EXPIRY_WARN_DAYS):",
        "        if False:",
        _ids(T_CYCLE, "test_the_digest_warns_of_an_expiry_within_three_days"),
    ),
    "redeliver-no-record": Mutant(
        DRIVER,
        "            record = store.record_digest(\n"
        "                mid, cycle_id,\n"
        "                ok=delivery.ok,\n"
        "                notifier=delivery.notifier,\n"
        "                delivered_at=seams.clock(),\n"
        "            )\n",
        "            record = {}\n",
        _ids(T_NOTIFIER, "test_redeliver_digest_records_the_delivery_so_the_veto_window_starts_from_it"),
    ),
    "redeliver-record-without-send": Mutant(
        DRIVER,
        "            delivery = notifiers.deliver(\n"
        "                REDELIVERED_PREFIX + stored.read_text(encoding=\"utf-8\"),\n"
        "                digests_dir=store.mandate_dir(mid) / \"redelivered\", cycle_id=cycle_id, root=cfg.plugin_root,\n"
        "            )\n",
        "            delivery = notifiers.Delivery(\"chat\", True, \"\")\n",
        _ids(T_NOTIFIER, "test_redeliver_digest_sends_the_stored_digest_through_the_notifier"),
    ),
    "redeliver-not-idempotent": Mutant(
        DRIVER,
        "            if rules.digest_counts_as_delivered(store.read_digests(mid).get(cycle_id)):\n",
        "            if False:\n",
        _ids(T_NOTIFIER, "test_redeliver_digest_is_idempotent_for_a_delivered_digest"),
    ),
    "inline-test-exclusion": Mutant(
        INLINE,
        "        and not _matches_any(path, TEST_PATTERNS)\n",
        "",
        _ids(
            T_INLINE,
            "test_a_test_file_is_excluded",
            "test_a_scripts_tests_helper_is_excluded",
            "test_exclusion_reasons_are_reported",
        )
        + _ids(T_INLINE_MEASURE, "test_json_counts_only_hand_written_production_lines"),
    ),
    "inline-generated-exclusion": Mutant(
        INLINE,
        "        and path not in generated\n",
        "",
        _ids(T_INLINE, "test_a_generated_path_is_excluded", "test_exclusion_reasons_are_reported")
        + _ids(T_INLINE_MEASURE, "test_json_counts_only_hand_written_production_lines"),
    ),
    "inline-bound-comparison": Mutant(
        INLINE,
        "    return lines <= bound",
        "    return lines < bound",
        _ids(T_INLINE, "test_equality_at_the_bound_is_within_and_one_over_is_not")
        + _ids(T_CLASSIFY, "test_changed_lines_at_the_bound_is_a_small_change"),
    ),
    "inline-binary-failsafe": Mutant(
        INLINE,
        "    if measured.binary_paths:\n        return False\n",
        "    if False:\n        return False\n",
        _ids(T_INLINE, "test_a_counted_binary_file_is_never_within_the_bound"),
    ),
    "inline-reason-source": Mutant(
        CLASSIFY,
        "reasons.append(inline_class.small_change_reason(thr.small_change_max_lines))",
        'reasons.append("small change")',
        _ids(T_CLASSIFY, "test_the_small_change_reason_carries_the_definition_and_the_bound"),
    ),
    "inline-memory-exclusion": Mutant(
        INLINE,
        "        and not _matches_any(path, MEMORY_PATTERNS)\n",
        "",
        _ids(
            T_INLINE,
            "test_a_memory_leaf_excluded",
            "test_an_experience_leaf_is_excluded",
            "test_exclusion_reasons_are_reported",
        ),
    ),
    "inline-constitution-fixed": Mutant(
        RULES,
        'FIXED_CONSTITUTION = (\n    "scripts/agentctl/inline_class.py",\n',
        'FIXED_CONSTITUTION = ()\n_REMOVED = (\n    "scripts/agentctl/inline_class.py",\n',
        _ids(
            T_RULES,
            "test_the_inline_size_definition_is_protected_whatever_the_mandates_own_list_says"
            "[scripts:agentctl:exempt_paths.py]",
            "test_the_inline_size_definition_is_protected_whatever_the_mandates_own_list_says"
            "[scripts:agentctl:config.py]",
            "test_the_fixed_entries_name_every_file_the_counted_set_depends_on",
        ),
    ),
    "triage-admits-any-size": Mutant(
        DRIVER,
        'Size is not a reason for "auto-no": a larger item is planned by the developer under a standing norm;',
        'Larger items are "auto-no": too big to fix unattended;',
        _ids(T_CYCLE, "test_the_triage_prompt_admits_an_in_repo_item_of_any_size_and_keeps_its_json_example"),
    ),
    "basis-skips-only-under-mandate": Mutant(
        CLI,
        "    if not by_agent and not mandate_approver_id:\n        blockers = (\n",
        "    if False:\n        blockers = (\n",
        _ids(T_BASIS, "test_a_non_mandate_approval_still_requires_presentation_and_grants_approval"),
    ),
    "basis-exact-norm": Mutant(
        CLI,
        "    return problems + mandate_rules.mandate_approval_problems(plan_text, inputs, mandate, gate_result)\n",
        "    return problems\n",
        _ids(T_BASIS, "test_a_mandate_approval_of_an_edited_norm_is_refused"),
    ),
    "basis-keeps-since-user": Mutant(
        CLI,
        "    if not mandate_approver_id:\n        _clear_since_user_decision(state, args.by)\n",
        "    if True:\n        _clear_since_user_decision(state, args.by)\n",
        _ids(T_BASIS, "test_a_mandate_approval_does_not_clear_the_since_user_counters_or_stamp_the_order_ledger"),
    ),
    "basis-binds-taken-item": Mutant(
        CLI,
        '    if not taken:\n        problems.append("the cycle recorded no item taken',
        '    if False:\n        problems.append("the cycle recorded no item taken',
        _ids(T_BASIS, "test_a_mandate_approval_of_a_norm_for_no_taken_item_is_refused"),
    ),
    "reject-records-actor": Mutant(
        CLI,
        'state.log("reject", reason=reason, stages=idxs, **({"by": rejecter} if rejecter else {}))',
        'state.log("reject", reason=reason, stages=idxs)',
        _ids(T_BASIS, "test_reject_by_mandate_moves_the_child_to_diagnosing_and_records_the_actor"),
    ),
    "child-is-linked": Mutant(
        DRIVER,
        'argv += ["--session", session, "--stage-index", str(STAGE_INDEX)]',
        "pass",
        _ids(T_CYCLE, "test_an_item_runs_as_a_linked_child_of_a_mandate_approved_parent_session"),
    ),
    "settled-is-mandates": Mutant(
        DRIVER,
        '"mandate-answer", {"issue": run.number, "kind": kind, "answer": verdict.answer, "round": run.rounds},\n'
        "                by=rules.mandate_approver(self.mid),",
        '"mandate-answer", {"issue": run.number, "kind": kind, "answer": verdict.answer, "round": run.rounds},\n'
        '                by="user",',
        _ids(T_CYCLE, "test_every_settled_answer_is_recorded_as_the_mandates_never_the_users"),
    ),
    "stop-keeps-breaker-closed": Mutant(
        DRIVER,
        'row.update(outcome="overrun", overrun_reasons=list(detail), cost_usd=self.item_cost(run.item_dir)[0])',
        'row.update(outcome="failed", failure_kind=KIND_INFRA, overrun_reasons=list(detail), '
        "cost_usd=self.item_cost(run.item_dir)[0])",
        _ids(T_CYCLE, "test_a_spend_fire_stops_the_item_as_overrun_without_the_breaker"),
    ),
    "check-item-bound": Mutant(
        DRIVER,
        'verdict.over_bound = not inline_class.within_bound(measured, int(inputs["bound"]))',
        "verdict.over_bound = False",
        _ids(T_CYCLE, "test_check_item_judges_constitution_org_neutral_suite_gates_and_bound[_bound-failed5]"),
    ),
    "check-item-empty-diff": Mutant(
        DRIVER,
        "    if not verdict.diff_lines:\n",
        "    if False:\n",
        _ids(T_CYCLE, "test_check_item_is_red_on_an_unchanged_tree"),
    ),
    "covered-denials-counted": Mutant(
        DRIVER,
        'denials = sum(1 for a in run.asks if a["kind"] == "permission-covered")',
        "denials = 0",
        _ids(T_CYCLE, "test_a_repeated_covered_denial_stops_the_item_as_exceeds_mandate"),
    ),
    "nested-cost-charged": Mutant(
        DRIVER,
        "for nested in nested_spawn_rows(rows, self._child):",
        "for nested in []:",
        _ids(T_CYCLE, "test_nested_spawn_spend_is_charged_to_the_item_and_the_mandate_windows"),
    ),
    "salvage-kept": Mutant(
        DRIVER,
        'if row["outcome"] != "pr-opened" and self._item_base:',
        "if False:",
        _ids(T_CYCLE, "test_every_stopped_item_with_a_diff_keeps_its_patch[garbage-True-M\\tscripts/foo.py\\n]"),
    ),
    "salvage-fail-open": Mutant(
        DRIVER,
        '        except (DriverError, OSError) as exc:\n            self.event("salvage-failed"',
        '        except ZeroDivisionError as exc:\n            self.event("salvage-failed"',
        _ids(T_CYCLE, "test_a_salvage_error_is_an_event_and_keeps_the_outcome"),
    ),
    "account-limit-graceful": Mutant(
        DRIVER,
        "    if status != STATUS_ACCOUNT_LIMIT and gate.may_start:\n",
        "    if gate.may_start:\n",
        _ids(T_CYCLE, "test_an_account_limit_on_a_spawn_stops_the_cycle_without_the_breaker[developer]"),
    ),
    "ask-unknown-exceeds": Mutant(
        RULES, 'return exceeds("unclassified")', 'return settled("unclassified")',
        _ids(T_RULES, "test_every_ask_kind_has_a_class_and_an_unknown_kind_exceeds"),
    ),
    "ask-fire-exceeds": Mutant(
        RULES,
        'return exceeds("effort-spend" if get("fire_scale", str) == "spend" else "observation:fire_scale")',
        'return settled("effort-spend")',
        _ids(T_RULES, "test_effort_fires_backstops_and_ceiling_exceed_the_mandate"),
    ),
    "ask-permission-covered": Mutant(
        RULES, "if not covered:", "if False:",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-missing-field": Mutant(
        RULES, "if covered is absent:", "if False:",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-repeat-denial": Mutant(
        RULES, "if denials >= 1:", "if False:",
        _ids(T_RULES, "test_a_repeated_covered_denial_exceeds"),
    ),
    "ask-fire-replans": Mutant(
        RULES,
        'return exceeds("effort-replans" if get("fire_scale", str) == "replans" else "observation:fire_scale")',
        'return settled("effort-replans")',
        _ids(T_RULES, "test_effort_fires_backstops_and_ceiling_exceed_the_mandate"),
    ),
    "ask-since-user-backstop": Mutant(
        RULES, 'return exceeds("backstop")', 'return settled("backstop")',
        _ids(T_RULES, "test_effort_fires_backstops_and_ceiling_exceed_the_mandate"),
    ),
    "ask-question-for-user": Mutant(
        RULES,
        'return exceeds("question-for-user" if get("fire_scale", str, type(None)) is None else "observation:fire_scale")',
        'return settled("question-for-user")',
        _ids(T_RULES, "test_questions_norm_mismatch_incomplete_and_user_only_verbs_exceed_the_mandate"),
    ),
    "ask-user-only-verb": Mutant(
        RULES,
        'return exceeds("observation:user_verb" if verb is absent else "needs-user:" + verb)',
        'return settled("needs-user")',
        _ids(T_RULES, "test_questions_norm_mismatch_incomplete_and_user_only_verbs_exceed_the_mandate"),
    ),
    "ask-loop-detected": Mutant(
        RULES, 'return exceeds("loop-detected")', 'return settled("loop-detected")',
        _ids(T_RULES, "test_questions_norm_mismatch_incomplete_and_user_only_verbs_exceed_the_mandate"),
    ),
    "ask-rounds-limit": Mutant(
        RULES, "under_limit = rounds < rounds_limit", "under_limit = rounds <= rounds_limit",
        _ids(T_RULES, "test_check_review_bound_and_covered_permission_are_settled_until_the_rounds_limit"),
    ),
    "stop-resource-retried": Mutant(
        RULES,
        'if reason in ("effort-spend", "effort-wall-clock"):',
        'if reason in ("effort-wall-clock",):',
        _ids(T_RULES, "test_stop_disposition_retries_resource_stops_and_declines_authority_stops"),
    ),
    "approval-exact-norm": Mutant(
        RULES, "if plan_text != rendered:", "if False:",
        _ids(T_RULES, "test_mandate_approval_requires_the_exact_item_norm_and_an_open_gate"),
    ),
    "approval-open-gate": Mutant(
        RULES, 'if not getattr(gate_result, "may_start", False):', "if False:",
        _ids(T_RULES, "test_mandate_approval_requires_the_exact_item_norm_and_an_open_gate"),
    ),
    "item-norm-grants": Mutant(
        RULES,
        "f'allow = [\"Bash(python3 -m pytest:*)\", {q(thinker)}]',",
        "f'allow = [\"Bash(python3 -m pytest:*)\", {q(thinker)}, \"Bash(git push:*)\"]',",
        _ids(T_RULES, "test_the_item_norm_grants_are_the_fixed_literal"),
    ),
    "account-limit-precedence": Mutant(
        RULES, 'if marker not in (None, "MALFORMED", "CHILD_INFRA_FAILURE"):', "if False:",
        _ids(T_RULES, "test_a_genuine_marker_outranks_a_rate_limit_record"),
    ),
    "account-limit-error-kind": Mutant(
        RULES,
        'return last.get("error") == ACCOUNT_LIMIT_ERROR or last.get("apiErrorStatus") == ACCOUNT_LIMIT_STATUS',
        "return True",
        _ids(T_RULES, "test_other_api_errors_and_model_text_are_not_an_account_limit"),
    ),
    "account-limit-last-record": Mutant(
        RULES, "last = record", "last = last or record",
        _ids(T_RULES, "test_other_api_errors_and_model_text_are_not_an_account_limit"),
    ),
    "account-limit-api-flag": Mutant(
        RULES, 'if last.get("isApiErrorMessage") is not True:', "if False:",
        _ids(T_RULES, "test_other_api_errors_and_model_text_are_not_an_account_limit"),
    ),
    "account-limit-infra-marker": Mutant(
        RULES, '(None, "MALFORMED", "CHILD_INFRA_FAILURE")', '(None, "MALFORMED")',
        _ids(T_RULES, "test_a_synthetic_rate_limit_record_is_an_account_limit"),
    ),
    "nested-cost-counted": Mutant(
        RULES,
        'EVENT_COST_KINDS = ("judge-cost", "overrun-charge", "nested-spawn-cost")',
        'EVENT_COST_KINDS = ("judge-cost", "overrun-charge")',
        _ids(T_RULES, "test_a_nested_spawn_cost_event_counts_toward_the_mandate_windows"),
    ),
    "item-norm-escape": Mutant(
        RULES, r'out.append("\\\\")', r'out.append("\\")',
        _ids(T_RULES, "test_hostile_issue_text_cannot_change_the_item_norm_structure"),
    ),
    "ask-self-approval-settled": Mutant(
        RULES, 'return settled("parent-norm")', 'return exceeds("parent-norm")',
        _ids(T_RULES, "test_check_review_bound_and_covered_permission_are_settled_until_the_rounds_limit"),
    ),
    "ask-over-bound-parent-norm": Mutant(
        RULES, 'if basis is absent or basis == "parent_norm":', "if basis is absent:",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-check-green": Mutant(
        RULES, 'if get("check_green", bool) is not False:', "if False:",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-landing-waiver": Mutant(
        RULES, 'if get("landing_waiver", bool) is True else', "if True else",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-resolution-node": Mutant(
        RULES, 'if get("child_node", str) == "RESOLUTION" else', "if True else",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-acceptance-active": Mutant(
        RULES, 'if get("acceptance_active", bool) is False else', "if True else",
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "ask-wall-clock-exceeds": Mutant(
        RULES,
        'return exceeds("effort-wall-clock" if get("fire_scale", str) == "wall-clock" else "observation:fire_scale")',
        'return settled("effort-wall-clock")',
        _ids(T_RULES, "test_effort_fires_backstops_and_ceiling_exceed_the_mandate"),
    ),
    "ask-wall-clock-scale": Mutant(
        RULES, 'get("fire_scale", str) == "wall-clock"', 'get("fire_scale", str) != absent',
        _ids(T_RULES, "test_a_missing_or_disagreeing_observed_key_exceeds"),
    ),
    "stop-limit-mapping": Mutant(
        RULES, "if reason == ACCOUNT_LIMIT_REASON:", "if False:",
        _ids(T_RULES, "test_stop_disposition_retries_resource_stops_and_declines_authority_stops"),
    ),
    "stop-declined-fallback": Mutant(
        RULES, 'return "declined", "exceeds-mandate"', 'return "overrun", [reason]',
        _ids(T_RULES, "test_stop_disposition_retries_resource_stops_and_declines_authority_stops"),
    ),
    "item-norm-title-cut": Mutant(
        RULES, "title = str(title)[:200]", "title = str(title)",
        _ids(T_RULES, "test_hostile_issue_text_cannot_change_the_item_norm_structure"),
    ),
    "item-norm-body-cut": Mutant(
        RULES, "if len(body) > 2000:", "if False:",
        _ids(T_RULES, "test_the_item_norm_cuts_a_long_body_below_the_inline_field_limit"),
    ),
    "item-norm-body-cut-length": Mutant(
        RULES, "body = body[:2000] +", "body = body[:2500] +",
        _ids(T_RULES, "test_hostile_issue_text_cannot_change_the_item_norm_structure"),
    ),
    "mandate-id-compared": Mutant(
        RULES, 'if rest.pop("mandate_id", None) != mandate.id:', 'if rest.pop("mandate_id", None) is None and False:',
        _ids(T_RULES, "test_mandate_approval_reads_and_strips_the_mandate_id_key"),
    ),
}


def _junit_status(xml_path: Path) -> "dict[tuple[str, str], list[str]]":
    """(module stem, test name) -> statuses seen: failure | error | skipped | passed."""
    seen: "dict[tuple[str, str], list[str]]" = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        stem = case.get("classname", "").rsplit(".", 1)[-1]
        status = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                status = tag
                break
        seen.setdefault((stem, case.get("name", "")), []).append(status)
    return seen


def _run_child(subject: Path, ids: "tuple[str, ...]", xml_path: Path) -> int:
    env = dict(os.environ)
    env.update(PYTHONPATH=str(subject), MANDATE_MUTATION_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--tb=no",
        f"--junitxml={xml_path}", *ids,
    ]
    return subprocess.run(command, cwd=subject, env=env, capture_output=True, text=True).returncode


def _copy_subject(parent: Path) -> Path:
    subject = parent / "scripts"
    shutil.copytree(SCRIPTS_DIR, subject, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.copy2(SCRIPTS_DIR.parent / "config.md", parent / "config.md")
    return subject


def _classify(ids: "tuple[str, ...]", xml_path: Path, child_rc: int) -> "tuple[str, int | None]":
    """Return (description, early exit code or None). None means every id passed or failed cleanly."""
    if child_rc in (2, 3, 4) or not xml_path.exists():
        return f"pytest rc {child_rc}: collection or usage error", COLLECT_ERROR
    seen = _junit_status(xml_path)
    outcomes = []
    for node in ids:
        path, name = node.split("::")
        statuses = seen.get((Path(path).stem, name), [])
        if len(statuses) != 1:
            return f"{node} collected {len(statuses)} times", BAD_ID
        if statuses[0] in ("error", "skipped"):
            return f"{node} {statuses[0]}", BAD_ID
        outcomes.append(statuses[0])
    return ",".join(outcomes), None


def run_mutant(name: str) -> int:
    mutant = CATALOGUE[name]
    try:
        with tempfile.TemporaryDirectory(prefix="mandate-mutant-") as tmp:
            subject = _copy_subject(Path(tmp))
            target = subject / mutant.relpath
            source = target.read_text(encoding="utf-8")
            if source.count(mutant.anchor) != 1:
                print(f"{name}: anchor matched {source.count(mutant.anchor)} times in {mutant.relpath}", file=sys.stderr)
                return ANCHOR_MISS
            target.write_text(source.replace(mutant.anchor, mutant.replacement), encoding="utf-8")
            xml_path = Path(tmp) / "result.xml"
            rc = _run_child(subject, mutant.ids, xml_path)
            description, early = _classify(mutant.ids, xml_path, rc)
            if early is not None:
                print(f"{name}: {description}", file=sys.stderr)
                return early
            return KILLED if all(s == "failure" for s in description.split(",")) else SURVIVED
    except Exception as exc:
        print(f"{name}: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        return CRASH


def run_control() -> int:
    ids = tuple(dict.fromkeys(i for m in CATALOGUE.values() for i in m.ids))
    with tempfile.TemporaryDirectory(prefix="mandate-control-") as tmp:
        subject = _copy_subject(Path(tmp))
        xml_path = Path(tmp) / "result.xml"
        rc = _run_child(subject, ids, xml_path)
        description, early = _classify(ids, xml_path, rc)
        if early is not None:
            print(f"control: {description}", file=sys.stderr)
            return CONTROL_RED
        return 0 if all(s == "passed" for s in description.split(",")) else CONTROL_RED


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mutant", choices=sorted(CATALOGUE))
    group.add_argument("--control", action="store_true")
    group.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for name, mutant in CATALOGUE.items():
            print(f"{name} {mutant.relpath} {len(mutant.ids)} ids")
        return 0
    if args.control:
        return run_control()
    code = run_mutant(args.mutant)
    print(f"{args.mutant}: exit {code} ({'killed' if code == KILLED else 'not killed'})")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(CRASH)
