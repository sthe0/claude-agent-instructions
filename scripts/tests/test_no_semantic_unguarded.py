"""Regression pin: every hard-outcome sink (deny/block/exit(2)) lacking a
same-scope fail-open judge is a NAMED, individually-grounded site — never a
silently growing set.

Difficulty removed: the prior version of this pin asserted that no code site
carries class == "semantic-unguarded" — a value the stage-2 classifier can
never produce — so it was vacuous. This version pins on data the ENUMERATOR
itself produces (crutch-inventory.py's enumerate_code_sites): the scope-level
sites whose outcome_class is a real hard sink (pretooluse_deny / stop_block /
exit_code_2 — the three enforcement contracts memory-global/leaves/
regex-not-for-semantic-classification.md's hand audit named) with no judge_*
call in the same scope. That set is frozen in
scripts/lib/unguarded_sink_allowlist.py, shared with
scripts/verify-semantic-gates.py so the two cannot disagree.

Any NEW site entering this set grows the enumerated set past the allowlist and
this test goes RED — the author must add a fail-open judge_* guard
(agentctl/advisor.py::judge_binary_ask) or, for a genuine false pairing, add
the site to the allowlist with its own why-safe ground. A stale allowlist
entry (a site that no longer appears) also fails.

Honest limits: (1) the enumerator's scope-local view — a regex severed from its
sink by more than one call hop is invisible; (2) the allowlist id is file and
scope name only, never the regex's content, so a meaning-regex added inside an
already-allowlisted scope does not trip this pin.

Complementary to scripts/verify-semantic-gates.py, which runs the same check
on every verify-all and additionally corroborates by file-level judge calls.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.unguarded_sink_allowlist import HARD_SINK_CLASSES, KNOWN_UNGUARDED_HARD_SINKS  # noqa: E402


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory_pin", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # mirrors test_crutch_inventory.py's load pattern
    spec.loader.exec_module(mod)
    return mod


inv = _load_inventory_module()


def test_hard_sink_sites_without_a_same_scope_judge_are_a_named_allowlist():
    sites = inv.enumerate_code_sites(SCRIPTS_DIR)
    unguarded = {
        s.id: s
        for s in sites
        if s.outcome_class in HARD_SINK_CLASSES and not s.judge_guarded
    }

    known_ids = set(KNOWN_UNGUARDED_HARD_SINKS)
    found_ids = set(unguarded)

    new_ids = found_ids - known_ids
    assert not new_ids, (
        "a NEW hard-outcome sink (deny/block/exit(2)) with no judge_* call in its own "
        "scope appeared and is not in the frozen allowlist: "
        f"{sorted((i, unguarded[i].file, unguarded[i].scope, unguarded[i].outcome_class) for i in new_ids)}. "
        "Fix it with a fail-open judge_* guard (see "
        "agentctl/advisor.py::judge_binary_ask) — never widen the regex's "
        "recall to compensate — or, if the pairing is a genuine false one "
        "(the regex reads unrelated structural syntax), add it to "
        "KNOWN_UNGUARDED_HARD_SINKS in scripts/lib/unguarded_sink_allowlist.py "
        "with its own one-line why-safe ground."
    )

    gone_ids = known_ids - found_ids
    assert not gone_ids, (
        "the allowlist is stale: these ids no longer appear as "
        "unguarded hard-sink sites (site renamed, removed, or now judge-"
        f"guarded) — remove them from KNOWN_UNGUARDED_HARD_SINKS: {sorted(gone_ids)}"
    )
