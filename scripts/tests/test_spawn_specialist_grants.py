"""Coverage for spawn-specialist.py's own engine-grant-materialization
surface: `stage_grant_rules`, `stage_grant_add_dir_args`,
`stage_grant_provenance_lines`, and `load_engine_stage_grants` -- the
functions stage 2 of the spawn-permission-grant-model plan adds so a
spawned child's `--settings`/`--add-dir` reflect the plan's own
declared/derived/runtime grant set, not just its fleet-wide KIND_BASELINES
row.

`build_child_settings`, `KIND_BASELINES`, and `resolve_permission_mode`
already carry broad coverage elsewhere (test_spawn_plans_reachability.py,
test_spawn_project_settings.py, test_spawn_pytest_allowlist_hygiene.py,
test_spawn_merge_verb_grant.py, test_spawn_autocompact_window.py,
test_review_attestation_capability.py) -- this file adds only the
`engine_grants` path through `build_child_settings` those files never
exercise, plus the four functions above, which no existing test file
touches at all (confirmed by grep before writing this file).

`load_engine_stage_grants` is tested against a REAL `agentctl` session via
the same `_to_executing`/`_write_declared_grants_plan` fixture flow
test_stage_grants.py already established -- cross-importing them (as
test_replan_authorization.py already does from test_plan_delivery_gate_presentation.py)
avoids re-deriving that plumbing rather than duplicating it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentctl import grants as GRANTS
from agentctl.grants import GrantValidationError
from test_stage_grants import _to_executing, _write_declared_grants_plan, ns

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_grants", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# --- (A) stage_grant_rules --------------------------------------------------


def test_rule_entry_becomes_an_allow_rule_with_no_deny():
    entries = [{"rule": "Bash(git status:*)", "provenance": "declared"}]
    allow, deny = MOD.stage_grant_rules(entries)
    assert allow == ["Bash(git status:*)"]
    assert deny == []


def test_rule_entry_is_validated_and_an_invalid_rule_raises():
    entries = [{"rule": "Bash(python3:*)", "provenance": "declared"}]
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_rules(entries)


def test_read_add_dir_entry_yields_no_allow_but_an_edit_deny(tmp_path):
    entries = [{"path": str(tmp_path), "mode": "read", "provenance": "declared"}]
    allow, deny = MOD.stage_grant_rules(entries)
    assert allow == []
    assert deny == [f"Edit(//{str(tmp_path).lstrip('/')}/**)"]


def test_write_add_dir_entry_yields_edit_allow_and_guard_denies(tmp_path):
    entries = [{"path": str(tmp_path), "mode": "write", "provenance": "declared"}]
    allow, deny = MOD.stage_grant_rules(entries)
    base = GRANTS.rule_file_arg(str(tmp_path))
    assert allow == [f"Edit({base}/**)"]
    assert deny == [
        f"Edit({base}/**/.claude/**)",
        f"Edit({base}/**/settings*.json)",
        f"Edit({base}/**/.git/**)",
        f"Edit({base}/**/.git)",
    ]


def test_add_dir_entry_is_validated_and_a_protected_write_root_raises(tmp_path):
    home = str(Path.home())
    entries = [{"path": home, "mode": "write", "provenance": "declared"}]
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_rules(entries)


def test_entry_missing_path_or_mode_is_silently_skipped(tmp_path):
    entries = [
        {"path": str(tmp_path), "provenance": "declared"},  # no mode
        {"mode": "read", "provenance": "declared"},  # no path
    ]
    allow, deny = MOD.stage_grant_rules(entries)
    assert allow == [] and deny == []


def test_mixed_rule_and_add_dir_entries_combine_in_order(tmp_path):
    entries = [
        {"rule": "Bash(git status:*)", "provenance": "declared"},
        {"path": str(tmp_path), "mode": "read", "provenance": "derived:DR-E"},
        {"rule": "Bash(git log:*)", "provenance": "runtime"},
    ]
    allow, deny = MOD.stage_grant_rules(entries)
    assert allow == ["Bash(git status:*)", "Bash(git log:*)"]
    assert deny == [f"Edit(//{str(tmp_path).lstrip('/')}/**)"]


def test_empty_entries_list_yields_empty_pair():
    assert MOD.stage_grant_rules([]) == ([], [])


# --- (B) stage_grant_add_dir_args -------------------------------------------


def test_add_dir_args_empty_for_rule_only_entries():
    entries = [{"rule": "Bash(git status:*)", "provenance": "declared"}]
    assert MOD.stage_grant_add_dir_args(entries) == []


def test_add_dir_args_pairs_flag_with_path_for_read_and_write(tmp_path):
    sub_read = tmp_path / "ro"
    sub_write = tmp_path / "rw"
    sub_read.mkdir()
    sub_write.mkdir()
    entries = [
        {"path": str(sub_read), "mode": "read", "provenance": "declared"},
        {"path": str(sub_write), "mode": "write", "provenance": "runtime"},
    ]
    assert MOD.stage_grant_add_dir_args(entries) == [
        "--add-dir", str(sub_read),
        "--add-dir", str(sub_write),
    ]


def test_add_dir_args_skips_malformed_entries(tmp_path):
    entries = [{"path": str(tmp_path)}]  # no mode
    assert MOD.stage_grant_add_dir_args(entries) == []


def test_add_dir_args_validates_and_raises_on_protected_write_root():
    entries = [{"path": str(Path.home()), "mode": "write", "provenance": "declared"}]
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_add_dir_args(entries)


# --- (C) stage_grant_provenance_lines ---------------------------------------


def test_provenance_line_for_a_rule_entry():
    entries = [{"rule": "Bash(git status:*)", "provenance": "declared"}]
    assert MOD.stage_grant_provenance_lines(entries) == [
        "- `Bash(git status:*)` — declared"
    ]


def test_provenance_line_for_an_add_dir_entry(tmp_path):
    entries = [{"path": str(tmp_path), "mode": "read", "provenance": "derived:DR-E"}]
    assert MOD.stage_grant_provenance_lines(entries) == [
        f"- `{tmp_path} (read)` — derived:DR-E"
    ]


def test_provenance_line_defaults_to_unknown_when_absent():
    entries = [{"rule": "Bash(git status:*)"}]
    assert MOD.stage_grant_provenance_lines(entries) == [
        "- `Bash(git status:*)` — unknown"
    ]


def test_provenance_lines_preserve_entry_order(tmp_path):
    entries = [
        {"rule": "Bash(git status:*)", "provenance": "declared"},
        {"path": str(tmp_path), "mode": "write", "provenance": "runtime"},
    ]
    lines = MOD.stage_grant_provenance_lines(entries)
    assert lines[0].startswith("- `Bash(git status:*)`")
    assert lines[1].startswith(f"- `{tmp_path} (write)`")


# --- (D) load_engine_stage_grants (real agentctl session) -------------------


def test_load_engine_stage_grants_returns_the_declared_grant_for_the_matching_kind(
    store, fixtures_dir, tmp_path,
):
    sid = "spawn-grants-kind-match"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    grants = MOD.load_engine_stage_grants(sid, 1, "developer", state_root=tmp_path / "state")
    assert grants is not None
    assert any(e.get("rule") == "Bash(git status:*)" for e in grants)


def test_load_engine_stage_grants_returns_none_for_a_kind_mismatch(
    store, fixtures_dir, tmp_path,
):
    sid = "spawn-grants-kind-mismatch"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    # stage 1's executor is "spawn:developer" (see plan_two_stage.toml) -- a
    # thinker spawn against the same stage must get no engine grants at all.
    assert MOD.load_engine_stage_grants(sid, 1, "thinker", state_root=tmp_path / "state") is None


def test_load_engine_stage_grants_returns_none_for_an_unknown_session(tmp_path):
    assert MOD.load_engine_stage_grants(
        "no-such-session", 1, "developer", state_root=tmp_path / "state"
    ) is None


def test_load_engine_stage_grants_returns_none_for_an_unknown_stage_index(
    store, fixtures_dir, tmp_path,
):
    sid = "spawn-grants-unknown-stage"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    assert MOD.load_engine_stage_grants(sid, 99, "developer", state_root=tmp_path / "state") is None


# --- (E) build_child_settings(..., engine_grants=...) -----------------------


def test_build_child_settings_merges_engine_declared_rule_alongside_baseline():
    engine_grants = [{"rule": "Bash(git status:*)", "provenance": "declared"}]
    settings = MOD.build_child_settings("thinker", engine_grants=engine_grants)
    allow = settings["permissions"]["allow"]
    assert allow[: len(MOD.KIND_BASELINES["thinker"])] == list(MOD.KIND_BASELINES["thinker"])
    assert "Bash(git status:*)" in allow


def test_build_child_settings_empty_engine_grants_list_is_a_no_op():
    with_empty = MOD.build_child_settings("thinker", engine_grants=[])
    without = MOD.build_child_settings("thinker")
    assert with_empty == without


def test_build_child_settings_engine_read_add_dir_adds_edit_deny(tmp_path):
    engine_grants = [{"path": str(tmp_path), "mode": "read", "provenance": "runtime"}]
    settings = MOD.build_child_settings("thinker", engine_grants=engine_grants)
    assert settings["permissions"]["deny"] == [f"Edit(//{str(tmp_path).lstrip('/')}/**)"]


def test_build_child_settings_invalid_engine_rule_raises():
    engine_grants = [{"rule": "Bash(python3:*)", "provenance": "declared"}]
    with pytest.raises(GrantValidationError):
        MOD.build_child_settings("thinker", engine_grants=engine_grants)
