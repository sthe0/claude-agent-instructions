#!/usr/bin/env python3
"""Standing per-run check: scripts/crutch_registry.toml still matches the live
Python tree, and no `semantic-*` entry's judge-guard was silently reverted.

Difficulty removed: crutch-inventory.py (stage 1) and crutch_registry.toml
(stage 2) are a one-time snapshot. Without a re-run wired into the normal
verification path, three regressions can land unnoticed: a brand-new
regex-to-hard-sink site with no registry entry at all; a registry entry whose
site was renamed/removed (stale); and — the gap
scripts/tests/test_no_semantic_unguarded.py names explicitly as its own
complementary check — a site the registry trusts as `semantic-*` (judge-
guarded) losing that guard in a later edit, which a (file, scope)-keyed
allowlist cannot see because neither its id nor its scope-level shape changes.

This script re-derives each of the three conditions from the CURRENT tree on
every run:
  (a) MISSING  — a site crutch-inventory.py enumerates now with no matching
                 registry id (a new crutch landing silently).
  (b) STALE    — a registry id whose site no longer exists in the current
                 enumeration (the registry drifted out of date).
  (c) REVERTED — a registry entry whose `class` starts "semantic" still
                 reaches a hard-outcome sink (pretooluse_deny / stop_block /
                 exit_code_2 / hard_behaviour) in the current tree, but
                 neither its own scope nor its file (file-rollup granularity)
                 carries a `judge_*` call any more — the guard
                 gen_crutch_registry.py's ground note relied on has been
                 removed.

Decision procedure: AST + import-graph over Python source ONLY, via
crutch-inventory.py's own enumerate_code_sites / enumerate_code_file_rollups
(imported dynamically — see _load_inventory_module — the established pattern
scripts/gen_crutch_registry.py and scripts/tests/test_no_semantic_unguarded.py
already use for this hyphenated filename). Nothing here regex-matches prose
or classifies meaning; the CLAUDE.md-preamble anti-pattern this whole plan
guards against is a hard block driven by a meaning-classifying regex, and a
verifier that did the same one level up would just relocate the defect.

Restricted to CODE-domain registry entries (`domain in {"code",
"code_file_rollup"}`) — the prose domain (Domain B) is a separate concern
with its own stage (5), and folding it in here would mean reasoning about
prose classification from an AST-only script, which is exactly the boundary
this script must not cross.

Honest limits (stated as limits, not coverage — ast_purity.py's style):
  - Inherits every limit crutch-inventory.py's own module docstring states:
    syntactic, per-file, one-hop bound-name resolution only; no dynamic
    construction (getattr/exec/a pattern assembled at runtime) is visible.
  - Condition (c) covers all FOUR outcome classes crutch-inventory.py
    enumerates (pretooluse_deny / stop_block / exit_code_2 / hard_behaviour)
    — narrower coverage here would leave exactly the `semantic-guarded`
    entries reached only via hard_behaviour (escalation-diagnosis-gate's
    routing call, three of hook-turn-end-gate's `*_blockers` guardians)
    unable to ever trip condition (c), a near-vacuous gap for the entries
    it exists to protect. This deliberately differs from
    test_no_semantic_unguarded.py's `_HARD_SINK_CLASSES`, which stays
    restricted to the three enforcement contracts (deny/block/exit) by
    design — that test pins the allowlist of NEW hard-sink sites entering
    the codomain the original hand audit named, while this condition (c)
    re-checks an ALREADY-semantic entry's existing guard across every sink
    class the enumerator can reach it through. Do not "reconcile" the two
    sets into one; they answer different questions.
  - Condition (c)'s "same path" is, at best, file-rollup granularity: a
    judge_* call anywhere in the same file corroborates a scope's guard (this
    mirrors gen_crutch_registry.py's own CODE_ID_OVERRIDES ground notes,
    e.g. hook-turn-end-gate.py's guardians are judge-guarded by a call in a
    DIFFERENT scope of the SAME file). A judge_* call living in a different
    FILE (an import one hop away) is invisible to both this script and the
    enumerator it reuses.
  - Condition (c) is scoped to registry entries ALREADY classified
    `semantic-*`. A brand-new MEANING-bearing regex added inside an existing
    scope that is currently classified `structural` (not `semantic-*`) does
    not change that scope's id (id = file + scope name only, never regex
    content — the same limit test_no_semantic_unguarded.py's own docstring
    names) and is invisible to conditions (a), (b), and (c) alike. Closing
    that residual would need a content-hash baseline the registry schema
    does not currently carry; left out of this stage as a named, not a
    silent, gap.
  - A finding here is a ROUTING device, not a verdict: it fails so a human or
    model must re-run gen_crutch_registry.py and re-disposition the site in
    the registry (stage 2's classification pass) — the same "prefilter
    feeding a judgment pass, never a hard block itself" boundary
    crutch-inventory.py's own Domain-B prefilter follows.

Cost-discipline record (why this shape, not a heavier one): the plan already
rejected (1) a pre-commit hook — this repo has no other pre-commit-only
check, and it would duplicate scripts/verify-all.py's own aggregation while
adding a second install surface; (2) a CI service — every other check in
this repo runs locally, by design, so a CI-only gate would be a new class of
dependency for one check; (3) a new plugin/registry framework — a single
verifier script wired into the two seams that already exist
(scripts/verify-all.py's CHECKS list, scripts/self-diagnose.py's advisory
scan) is the lightest form that closes the recurrence path; nothing here
introduces a new mechanism class. See task_id "anti-crutch-audit-and-registry"
stage 4.

Measured runtime: ~3.5s wall-clock (`time python3 verify-semantic-gates.py`)
for a full --root scripts/ run against the real tree (912 code sites,
2025 registry entries) on the authoring machine — dominated by parsing
~700 Python files twice (enumerate_code_sites + enumerate_code_file_rollups
each re-walk the tree); well inside a per-session/per-commit budget.

Usage:
    verify-semantic-gates.py [--root PATH] [--registry PATH] [--staged]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = SCRIPTS_DIR / "crutch_registry.toml"

# Deliberately wider than test_no_semantic_unguarded.py's own
# _HARD_SINK_CLASSES (which stays {pretooluse_deny, stop_block, exit_code_2}
# by design — the allowlist-growth pin for NEW enforcement sinks entering the
# original three-contract audit's domain). Condition (c) below re-checks
# whether an ALREADY-semantic registry entry's guard still holds, across
# every sink class the enumerator can reach it through; excluding
# hard_behaviour would make (c) near-vacuous for exactly the entries that
# reach a sink only that way. The two sets answer different questions —
# do not merge them.
_HARD_SINK_CLASSES = frozenset({"pretooluse_deny", "stop_block", "exit_code_2", "hard_behaviour"})
_CODE_DOMAINS = frozenset({"code", "code_file_rollup"})


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory_semantic_gates", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # mirrors gen_crutch_registry.py / test_no_semantic_unguarded.py
    spec.loader.exec_module(mod)
    return mod


def _scope_label(site) -> str:
    return getattr(site, "scope", "<file-rollup>")


def find_regressions(inv, root: Path, registry_path: Path):
    """Return (missing_ids, stale_ids, reverted, all_code) for the current
    tree vs the registry at registry_path. reverted is a list of (id, site)
    pairs; all_code is the id -> site index of the current enumeration."""
    code_sites = inv.enumerate_code_sites(root)
    file_rollups = inv.enumerate_code_file_rollups(root)
    all_code = inv._index_by_id([*code_sites, *file_rollups])

    registry = inv._load_registry(registry_path)
    code_registry = {eid: e for eid, e in registry.items() if e.get("domain") in _CODE_DOMAINS}

    missing = sorted(set(all_code) - set(code_registry))
    stale = sorted(set(code_registry) - set(all_code))

    rollup_judge_by_file = {r.file: r.judge_guarded for r in file_rollups}

    reverted = []
    for eid in sorted(code_registry):
        entry = code_registry[eid]
        if not str(entry.get("class", "")).startswith("semantic"):
            continue
        site = all_code.get(eid)
        if site is None:
            continue  # already reported as stale; do not double-report
        if site.outcome_class not in _HARD_SINK_CLASSES:
            continue
        file_guarded = rollup_judge_by_file.get(site.file, False)
        if not site.judge_guarded and not file_guarded:
            reverted.append((eid, site))

    return missing, stale, reverted, all_code


def run_check(inv, root: Path, registry_path: Path) -> int:
    missing, stale, reverted, all_code = find_regressions(inv, root, registry_path)

    print(f"verify-semantic-gates: {len(all_code)} code site(s) (scope + file-rollup) at {root}")
    print(f"unregistered, condition (a): {len(missing)}")
    for eid in missing[:20]:
        site = all_code[eid]
        print(f"  UNREGISTERED {eid}  {site.file}  {_scope_label(site)}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")

    registry = inv._load_registry(registry_path)
    print(f"stale registry entries, condition (b): {len(stale)}")
    for eid in stale[:20]:
        entry = registry.get(eid, {})
        print(f"  STALE {eid}  {entry.get('file', '<unknown>')}  class={entry.get('class', '<unknown>')}")
    if len(stale) > 20:
        print(f"  ... and {len(stale) - 20} more")

    print(f"judge-guard reverted, condition (c): {len(reverted)}")
    for eid, site in reverted[:20]:
        print(f"  JUDGE-GUARD-REVERTED {eid}  {site.file}  {_scope_label(site)}  outcome={site.outcome_class}")
    if len(reverted) > 20:
        print(f"  ... and {len(reverted) - 20} more")

    ok = not (missing or stale or reverted)
    if ok:
        print("verify-semantic-gates: OK")
        return 0

    print("verify-semantic-gates: FAIL")
    print(
        "\nConditions (a) and (b) are bookkeeping: the registry is generated, "
        "so regenerate it with `python3 scripts/gen_crutch_registry.py` and "
        "then disposition the entries that appear — never hand-edit "
        f"{registry_path.name}. Condition (c) is NOT bookkeeping: a reverted "
        "judge guard means a site that used to route a semantic decision "
        "through a model judge now decides it structurally again. Restore the "
        "guard; regenerating the registry would only record the regression."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=SCRIPTS_DIR, help="root to scan for Python code sites (crutch-inventory.py's --root)")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH, help="registry TOML to diff against")
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    args = parser.parse_args(argv)

    inv = _load_inventory_module()
    return run_check(inv, args.root, args.registry)


if __name__ == "__main__":
    sys.exit(main())
