"""Negative control for the two-sided order: each planted defect must turn a test red.

    python3 scripts/tests/order_two_sides_mutants.py --mutant no-search-record
    python3 scripts/tests/order_two_sides_mutants.py --all-killed

`--mutant NAME` plants one defect from `order_agreement_mutants.MUTANTS` (the dashes of NAME read
as underscores) and runs the test files that own it under `ORDER_AGREEMENT_MUTANT`, serially and
in one process, because a monkeypatch is invisible to a worker process. Exit 1 means a test failed
under the defect (killed, the intended result), 0 that every test passed (survived), 4 that the
name is unknown or the defect cannot be applied. `--all-killed` runs the ten defects of this
change and exits 0 only when each is registered and killed, 4 when one is not registered, 1 when
one survives.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from order_agreement_mutants import ENV, MUTANTS, USAGE_ERROR  # noqa: E402

KILLED, SURVIVED = 1, 0

OWNED = {
    "no_search_record": ["test_order_two_sides.py"],
    "correction_unrecorded": ["test_order_two_sides.py"],
    "binding_decision_unrecorded": ["test_order_two_sides.py"],
    "old_form_estimate_unhashed": ["test_order_form_migration.py"],
    "executor_side_digested": ["test_order_two_sides.py"],
    "new_parts_always_digested": ["test_order_form_migration.py"],
    "executor_parts_in_understanding": ["test_order_two_sides.py"],
    "beside_record_dropped": ["test_order_two_sides.py"],
    "beside_in_digest": ["test_order_two_sides.py"],
    "executor_revision_unguarded": ["test_order_two_sides.py"],
}


def run_under(name: str) -> int:
    """Pytest's exit code over the files that own mutant `name`, run with it planted."""
    if name not in OWNED or name not in MUTANTS:
        sys.stderr.write(f"order_two_sides_mutants: {name!r} is not a registered mutant of the "
                         f"two-sided order; known: {sorted(OWNED)}\n")
        return USAGE_ERROR
    argv = [sys.executable, "-m", "pytest", "-q", "-x", "-n", "0", "-p", "no:cacheprovider",
            *(str(TESTS / f) for f in OWNED[name])]
    result = subprocess.run(argv, env={**os.environ, ENV: name}, cwd=TESTS.parent,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mutant", metavar="NAME")
    group.add_argument("--all-killed", action="store_true")
    args = parser.parse_args(argv)
    if args.mutant:
        return run_under(args.mutant.replace("-", "_"))
    survivors = []
    for name in OWNED:
        code = run_under(name)
        if code == USAGE_ERROR:
            return USAGE_ERROR
        if code != KILLED:
            survivors.append(name)
        print(f"{name}: {'killed' if code == KILLED else f'NOT killed (pytest exit {code})'}", flush=True)
    return KILLED if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
