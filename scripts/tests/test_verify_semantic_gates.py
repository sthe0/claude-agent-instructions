"""Tests for scripts/verify-semantic-gates.py (stage 4 of the
anti-crutch-audit plan).

Proves each of the three failure conditions fires on a synthetic fixture
(unregistered new site; stale registry entry; a semantic-guarded site whose
judge-guard was removed) and that a legitimate structural regex — including
the file-rollup-corroborated shape gen_crutch_registry.py's own
CODE_ID_OVERRIDES rely on — does NOT false-positive. All fixtures live under
tmp_path; nothing here touches the real tree or the real registry.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_module(name: str, filename: str):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


inv = _load_module("crutch_inventory_vsg_test", "crutch-inventory.py")
vsg = _load_module("verify_semantic_gates_test", "verify-semantic-gates.py")


def _write_registry(path: Path, entries: "list[dict]") -> None:
    lines = []
    for e in entries:
        lines.append("[[entry]]")
        for key in ("id", "domain", "file", "class", "disposition", "ground"):
            if key in e:
                lines.append(f'{key} = {e[key]!r}'.replace("'", '"'))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _registry_entry_for(site, cls: str, disposition: str = "keep", ground: str = "test fixture") -> dict:
    return {
        "id": site.id,
        "domain": site.domain,
        "file": site.file,
        "class": cls,
        "disposition": disposition,
        "ground": ground,
    }


_HARD_SINK_FIXTURE = '''
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

_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE = '''
import re


def decide(text, runner):
    if not judge_meaning(text, runner):
        return None
    if re.search(r"correction|feedback", text):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None


def judge_meaning(text, runner):
    return False
'''

_HARD_SINK_WITH_FILE_LEVEL_JUDGE_FIXTURE = '''
import re


def judge_meaning(text, runner):
    return False


def prefilter(text):
    return bool(re.search(r"correction|feedback", text))


def check(text, runner):
    return prefilter(text) and judge_meaning(text, runner)


def decide(text, runner):
    if check(text, runner):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None
'''

# A hard_behaviour sink (not deny/block/exit): the function's own name
# ("route_decision") matches _HARD_BEHAVIOUR_TOKENS ("route"), so the
# enumerator credits it as a hard_behaviour outcome_class regardless of what
# its body does with the regex match — the same shape hook-escalation-
# diagnosis-gate.py's routing call and hook-turn-end-gate.py's `*_blockers`
# guardians have in the real tree.
_HARD_BEHAVIOUR_SINK_FIXTURE = '''
import re


def route_decision(text):
    if re.search(r"correction|feedback", text):
        return "escalate"
    return "default"
'''


# --- condition (a): unregistered new site -----------------------------------

def test_unregistered_site_is_reported_missing(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [])  # empty: nothing registered at all

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)

    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.id in missing
    assert not stale
    assert not reverted

    rc = vsg.run_check(inv, tmp_path, registry_path)
    assert rc == 1


def test_fully_registered_tree_reports_nothing_missing(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    rollups = inv.enumerate_code_file_rollups(tmp_path)
    registry_path = tmp_path / "registry.toml"
    entries = [_registry_entry_for(s, "structural") for s in sites]
    entries += [_registry_entry_for(r, "structural") for r in rollups]
    _write_registry(registry_path, entries)

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    assert not missing
    assert not stale
    assert not reverted
    assert vsg.run_check(inv, tmp_path, registry_path) == 0


# --- condition (b): stale registry entry -------------------------------------

def test_stale_registry_entry_is_reported(tmp_path):
    (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [{
        "id": "0000000000000000",
        "domain": "code",
        "file": "scripts/gone.py",
        "class": "structural",
        "disposition": "keep",
        "ground": "a site that no longer exists",
    }])

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    assert "0000000000000000" in stale
    assert not reverted
    assert vsg.run_check(inv, tmp_path, registry_path) == 1


# --- condition (c): judge-guard silently reverted ----------------------------

def test_semantic_guarded_site_with_judge_removed_is_reported_reverted(tmp_path):
    """RED: a scope classified semantic-guarded in the registry (recorded when
    it had a same-scope judge_* call) reaches a hard sink and NOW has no
    judge_* call anywhere in its own scope or its file at all — the exact
    'judge-guard silently reverted' regression condition (c) exists for."""
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.judge_guarded is False  # the fixture has no judge_* call at all
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]

    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [
        _registry_entry_for(decide_site, "semantic-guarded"),
        _registry_entry_for(rollup, "structural"),
    ])

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    reverted_ids = {eid for eid, _ in reverted}
    assert decide_site.id in reverted_ids
    assert not missing
    assert not stale
    assert vsg.run_check(inv, tmp_path, registry_path) == 1


def test_semantic_guarded_site_with_same_scope_judge_is_not_reverted(tmp_path):
    """GREEN (mutation reverted): same registry class, but the scope itself
    calls judge_meaning() — the guard is genuinely present, must not fire."""
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.judge_guarded is True
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]

    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [
        _registry_entry_for(decide_site, "semantic-guarded"),
        _registry_entry_for(rollup, "structural"),
    ])

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    assert not reverted
    assert vsg.run_check(inv, tmp_path, registry_path) == 0


def test_semantic_guarded_site_corroborated_by_file_rollup_is_not_a_false_positive(tmp_path):
    """The gen_crutch_registry.py CODE_ID_OVERRIDES shape (e.g.
    hook-turn-end-gate.py's decide()): the judge_* call lives in a DIFFERENT
    scope of the SAME file (check() calls judge_meaning(); decide() itself
    calls no judge_* function). Scope-local judge_guarded is False, but the
    file-rollup corroborates a real guard — must NOT be reported reverted."""
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_FILE_LEVEL_JUDGE_FIXTURE, encoding="utf-8")
    sites = inv.enumerate_code_sites(tmp_path)
    decide_site = next(s for s in sites if s.scope == "decide")
    assert decide_site.judge_guarded is False  # scope-local: decide() itself calls no judge_*
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]
    assert rollup.judge_guarded is True  # file-level: judge_meaning() exists in the same file

    registry_path = tmp_path / "registry.toml"
    entries = [
        _registry_entry_for(s, "semantic-guarded" if s.scope == "decide" else "structural")
        for s in sites
    ]
    entries.append(_registry_entry_for(rollup, "structural"))
    _write_registry(registry_path, entries)

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    assert not reverted
    assert vsg.run_check(inv, tmp_path, registry_path) == 0


def test_semantic_guarded_hard_behaviour_site_with_judge_removed_is_reported_reverted(tmp_path):
    """RED: condition (c) must fire through hard_behaviour, not only
    deny/block/exit(2) — the real tree's escalation-diagnosis-gate routing
    call and hook-turn-end-gate's `*_blockers` guardians are semantic-guarded
    entries that reach the sink ONLY via hard_behaviour. Before
    _HARD_SINK_CLASSES included hard_behaviour, this fixture's outcome_class
    fell outside the checked set and (c) could never fire for it."""
    (tmp_path / "hard_behaviour_site.py").write_text(_HARD_BEHAVIOUR_SINK_FIXTURE, encoding="utf-8")
    route_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "route_decision")
    assert route_site.outcome_class == "hard_behaviour"
    assert route_site.judge_guarded is False
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]

    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [
        _registry_entry_for(route_site, "semantic-guarded"),
        _registry_entry_for(rollup, "structural"),
    ])

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    reverted_ids = {eid for eid, _ in reverted}
    assert route_site.id in reverted_ids
    assert not missing
    assert not stale
    assert vsg.run_check(inv, tmp_path, registry_path) == 1


def test_structural_site_with_no_judge_is_not_reverted(tmp_path):
    """A registry class of 'structural' (never semantic-*) with no judge_*
    call anywhere must not be flagged by condition (c) — that classification
    means the regex was individually confirmed to read non-meaning-bearing
    syntax; condition (c) applies only to sites the registry already trusts
    as judge-guarded."""
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.judge_guarded is False
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]

    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [
        _registry_entry_for(decide_site, "structural"),
        _registry_entry_for(rollup, "structural"),
    ])

    missing, stale, reverted, all_code = vsg.find_regressions(inv, tmp_path, registry_path)
    assert not reverted
    assert vsg.run_check(inv, tmp_path, registry_path) == 0


def test_main_exits_zero_on_clean_fixture(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    rollup = inv.enumerate_code_file_rollups(tmp_path)[0]
    registry_path = tmp_path / "registry.toml"
    _write_registry(registry_path, [
        _registry_entry_for(decide_site, "semantic-guarded"),
        _registry_entry_for(rollup, "structural"),
    ])

    rc = vsg.main(["--root", str(tmp_path), "--registry", str(registry_path), "--staged"])
    assert rc == 0
