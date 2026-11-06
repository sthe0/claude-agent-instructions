"""Negative control for the telemetry tests: break the code on purpose, expect a red test.

A mutant is a one-place edit of the code under test (see `telemetry_mutants/`): the file,
an anchor that occurs exactly once in it, its replacement, and the tests that must fail.
`--mutant NAME` copies the tree (without `.git`) to a scratch directory, applies the edit there
(the real tree is never touched), runs the named tests in a child pytest and reports by exit
code. The whole tree is copied, not only `scripts/`: the tests read `config.md`, `githooks/` and
the skill files, and a test that fails on the copy for want of them would read as a kill:

    0  the mutant survived (the named tests stayed green): the test suite is too weak
    3  the mutant was killed (a named test failed)
    4  no mutant of that name
    5  the anchor is absent or occurs more than once, so the edit was not applied
    6  the named tests could not give a verdict (collection error, skipped, errored, timeout)
    2  usage error
    1  the harness itself crashed

`--control` runs the same tests on the unmutated copy: 0 when all pass, 7 otherwise; a
control that fails would make every kill meaningless. `--list` prints the mutant names.
`--root` names the tree holding `scripts/` (tests build a synthetic one). Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

EXIT_SURVIVED, EXIT_CRASH, EXIT_USAGE, EXIT_KILLED = 0, 1, 2, 3
EXIT_UNKNOWN, EXIT_ANCHOR, EXIT_NO_VERDICT, EXIT_CONTROL_RED = 4, 5, 6, 7
MUTANTS_SUBDIR = Path("scripts") / "tests" / "telemetry_mutants"
CHILD_TIMEOUT_S = 600
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent


def discover(root: Path = DEFAULT_ROOT) -> list:
    """Every mutant declared in `<root>/scripts/tests/telemetry_mutants/*.py` (MUTANTS lists)."""
    found = []
    directory = Path(root) / MUTANTS_SUBDIR
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        spec = importlib.util.spec_from_file_location(f"_telemetry_mutants_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found.extend(getattr(module, "MUTANTS", []))
    return found


def apply_mutant(scratch: Path, mutant: dict) -> bool:
    """Apply the edit to the scratch copy; False unless the anchor occurs exactly once."""
    target = scratch / mutant["file"]
    if not target.is_file():
        return False
    text = target.read_text(encoding="utf-8")
    if text.count(mutant["anchor"]) != 1:
        return False
    target.write_text(text.replace(mutant["anchor"], mutant["replacement"]), encoding="utf-8")
    return True


def _run_tests(scratch: Path, test_ids: list) -> str:
    """'green', 'red' or 'no-verdict' for the given tests run in the scratch tree."""
    report = scratch / "junit.xml"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTEST_ADDOPTS")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--tb=no",
               f"--junitxml={report}", *test_ids]
    try:
        subprocess.run(command, cwd=scratch, env=env, capture_output=True, timeout=CHILD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return "no-verdict"
    if not report.exists():
        return "no-verdict"
    suites = ET.parse(report).getroot().iter("testcase")
    cases = list(suites)
    if not cases:
        return "no-verdict"
    outcome = "green"
    for case in cases:
        if case.find("skipped") is not None:
            return "no-verdict"
        if case.find("error") is not None:
            return "no-verdict"
        if case.find("failure") is not None:
            outcome = "red"
    return outcome


def _scratch_copy(root: Path, parent: Path) -> Path:
    scratch = parent / "tree"
    shutil.copytree(Path(root), scratch,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    return scratch


def run_control(root: Path) -> int:
    tests = sorted({t for m in discover(root) for t in m["tests"]})
    with tempfile.TemporaryDirectory(prefix="telemetry-mutation-") as parent:
        verdict = _run_tests(_scratch_copy(root, Path(parent)), tests)
    return EXIT_SURVIVED if verdict == "green" else EXIT_CONTROL_RED


def run_mutant(root: Path, name: str) -> int:
    mutant = next((m for m in discover(root) if m["name"] == name), None)
    if mutant is None:
        print(f"unknown mutant {name!r}", file=sys.stderr)
        return EXIT_UNKNOWN
    with tempfile.TemporaryDirectory(prefix="telemetry-mutation-") as parent:
        scratch = _scratch_copy(root, Path(parent))
        if not apply_mutant(scratch, mutant):
            print(f"mutant {name}: anchor not applied (absent or not exactly once) in {mutant['file']}",
                  file=sys.stderr)
            return EXIT_ANCHOR
        verdict = _run_tests(scratch, mutant["tests"])
    return {"red": EXIT_KILLED, "green": EXIT_SURVIVED}.get(verdict, EXIT_NO_VERDICT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--control", action="store_true")
    group.add_argument("--mutant")
    group.add_argument("--list", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    try:
        if args.list:
            print("\n".join(m["name"] for m in discover(args.root)))
            return 0
        if args.control:
            return run_control(args.root)
        return run_mutant(args.root, args.mutant)
    except Exception as exc:
        print(f"harness crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
