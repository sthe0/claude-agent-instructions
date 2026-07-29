"""Regression pin: every hard-outcome sink (deny/block/exit(2)) lacking a
same-scope fail-open judge is a NAMED, individually-grounded site — never a
silently growing set.

Difficulty removed: the prior version of this pin asserted that no code site
carries class == "semantic-unguarded" — a value the stage-2 classifier
(gen_crutch_registry.py) can NEVER produce (its codomain is structural /
semantic-guarded / not-a-gate / decidable / perception / already-mechanized /
not-normative). A code review confirmed the test was vacuous: injecting a real
bare-regex-decides-meaning deny hook with no judge_* guard left it GREEN,
because a brand-new hook falls through to the "other guardian hooks"
partition's `structural` default rather than ever being classified
`semantic-unguarded` — that value belongs to a taxonomy the actual classifier
never emits, so the assertion could never fire on any input.

This version pins on data the ENUMERATOR itself produces
(crutch-inventory.py's enumerate_code_sites), independent of the stage-2
perception table: the set of scope-level sites whose outcome_class is a real
hard sink (pretooluse_deny / stop_block / exit_code_2 — the THREE enforcement
contracts crutch-inventory.py itself enumerates in priority order, and the
same three memory-global/leaves/regex-not-for-semantic-classification.md's
hand audit named; a bare regex feeding any one of them can produce a
genuinely wrong verdict) with no judge_* call in the same scope. That set is
frozen below as _KNOWN_UNGUARDED_HARD_SINKS, one entry per site, each with
its own one-line why-safe ground (cross-checked against
scripts/crutch_registry.toml, where every id below carries class
"structural" or "semantic-guarded", disposition "keep").

Any NEW site entering this set (a brand-new deny/block/exit(2) hook, or an
existing guarded one losing its judge_* call) grows the enumerated set past
the frozen allowlist and this test goes RED — the author must either add a
fail-open judge_* guard (agentctl/advisor.py::judge_binary_ask) or, if the
site is a genuine false pairing (the regex reads unrelated structural
syntax), add it to this allowlist with its own why-safe ground and a
matching per-id override in gen_crutch_registry.py's CODE_ID_OVERRIDES. A
stale allowlist entry (a site that no longer appears, e.g. renamed or now
guarded) also fails, so the allowlist cannot silently drift from what the
enumerator actually sees.

Honest limits, so this pin is not read as covering more than it does:
(1) it inherits the enumerator's own scope-local view — a regex severed from
its sink by more than one call hop is invisible to both, the same limit
crutch-inventory.py's own docstring states; (2) the allowlist id is
`_stable_id("code", rel, scope_name)` — FILE and SCOPE NAME only, never the
regex's own content. A meaning-regex added *inside* an already-allowlisted
scope (one of the ids below) does not change the id and does NOT trip this
pin — it stays silently allowlisted. That reintroduction path is guarded
instead by scripts/verify-semantic-gates.py (stage 4), which traces whether
the SAME regex feeds the sink, not just whether the (file, scope) pair is
already known.

Complementary to, not a duplicate of: scripts/verify-semantic-gates.py
(stage 4's standing per-commit AST verifier, which additionally traces
whether the SAME regex feeds the sink) and scripts/crutch-inventory.py
--check (which fails on any undispositioned or stale registry entry in
general, not specifically on this hard-sink subset). This test is the narrow,
fast, always-run pin on the enumerator's own current output.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory_pin", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # mirrors test_crutch_inventory.py's load pattern
    spec.loader.exec_module(mod)
    return mod


inv = _load_inventory_module()

_HARD_SINK_CLASSES = frozenset({"pretooluse_deny", "stop_block", "exit_code_2"})

# Every scope-level site the enumerator currently reports with a real
# deny/block sink and NO judge_* call in the same scope, keyed by its stable
# id (crutch-inventory.py's content-derived hash, which survives unrelated
# line drift). id -> (file, scope, why-safe ground).
_KNOWN_UNGUARDED_HARD_SINKS = {
    "156a5c93ad796c48": (
        "scripts/hook-escalation-diagnosis-gate.py", "deny_with",
        "semantic-guarded per the registry: the judge_outage_escalation call "
        "that guards this deny happens one scope up, in decide() — the "
        "scope-local severing crutch-inventory.py's own docstring names; "
        "confirmed by reading decide() -> deny_with() at source.",
    ),
    "c66f1c838758119e": (
        "scripts/hook-guard-canon-readonly.py", "main",
        "structural per the registry: this file has no local regex; its "
        "one-hop transitive import lib.shell_tokens carries only "
        "shell-token-syntax regexes (assignment-prefix / delimiter-word / "
        "definition patterns), never natural-language meaning.",
    ),
    "943098c19a6861d7": (
        "scripts/hook-guard-destructive-rm.py", "main",
        "structural per the registry: only regex is shell-variable syntax "
        "(`\\$\\{?\\w+\\}?`), never natural-language meaning.",
    ),
    "47508b590a381f20": (
        "scripts/hook-multi-mount-search-guard.py", "main",
        "structural per the registry: only regex is /proc/mounts octal "
        "decoding (`re.sub(r'\\\\(\\d{3})', ...)`), not meaning.",
    ),
    "6b802bd6c3ae8553": (
        "scripts/hook-plan-delivery-gate.py", "deny_with",
        "structural per the registry: this file contains no regex, at scope "
        "or anywhere in its full transitive import closure (agentctl.delivery, "
        "agentctl.gates, agentctl.state, agentctl.text_shape, lib.config_root, "
        "lib.transcript_turns — all verified regex-free); the deny is driven "
        "by engine-state / path predicates.",
    ),
    "756f3a83003805ac": (
        "scripts/hook-scope-conflict.py", "deny",
        "structural per the registry: this file has no local regex; its "
        "two-hop transitive import (session_scope.detector -> "
        "agentctl.exempt_paths) carries only a file-extension path-syntax "
        "regex (_PRODUCTION_FILE_RE), never natural-language meaning.",
    ),
    "c487fa3edf74bd84": (
        "scripts/hook-state-gate.py", "deny_with",
        "structural per the registry: this file has no local regex; its "
        "one-hop transitive import agentctl.exempt_paths carries only a "
        "file-extension path-syntax regex (_PRODUCTION_FILE_RE), never "
        "natural-language meaning.",
    ),
    "23f1443ae65073d6": (
        "scripts/hook-turn-end-gate.py", "decide",
        "semantic-guarded per CODE_ID_OVERRIDES: decide() calls "
        "build_context() (which runs every prefilter + judge_* call and "
        "freezes the results) then collect_blockers() — the propagation "
        "point where the judge-guarded booleans reach this stop_block sink.",
    ),
}


def test_hard_sink_sites_without_a_same_scope_judge_are_a_named_allowlist():
    sites = inv.enumerate_code_sites(SCRIPTS_DIR)
    unguarded = {
        s.id: s
        for s in sites
        if s.outcome_class in _HARD_SINK_CLASSES and not s.judge_guarded
    }

    known_ids = set(_KNOWN_UNGUARDED_HARD_SINKS)
    found_ids = set(unguarded)

    new_ids = found_ids - known_ids
    assert not new_ids, (
        "a NEW hard-outcome sink (deny/block/exit(2)) with no judge_* call in its own "
        "scope appeared and is not in the frozen allowlist above: "
        f"{sorted((i, unguarded[i].file, unguarded[i].scope, unguarded[i].outcome_class) for i in new_ids)}. "
        "Fix it with a fail-open judge_* guard (see "
        "agentctl/advisor.py::judge_binary_ask) — never widen the regex's "
        "recall to compensate — or, if the pairing is a genuine false one "
        "(the regex reads unrelated structural syntax), add it to "
        "_KNOWN_UNGUARDED_HARD_SINKS above with its own one-line why-safe "
        "ground and a matching grounded per-id override in "
        "gen_crutch_registry.py's CODE_ID_OVERRIDES."
    )

    gone_ids = known_ids - found_ids
    assert not gone_ids, (
        "the allowlist above is stale: these ids no longer appear as "
        "unguarded hard-sink sites (site renamed, removed, or now judge-"
        f"guarded) — remove them from _KNOWN_UNGUARDED_HARD_SINKS: {sorted(gone_ids)}"
    )
