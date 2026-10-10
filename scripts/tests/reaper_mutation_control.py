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
        '            _append_removal_log(log_path, now, reaper, verdict)\n'
        '            if reaper.module.remove(verdict.path, ctx) is False:\n'
        '                raise RuntimeError("remove() returned False")\n',
        '            if reaper.module.remove(verdict.path, ctx) is False:\n'
        '                raise RuntimeError("remove() returned False")\n'
        '            _append_removal_log(log_path, now, reaper, verdict)\n',
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
