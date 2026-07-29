"""Tests for scripts/crutch-inventory.py (stage 1 of the anti-crutch-audit plan).

Proves: a regex feeding a hard-outcome sink is enumerated with a non-"none"
outcome_class; a regex feeding only a log line is enumerated with outcome_class
"none"; a bound `NAME = re.compile(...)` used via `NAME.search(...)` in
another scope is credited to that scope (one-hop bound-name propagation); a
regex and a sink that live in different scopes of the same file are still
joined in the per-file rollup, including when the joining scope (e.g. one
that only calls a judge_* function) is not itself a candidate site; a
markdown fixture yields exactly one candidate per normative statement; two
runs over the same tree are byte-identical (determinism); and every one of
the 12 sites audited by hand in
memory-global/leaves/regex-not-for-semantic-classification.md is reachable,
AT THE GRANULARITY THE AUDIT NAMES (scope where the audit names a function,
file rollup otherwise), through this enumerator's real-repo output
(regression-recall).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass() resolves cls.__module__ via sys.modules
    spec.loader.exec_module(mod)
    return mod


inv = _load_inventory_module()


# --- Domain A: code sites -----------------------------------------------------

_HARD_OUTCOME_FIXTURE = '''
import re


def decide(text):
    if re.search(r"correction|feedback", text):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None
'''

_NON_HARD_FIXTURE = '''
import re


def note(text):
    if re.search(r"debug marker", text):
        print("saw a debug marker in:", text)
'''


def test_regex_feeding_hard_sink_is_enumerated_hard(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_OUTCOME_FIXTURE, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    decide_sites = [s for s in sites if s.scope == "decide"]
    assert len(decide_sites) == 1
    site = decide_sites[0]
    assert site.outcome_class == "pretooluse_deny"
    assert "correction|feedback" in site.pattern_source


def test_regex_feeding_only_log_line_is_enumerated_non_hard(tmp_path):
    (tmp_path / "soft_site.py").write_text(_NON_HARD_FIXTURE, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    note_sites = [s for s in sites if s.scope == "note"]
    assert len(note_sites) == 1
    assert note_sites[0].outcome_class == "none"


def test_nested_function_scope_is_distinct_from_enclosing_scope(tmp_path):
    fixture = '''
import re


def outer(text):
    def inner(t):
        return re.search(r"nested", t)
    return inner(text)
'''
    (tmp_path / "nested_site.py").write_text(fixture, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    scopes = {s.scope for s in sites}
    assert "outer.inner" in scopes
    assert "outer" not in scopes  # outer's own body constructs no regex, reaches no sink


def test_bound_name_regex_propagates_to_referencing_scope(tmp_path):
    """A module-level `NAME = re.compile(...)` used via `NAME.search(...)` in a
    helper is credited to the HELPER's scope, not only the module scope — the
    one-hop bound-name propagation named in the module docstring."""
    fixture = '''
import re

_PAT = re.compile(r"secret")


def helper(text):
    return _PAT.search(text)
'''
    (tmp_path / "bound_site.py").write_text(fixture, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    helper_sites = [s for s in sites if s.scope == "helper"]
    assert len(helper_sites) == 1
    assert "secret" in helper_sites[0].pattern_source


def test_file_rollup_pairs_regex_and_sink_across_scopes(tmp_path):
    """The BLOCKING-1 case in miniature: a regex compiled and used in one
    scope, consumed by a sink built in a totally different scope. Neither
    scope-local site carries both halves; the file rollup must."""
    fixture = '''
import re


def _helper(text):
    return re.search(r"needle", text)


def decide(text):
    if _helper(text):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None
'''
    (tmp_path / "severed_site.py").write_text(fixture, encoding="utf-8")

    scope_sites = inv.enumerate_code_sites(tmp_path)
    decide_site = next(s for s in scope_sites if s.scope == "decide")
    helper_site = next(s for s in scope_sites if s.scope == "_helper")
    # scope-local view: the regex and the sink are NOT in the same record.
    assert decide_site.pattern_source == ""
    assert decide_site.outcome_class == "pretooluse_deny"
    assert "needle" in helper_site.pattern_source
    assert helper_site.outcome_class == "none"

    rollups = inv.enumerate_code_file_rollups(tmp_path)
    assert len(rollups) == 1
    rollup = rollups[0]
    assert any("needle" in p for p in rollup.regex_patterns)
    assert rollup.outcome_class == "pretooluse_deny"


def test_file_rollup_captures_judge_guard_from_non_candidate_scope(tmp_path):
    """A scope that only calls a judge_* function (no local regex, no local
    sink) is not itself a candidate site — enumerate_code_sites correctly
    drops it. But its judge_guarded signal must still surface in the file
    rollup, because that is the ONLY place hook-turn-end-gate.py's
    build_context() (which calls every judge_*/detect function in that file
    but reaches no sink itself) is visible at all."""
    fixture = '''
def judge_something(text, runner):
    return False


def check(text, runner):
    return judge_something(text, runner)


def deny_if(text, runner):
    if check(text, runner):
        return {"decision": "block"}
    return None
'''
    (tmp_path / "judge_site.py").write_text(fixture, encoding="utf-8")

    scope_sites = inv.enumerate_code_sites(tmp_path)
    assert [s for s in scope_sites if s.scope == "check"] == []

    rollups = inv.enumerate_code_file_rollups(tmp_path)
    assert len(rollups) == 1
    assert rollups[0].judge_guarded is True


# --- Domain B: prose sites -----------------------------------------------------

_PROSE_FIXTURE = """\
# Top heading

## A rule section

You must never skip the verification step before merging.

This sentence has no obligation keyword at all.

- Always run the full suite before a release.
- This bullet also has no modal keyword.

```
must never # inside a fenced code block, should not be picked up
```

## A table

| Rule | Note |
|---|---|
| Required: sign off on every change | see policy |
"""


def test_prose_yields_one_candidate_per_normative_statement(tmp_path):
    path = tmp_path / "fixture.md"
    path.write_text(_PROSE_FIXTURE, encoding="utf-8")
    sites = inv.enumerate_prose_sites([path])

    sentences = {s.sentence for s in sites}
    assert any("must never skip the verification step" in s for s in sentences)
    assert any("Always run the full suite" in s for s in sentences)
    assert any("Required: sign off on every change" in s for s in sentences)

    assert not any("no obligation keyword at all" in s for s in sentences)
    assert not any("no modal keyword" in s for s in sentences)
    assert not any("inside a fenced code block" in s for s in sentences)

    assert len(sites) == 3
    for site in sites:
        assert site.heading_path.startswith("Top heading")


# --- Determinism ---------------------------------------------------------------

def test_determinism_across_two_runs(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_OUTCOME_FIXTURE, encoding="utf-8")
    (tmp_path / "soft_site.py").write_text(_NON_HARD_FIXTURE, encoding="utf-8")
    prose_path = tmp_path / "fixture.md"
    prose_path.write_text(_PROSE_FIXTURE, encoding="utf-8")

    code_run_1 = [s.to_dict() for s in inv.enumerate_code_sites(tmp_path)]
    code_run_2 = [s.to_dict() for s in inv.enumerate_code_sites(tmp_path)]
    assert code_run_1 == code_run_2
    assert json.dumps(code_run_1, sort_keys=True) == json.dumps(code_run_2, sort_keys=True)

    prose_run_1 = [s.to_dict() for s in inv.enumerate_prose_sites([prose_path])]
    prose_run_2 = [s.to_dict() for s in inv.enumerate_prose_sites([prose_path])]
    assert prose_run_1 == prose_run_2


def test_determinism_on_real_tree():
    """The stage criterion is stability against the UNCHANGED real tree, not just
    a synthetic fixture — assert byte-identical enumeration across two runs over
    the actual scripts dir (both the scope-level sites and the file rollups)."""
    sites_1 = json.dumps([s.to_dict() for s in inv.enumerate_code_sites(inv.SCRIPTS_DIR)], sort_keys=True)
    sites_2 = json.dumps([s.to_dict() for s in inv.enumerate_code_sites(inv.SCRIPTS_DIR)], sort_keys=True)
    assert sites_1 == sites_2
    roll_1 = json.dumps([r.to_dict() for r in inv.enumerate_code_file_rollups(inv.SCRIPTS_DIR)], sort_keys=True)
    roll_2 = json.dumps([r.to_dict() for r in inv.enumerate_code_file_rollups(inv.SCRIPTS_DIR)], sort_keys=True)
    assert roll_1 == roll_2


# --- Regression recall: the 12 hand-audited sites -------------------------------

# One row per site in the audit table of
# memory-global/leaves/regex-not-for-semantic-classification.md
# ("Structural-vs-semantic audit of the hook suite"). `scope` is the exact
# function name the audit table itself names via its own "-> functionname"
# notation — all five such rows live in hook-turn-end-gate.py and are checked
# against enumerate_code_sites at that exact scope. The other seven rows are
# NOT named by function in the audit table, because in each of those files the
# dict/exit literal that actually fires is built in a DIFFERENT scope than any
# local regex (or, for 5 of the 7 — all but hook-guard-destructive-rm.py and
# hook-multi-mount-search-guard.py — there is no regex anywhere in the file at
# all) — those rows are checked against the FILE ROLLUP (scope=None below),
# the record BLOCKING-1 introduced specifically so a file-wide (regex/judge,
# sink) pairing has somewhere to live when no single scope carries both
# halves. `is_semantic` marks the 4 rows the audit table calls SEMANTIC
# (prefilter + fail-open judge); for those the file rollup must additionally
# show judge_guarded=True.
#
# The prior wording here attributed recall-at-file-granularity to "five rows
# sharing one file" — those five are distinct top-level functions, so
# scope-level checking WAS available for them, and IS what's done below. The
# real reason seven rows need file-level (not scope-level) checking is the
# regex/sink LINKAGE SEVERING crutch-inventory.py's module docstring
# describes (BLOCKING 1): the sink and any local regex live in different
# scopes of the same file.
_AUDITED_ROWS = [
    ("hook-guard-destructive-rm.py (deny)", "hook-guard-destructive-rm.py", None, False),
    ("hook-guard-canon-readonly.py (deny)", "hook-guard-canon-readonly.py", None, False),
    ("hook-multi-mount-search-guard.py (deny)", "hook-multi-mount-search-guard.py", None, False),
    ("hook-state-gate.py (deny)", "hook-state-gate.py", None, False),
    ("hook-scope-conflict.py (deny + Stop block)", "hook-scope-conflict.py", None, False),
    ("hook-plan-delivery-gate.py (deny)", "hook-plan-delivery-gate.py", None, False),
    ("hook-escalation-diagnosis-gate.py (deny)", "hook-escalation-diagnosis-gate.py", None, True),
    ("hook-turn-end-gate.py -> self_improvement_blockers", "hook-turn-end-gate.py", "self_improvement_blockers", True),
    ("hook-turn-end-gate.py -> escalation_without_diagnosis_blockers", "hook-turn-end-gate.py", "escalation_without_diagnosis_blockers", True),
    ("hook-turn-end-gate.py -> prose_binary_ask_blockers", "hook-turn-end-gate.py", "prose_binary_ask_blockers", True),
    ("hook-turn-end-gate.py -> resolution_turn_blockers", "hook-turn-end-gate.py", "resolution_turn_blockers", False),
    ("hook-turn-end-gate.py -> long_job_autowake_blockers", "hook-turn-end-gate.py", "long_job_autowake_blockers", False),
]

assert len(_AUDITED_ROWS) == 12


@pytest.fixture(scope="module")
def real_code_sites():
    return inv.enumerate_code_sites(SCRIPTS_DIR)


@pytest.fixture(scope="module")
def real_file_rollups():
    return inv.enumerate_code_file_rollups(SCRIPTS_DIR)


@pytest.mark.parametrize("row_label,filename,scope,is_semantic", _AUDITED_ROWS)
def test_regression_recall_audited_site(real_code_sites, real_file_rollups, row_label, filename, scope, is_semantic):
    if scope is not None:
        matches = [s for s in real_code_sites if s.file.endswith(filename) and s.scope == scope]
        assert matches, f"audited row {row_label!r}: scope {scope!r} not enumerated in {filename}"
        assert matches[0].outcome_class != "none", (
            f"audited row {row_label!r}: scope {scope!r} enumerated but reaches no hard-outcome sink"
        )
    else:
        rollups = [r for r in real_file_rollups if r.file.endswith(filename)]
        assert rollups, f"audited row {row_label!r}: no file rollup found for {filename}"
        assert rollups[0].outcome_class != "none", (
            f"audited row {row_label!r}: file rollup for {filename} reaches no hard-outcome sink"
        )

    if is_semantic:
        rollups = [r for r in real_file_rollups if r.file.endswith(filename)]
        # NOTE: judge_guarded is a FILE-LEVEL OR — it asserts some judge_* call
        # exists somewhere in {filename}, not that THIS row's own guardian is
        # judge-guarded. Per-guardian judge loss is not observable at this
        # granularity (a sibling guardian's judge keeps the file boolean True);
        # that limit is stated in crutch-inventory.py's "Honest limits".
        assert rollups and rollups[0].judge_guarded, (
            f"audited row {row_label!r}: SEMANTIC per the leaf's audit table, but the file "
            f"rollup for {filename} shows no judge_* call anywhere in the file — the "
            f"prefilter-and-judge shape is unobservable at file granularity"
        )


def test_regression_recall_covers_all_twelve_rows(real_code_sites, real_file_rollups):
    """A negative end-state check alongside the parametrized positive checks
    above: no audited row is silently absent from BOTH the scope-level and
    file-rollup output."""
    found_files_code = {Path(s.file).name for s in real_code_sites}
    found_files_rollup = {Path(r.file).name for r in real_file_rollups}
    audited_files = {filename for _, filename, _, _ in _AUDITED_ROWS}
    missing = audited_files - found_files_code - found_files_rollup
    assert not missing, f"audited files with zero enumerated sites or rollups: {sorted(missing)}"
