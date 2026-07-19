"""hook-prewrite-plan-check.py: plan_files_exist detects a TOML-only plans dir.

Regression: the hook globbed only `*.md`, so on a substantive session — whose plan
is the TOML the engine tracks — it never saw the plan and kept nudging. It must now
detect either extension.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "hook-prewrite-plan-check.py"


def _load(plan_dir: Path, legacy_dir: Path):
    spec = importlib.util.spec_from_file_location("prewrite_plan_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PLAN_DIR = plan_dir
    mod.LEGACY_PLAN_DIR = legacy_dir
    return mod


def test_toml_only_plans_dir_is_detected(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    (plan_dir / "some-task.toml").write_text("[meta]\ntask_id='x'\n")
    mod = _load(plan_dir, tmp_path / "legacy")
    assert mod.plan_files_exist() is True


def test_detects_markdown_plan(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    (plan_dir / "some-task.md").write_text("# plan")
    mod = _load(plan_dir, tmp_path / "legacy")
    assert mod.plan_files_exist() is True


def test_empty_dir_has_no_plan(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    mod = _load(plan_dir, tmp_path / "legacy")
    assert mod.plan_files_exist() is False


def test_nudge_message_names_toml(tmp_path):
    # The user-facing nudge must describe the TOML deliverable, not a `.md` plan.
    text = SCRIPT.read_text()
    assert "<slug>.toml" in text
    assert "<slug>.md shown to the user" not in text
