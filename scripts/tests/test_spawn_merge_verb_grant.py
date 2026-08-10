"""A developer told to integrate trunk must hold the verbs integration takes.

The difficulty, observed 2026-08-10: a stage whose entire material was "merge
`origin/main` into the delivery branch, resolve the conflicts, re-measure" was
dispatched against a grant that stopped at `git commit`. Every mutating verb the
stage needed was refused, across five distinct command shapes, and the spawn
returned PERMISSION-REQUEST after burning its budget on recon it could not act
on. That is the same shape as the 2026-08-05 defect the grant already documents
— a requirement whose means are withheld — one step later in the lifecycle.

Two surfaces are guarded here, and they pull in opposite directions on purpose:

  1. The merge verbs ARE present, so a brief that orders a merge is executable.
  2. `git push` is STILL absent, so widening the grant toward trunk-integration
     did not quietly widen it toward publication. Merging trunk IN and pushing
     work OUT are different authorities; the file's own comment reserves the
     second for the coordinator, and that reservation is what test two pins.

Deliberately literal about the verbs, unlike
test_spawn_plans_reachability.py's equality-to-the-constant assertion: that test
asks "did another grant eat this one", which the constant answers; this one asks
"can a merge actually run", which only the named verbs answer.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"

# The verbs a trunk-into-branch integration cannot be performed without:
# reading trunk's baseline (detached checkout), classifying the merge
# (merge-base, rev-list), performing it (fetch, merge), and resolving a
# conflicted path (checkout --ours/--theirs, restore).
MERGE_VERBS = (
    "Bash(git fetch:*)",
    "Bash(git merge:*)",
    "Bash(git merge-base:*)",
    "Bash(git rev-list:*)",
    "Bash(git checkout:*)",
    "Bash(git restore:*)",
)


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_merge_grant", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_a_developer_asked_to_merge_holds_every_verb_a_merge_takes():
    allow = MOD.build_child_settings("developer")["permissions"]["allow"]
    missing = [verb for verb in MERGE_VERBS if verb not in allow]
    assert not missing, f"a merge-ordering brief would be refused at: {missing}"


def test_landing_stays_the_coordinators_even_though_merging_is_the_spawns():
    """`git push` absent is a decision, not an omission — assert the property
    (nothing in the allow list publishes) rather than one spelling of it."""
    allow = MOD.build_child_settings("developer")["permissions"]["allow"]
    publishing = [entry for entry in allow if "git push" in entry]
    assert not publishing, f"landing must stay the coordinator's gate: {publishing}"


def test_the_merge_verbs_are_scoped_to_developer_and_not_handed_to_reviewers():
    """The grant is per-kind. A thinker or code-reviewer reviews bytes; giving
    it `git checkout` would let a read-only role mutate the tree it reviews.

    Written to tolerate the stronger form actually in force — those kinds get no
    `permissions` block at all when no directory grant applies — so the test
    stays honest whether the verbs are withheld by omission or by absence."""
    for kind in ("thinker", "code-reviewer", "planner"):
        allow = MOD.build_child_settings(kind).get("permissions", {}).get("allow", [])
        leaked = [verb for verb in MERGE_VERBS if verb in allow]
        assert not leaked, f"{kind} must not hold tree-mutating verbs: {leaked}"
