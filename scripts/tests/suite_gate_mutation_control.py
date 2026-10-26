#!/usr/bin/env python3
"""Mutation catalogue for the pre-land suite gate: proves its tests discriminate.

Difficulty removed: a gate whose tests no wrong implementation can turn red is decoration.
Each entry is one wrong version of a production line (a gate that lets a new failure through, skips
the baseline subtraction, ignores the tip binding, widens the exemption, ...), applied by an exact
textual substitution to a scratch copy of scripts/ and githooks/, plus the test ids that must fail
on it.

  python3 scripts/tests/suite_gate_mutation_control.py --mutant NAME   apply one mutant, run its tests
  python3 scripts/tests/suite_gate_mutation_control.py --killed NAME   exit 1 iff --mutant NAME exits 1, else 0
  python3 scripts/tests/suite_gate_mutation_control.py --control        run every listed test, unmutated
  python3 scripts/tests/suite_gate_mutation_control.py --list

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
--killed exists for a negative control whose engine check goes red on any exit other than the
expected one: it reports "killed" as exit 1 and every other outcome as 0.

The child pytest runs only the listed ids, serially, with SUITE_GATE_MUTATION_CHILD=1 so the
in-suite catalogue test does not recurse. Relative paths are relative to the scratch copy's root,
which holds the copies of scripts/ and githooks/.
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
GITHOOKS_DIR = SCRIPTS_DIR.parent / "githooks"
LIB = "scripts/lib/suite_gate.py"
SHARED = "scripts/lib/landing_gate.py"
HOOK = "githooks/pre-push"
LAND = "scripts/land-branch.py"
TESTS = "tests/test_suite_gate.py"
LAND_TESTS = "tests/test_land_branch_suite_gate.py"
SMOKE_LAND_TESTS = "tests/test_land_branch_smoke_gate.py"
SUITE_HOOK_LINE = 'printf \'%s\\n\' "$STDIN" | python3 "$HERE/../scripts/suite-gate.py" pre-push "$@" || suite=$?'
HOOK_VERDICT = '[ "$smoke" -eq 0 ] && [ "$suite" -eq 0 ]'

KILLED, SURVIVED, ANCHOR_MISS, COLLECT_ERROR, BAD_ID, CRASH = 1, 0, 3, 4, 5, 6
CONTROL_RED = 7

DIFF_TREE = '"diff-tree", "-r", "--no-renames", "-z"'
EXEMPT_ALL = "    if paths and all(is_exempt_path(p) for p in paths):"


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    ids: "tuple[str, ...]"


def _ids(*names: str, module: str = TESTS) -> "tuple[str, ...]":
    return tuple(f"{module}::{name}" for name in names)


CATALOGUE: "dict[str, Mutant]" = {
    "suite-new-failure-blocks": Mutant(
        LIB,
        "    if new_failures:\n        return FAIL",
        "    if new_failures:\n        return PASS",
        _ids(
            "test_a_new_failure_blocks_and_is_named",
            "test_a_confirmed_failure_blocks_even_when_another_did_not_repeat",
            "test_cli_run_refuses_a_new_failure_and_names_it",
        ),
    ),
    "suite-baseline-subtraction": Mutant(
        LIB,
        "    return sorted(i for i in set(candidate_ids) if i not in known)",
        "    return sorted(set(candidate_ids))",
        _ids(
            "test_a_failure_the_tip_also_has_does_not_block",
            "test_only_the_failures_absent_on_the_tip_are_new",
            "test_subtract_is_by_node_id",
        ),
    ),
    "suite-tip-binding": Mutant(
        SHARED,
        '    if record.get("base_sha") != live_remote_sha:',
        "    if False:",
        _ids(
            "test_a_record_built_on_a_stale_tip_is_refused",
            "test_a_stored_pass_record_admits_and_a_remote_advance_stales_it",
            "test_pre_push_refuses_a_record_built_on_an_older_tip",
        ),
    ),
    "suite-candidate-binding": Mutant(
        SHARED,
        '    if record.get("candidate_sha") != candidate_sha:',
        "    if False:",
        _ids(
            "test_a_record_for_another_candidate_is_refused_even_with_a_matching_sandbox",
            "test_a_record_stored_under_another_commits_name_is_refused",
        ),
    ),
    "suite-waiver-unavailable-only": Mutant(
        LIB,
        '    if record.get("status") != UNAVAILABLE:',
        "    if False:",
        _ids(
            "test_waive_refuses_a_fail_record_and_a_missing_one",
            "test_cli_waive_covers_unavailable_only",
        ),
    ),
    "suite-unavailable-refuses": Mutant(
        LIB,
        '    if not _waiver_ok(record.get("waiver")):',
        "    if False:",
        _ids(
            "test_an_unavailable_record_needs_a_waiver_with_reason_and_time",
            "test_an_unavailable_run_without_a_waiver_is_refused",
        ),
    ),
    "suite-collection-errors-count": Mutant(
        LIB,
        "    def pytest_collectreport(self, report):",
        "    def pytest_collectreport_ignored(self, report):",
        _ids("test_the_plugin_records_failed_tests_and_collection_errors"),
    ),
    "suite-unconfirmed-not-admitted": Mutant(
        LIB,
        "            if unconfirmed and not new_failures and not reason:",
        "            if False:",
        _ids("test_a_new_failure_that_passes_on_rerun_is_unavailable_not_admitted"),
    ),
    "suite-baseline-keyed-by-tip": Mutant(
        LIB,
        '        and record.get("tip_sha") == tip\n',
        "",
        _ids(
            "test_a_baseline_stored_under_the_tip_but_made_for_another_tip_is_not_used",
            "test_a_baseline_made_for_another_tip_is_not_usable",
        ),
    ),
    "suite-baseline-from-tip": Mutant(
        LIB,
        "            run = runner(repo, tip, root, select=(), timeout_s=timeout_s)",
        '            run = runner(repo, "HEAD", root, select=(), timeout_s=timeout_s)',
        _ids(
            "test_the_baseline_is_built_from_the_tips_own_tree_and_never_from_the_candidate",
            "test_a_failure_the_tip_also_has_does_not_block",
        ),
    ),
    "suite-exit-code-unavailable": Mutant(
        LIB,
        '    return (), f"pytest exited {run.pytest_exit}{_detail(run)}"',
        '    return ids, ""',
        _ids(
            "test_a_pytest_usage_error_exit_is_unavailable",
            "test_a_bad_pytest_exit_is_unavailable_not_pass",
        ),
    ),
    "suite-exit-failure-consistency": Mutant(
        LIB,
        '        if not ids:\n            return (), "pytest exited 1 but the recorder saw no failed test"\n',
        "",
        _ids(
            "test_exit_one_without_a_recorded_failure_is_unavailable",
            "test_a_failing_pytest_exit_without_recorded_failures_is_not_a_pass",
        ),
    ),
    "suite-env-scrub": Mutant(
        LIB,
        '        if name in scrub or name.startswith("AGENTCTL_") or name in ("PYTEST_ADDOPTS", "PYTHONPATH"):',
        "        if False:",
        _ids(
            "test_suite_env_drops_scrubbed_agent_and_pytest_names",
            "test_suite_env_never_passes_the_agent_session_on",
        ),
    ),
    "suite-userbase": Mutant(
        LIB,
        "        PYTHONUSERBASE=userbase,",
        "        PYTHONUSERBASE=str(home),",
        _ids("test_suite_env_keeps_the_real_user_site"),
    ),
    "suite-timeout-unavailable": Mutant(
        LIB,
        '    if run.timed_out:\n        return (), f"pytest did not finish within the timeout{_detail(run)}"\n',
        "",
        _ids(
            "test_a_timed_out_run_is_unavailable_whatever_it_recorded",
            "test_a_timeout_cannot_turn_recorded_failures_into_a_verdict",
        ),
    ),
    "suite-sandbox-removed": Mutant(
        LIB,
        "        shutil.rmtree(root, ignore_errors=True)",
        "        pass",
        _ids(
            "test_the_sandbox_is_removed_after_a_run",
            "test_the_sandbox_is_removed_when_the_runner_raises",
            "test_remove_sandbox_only_touches_a_sandbox_directly_under_its_parent",
        ),
    ),
    "suite-prepush-refuses-unbound": Mutant(
        LIB,
        "        return Decision(found.admitted, found.kind, found.reason)",
        "        return Decision(True, found.kind, found.reason)",
        _ids(
            "test_pre_push_refuses_a_candidate_without_a_record",
            "test_pre_push_refuses_a_record_built_on_an_older_tip",
        ),
    ),
    "suite-output-defused": Mutant(
        LIB,
        "    return LandingDecision(admitted, kind, defuse(line), record, reason)",
        "    return LandingDecision(admitted, kind, line, record, reason)",
        _ids(
            "test_the_decision_line_is_defused",
            "test_cli_check_output_is_defused",
        ),
    ),
    "suite-exempt-requires-all": Mutant(
        LIB,
        EXEMPT_ALL,
        "    if paths and any(is_exempt_path(p) for p in paths):",
        _ids(
            "test_one_code_file_among_docs_makes_the_landing_required",
            "test_deleting_a_code_file_beside_a_doc_edit_is_required",
        ),
    ),
    "suite-exempt-md-only": Mutant(
        LIB,
        "    return path.endswith(EXEMPT_SUFFIX) and path.startswith(EXEMPT_DIRS)",
        "    return path.startswith(EXEMPT_DIRS)",
        _ids(
            "test_a_python_file_under_docs_is_not_an_exempt_path",
            "test_a_non_markdown_file_under_docs_is_required",
        ),
    ),
    "suite-exempt-trees-only": Mutant(
        LIB,
        "    return path.endswith(EXEMPT_SUFFIX) and path.startswith(EXEMPT_DIRS)",
        "    return path.endswith(EXEMPT_SUFFIX)",
        _ids(
            "test_markdown_at_the_top_level_is_not_an_exempt_path",
            "test_markdown_outside_the_two_trees_is_required",
        ),
    ),
    "suite-exempt-rename-both-sides": Mutant(
        SHARED,
        DIFF_TREE,
        '"diff-tree", "-r", "-M", "-z"',
        _ids("test_moving_a_code_file_into_docs_is_required_because_both_sides_count"),
    ),
    "suite-exempt-deletions-count": Mutant(
        SHARED,
        DIFF_TREE,
        '"diff-tree", "-r", "--no-renames", "--diff-filter=d", "-z"',
        _ids("test_deleting_a_code_file_beside_a_doc_edit_is_required"),
    ),
    "suite-exempt-empty-range": Mutant(
        LIB,
        EXEMPT_ALL,
        "    if all(is_exempt_path(p) for p in paths):",
        _ids("test_a_landing_that_changes_no_path_is_not_exempt"),
    ),
    "suite-exempt-lists-files": Mutant(
        LIB,
        '    return ", ".join(paths)',
        '    return f"{len(paths)} file(s)"',
        _ids(
            "test_a_docs_only_landing_needs_no_record",
            "test_the_exempt_line_lists_every_file_untruncated",
            "test_cli_run_on_a_docs_only_landing_is_exempt_and_writes_no_record",
        ),
    ),
    "suite-prepush-exempt": Mutant(
        LIB,
        "        if decision.kind == EXEMPT:",
        "        if False:",
        _ids("test_pre_push_lets_a_docs_only_landing_through_and_names_the_files"),
    ),
    "suite-land-runs-gate": Mutant(
        LAND,
        "        refusal, suite_line = _suite_gate(repo_root, assessment, remote_tip, suite_timeout, suite_waiver, suite_runner)",
        '        refusal, suite_line = None, "SUITE: not required (repository has no suite gate)"',
        _ids(
            "test_a_code_diff_runs_the_suite_and_lands_on_pass",
            "test_a_new_failure_refuses_names_it_and_pushes_nothing",
            module=LAND_TESTS,
        ),
    ),
    "suite-land-refusal-stops": Mutant(
        LAND,
        "    if not decision.admitted:\n        return f\"suite gate: {decision.line.removeprefix('SUITE: ')}\", None",
        "    if False:\n        return f\"suite gate: {decision.line.removeprefix('SUITE: ')}\", None",
        _ids(
            "test_a_new_failure_refuses_names_it_and_pushes_nothing",
            "test_a_refused_suite_stops_before_the_smoke_launch",
            module=LAND_TESTS,
        ),
    ),
    "suite-land-error-refuses": Mutant(
        LAND,
        '        return f"suite gate: {gate.one_line(exc, 400)}", None',
        '        return None, "SUITE: not required (repository has no suite gate)"',
        _ids(
            "test_a_gate_crash_refuses_and_pushes_nothing",
            "test_a_runner_crash_refuses_and_pushes_nothing",
            module=LAND_TESTS,
        ),
    ),
    "suite-land-waiver-wired": Mutant(
        LAND,
        "runner=runner, timeout_s=timeout_s, waiver=waiver,",
        "runner=runner, timeout_s=timeout_s, waiver=None,",
        _ids("test_an_unavailable_suite_needs_a_waiver_to_land", module=LAND_TESTS),
    ),
    "suite-land-timeout-wired": Mutant(
        LAND,
        "runner=runner, timeout_s=timeout_s, waiver=waiver,",
        "runner=runner, timeout_s=None, waiver=waiver,",
        _ids("test_the_suite_timeout_reaches_the_runner", module=LAND_TESTS),
    ),
    "suite-land-tip-wired": Mutant(
        LAND,
        "repo_root, assessment.branch_sha, tip, runner=runner,",
        "repo_root, assessment.branch_sha, assessment.branch_sha, runner=runner,",
        _ids("test_suite_gate_gets_the_fetched_remote_tip", module=LAND_TESTS),
    ),
    "suite-land-ancestry-refuses": Mutant(
        LAND,
        "        if not gate.is_ancestor(repo_root, tip, sha):",
        "        if False:",
        (
            *_ids("test_a_remote_tip_the_branch_lacks_is_refused_before_any_suite_run", module=LAND_TESTS),
            *_ids(
                "test_a_remote_tip_the_branch_lacks_is_refused_before_any_launch",
                module=SMOKE_LAND_TESTS,
            ),
        ),
    ),
    "suite-hook-runs-suite-gate": Mutant(
        HOOK,
        SUITE_HOOK_LINE,
        "suite=0",
        _ids("test_the_hook_refuses_a_push_without_a_suite_record_and_admits_one_with_it", module=LAND_TESTS),
    ),
    "suite-hook-runs-both-gates": Mutant(
        HOOK,
        'printf \'%s\\n\' "$STDIN" | python3 "$HERE/../scripts/instruction-smoke-gate.py" pre-push "$@" || smoke=$?',
        'exec python3 "$HERE/../scripts/instruction-smoke-gate.py" pre-push "$@"',
        _ids(
            "test_the_hook_names_both_gates_and_never_execs_one_of_them",
            "test_the_hook_reports_both_refusals_of_one_push",
            module=LAND_TESTS,
        ),
    ),
    "suite-hook-keeps-smoke-refusal": Mutant(
        HOOK,
        HOOK_VERDICT,
        '[ "$suite" -eq 0 ]',
        _ids("test_the_hook_refuses_when_only_the_smoke_gate_refuses", module=LAND_TESTS),
    ),
    "suite-hook-keeps-suite-refusal": Mutant(
        HOOK,
        HOOK_VERDICT,
        '[ "$smoke" -eq 0 ]',
        _ids("test_the_hook_refuses_a_push_without_a_suite_record_and_admits_one_with_it", module=LAND_TESTS),
    ),
    "suite-hook-stdin-shared": Mutant(
        HOOK,
        SUITE_HOOK_LINE,
        'python3 "$HERE/../scripts/suite-gate.py" pre-push "$@" || suite=$?',
        _ids(
            "test_the_hook_refuses_a_push_without_a_suite_record_and_admits_one_with_it",
            "test_the_hook_reports_both_refusals_of_one_push",
            module=LAND_TESTS,
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
    env.update(PYTHONPATH=str(subject), SUITE_GATE_MUTATION_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--tb=no",
        f"--junitxml={xml_path}", *ids,
    ]
    return subprocess.run(command, cwd=subject, env=env, capture_output=True, text=True).returncode


def _copy_subject(parent: Path) -> Path:
    """Copy scripts/ and githooks/ side by side under ``parent``; return the scripts copy."""
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
    subject = parent / "scripts"
    shutil.copytree(SCRIPTS_DIR, subject, symlinks=True, ignore=ignore)
    shutil.copytree(GITHOOKS_DIR, parent / "githooks", symlinks=True, ignore=ignore)
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
        with tempfile.TemporaryDirectory(prefix="suite-gate-mutant-") as tmp:
            subject = _copy_subject(Path(tmp))
            target = Path(tmp) / mutant.relpath
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
    with tempfile.TemporaryDirectory(prefix="suite-gate-control-") as tmp:
        subject = _copy_subject(Path(tmp))
        xml_path = Path(tmp) / "result.xml"
        rc = _run_child(subject, ids, xml_path)
        description, early = _classify(ids, xml_path, rc)
        if early is not None:
            print(f"control: {description}", file=sys.stderr)
            return CONTROL_RED
        red = [node for node, s in zip(ids, description.split(",")) if s != "passed"]
        if red:
            print("control: not green on the unmutated tree: " + ", ".join(red), file=sys.stderr)
            return CONTROL_RED
        return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mutant", choices=sorted(CATALOGUE))
    group.add_argument("--killed", choices=sorted(CATALOGUE))
    group.add_argument("--control", action="store_true")
    group.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for name, mutant in CATALOGUE.items():
            print(f"{name} {mutant.relpath} {len(mutant.ids)} ids")
        return 0
    if args.control:
        return run_control()
    name = args.mutant or args.killed
    code = run_mutant(name)
    print(f"KILLED {name}" if code == KILLED else f"{name}: exit {code} (not killed)")
    if args.killed:
        return 1 if code == KILLED else 0
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(CRASH)
