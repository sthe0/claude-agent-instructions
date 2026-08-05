"""Fix #2 (pytest exec-permission + prompt hygiene): a spawned developer must be
able to run pytest without an approval prompt, and must be told to invoke
verification as bare single commands so the allow entry actually fires (a
compound/`$VAR`/`-c` form re-trips the approval classifier even with the allow
entry present).

The exec permission is scoped to developer spawns via spawn-specialist.py's
`--settings` injection, NOT settings/base.json (which is merged fleet-wide on
`git pull` without a prompt and must stay read-only-only per
lint-settings-base.py — see memory-global leaf settings-permission-tiers.md).

Guards three surfaces against silent regression:
  1. build_child_settings injects the pytest allow for kind=="developer" only.
  2. settings/base.json carries NO pytest entry (stays read-only-only) while
     its read-only sibling `python3 -m json.tool` entry is untouched.
  3. assemble_prompt's developer branch instructs bare-command verification
     hygiene (no &&/;/pipes, no $VAR, no `python3 -c`).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
REPO_ROOT = SCRIPT.parent.parent
BASE_SETTINGS = REPO_ROOT / "settings" / "base.json"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_pytest_hygiene", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _args(tmp_path, **overrides):
    plan = tmp_path / "plan.toml"
    plan.write_text('[meta]\ntask_id = "t"\n', encoding="utf-8")
    base = dict(
        plan=plan,
        constraints="",
        context_dossier=None,
        done_criterion="do the thing",
        criterion_type="measurable",
        continue_worktree=None,
        kind="developer",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_developer_child_settings_carry_pytest_allow():
    settings = MOD.build_child_settings("developer")
    assert "Bash(python3 -m pytest:*)" in settings["permissions"]["allow"]


def test_developer_child_settings_grant_every_mandated_verb():
    """The grant must cover what a developer brief REQUIRES, not just pytest.

    `acceptEdits` auto-grants file writes and nothing else — it does not carry
    `defaultMode: auto`'s side-effect-free classifier — so a brief saying "run the
    verifiers, commit when green" against a pytest-only grant leaves a stage's work
    green but uncommitted, which is what happened on 2026-08-05.
    """
    allow = MOD.build_child_settings("developer")["permissions"]["allow"]
    for entry in ("Bash(python3 scripts/verify-all.py:*)", "Bash(git add:*)",
                  "Bash(git commit:*)", "Bash(git status:*)", "Bash(grep:*)"):
        assert entry in allow, entry


def test_developer_child_settings_never_grant_push():
    """Landing is the coordinator's gate; a spawn records, it does not publish."""
    allow = MOD.build_child_settings("developer")["permissions"]["allow"]
    assert not any(e.startswith("Bash(git push") for e in allow)


def test_non_developer_child_settings_omit_permissions_key():
    settings = MOD.build_child_settings("thinker")
    assert "permissions" not in settings


def test_developer_child_settings_still_carry_autocompact_env():
    settings = MOD.build_child_settings("developer")
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in settings["env"]


def test_base_settings_has_no_pytest_entry():
    data = json.loads(BASE_SETTINGS.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    assert "Bash(python3 -m pytest:*)" not in allow


def test_base_settings_sibling_json_tool_entry_untouched():
    data = json.loads(BASE_SETTINGS.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    assert "Bash(python3 -m json.tool:*)" in allow


def test_developer_prompt_carries_bare_command_hygiene(tmp_path):
    args = _args(tmp_path, kind="developer")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "## Verification command hygiene" in prompt
    assert "BARE" in prompt
    assert "&&" in prompt
    assert "python3 -c" in prompt


def test_non_developer_prompt_omits_hygiene_section(tmp_path):
    args = _args(tmp_path, kind="thinker")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "Verification command hygiene" not in prompt


def test_prompt_omits_hygiene_section_when_kind_attribute_missing_entirely(tmp_path):
    """A hand-built Namespace lacking `kind` must not raise — assemble_prompt
    uses getattr, not args.kind directly (mirrors the continue_worktree
    back-compat guard in test_spawn_prompt_continuity.py)."""
    args = _args(tmp_path)
    del args.kind
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "Verification command hygiene" not in prompt
