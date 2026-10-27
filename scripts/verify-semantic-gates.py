#!/usr/bin/env python3
"""Standing per-run check: no hard-outcome sink is decided by a regex with no
judge_* guard, computed from the live Python tree.

Difficulty removed: a regex that classifies free-text meaning and drives a hard
block (a PreToolUse deny, a Stop block, exit(2)) is the defect
memory-global/leaves/regex-not-for-semantic-classification.md names; without a
standing check, a new such site, or an existing guarded one losing its
judge_* call, lands unnoticed. This script enumerates the sites with
crutch-inventory.py's own enumerate_code_sites / enumerate_code_file_rollups
(imported dynamically, the pattern gen_crutch_registry.py and
scripts/tests/test_no_semantic_unguarded.py already use for this hyphenated
filename) on every run, and nothing is stored: there is no registry file to
go stale.

A site is semantic-unguarded, and fails the run, when ALL of:
  - its outcome class is a hard sink (lib/unguarded_sink_allowlist.py's
    HARD_SINK_CLASSES: pretooluse_deny / stop_block / exit_code_2);
  - neither its own scope nor its file (file-rollup granularity) carries a
    judge_* call;
  - its id is not in the shared KNOWN_UNGUARDED_HARD_SINKS allowlist (the same
    one scripts/tests/test_no_semantic_unguarded.py pins).

Decision procedure: AST + import-graph over Python source only; nothing here
regex-matches prose or classifies meaning.

Honest limits: inherits crutch-inventory.py's (syntactic, per-file, one-hop
bound-name resolution; no dynamic construction); a judge_* call living in a
different FILE is invisible; the site id is file + scope name, never regex
content, so a meaning-regex added inside an already-allowlisted scope is not
seen. A finding is a routing device: restore the judge guard, or, for a
genuine false pairing, add the site to the allowlist with its why-safe ground.

Usage:
    verify-semantic-gates.py [--root PATH] [--staged]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.unguarded_sink_allowlist import HARD_SINK_CLASSES, KNOWN_UNGUARDED_HARD_SINKS  # noqa: E402


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory_semantic_gates", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # mirrors gen_crutch_registry.py / test_no_semantic_unguarded.py
    spec.loader.exec_module(mod)
    return mod


def find_unguarded_hard_sinks(inv, root: Path):
    """Return (unguarded, all_code): the semantic-unguarded hard-sink sites
    under root (sorted by id) and the id -> site index of every enumerated
    code site."""
    code_sites = inv.enumerate_code_sites(root)
    file_rollups = inv.enumerate_code_file_rollups(root)
    all_code = inv._index_by_id([*code_sites, *file_rollups])
    rollup_judge_by_file = {r.file: r.judge_guarded for r in file_rollups}

    unguarded = [
        site
        for site in sorted(code_sites, key=lambda s: s.id)
        if site.outcome_class in HARD_SINK_CLASSES
        and not site.judge_guarded
        and not rollup_judge_by_file.get(site.file, False)
        and site.id not in KNOWN_UNGUARDED_HARD_SINKS
    ]
    return unguarded, all_code


def run_check(inv, root: Path) -> int:
    unguarded, all_code = find_unguarded_hard_sinks(inv, root)

    print(f"verify-semantic-gates: {len(all_code)} code site(s) (scope + file-rollup) at {root}")
    print(f"semantic-unguarded hard sinks: {len(unguarded)}")
    for site in unguarded[:20]:
        print(f"  SEMANTIC-UNGUARDED {site.id}  {site.file}  {site.scope}  outcome={site.outcome_class}")
    if len(unguarded) > 20:
        print(f"  ... and {len(unguarded) - 20} more")

    if not unguarded:
        print("verify-semantic-gates: OK")
        return 0

    print("verify-semantic-gates: FAIL")
    print(
        "\nA hard-outcome sink (deny / block / exit 2) is reached from a regex "
        "with no judge_* call in its scope or file. Guard it with a fail-open "
        "judge (agentctl/advisor.py::judge_binary_ask) and keep the regex as a "
        "high-recall prefilter; for a genuine false pairing (the regex reads "
        "unrelated structural syntax) add the id to KNOWN_UNGUARDED_HARD_SINKS "
        "in scripts/lib/unguarded_sink_allowlist.py with its why-safe ground."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=SCRIPTS_DIR, help="root to scan for Python code sites (crutch-inventory.py's --root)")
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    args = parser.parse_args(argv)

    inv = _load_inventory_module()
    return run_check(inv, args.root)


if __name__ == "__main__":
    sys.exit(main())
