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
T_NOTIFIER = "tests/test_mandate_notifier.py"
STATE = "agentctl/state.py"
PLAN = "agentctl/plan.py"
CLI = "agentctl/cli.py"
GATES = "agentctl/gates.py"
T_OPTIONAL = "tests/test_optional_stages.py"

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
        RULES, 'or delivery.get("notifier") in (None, "", "file")', 'or delivery.get("notifier") in (None, "")',
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
        WIDENING, '"plugin-record", "mandate-grant", "mandate-extend", "mandate-resume",', '"plugin-record",',
        _ids(T_CLI, "test_grant_extend_and_resume_are_user_authority_verbs"),
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
        "        failing = rules.failing_ids(baseline.passed, item_suite.passed)\n"
        "        if failing:\n"
        '            rerun = self.run_suite(worktree, item_dir / "rerun.xml", files=rerun_targets(failing, worktree))\n'
        "            green = rules.passes_after_rerun(baseline.passed, item_suite.passed, rerun.passed)\n",
        "        failing = rules.failing_ids(baseline.seen, item_suite.passed)\n"
        "        if failing:\n"
        '            rerun = self.run_suite(worktree, item_dir / "rerun.xml", files=rerun_targets(failing, worktree))\n'
        "            green = rules.passes_after_rerun(baseline.seen, item_suite.passed, rerun.passed)\n",
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
        "if pinned[0] != checked:",
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
        "elif rules.opens_breaker(outcome):",
        "elif True:",
        _ids(
            T_CYCLE,
            "test_an_overrun_does_not_open_the_breaker_and_the_cycle_continues",
            "test_a_killed_spawn_with_no_cost_row_is_charged_the_whole_item_cap",
        ),
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
        "    return stage.optional and stage.backlog_issue in declined_issues",
        "    return False",
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
        '    out["declined_live_optional"] = declined_live_optional(stages, _oa.declined_issues_of(last))\n',
        '    out["declined_live_optional"] = []\n',
        _ids(
            T_OPTIONAL,
            "test_a_new_session_cannot_self_approve_the_stage_the_customer_declined[keeps-it-declined]",
            "test_a_new_session_cannot_self_approve_the_stage_the_customer_declined[takes-it]",
            "test_a_reset_session_on_the_same_order_cannot_self_approve_the_declined_stage",
            "test_the_boundary_reads_the_decline_from_the_record_by_issue",
        ),
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
