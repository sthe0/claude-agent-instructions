#!/usr/bin/env python3
"""Mutation catalogue for the reaper framework: proves its tests discriminate.

Difficulty removed: a safety test that no wrong implementation can turn red is decoration.
Each catalogue entry is one wrong version of a production line, applied by an exact textual
substitution to a scratch copy of scripts/, plus the test ids that must fail on it.

  python3 scripts/tests/reaper_mutation_control.py --mutant NAME   apply one mutant, run its tests
  python3 scripts/tests/reaper_mutation_control.py --control        run every listed test, unmutated
  python3 scripts/tests/reaper_mutation_control.py --list

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

The child pytest runs only the listed ids, serially, with REAPER_MUTATION_CHILD=1 so the
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
RUNNER = "reaper/runner.py"
GIT_WORKTREES = "reaper/builtin/git_worktrees.py"
CONTRACT = "reaper/contract.py"
SCOPE_REGISTRY = "session_scope/registry.py"
T_RUNNER = "tests/test_reaper_runner.py"
T_GIT = "tests/test_reaper_git_worktrees.py"
AGENTCTL_STATE = "reaper/builtin/agentctl_state.py"
T_STATE = "tests/test_reaper_agentctl_state.py"
PYTEST_BASETEMP = "reaper/builtin/pytest_basetemp.py"
T_BASETEMP = "tests/test_reaper_pytest_basetemp.py"
T_PRESSURE = "tests/test_reaper_pressure.py"
HOOK_BACKUP = "hook-branch-backup.py"
T_HOOK = "tests/test_hook_branch_backup.py"
LIVENESS = "reaper/liveness.py"
TEMP_RESIDUE = "reaper/builtin/temp_residue.py"
T_LIVENESS = "tests/test_reaper_liveness.py"
T_RESIDUE = "tests/test_reaper_temp_residue.py"

PLANTED_HOLDERS = "test_each_holder_kind_in_a_planted_proc_root_is_found_under_its_own_kind"

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
    "keep-wins": Mutant(
        RUNNER, "if v.action == KEEP\n", 'if v.action == "never"\n',
        _ids(
            T_RUNNER,
            "test_keep_wins_over_a_remove_on_the_same_path",
            "test_keep_from_a_reaper_that_is_not_due_still_vetoes",
            "test_dry_run_prints_an_overridden_remove_as_keep_naming_the_vetoer",
        ),
    ),
    "error-isolation": Mutant(
        RUNNER,
        "        except Exception as exc:\n            failed_removals.add(reaper.name)",
        "        except KeyboardInterrupt as exc:\n            failed_removals.add(reaper.name)",
        _ids(
            T_RUNNER,
            "test_remove_exception_keeps_the_path_and_leaves_only_that_stamp_behind",
            "test_remove_returning_false_is_a_failure",
        ),
    ),
    "error-isolation-scan": Mutant(
        RUNNER,
        "        except Exception as exc:\n            scanned[reaper.name] = []",
        "        except KeyboardInterrupt as exc:\n            scanned[reaper.name] = []",
        _ids(
            T_RUNNER,
            "test_scan_exception_cancels_every_removal_and_advances_no_stamp",
            "test_session_start_mode_exits_zero_whatever_a_reaper_does",
            "test_invalid_verdict_action_counts_as_a_failed_scan",
        ),
    ),
    "removal-log-before-remove": Mutant(
        RUNNER,
        '            _append_removal_log(log_path, now, reaper, verdict, provenance)\n'
        '            if reaper.module.remove(verdict.path, ctx) is False:\n'
        '                raise RuntimeError("remove() returned False")\n',
        '            if reaper.module.remove(verdict.path, ctx) is False:\n'
        '                raise RuntimeError("remove() returned False")\n'
        '            _append_removal_log(log_path, now, reaper, verdict, provenance)\n',
        _ids(T_RUNNER, "test_removal_is_logged_before_remove_runs"),
    ),
    "realpath": Mutant(
        RUNNER, "joined.setdefault(os.path.realpath(verdict.path), [])", "joined.setdefault(verdict.path, [])",
        _ids(T_RUNNER, "test_verdicts_are_joined_by_realpath"),
    ),
    "realpath-temp-root": Mutant(
        GIT_WORKTREES, "root = os.path.realpath(root)", "root = root",
        _ids(T_GIT, "test_symlinked_temp_root_matches_the_resolved_worktree_path"),
    ),
    "realpath-owner": Mutant(
        CONTRACT, "_at_or_inside(real, os.path.realpath(held))", "_at_or_inside(real, held)",
        _ids(T_GIT, "test_owner_record_spelled_through_a_symlink_still_owns_the_worktree"),
    ),
    "landed-check": Mutant(
        GIT_WORKTREES, 'line.startswith("+")', 'line.startswith("+") and False',
        _ids(
            T_GIT,
            "test_unlanded_branch_worktree_is_kept_with_the_commit_count",
            "test_summary_names_a_stale_unlanded_worktree_older_than_a_week",
        ),
    ),
    "landed-check-patch-equivalence": Mutant(
        GIT_WORKTREES, 'line.startswith("+")', 'line.startswith(("+", "-"))',
        _ids(T_GIT, "test_rebase_landed_branch_worktree_is_removed"),
    ),
    "worktree-remove-force": Mutant(
        GIT_WORKTREES,
        '_git_checked(repo, "worktree", "remove", entry.path)',
        '_git_checked(repo, "worktree", "remove", "--force", entry.path)',
        _ids(
            T_GIT,
            "test_remove_refuses_a_worktree_that_turned_dirty_after_the_scan",
            "test_detached_worktree_that_turned_dirty_after_the_scan_survives_the_pass",
        ),
    ),
    "detached-trunk-ancestor": Mutant(
        GIT_WORKTREES, "if verdict == REMOVE and not head_in_trunk(wt.path):", "if False:",
        _ids(T_GIT, "test_detached_worktree_with_a_commit_not_in_trunk_is_kept"),
    ),
    "scope-registry-fail-open": Mutant(
        RUNNER, "if ctx.scope_registry_error:", "if False:",
        _ids(
            T_RUNNER,
            "test_unreadable_scope_registry_cancels_every_removal_and_advances_no_stamp",
            "test_unreadable_scope_registry_shows_as_keep_in_a_dry_run",
        ),
    ),
    "scope-registry-lenient-load": Mutant(
        RUNNER, "strict=True)", "strict=False)",
        _ids(T_RUNNER, "test_main_removes_nothing_when_one_scope_record_is_corrupt"),
    ),
    "scope-registry-strict-ignored": Mutant(
        SCOPE_REGISTRY, "            if strict:\n                raise\n", "",
        _ids(
            T_RUNNER,
            "test_main_removes_nothing_when_one_scope_record_is_corrupt",
            "test_load_all_skips_a_corrupt_record_by_default_and_raises_when_strict",
        ),
    ),
    "skipped-module-ignored": Mutant(
        RUNNER, "if skipped_modules:", "if False:",
        _ids(
            T_RUNNER,
            "test_a_module_skipped_at_discovery_cancels_every_removal_but_still_prints",
            "test_main_removes_nothing_when_discovery_skipped_a_module",
        ),
    ),
    "keep-containment": Mutant(
        RUNNER,
        "return os.path.commonpath([real_a, real_b]) in (real_a, real_b)",
        "return real_a == real_b",
        _ids(
            T_RUNNER,
            "test_keep_on_a_directory_vetoes_a_remove_of_a_path_inside_it",
            "test_keep_on_a_path_vetoes_a_remove_of_a_directory_containing_it",
        ),
    ),
    "state-node-filter": Mutant(
        AGENTCTL_STATE, "if node != RESIDUE_NODE:", "if node is None:",
        _ids(
            T_STATE,
            "test_scan_proposes_only_the_old_unowned_classified_plain_file",
            "test_pass_removes_exactly_the_residue_file_and_logs_it",
            "test_dry_run_removes_nothing_and_prints_the_removal",
        ),
    ),
    "state-age-floor": Mutant(
        AGENTCTL_STATE, "if age_days < MIN_AGE_DAYS:", "if age_days < 0:",
        _ids(
            T_STATE,
            "test_scan_proposes_only_the_old_unowned_classified_plain_file",
            "test_fresh_classified_file_is_kept_below_the_age_floor",
            "test_pass_removes_exactly_the_residue_file_and_logs_it",
        ),
    ),
    "state-suffix-filter": Mutant(
        AGENTCTL_STATE, 're.compile(r"[0-9A-Za-z_-]+\\.json")', 're.compile(r".+\\.json")',
        _ids(
            T_STATE,
            "test_scan_never_looks_at_a_name_outside_the_plain_session_pattern",
            "test_pass_removes_exactly_the_residue_file_and_logs_it",
            "test_remove_refuses_a_name_outside_the_plain_session_pattern",
        ),
    ),
    "state-name-fullmatch": Mutant(
        AGENTCTL_STATE, "if STATE_FILE_NAME.fullmatch(name)]", "if re.match(r\"[0-9A-Za-z_-]+\\.json$\", name)]",
        _ids(T_STATE, "test_a_name_with_a_trailing_newline_is_not_a_state_file"),
    ),
    "state-symlink": Mutant(
        AGENTCTL_STATE, "if path.is_symlink():", "if False:",
        _ids(T_STATE, "test_a_symlinked_state_name_is_kept_in_scan_and_in_remove"),
    ),
    "state-session-owner": Mutant(
        AGENTCTL_STATE, "if ctx.owned_session(", "if False and ctx.owned_session(",
        _ids(
            T_STATE,
            "test_scan_proposes_only_the_old_unowned_classified_plain_file",
            "test_pass_removes_exactly_the_residue_file_and_logs_it",
        ),
    ),
    "session-owner-liveness": Mutant(
        CONTRACT,
        "registry._safe(rec.session_id) == wanted and _record_alive(rec, now_ts, ttl_s)",
        "registry._safe(rec.session_id) == wanted",
        _ids(T_STATE, "test_session_owned_by_live_pid_or_fresh_heartbeat_and_by_sanitized_id",
             "test_session_ownership_ends_when_the_heartbeat_passes_the_ttl"),
    ),
    "backup-push-duty": Mutant(
        GIT_WORKTREES, "return backup_push(worktrees[0].path, ctx)", "return []",
        _ids(T_GIT, "test_unpushed_branch_is_pushed", "test_new_branch_gets_a_remote_ref"),
    ),
    "backup-never-force": Mutant(
        GIT_WORKTREES,
        'refspec = f"refs/heads/{branch}:refs/heads/{branch}"',
        'refspec = f"+refs/heads/{branch}:refs/heads/{branch}"',
        _ids(
            T_GIT,
            "test_remote_that_moved_on_is_rejected_not_overwritten",
            "test_push_argv_is_the_plain_refspec_form",
        ),
    ),
    "backup-trunk-excluded": Mutant(
        GIT_WORKTREES,
        'return name in ("main", "master") or name.startswith(("release-", "release/"))',
        "return False",
        _ids(T_GIT, "test_trunk_named_branches_are_never_pushed"),
    ),
    "backup-skip-backed-up": Mutant(
        GIT_WORKTREES, "if _is_ancestor(repo, sha, remote):\n        return None", "if False:\n        return None",
        _ids(T_GIT, "test_backed_up_branch_is_not_pushed_again"),
    ),
    "backup-dry-run": Mutant(
        GIT_WORKTREES, 'if ctx.dry_run:\n        return "would-push", ""', 'if False:\n        return "would-push", ""',
        _ids(T_GIT, "test_dry_run_pushes_nothing_logs_nothing_and_reports_the_partition"),
    ),
    "backup-fail-open": Mutant(
        GIT_WORKTREES,
        'except Exception as exc:\n            outcome, detail = "error", f"{type(exc).__name__}: {exc}"',
        'except KeyboardInterrupt as exc:\n            outcome, detail = "error", f"{type(exc).__name__}: {exc}"',
        _ids(T_GIT, "test_one_branch_failing_does_not_stop_the_others"),
    ),
    "basetemp-live-lock": Mutant(
        PYTEST_BASETEMP, "if pid_alive(pid):", "if False:",
        _ids(T_BASETEMP, "test_dir_with_a_live_lock_pid_is_kept",
             "test_remove_keeps_a_dir_whose_lock_became_live_after_the_scan"),
    ),
    "basetemp-age-floor": Mutant(
        PYTEST_BASETEMP, "if age_hours < SHORT_FLOOR_HOURS:", "if age_hours < 0:",
        _ids(T_BASETEMP, "test_dir_with_a_dead_pid_lock_younger_than_the_floor_is_kept",
             "test_a_fresh_lock_keeps_an_old_directory",
             "test_old_lockless_dir_is_removed_and_fresh_lockless_dir_is_kept"),
    ),
    "basetemp-name-filter": Mutant(
        PYTEST_BASETEMP, 'NUMBERED_DIR = re.compile(r"pytest-[0-9]+")', 'NUMBERED_DIR = re.compile(r"pytest-.*")',
        _ids(T_BASETEMP, "test_names_other_than_numbered_pytest_dirs_get_no_verdict",
             "test_remove_refuses_a_path_that_is_not_a_direct_child_of_the_root"),
    ),
    "basetemp-cwd-holder": Mutant(
        PYTEST_BASETEMP, "if any(_inside(real, held) for held in holders):", "if False:",
        _ids(T_BASETEMP, "test_a_process_with_its_cwd_inside_the_dir_keeps_it",
             "test_remove_keeps_a_dir_a_process_entered_after_the_scan"),
    ),
    "basetemp-holders-unavailable": Mutant(
        PYTEST_BASETEMP, 'return _keep(path, "cwd scan unavailable")', "pass",
        _ids(T_BASETEMP, "test_an_unavailable_cwd_scan_keeps_every_dir"),
    ),
    "basetemp-symlink": Mutant(
        PYTEST_BASETEMP, "st = os.lstat(path)", "st = os.stat(path)",
        _ids(T_BASETEMP, "test_a_symlink_named_like_a_numbered_dir_is_kept"),
    ),
    "basetemp-foreign-uid": Mutant(
        PYTEST_BASETEMP, "if st.st_uid != _current_uid():", "if False:",
        _ids(T_BASETEMP, "test_a_dir_of_another_uid_is_kept"),
    ),
    "basetemp-owned-scope": Mutant(
        PYTEST_BASETEMP, "if ctx.owned_path(str(path)):", "if False:",
        _ids(T_BASETEMP, "test_a_dir_owned_by_a_live_scope_record_is_kept"),
    ),
    "basetemp-remove-rejudge": Mutant(
        PYTEST_BASETEMP, "if judge(target, ctx, _holders_or_none()).action != REMOVE:", "if False:",
        _ids(T_BASETEMP, "test_remove_keeps_a_dir_whose_lock_became_live_after_the_scan",
             "test_remove_keeps_a_dir_a_process_entered_after_the_scan",
             "test_an_error_while_judging_one_dir_keeps_only_that_dir"),
    ),
    "pressure-threshold": Mutant(
        RUNNER, "if free >= threshold:", "if free > threshold:",
        _ids(T_PRESSURE, "test_equal_to_threshold_is_not_due"),
    ),
    "pressure-keeps-veto": Mutant(
        RUNNER,
        "_normalize(reaper.module.scan(dataclasses.replace(ctx, due=reaper.name in due)))",
        "(_normalize(reaper.module.scan(dataclasses.replace(ctx, due=reaper.name in due))) if reaper.name in due else [])",
        _ids(T_PRESSURE, "test_non_due_keep_vetoes_pressured_removal",
             "test_every_reaper_scans_in_a_pressure_pass_but_only_the_pressured_one_is_due"),
    ),
    "pressure-cooldown": Mutant(
        RUNNER,
        "elapsed = prev is None or (ctx.now - prev) >= PRESSURE_COOLDOWN_MIN * 60.0",
        "elapsed = True",
        _ids(T_PRESSURE, "test_cooldown_not_elapsed_is_not_due",
             "test_a_second_pass_inside_the_cooldown_does_not_run"),
    ),
    "pressure-no-throttle-stamp": Mutant(
        RUNNER,
        "manual = dry_run or force_run or only is not None or pressured is not None",
        "manual = dry_run or force_run or only is not None",
        _ids(T_PRESSURE, "test_a_pressure_pass_writes_no_throttle_stamp"),
    ),
    "pressure-disarm-on-bad-row": Mutant(
        RUNNER, "return value if 0.0 < value < 100.0 else None", "return value",
        _ids(T_PRESSURE, "test_a_row_that_is_missing_or_not_a_percentage_disarms",
             "test_main_disarms_on_a_bad_row_and_runs_nothing"),
    ),
    "pressure-hook-spawn": Mutant(
        HOOK_BACKUP, "        spawn_pressure()\n", "        pass\n",
        _ids(T_HOOK, "test_it_starts_the_pressure_check_detached_beside_the_upkeep",
             "test_the_pressure_check_starts_for_a_stop_payload"),
    ),
    "residue-idle-floor": Mutant(
        LIVENESS, "if idle < floor_s:", "if idle < 0:",
        _ids(T_LIVENESS, "test_a_tree_younger_than_the_idle_floor_is_kept",
             "test_the_idle_floor_is_exact_at_the_newest_time_in_the_tree",
             "test_a_fresh_file_deep_in_the_tree_keeps_it")
        + _ids(T_RESIDUE, "test_a_candidate_inside_the_idle_floor_is_kept_with_the_idle_floor_slug"),
    ),
    "residue-holder-cwd": Mutant(
        LIVENESS,
        '            ("cwd", _absolute(_readlink(os.path.join(pid_dir, "cwd"), sweep, f"{pid_dir}/cwd"))),\n',
        "",
        _ids(T_LIVENESS, PLANTED_HOLDERS, "test_a_process_with_its_cwd_inside_the_tree_is_found"),
    ),
    "residue-holder-exe": Mutant(
        LIVENESS,
        '            ("exe", _absolute(_readlink(os.path.join(pid_dir, "exe"), sweep, f"{pid_dir}/exe"))),\n',
        "",
        _ids(T_LIVENESS, PLANTED_HOLDERS, "test_a_process_running_an_executable_inside_the_tree_is_found"),
    ),
    "residue-holder-fd": Mutant(
        LIVENESS, '        held["fd"].update(_fd_targets(pid_dir, sweep))\n', "        pass\n",
        _ids(T_LIVENESS, PLANTED_HOLDERS, "test_a_process_with_an_open_file_inside_the_tree_is_found",
             "test_a_deleted_suffix_pipes_and_spaces_in_proc_targets_are_read_correctly"),
    ),
    "residue-holder-maps": Mutant(
        LIVENESS, '        held["maps"].update(_map_paths(pid_dir, sweep))\n', "        pass\n",
        _ids(T_LIVENESS, PLANTED_HOLDERS, "test_a_process_with_a_mapped_file_inside_the_tree_is_found_under_the_maps_kind",
             "test_a_deleted_suffix_pipes_and_spaces_in_proc_targets_are_read_correctly"),
    ),
    "residue-session-owned": Mutant(
        LIVENESS, "if owned(path):", "if False:",
        _ids(T_LIVENESS, "test_a_candidate_owned_by_a_live_session_is_kept")
        + _ids(T_RESIDUE, "test_a_candidate_of_a_live_session_is_kept_session_owned"),
    ),
    "residue-owner-uid": Mutant(
        LIVENESS, "if top.st_uid != uid:", "if False:",
        _ids(T_LIVENESS, "test_a_candidate_of_another_uid_is_kept"),
    ),
    "residue-proc-unreadable-keeps": Mutant(
        LIVENESS, "if sweep.incomplete:", "if False:",
        _ids(T_LIVENESS, "test_an_incomplete_sweep_keeps_every_candidate_with_its_own_slug")
        + _ids(T_RESIDUE, "test_an_incomplete_sweep_keeps_every_candidate"),
    ),
    "residue-mount-keep": Mutant(
        LIVENESS, "if facts.crossed_device or mounted_inside(path, mounts):", "if False:",
        _ids(T_LIVENESS, "test_a_mount_point_inside_the_tree_keeps_it",
             "test_an_entry_on_another_filesystem_is_not_entered_and_keeps_the_tree"),
    ),
    "residue-nested-git": Mutant(
        LIVENESS, "if facts.nested_git:", "if False:",
        _ids(T_LIVENESS, "test_a_nested_git_directory_or_file_keeps_the_tree"),
    ),
    "residue-symlink": Mutant(
        LIVENESS, "child = lstat_fn(entry.path)", "child = os.stat(entry.path)",
        _ids(T_LIVENESS, "test_a_symlink_inside_the_tree_is_not_followed_and_its_target_is_not_read"),
    ),
    "residue-one-fs": Mutant(
        LIVENESS, "if child.st_dev != root_dev:", "if False:",
        _ids(T_LIVENESS, "test_an_entry_on_another_filesystem_is_not_entered_and_keeps_the_tree"),
    ),
    "residue-root-realpath": Mutant(
        TEMP_RESIDUE, "real = os.path.realpath(raw)", "real = os.path.abspath(raw)",
        _ids(T_RESIDUE, "test_a_symlinked_session_root_is_scanned_once_at_its_resolved_path",
             "test_a_session_root_that_is_a_symlink_to_the_sandbox_root_is_one_root"),
    ),
    "residue-opt-in": Mutant(
        TEMP_RESIDUE, "if not ctx.dry_run and not enabled:", "if False:",
        _ids(T_RESIDUE, "test_without_opt_in_a_live_pass_keeps_what_it_would_remove_and_says_why"),
    ),
    "residue-opt-in-remove": Mutant(
        TEMP_RESIDUE, "    if not opt_in.enabled(NAME):\n        return False\n", "",
        _ids(T_RESIDUE, "test_remove_refuses_without_opt_in_even_for_a_dead_candidate"),
    ),
    "residue-remove-rejudge": Mutant(
        TEMP_RESIDUE,
        "    if _judge(candidate, source, ctx, liveness.sweep_holders(), _mounts()).action != REMOVE:",
        "    if False:",
        _ids(T_RESIDUE, "test_remove_judges_again_and_refuses_what_became_held_or_fresh"),
    ),
    "residue-error-keeps": Mutant(
        LIVENESS,
        '    except Exception as exc:\n        return Judgment(KEEP, OS_ERROR, f"{type(exc).__name__}: {exc}")',
        '    except KeyboardInterrupt as exc:\n        return Judgment(KEEP, OS_ERROR, f"{type(exc).__name__}: {exc}")',
        _ids(T_LIVENESS, "test_an_os_error_while_judging_keeps_with_the_error_as_reason",
             "test_a_vanished_candidate_resolves_to_keep"),
    ),
    "residue-dry-run-no-write": Mutant(
        RUNNER, "    if dry_run:\n        for reaper in selected:\n", "    if False:\n        for reaper in selected:\n",
        _ids(T_RUNNER, "test_a_dry_run_writes_no_kept_snapshot_and_no_removal_row")
        + _ids(T_RESIDUE, "test_a_dry_run_through_the_runner_writes_no_stamp_snapshot_or_removal_row"),
    ),
    "residue-summary-format": Mutant(
        TEMP_RESIDUE, 'f"temp-residue kind={kind} removable=', 'f"temp-residue kind:{kind} removable=',
        _ids(T_RESIDUE, "test_the_summary_has_root_lines_every_kind_line_and_slug_only_reason_lines",
             "test_the_dry_run_prints_remove_lines_and_the_summary_and_writes_nothing"),
    ),
    "kept-snapshot-written": Mutant(
        RUNNER, "            _write_kept(kept_dir, reaper.name, now, provenance, keeps)\n", "            pass\n",
        _ids(T_RUNNER, "test_the_kept_snapshot_lists_every_keep_with_reason_and_bytes_and_none_of_the_removals")
        + _ids(T_RESIDUE, "test_the_snapshot_lists_every_keep_with_a_reason_when_the_machine_has_not_opted_in"),
    ),
    "residue-pressure-paths": Mutant(
        TEMP_RESIDUE, "    return list(dict.fromkeys(s.base for s in _sources()))", "    return []",
        _ids(T_RESIDUE, "test_the_pressure_paths_are_the_resolved_existing_roots_without_duplicates",
             "test_a_pressure_pass_through_the_runner_removes_dead_candidates_and_never_a_held_one"),
    ),
    "pressure-hook-stop-only": Mutant(
        HOOK_BACKUP,
        "        sys.stdin.read()\n    except Exception:\n        pass\n"
        "    try:\n        spawn_upkeep()\n    except Exception:\n        pass\n"
        "    try:\n        spawn_pressure()\n",
        "        raw = sys.stdin.read()\n    except Exception:\n        raw = \"\"\n"
        "    try:\n        spawn_upkeep()\n    except Exception:\n        pass\n"
        "    try:\n        if \"Stop\" in raw:\n            spawn_pressure()\n",
        _ids(T_HOOK, "test_the_pressure_check_starts_for_a_session_start_payload"),
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
    env.update(PYTHONPATH=str(subject), REAPER_MUTATION_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--tb=no",
        f"--junitxml={xml_path}", *ids,
    ]
    return subprocess.run(command, cwd=subject, env=env, capture_output=True, text=True).returncode


def _copy_subject(parent: Path) -> Path:
    subject = parent / "scripts"
    shutil.copytree(SCRIPTS_DIR, subject, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
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
        with tempfile.TemporaryDirectory(prefix="reaper-mutant-") as tmp:
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
    with tempfile.TemporaryDirectory(prefix="reaper-control-") as tmp:
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
