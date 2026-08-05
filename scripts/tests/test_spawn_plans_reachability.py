"""Stage 2 (config-root-spawn-defects): the coordination plans directory
(lib.config_root.plans_dir()) is reachable by the spawns whose contract
names it — a planner must WRITE its deliverable there, a thinker/
code-reviewer must READ it to review, and neither had any grant before this
change (an interactive approval a headless `-p` child cannot answer, which
manifests as an empty output file — instance 17 of the venue-worktree leaf).

These are the machine-checkable half of the criterion: the settings payload
and argv are assembled correctly. They cannot prove the CLI *honours* a
`permissions.allow`/`permissions.deny` delivered through --settings in a
headless child under the inherited acceptEdits mode — that is the live probe
run separately (plan stage 2, method step 5) and is not repeatable in a
hermetic test.

Rule-spelling details (absolute-path / Write vs Edit / deny-bearing form)
are documented in spawn-specialist.py's comment block before PLANS_WRITE_KINDS.
Assertions below are written against the corrected form; the earlier form
passed unit tests while granting nothing (write) and denying nothing (read),
which is the regression-guard this test exercises.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_plans_reachability", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_planner_gets_edit_rule_for_plans_dir_double_slash_spelling(tmp_path):
    allow, deny = MOD.plans_permission_rules("planner", tmp_path)
    assert f"Edit(/{tmp_path}/**)" in allow
    assert deny == []


def test_planner_gets_no_write_rule(tmp_path):
    """Write(...) rules are not matched by file permission checks at all
    (CLI's own documented behavior) — the write grant is Edit-only."""
    allow, _deny = MOD.plans_permission_rules("planner", tmp_path)
    assert not any(r.startswith("Write(") for r in allow)


def test_thinker_gets_read_rule_and_digest_bash_for_plans_dir(tmp_path):
    allow, _deny = MOD.plans_permission_rules("thinker", tmp_path)
    assert f"Read(/{tmp_path}/**)" in allow
    assert "Bash(shasum -a 256:*)" in allow


def test_code_reviewer_gets_read_rule_and_digest_bash_for_plans_dir(tmp_path):
    allow, _deny = MOD.plans_permission_rules("code-reviewer", tmp_path)
    assert f"Read(/{tmp_path}/**)" in allow
    assert "Bash(shasum -a 256:*)" in allow


def test_thinker_gets_no_write_or_edit_allow_rule(tmp_path):
    allow, _deny = MOD.plans_permission_rules("thinker", tmp_path)
    assert not any(r.startswith("Write(") or r.startswith("Edit(") for r in allow)


def test_thinker_gets_explicit_edit_deny_rule(tmp_path):
    """Load-bearing: a non-developer spawn gets no --permission-mode flag and
    so inherits acceptEdits, under which --add-dir alone already makes the
    directory writable. Without this deny, "read but not write" is not
    actually enforced (probes A/B: acceptEdits + --add-dir wrote regardless
    of the allow list; probe C: the deny, spelled with //, blocked it)."""
    _allow, deny = MOD.plans_permission_rules("thinker", tmp_path)
    assert deny == [f"Edit(/{tmp_path}/**)"]


def test_code_reviewer_gets_explicit_edit_deny_rule(tmp_path):
    _allow, deny = MOD.plans_permission_rules("code-reviewer", tmp_path)
    assert deny == [f"Edit(/{tmp_path}/**)"]


def test_planner_gets_no_read_rule_or_digest_bash(tmp_path):
    """The write grant does not imply the read grant — the digest Bash
    prefix is reviewer-only."""
    allow, _deny = MOD.plans_permission_rules("planner", tmp_path)
    assert not any(r.startswith("Read(") for r in allow)
    assert "Bash(shasum -a 256:*)" not in allow


def test_relative_path_raises_valueerror():
    """Relative paths produce inert rules (leading / interpreted as project-relative).
    Fail loudly rather than silently grant nothing."""
    with pytest.raises(ValueError, match="must be absolute"):
        MOD.plans_permission_rules("planner", Path("relative/path"))


def test_ungranted_kind_gets_no_plans_rules(tmp_path):
    assert MOD.plans_permission_rules("yandex-cloud-expert", tmp_path) == ([], [])
    assert MOD.plans_permission_rules("developer", tmp_path) == ([], [])
    assert MOD.plans_permission_rules("tech-writer", tmp_path) == ([], [])


def test_add_dir_present_for_granted_kinds(tmp_path):
    assert MOD.plans_add_dir_args("planner", tmp_path) == ["--add-dir", str(tmp_path)]
    assert MOD.plans_add_dir_args("thinker", tmp_path) == ["--add-dir", str(tmp_path)]
    assert MOD.plans_add_dir_args("code-reviewer", tmp_path) == ["--add-dir", str(tmp_path)]


def test_add_dir_absent_for_ungranted_kinds(tmp_path):
    assert MOD.plans_add_dir_args("developer", tmp_path) == []
    assert MOD.plans_add_dir_args("tech-writer", tmp_path) == []


def test_build_child_settings_merges_planner_plans_rules(tmp_path):
    settings = MOD.build_child_settings("planner", tmp_path)
    allow = settings["permissions"]["allow"]
    assert f"Edit(/{tmp_path}/**)" in allow
    assert "deny" not in settings["permissions"]


def test_build_child_settings_thinker_carries_allow_and_deny(tmp_path):
    settings = MOD.build_child_settings("thinker", tmp_path)
    allow = settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    assert f"Read(/{tmp_path}/**)" in allow
    assert "Bash(shasum -a 256:*)" in allow
    assert deny == [f"Edit(/{tmp_path}/**)"]


def test_build_child_settings_developer_pytest_allow_survives_alongside_plans_grant(tmp_path):
    """Regression guard named in the stage's invariants: adding the plans
    grant must not eat the developer's existing allow. Developer is not itself
    a plans-granted kind, so passing a plans_directory must be a no-op for it,
    and the allow must be exactly what DEVELOPER_SETTINGS_ALLOW declares.

    Bound to the constant, not to a snapshot of its contents: the list is owned
    by the developer-brief grant and grows independently of this stage, so a
    literal copy here would go red on every unrelated verb added to it. The
    non-empty assertion keeps the equality from passing vacuously."""
    settings = MOD.build_child_settings("developer", tmp_path)
    allow = settings["permissions"]["allow"]
    assert MOD.DEVELOPER_SETTINGS_ALLOW
    assert allow == list(MOD.DEVELOPER_SETTINGS_ALLOW)
    assert "Bash(python3 -m pytest:*)" in allow
    assert "deny" not in settings["permissions"]


def test_build_child_settings_no_plans_directory_arg_still_works(tmp_path):
    """Back-compat: existing callers that pass only `kind` (no plans_directory)
    must behave exactly as before this change."""
    settings = MOD.build_child_settings("planner")
    assert "permissions" not in settings


def test_build_child_settings_still_carries_both_autocompact_keys_for_every_granted_kind(tmp_path):
    for kind in ("planner", "thinker", "code-reviewer", "developer"):
        settings = MOD.build_child_settings(kind, tmp_path)
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in settings["env"]
        assert "autoCompactWindow" in settings


def test_ungranted_kind_settings_omit_permissions_key(tmp_path):
    settings = MOD.build_child_settings("tech-writer", tmp_path)
    assert "permissions" not in settings
