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
from agentctl.grants import derive_stage_grants
from agentctl.state import Actor, Criterion, Means, Stage, Subject
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


# --- (F) stage_grant_rules: read/write add_dir collision on the same base --
#
# A `read` add_dir (e.g. DR-R) and a `write` add_dir (e.g. a runtime grant)
# on the SAME base directory used to both materialize the byte-identical
# `Edit(<base>/**)` string -- one into `allow`, one into `deny` -- and the
# Claude client resolves that collision as DENY, silently voiding the write
# grant. `stage_grant_rules` now decides each read by coverage: a read on a
# base some write already covers emits no deny. gc1-gc8 pin the same-base
# case; scripts/tests/gc_mutation_control.py checks that each case goes red
# under the weakening it exists to catch.


def test_gc1_no_string_in_both_lists():
    entries = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow, deny = MOD.stage_grant_rules(entries)
    assert not (set(allow) & set(deny))


def test_gc2_write_allow_survives():
    entries = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow, _deny = MOD.stage_grant_rules(entries)
    assert "Edit(//tmp/gcbase/**)" in allow


def test_gc3_write_guard_denies_survive():
    entries = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    _allow, deny = MOD.stage_grant_rules(entries)
    assert "Edit(//tmp/gcbase/**/.claude/**)" in deny
    assert "Edit(//tmp/gcbase/**/settings*.json)" in deny
    assert "Edit(//tmp/gcbase/**/.git/**)" in deny
    assert "Edit(//tmp/gcbase/**/.git)" in deny


def test_gc4_read_only_base_still_denies():
    # CONTROL: no write entry anywhere in the call -- this is what fails if
    # the fix drops read-derived denies outright instead of resolving them
    # per base.
    entries = [{"path": "/tmp/gcbase", "mode": "read"}]
    _allow, deny = MOD.stage_grant_rules(entries)
    assert "Edit(//tmp/gcbase/**)" in deny


def test_gc5_order_independence():
    write_then_read = [
        {"path": "/tmp/gcbase", "mode": "write"},
        {"path": "/tmp/gcbase", "mode": "read"},
    ]
    read_then_write = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow_wr, deny_wr = MOD.stage_grant_rules(write_then_read)
    allow_rw, deny_rw = MOD.stage_grant_rules(read_then_write)
    assert set(allow_wr) == set(allow_rw)
    assert set(deny_wr) == set(deny_rw)


def test_gc6_per_base_scope():
    # This is what fails if the fix suppresses read denies globally whenever
    # any write entry is present anywhere in the call.
    entries = [
        {"path": "/tmp/gcbase", "mode": "write"},
        {"path": "/tmp/other", "mode": "read"},
    ]
    _allow, deny = MOD.stage_grant_rules(entries)
    assert "Edit(//tmp/other/**)" in deny


def _stage_with_output_artifact(artifact: str) -> Stage:
    return Stage(
        index=1,
        title="probe",
        subject=Subject(material="m", result="r", material_refs=[], knowledge_refs=[]),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="d", verify_command=None
        ),
        output_artifacts=[artifact],
    )


def test_gc7_real_producer_via_derive_stage_grants(tmp_path):
    # The read entry is obtained from the REAL producer -- rule DR-R, via
    # `grants.derive_stage_grants` on a stage whose output_artifacts are
    # ABSOLUTE paths under a directory this case creates from pytest's own
    # tmp_path fixture -- not hand-built, so the case still proves something
    # if DR-R's own emission changes. tmp_path rather than a literal path:
    # this file is committed to a public repo and the machine-specific path
    # the real collision was found on is no part of what the case proves.
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    artifact = str(outside_dir / "report.md")
    derived, dropped = derive_stage_grants(_stage_with_output_artifact(artifact), venue="/repo")
    assert not dropped
    assert [d.mode for d in derived.add_dirs] == ["read"]
    assert derived.add_dirs[0].path == str(outside_dir)

    # Concatenated in the order `_stage_grants` uses: declared, then
    # derived, then runtime -- there is no declared entry here.
    runtime_entries = [{"path": str(outside_dir), "mode": "write", "provenance": "runtime"}]
    entries = [d.to_dict() for d in derived.add_dirs] + runtime_entries

    allow, deny = MOD.stage_grant_rules(entries)
    base = GRANTS.rule_file_arg(str(outside_dir))
    assert f"Edit({base}/**)" in allow
    assert f"Edit({base}/**)" not in deny


def test_gc8_trailing_slash_normalization():
    # This is what fails if a base is compared before its trailing slash is
    # normalized away.
    trailing_pair = [
        {"path": "/tmp/gcbase/", "mode": "write"},
        {"path": "/tmp/gcbase", "mode": "read"},
    ]
    gc1_pair = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow_t, deny_t = MOD.stage_grant_rules(trailing_pair)
    allow_p, deny_p = MOD.stage_grant_rules(gc1_pair)
    assert allow_t == allow_p
    assert deny_t == deny_p
    assert "Edit(//tmp/gcbase/**)" not in deny_t


# On NESTED bases the two directions differ. A read under a write is covered:
# the write allow already spans it, so the read emits no deny (gc10). A read
# over a write, covered by no write, would punch its deny into the middle of
# the write allow with nothing subsuming either, so it is refused with
# GrantShadowError (gc9). Nesting is by path segment, not string prefix: a
# sibling sharing a name prefix is an unrelated directory (gc11).


def test_gc9_nested_read_parent_write_child_refused():
    entries = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase/sub", "mode": "write"},
    ]
    with pytest.raises(MOD.GrantShadowError) as excinfo:
        MOD.stage_grant_rules(entries)
    assert "/tmp/gcbase/sub" in str(excinfo.value)
    assert "'/tmp/gcbase'" in str(excinfo.value)


def test_gc10_read_child_is_covered_by_write_parent():
    # This is what fails if coverage matches only an identical base and not
    # a read strictly under the write.
    entries = [
        {"path": "/tmp/gcbase/sub", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow, deny = MOD.stage_grant_rules(entries)
    assert "Edit(//tmp/gcbase/**)" in allow
    assert "Edit(//tmp/gcbase/sub/**)" not in deny


def test_gc11_prefix_sibling_is_not_nested():
    read_sibling = [
        {"path": "/tmp/gcbase2", "mode": "read"},
        {"path": "/tmp/gcbase", "mode": "write"},
    ]
    allow, deny = MOD.stage_grant_rules(read_sibling)
    assert "Edit(//tmp/gcbase/**)" in allow
    assert "Edit(//tmp/gcbase2/**)" in deny
    assert "Edit(//tmp/gcbase/**)" not in deny

    write_sibling = [
        {"path": "/tmp/gcbase", "mode": "read"},
        {"path": "/tmp/gcbase2", "mode": "write"},
    ]
    allow, deny = MOD.stage_grant_rules(write_sibling)
    assert "Edit(//tmp/gcbase2/**)" in allow
    assert "Edit(//tmp/gcbase/**)" in deny


# Bases are compared only after canonicalization, so every spelling of one
# directory collides the way its plain spelling does (gc12, gc13). A `..`
# segment cannot be canonicalized without resolving the filesystem -- it
# names a different directory once a segment is a symlink -- so a base
# carrying one is refused rather than emitted (gc14).


def _shadow_refusal(entries):
    with pytest.raises(MOD.GrantShadowError) as excinfo:
        MOD.stage_grant_rules(entries)
    return str(excinfo.value)


def test_gc12_dot_segment_spelling_refused_like_plain():
    plain = _shadow_refusal(
        [{"path": "/tmp/gcp", "mode": "read"}, {"path": "/tmp/gcp/sub", "mode": "write"}]
    )
    read_dotted = [{"path": "/tmp/./gcp", "mode": "read"}, {"path": "/tmp/gcp/sub", "mode": "write"}]
    write_dotted = [{"path": "/tmp/gcp", "mode": "read"}, {"path": "/tmp/./gcp/sub", "mode": "write"}]
    assert _shadow_refusal(read_dotted) == plain
    assert _shadow_refusal(write_dotted) == plain


def test_gc13_double_slash_spelling_refused_like_plain():
    plain = _shadow_refusal(
        [{"path": "/tmp/gcp", "mode": "read"}, {"path": "/tmp/gcp/sub", "mode": "write"}]
    )
    read_doubled = [{"path": "/tmp//gcp", "mode": "read"}, {"path": "/tmp/gcp/sub", "mode": "write"}]
    write_doubled = [{"path": "/tmp/gcp", "mode": "read"}, {"path": "/tmp//gcp/sub", "mode": "write"}]
    assert _shadow_refusal(read_doubled) == plain
    assert _shadow_refusal(write_doubled) == plain


def test_gc14_dotdot_segment_refused_as_invalid():
    # Uncanonicalized, `/tmp/x/../gcp` compares unequal to `/tmp/gcp` and its
    # deny would silently shadow the write under it.
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_rules(
            [{"path": "/tmp/x/../gcp", "mode": "read"}, {"path": "/tmp/gcp/sub", "mode": "write"}]
        )
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_rules([{"path": "/tmp/x/../gcp", "mode": "read"}])
    with pytest.raises(GrantValidationError):
        MOD.stage_grant_rules([{"path": "/tmp/x/../gcp", "mode": "write"}])


def test_gc15_derived_read_under_runtime_write_is_covered(tmp_path):
    # A stage granted write on its output directory declares an artifact in a
    # subdirectory of it; rule DR-R derives a read add_dir on that
    # subdirectory, and the stage must still be able to write its own output.
    out_dir = tmp_path / "out"
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True)
    derived, dropped = derive_stage_grants(
        _stage_with_output_artifact(str(reports_dir / "report.md")), venue="/repo"
    )
    assert not dropped
    assert [(d.path, d.mode) for d in derived.add_dirs] == [(str(reports_dir), "read")]

    runtime_entries = [{"path": str(out_dir), "mode": "write", "provenance": "runtime"}]
    entries = [d.to_dict() for d in derived.add_dirs] + runtime_entries

    allow, deny = MOD.stage_grant_rules(entries)
    assert f"Edit({GRANTS.rule_file_arg(str(out_dir))}/**)" in allow
    assert f"Edit({GRANTS.rule_file_arg(str(reports_dir))}/**)" not in deny


def test_gc16_covered_read_is_no_conflict_with_a_write_under_it():
    # The write on /tmp/p covers the read on /tmp/p, so the read derives no
    # deny, and a deny that is never emitted cannot overlap the write on
    # /tmp/p/out -- this is what fails if the read-over-write refusal is
    # checked before coverage.
    entries = [
        {"path": "/tmp/p", "mode": "write"},
        {"path": "/tmp/p/out", "mode": "write"},
        {"path": "/tmp/p", "mode": "read"},
    ]
    allow, deny = MOD.stage_grant_rules(entries)
    assert allow == ["Edit(//tmp/p/**)", "Edit(//tmp/p/out/**)"]
    assert "Edit(//tmp/p/**)" not in deny


# Nesting between two grants of the SAME mode is never a conflict: two denies
# or two allows cannot void each other. Rule DR-R derives nested reads
# routinely, so a refusal here would break real plans.


def test_gc17_nested_reads_each_keep_their_deny():
    allow, deny = MOD.stage_grant_rules(
        [{"path": "/x/a", "mode": "read"}, {"path": "/x/a/b", "mode": "read"}]
    )
    assert allow == []
    assert deny == ["Edit(//x/a/**)", "Edit(//x/a/b/**)"]


def test_gc18_nested_writes_each_keep_their_allow_and_guards():
    allow, deny = MOD.stage_grant_rules(
        [{"path": "/x/a", "mode": "write"}, {"path": "/x/a/b", "mode": "write"}]
    )
    assert allow == ["Edit(//x/a/**)", "Edit(//x/a/b/**)"]
    for base in ("//x/a", "//x/a/b"):
        assert f"Edit({base}/**/.claude/**)" in deny
        assert f"Edit({base}/**/settings*.json)" in deny
        assert f"Edit({base}/**/.git/**)" in deny
        assert f"Edit({base}/**/.git)" in deny


def test_gc19_refusal_names_both_provenances():
    # A DR-R read names a directory that appears nowhere in the plan, only as
    # the dirname of some ref, so the path alone does not lead the author to
    # the entry to change; its provenance does.
    message = _shadow_refusal(
        [
            {"path": "/tmp/gcbase", "mode": "read", "provenance": "derived:DR-R"},
            {"path": "/tmp/gcbase/sub", "mode": "write", "provenance": "runtime"},
        ]
    )
    assert "derived:DR-R" in message
    assert "runtime" in message
