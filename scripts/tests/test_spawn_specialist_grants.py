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
import shutil
import subprocess
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

    # The same pair listed write-first is refused with the same message.
    with pytest.raises(MOD.GrantShadowError) as write_first:
        MOD.stage_grant_rules(list(reversed(entries)))
    assert str(write_first.value) == str(excinfo.value)


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


# --- (G) build_child_settings: an Edit allow some Edit deny covers entirely --
#
# The guard globs every writable directory is paired with are spelled once,
# in `write_guard_deny_rules` (gs1). Once every source has contributed,
# `build_child_settings` refuses a payload in which any Edit deny covers an
# Edit allow ENTIRELY, whatever the two sources, since the client would void
# the allow in silence; a deny covering only part of an allow is the guards'
# ordinary job (gs3, gs4). `**` spans zero or more whole segments, `*` stays
# inside one (gs2, gs7); a deny not ending in `/**` covers exact paths only,
# so it never voids a subtree allow (gs5, gs6). gc_mutation_control.py
# checks each case goes red under the weakening it exists to catch.

_GUARD_GLOB_TAILS = ("/**/.claude/**)", "/**/settings*.json)", "/**/.git/**)", "/**/.git)")


def _guards(base: str) -> list[str]:
    return [f"Edit({base}{tail}" for tail in _GUARD_GLOB_TAILS]


def _settings_with_pair(monkeypatch, allow: list[str], deny: list[str]) -> dict:
    # The allow is injected as the target project's own rule: that is the
    # one source whose rules reach the final check unfiltered by shape, so
    # a pair `grants.validate_rule` refuses upstream (an exact `.git` path)
    # still exercises the coverage relation itself. The deny comes from our
    # own machinery, because a pair with BOTH sides from project settings is
    # only warned about, never refused.
    monkeypatch.setattr(MOD, "project_settings_permission_rules", lambda _file: (list(allow), []))
    monkeypatch.setattr(MOD, "write_grant_cwd_deny_rules", lambda *_args: list(deny))
    return MOD.build_child_settings("developer")


def test_gs1_guard_globs_have_one_home(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path))
    monkeypatch.setattr(MOD, "_vcs_root", lambda _cwd: str(tmp_path))
    assert MOD.write_guard_deny_rules(base) == MOD.repo_root_deny_rules("developer", str(tmp_path))
    source = SCRIPT.read_text()
    # Each tail is delimited by its closing paren: `/**/.git)` is not a
    # substring of `/**/.git/**)`.
    for tail in _GUARD_GLOB_TAILS:
        assert source.count(tail) == 1, tail


def test_gs2_file_allow_under_a_claude_guard_is_refused(tmp_path, monkeypatch):
    root = GRANTS.rule_file_arg(str(tmp_path / "root"))
    allow = f"Edit({root}/.claude/worktrees/w/scripts/tests/t.py)"
    deny = f"Edit({root}/**/.claude/**)"
    with pytest.raises(MOD.GrantShadowError):
        _settings_with_pair(monkeypatch, [allow], [deny])


def test_gs3_write_add_dir_is_not_voided_by_its_own_guards(tmp_path):
    target = tmp_path / "b"
    base = GRANTS.rule_file_arg(str(target))
    settings = MOD.build_child_settings(
        "developer", engine_grants=[{"path": str(target), "mode": "write", "provenance": "runtime"}]
    )
    assert f"Edit({base}/**)" in settings["permissions"]["allow"]
    assert settings["permissions"]["deny"] == _guards(base)


def test_gs4_subtree_allow_beside_a_non_subtree_deny_survives(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "b"))
    settings = _settings_with_pair(monkeypatch, [f"Edit({base}/**)"], [f"Edit({base}/**/.git)"])
    assert f"Edit({base}/**)" in settings["permissions"]["allow"]


def test_gs5_exact_path_allow_matched_by_a_non_subtree_deny_is_refused(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "b"))
    with pytest.raises(MOD.GrantShadowError):
        _settings_with_pair(monkeypatch, [f"Edit({base}/x/.git)"], [f"Edit({base}/**/.git)"])


def test_gs6_subtree_allow_is_not_covered_by_a_non_subtree_deny(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "b"))
    settings = _settings_with_pair(monkeypatch, [f"Edit({base}/x/.git/**)"], [f"Edit({base}/**/.git)"])
    assert f"Edit({base}/x/.git/**)" in settings["permissions"]["allow"]


def test_gs7_single_star_stays_inside_one_segment(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "b"))
    allow = f"Edit({base}/x/settings/a.json)"
    settings = _settings_with_pair(monkeypatch, [allow], [f"Edit({base}/**/settings*.json)"])
    assert allow in settings["permissions"]["allow"]


def test_gs8_subtree_deny_does_not_reach_a_prefix_sibling(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "b"))
    allow = f"Edit({base}2/f)"
    settings = _settings_with_pair(monkeypatch, [allow], [f"Edit({base}/**)"])
    assert allow in settings["permissions"]["allow"]


def _write_grant_under_repo_claude_dir(tmp_path, monkeypatch):
    root = tmp_path / "root"
    venue = root / ".claude" / "worktrees" / "w"
    target = venue / "scripts"
    target.mkdir(parents=True)
    monkeypatch.setattr(MOD, "_vcs_root", lambda _cwd: str(root))
    with pytest.raises(MOD.GrantShadowError) as excinfo:
        MOD.build_child_settings(
            "developer",
            engine_grants=[{"path": str(target), "mode": "write", "provenance": "runtime"}],
            workdir=str(venue),
        )
    return root, target, str(excinfo.value)


def test_gs9_stage_write_grant_under_the_repo_claude_guard_is_refused(tmp_path, monkeypatch):
    _write_grant_under_repo_claude_dir(tmp_path, monkeypatch)


def test_gs10_refusal_quotes_both_rules_and_their_sources(tmp_path, monkeypatch):
    root, target, message = _write_grant_under_repo_claude_dir(tmp_path, monkeypatch)
    assert f"Edit({GRANTS.rule_file_arg(str(target))}/**)" in message
    assert f"Edit({GRANTS.rule_file_arg(str(root))}/**/.claude/**)" in message
    assert "runtime" in message
    assert "repo_root_deny_rules" in message


def test_gs11_file_allow_under_a_derived_read_names_both_sources(tmp_path):
    read_dir = tmp_path / "b"
    read_dir.mkdir()
    engine_grants = [
        {"rule": f"Edit({GRANTS.rule_file_arg(str(read_dir / 'w.sh'))})", "provenance": "runtime"},
        {"path": str(read_dir), "mode": "read", "provenance": "derived:DR-R"},
    ]
    with pytest.raises(MOD.GrantShadowError) as excinfo:
        MOD.build_child_settings("developer", engine_grants=engine_grants)
    assert "runtime" in str(excinfo.value)
    assert "derived:DR-R" in str(excinfo.value)


def test_gs12_planner_plans_dir_inside_repo_claude_dir_gets_no_guards(tmp_path, monkeypatch):
    root = tmp_path / "root"
    venue = root / "venue"
    plans = root / ".claude" / "plans"
    venue.mkdir(parents=True)
    plans.mkdir(parents=True)
    monkeypatch.setattr(MOD, "_vcs_root", lambda _cwd: str(root))
    guards = _guards(GRANTS.rule_file_arg(str(root)))

    settings = MOD.build_child_settings("planner", plans_directory=plans, workdir=str(venue))
    assert f"Edit({GRANTS.rule_file_arg(str(plans))}/**)" in settings["permissions"]["allow"]
    assert not set(guards) & set(settings["permissions"].get("deny", []))

    assert MOD.repo_root_deny_rules("developer", str(venue)) == guards


# These cases continue the gs series in gc_mutation_control.py (gs13..gs15)
# but carry no `gs`/`gc` ordinal in their names: an external plan pins
# `pytest -k test_gc` and `pytest -k test_gs` to fixed pass counts, and `-k`
# matches any name containing the substring.


def _refusal_sides(message: str, rule: str) -> tuple[str, str]:
    # The allow and the deny are the same rule text here, so the message is
    # split on its two quotations of it: what follows the first is the
    # allow's source, what follows the second the deny's.
    _, allow_part, deny_part = message.split(repr(rule))
    return allow_part, deny_part


def test_shadow_refusal_names_each_side_producer_when_the_rule_texts_match(tmp_path, monkeypatch):
    rule = f"Edit({GRANTS.rule_file_arg(str(tmp_path / 'p' / 'f.py'))})"
    monkeypatch.setattr(MOD, "project_settings_permission_rules", lambda _file: ([], [rule]))
    with pytest.raises(MOD.GrantShadowError) as excinfo:
        MOD.build_child_settings("developer", engine_grants=[{"rule": rule, "provenance": "runtime"}])
    allow_part, deny_part = _refusal_sides(str(excinfo.value), rule)
    assert "runtime" in allow_part and MOD.PROJECT_SETTINGS_SOURCE not in allow_part
    assert MOD.PROJECT_SETTINGS_SOURCE in deny_part and "runtime" not in deny_part


def test_project_settings_own_shadowed_pair_warns_and_proceeds(tmp_path, monkeypatch, capsys):
    base = GRANTS.rule_file_arg(str(tmp_path / "p"))
    allow, deny = f"Edit({base}/docs/x.md)", f"Edit({base}/docs/**)"
    monkeypatch.setattr(MOD, "project_settings_permission_rules", lambda _file: ([allow], [deny]))
    settings = MOD.build_child_settings("developer")
    assert allow in settings["permissions"]["allow"]
    stderr = capsys.readouterr().err
    assert allow in stderr and deny in stderr


def test_project_settings_deny_over_an_engine_allow_still_refuses(tmp_path, monkeypatch):
    base = GRANTS.rule_file_arg(str(tmp_path / "p"))
    allow = f"Edit({base}/docs/x.md)"
    monkeypatch.setattr(MOD, "project_settings_permission_rules", lambda _file: ([], [f"Edit({base}/docs/**)"]))
    with pytest.raises(MOD.GrantShadowError):
        MOD.build_child_settings("developer", engine_grants=[{"rule": allow, "provenance": "runtime"}])


# --- (G) point exemptions from the settings*.json guard ----------------------
#
# A caller may name files whose own `settings*.json` guard deny is lifted;
# the one recursive glob is then decomposed over the real directory tree so
# every OTHER match stays denied. Names avoid the `gs`/`gc` substrings for the
# `-k` reason given above.


def _literal_guards(base: str) -> list[str]:
    return [
        f"Edit({base}/**/.claude/**)",
        f"Edit({base}/**/settings*.json)",
        f"Edit({base}/**/.git/**)",
        f"Edit({base}/**/.git)",
    ]


def _exempt_fixture(tmp_path) -> Path:
    repo = tmp_path / "repo"
    for rel in (
        "settings.json",
        "proj/settings.json",
        "proj/presets/deep/settings.json",
        "proj/presets/deep/settings.local.json",
        "proj/presets/deep/README.md",
        "proj/presets/other/settings.json",
        "lib/x/settings.json",
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("{}")
    return repo


_EXEMPT_REL = "proj/presets/deep/settings.json"
_STILL_DENIED_REL = (
    "settings.json",
    "proj/settings.json",
    "proj/presets/deep/settings.local.json",
    "proj/presets/other/settings.json",
    "lib/x/settings.json",
    "lib/x/y/settings.local.json",
)


def _file_rule(path: Path) -> str:
    return f"Edit({GRANTS.rule_file_arg(str(path))})"


def _denied(deny: list[str], path: Path) -> bool:
    return any(MOD._edit_deny_covers_allow(rule, _file_rule(path)) for rule in deny)


def test_guard_exempt_absent_leaves_every_guard_source_byte_identical(tmp_path, monkeypatch):
    repo = _exempt_fixture(tmp_path)
    base = GRANTS.rule_file_arg(str(repo))
    outside = str(tmp_path / "elsewhere" / "settings.json")
    assert MOD.write_guard_deny_rules(base) == _literal_guards(base)
    assert MOD.write_guard_deny_rules(base, ()) == _literal_guards(base)

    monkeypatch.setattr(MOD, "_vcs_root", lambda _cwd: str(repo))
    assert MOD.repo_root_deny_rules("developer", str(repo)) == _literal_guards(base)
    assert MOD.repo_root_deny_rules("developer", str(repo), [outside]) == _literal_guards(base)

    entries = [{"path": str(repo), "mode": "write", "provenance": "declared"}]
    for exempt in (None, [], [outside]):
        assert MOD.stage_grant_rules(entries, exempt_abs_paths=exempt) == ([f"Edit({base}/**)"], _literal_guards(base))


def test_guard_exempt_lifts_only_the_named_settings_file(tmp_path):
    repo = _exempt_fixture(tmp_path)
    base = GRANTS.rule_file_arg(str(repo))
    allow = _file_rule(repo / _EXEMPT_REL)
    with pytest.raises(MOD.GrantShadowError):
        MOD._check_no_allow_fully_denied([allow], MOD.write_guard_deny_rules(base), {}, {})

    deny = MOD.write_guard_deny_rules(base, [_EXEMPT_REL])
    MOD._check_no_allow_fully_denied([allow], deny, {}, {})
    assert f"Edit({base}/proj/presets/deep/settings.local.json)" in deny
    assert f"Edit({base}/proj/presets/other/**/settings*.json)" in deny
    for rel in _STILL_DENIED_REL:
        assert _denied(deny, repo / rel), rel
    assert not _denied(deny, repo / _EXEMPT_REL)
    for guard in (f"Edit({base}/**/.claude/**)", f"Edit({base}/**/.git/**)", f"Edit({base}/**/.git)"):
        assert guard in deny


def test_guard_exempt_never_lifts_the_claude_or_git_guards(tmp_path):
    repo = _exempt_fixture(tmp_path)
    base = GRANTS.rule_file_arg(str(repo))
    rel = "proj/.claude/settings.json"
    with pytest.raises(MOD.GrantShadowError):
        MOD._check_no_allow_fully_denied([_file_rule(repo / rel)], MOD.write_guard_deny_rules(base, [rel]), {}, {})


def test_guard_exempt_two_paths_do_not_widen_each_other(tmp_path):
    repo = _exempt_fixture(tmp_path)
    (repo / "proj/presets/other/settings.local.json").write_text("{}")
    base = GRANTS.rule_file_arg(str(repo))
    exempts = [_EXEMPT_REL, "proj/presets/other/settings.json"]
    deny = MOD.write_guard_deny_rules(base, exempts)
    for rel in exempts:
        assert not _denied(deny, repo / rel), rel
    for rel in ("proj/presets/deep/settings.local.json", "proj/presets/other/settings.local.json",
                "proj/settings.json", "lib/x/settings.json"):
        assert _denied(deny, repo / rel), rel


def test_guard_exempt_not_yet_created_path_degrades_to_nothing_to_list(tmp_path):
    repo = _exempt_fixture(tmp_path)
    base = GRANTS.rule_file_arg(str(repo))
    rel = "proj/new/deeper/settings.json"
    assert not (repo / "proj/new").exists()
    deny = MOD.write_guard_deny_rules(base, [rel])
    MOD._check_no_allow_fully_denied([_file_rule(repo / rel)], deny, {}, {})
    assert not any("/proj/new/deeper/" in rule for rule in deny)
    for rel_denied in ("proj/settings.json", "proj/presets/deep/settings.json", "proj/new/settings.json"):
        assert _denied(deny, repo / rel_denied), rel_denied


def test_guard_exempt_metachar_sibling_name_is_widened_not_emitted(tmp_path):
    repo = _exempt_fixture(tmp_path)
    (repo / "odd[1]{x}").mkdir()
    base = GRANTS.rule_file_arg(str(repo))
    deny = MOD.write_guard_deny_rules(base, [_EXEMPT_REL])
    assert f"Edit({base}/odd*1*x*/**/settings*.json)" in deny
    assert not any(ch in rule for rule in deny for ch in MOD._UNDECIDABLE_GLOB_CHARS)


def test_guard_exempt_dotdot_segment_is_refused(tmp_path):
    with pytest.raises(GrantValidationError):
        MOD.write_guard_deny_rules(GRANTS.rule_file_arg(str(tmp_path)), ["a/../settings.json"])


def test_build_child_settings_guard_exempt_admits_a_write_grant_onto_that_file(tmp_path, monkeypatch):
    repo = _exempt_fixture(tmp_path)
    target = repo / _EXEMPT_REL
    monkeypatch.setattr(MOD, "_vcs_root", lambda _cwd: str(repo))
    engine_grants = [
        {"path": str(repo / "proj"), "mode": "write", "provenance": "declared"},
        {"rule": _file_rule(target), "provenance": "derived:stage-3"},
    ]
    with pytest.raises(MOD.GrantShadowError):
        MOD.build_child_settings("developer", engine_grants=engine_grants, workdir=str(repo))

    settings = MOD.build_child_settings(
        "developer", engine_grants=engine_grants, workdir=str(repo), guard_exempt_paths=[str(target)]
    )
    assert _file_rule(target) in settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    for rel in _STILL_DENIED_REL:
        assert _denied(deny, repo / rel), rel


def _main_guard_exempt_paths(tmp_path, monkeypatch, *flags: str):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    seen: dict = {}

    def fake_build_child_settings(*_args, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(MOD, "build_child_settings", fake_build_child_settings)
    monkeypatch.setattr(MOD, "log_refused", lambda *_args: None)
    rc = MOD.main([
        "--kind", "developer", "--plan", str(plan), "--done-criterion", "done",
        "--criterion-type", "measurable", "--complexity", "low", "--effort", "low",
        "--workdir", str(tmp_path), "--dry-run", *flags,
    ])
    return rc, seen.get("guard_exempt_paths")


def test_guard_exempt_cli_flag_resolves_relative_paths_against_workdir(tmp_path, monkeypatch, capsys):
    rc, paths = _main_guard_exempt_paths(
        tmp_path, monkeypatch, "--guard-exempt", "a/./settings.json", "--guard-exempt", "/b/settings.json"
    )
    assert rc == 0
    assert paths == [str(tmp_path / "a" / "settings.json"), "/b/settings.json"]
    assert _main_guard_exempt_paths(tmp_path, monkeypatch)[1] == []


def test_guard_exempt_cli_flag_refuses_a_dotdot_segment(tmp_path, monkeypatch, capsys):
    rc, paths = _main_guard_exempt_paths(tmp_path, monkeypatch, "--guard-exempt", "a/../settings.json")
    assert rc == 2
    assert paths is None


# --- (H) the mutation control runs as part of this suite ---------------------


def _git_history_unavailable() -> bool:
    if shutil.which("git") is None:
        return True
    probe = subprocess.run(
        ["git", "-C", str(SCRIPT.parent), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
    )
    return probe.returncode != 0 or probe.stdout.strip() != "false"


pytestmark_git = pytest.mark.skipif(
    _git_history_unavailable(),
    reason="gc_mutation_control.py recovers its pre-fix subjects from a full, non-shallow git history",
)


@pytestmark_git
def test_mutation_control_is_collected_and_discriminates():
    # Wall-clock ~2.9 min: forty-four subjects at ~4.0 s each, measured 2026-09-30.
    control_path = Path(__file__).resolve().parent / "gc_mutation_control.py"
    spec = importlib.util.spec_from_file_location("gc_mutation_control", control_path)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    assert control.main() == 0
