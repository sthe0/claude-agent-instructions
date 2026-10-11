#!/usr/bin/env python3
"""Mutation catalogue for the pre-land smoke gate: proves its tests discriminate.

Difficulty removed: a gate whose tests no wrong implementation can turn red is decoration.
Each entry is one wrong version of a production line (a gate that admits UNAVAILABLE without a
waiver, admits a FAIL, ignores the base binding, ...), applied by an exact textual substitution to
a scratch copy of scripts/ and githooks/, plus the test ids that must fail on it.

  python3 scripts/tests/smoke_gate_mutation_control.py --mutant NAME   apply one mutant, run its tests
  python3 scripts/tests/smoke_gate_mutation_control.py --control        run every listed test, unmutated
  python3 scripts/tests/smoke_gate_mutation_control.py --list

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

The child pytest runs only the listed ids, serially, with SMOKE_GATE_MUTATION_CHILD=1 so the
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
LIB = "scripts/lib/instruction_smoke_gate.py"
HOOK = "githooks/pre-push"
LAND = "scripts/land-branch.py"
TESTS = "tests/test_instruction_smoke_gate.py"
LAND_TESTS = "tests/test_land_branch_smoke_gate.py"

KILLED, SURVIVED, ANCHOR_MISS, COLLECT_ERROR, BAD_ID, CRASH = 1, 0, 3, 4, 5, 6
CONTROL_RED = 7


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    ids: "tuple[str, ...]"


def _ids(*names: str, module: str = TESTS) -> "tuple[str, ...]":
    return tuple(f"{module}::{name}" for name in names)


CATALOGUE: "dict[str, Mutant]" = {
    "unavailable-admitted": Mutant(
        LIB,
        '    if not (isinstance(waiver, dict) and one_line(waiver.get("reason") or "") and waiver.get("at")):',
        "    if False:",
        _ids(
            "test_an_unavailable_record_needs_a_waiver_with_reason_and_time",
            "test_run_smoke_unavailable_is_refused_until_waived",
            "test_cli_run_exit_codes_and_waiver_banner",
        ),
    ),
    "fail-admitted": Mutant(
        LIB,
        '        return False, f"smoke result is FAIL (',
        '        return True, f"smoke result is FAIL (',
        _ids(
            "test_a_fail_record_is_refused_even_with_a_waiver_attached",
            "test_run_smoke_never_waives_a_fail",
        ),
    ),
    "waiver-scope": Mutant(
        LIB,
        'not (c["status"] == UNAVAILABLE and _is_live(c["name"]))',
        'not (c["status"] == UNAVAILABLE)',
        _ids("test_waiver_never_covers_an_unavailable_static_or_canon_check"),
    ),
    "base-binding": Mutant(
        LIB,
        '    if record.get("base_sha") != live_remote_sha:',
        "    if False:",
        _ids(
            "test_a_record_built_on_a_stale_base_is_refused",
            "test_a_stored_pass_record_admits_and_a_remote_advance_stales_it",
        ),
    ),
    "sha-binding": Mutant(
        LIB,
        '    if record.get("candidate_sha") != candidate_sha:',
        "    if False:",
        _ids(
            "test_a_record_for_another_commit_is_refused",
            "test_a_record_stored_under_another_commits_name_is_refused",
        ),
    ),
    "sandbox-binding": Mutant(
        LIB,
        '    if record.get("sandbox_core_sha") != candidate_sha:',
        "    if False:",
        _ids("test_a_sandbox_built_from_another_commit_is_refused"),
    ),
    "required-checks": Mutant(
        LIB,
        "        if required not in names:",
        "        if False:",
        _ids("test_a_record_lacking_a_required_check_is_refused"),
    ),
    "surface-exempt": Mutant(
        LIB,
        "    return path.startswith(EXEMPT_DIRS)",
        '    return path.startswith(EXEMPT_DIRS) or path.endswith(".md")',
        _ids(
            "test_everything_else_including_the_instruction_files_is_surface",
            "test_a_surface_change_without_a_record_is_refused",
        ),
    ),
    "surface-traversal": Mutant(
        LIB,
        '    if not path or path.startswith("/") or "" in parts or "." in parts or ".." in parts:',
        '    if not path or path.startswith("/") or "" in parts or "." in parts:',
        _ids("test_a_traversal_out_of_an_exempt_directory_is_surface"),
    ),
    "rename-detect": Mutant(
        LIB,
        '"diff-tree", "-r", "--no-renames", "-z"',
        '"diff-tree", "-r", "-M", "-z"',
        _ids("test_changed_paths_lists_both_sides_of_a_rename"),
    ),
    "prepush-wired": Mutant(
        HOOK,
        'exec python3 "$HERE/../scripts/instruction-smoke-gate.py" pre-push "$@"',
        "exit 0",
        _ids("test_git_push_runs_the_hook_and_a_recorded_candidate_lands"),
    ),
    "land-gate-wired": Mutant(
        LAND,
        "    refusal, smoke_line = _smoke_gate(repo_root, assessment, smoke_timeout, smoke_waiver, runner)",
        '    refusal, smoke_line = None, "SMOKE: not required (diff touches only exempt paths)"',
        _ids(
            "test_a_surface_diff_runs_the_smoke_and_lands_on_pass",
            "test_a_failing_smoke_refuses_and_pushes_nothing",
            "test_an_unavailable_live_launch_needs_a_waiver_to_land",
            "test_a_remote_tip_the_branch_lacks_is_refused_before_any_launch",
            module=LAND_TESTS,
        ),
    ),
    "land-fabricates-literal": Mutant(
        LAND,
        '    print(f"[land-branch] pushed {branch} -> {remote}/{trunk} ({sha})")',
        '    print(gate.admission_line(sha))\n'
        '    print(f"[land-branch] pushed {branch} -> {remote}/{trunk} ({sha})")',
        _ids(
            "test_without_the_hook_land_branch_never_claims_a_hook_admission",
            "test_pre_push_lines_are_relayed_when_the_hook_is_enabled",
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
    env.update(PYTHONPATH=str(subject), SMOKE_GATE_MUTATION_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
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
        with tempfile.TemporaryDirectory(prefix="smoke-gate-mutant-") as tmp:
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
    with tempfile.TemporaryDirectory(prefix="smoke-gate-control-") as tmp:
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
    print(f"KILLED {args.mutant}" if code == KILLED else f"{args.mutant}: exit {code} (not killed)")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(CRASH)
