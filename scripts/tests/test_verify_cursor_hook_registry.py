"""Positive and negative tests for verify-cursor-hook-registry.py."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_registry  # noqa: E402

REGISTRY_PATH = SCRIPTS_DIR / "hooks" / "desired.json"
VERIFY_SCRIPT = SCRIPTS_DIR / "verify-cursor-hook-registry.py"


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_cursor_hook_registry", VERIFY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_live_registry_passes():
    mod = _load_verify_module()
    assert mod.main([]) == 0


def test_validate_registry_detects_missing_metadata_row(tmp_path):
    data = _load_registry()
    data["hooks"] = [row for row in data["hooks"] if row.get("id") != hook_registry.METADATA_ROW_ID]
    registry_path = tmp_path / "desired.json"
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    problems = hook_registry.validate_registry(data, path=registry_path)
    assert any("native-auto-memory-extraction" in problem for problem in problems)


def test_validate_registry_detects_both_cursor_sides_set(tmp_path):
    data = _load_registry()
    broken = copy.deepcopy(data)
    for row in broken["hooks"]:
        if row.get("command") == "hook-state-gate.py":
            row["cursor_skip_reason"] = "should not coexist with cursor_event"
            break
    problems = hook_registry.validate_registry(broken)
    assert any("hook-state-gate.py" in problem and "cursor_event" in problem for problem in problems)


def test_validate_registry_detects_skipped_write_path_hook():
    data = _load_registry()
    broken = copy.deepcopy(data)
    for row in broken["hooks"]:
        if row.get("command") == "hook-state-gate.py":
            row.pop("cursor_event", None)
            row["cursor_skip_reason"] = "accidental skip"
            break
    guardian_problems = hook_registry.check_cursor_guardians(data=broken)
    assert any("hook-state-gate.py" in problem for problem in guardian_problems)


def test_required_cursor_mapped_hooks_must_have_cursor_event():
    data = _load_registry()
    broken = copy.deepcopy(data)
    for row in broken["hooks"]:
        if row.get("command") == "hook-skill-first.py":
            row.pop("cursor_event", None)
            row["cursor_skip_reason"] = "native Skill tool only"
            break
    problems = hook_registry.validate_registry(broken)
    assert any("REQUIRED_CURSOR_MAPPED" in problem for problem in problems)


def test_gate_row_requires_explicit_cursor_fail_closed():
    data = _load_registry()
    broken = copy.deepcopy(data)
    for row in broken["hooks"]:
        if row.get("command") == "hook-state-gate.py":
            row.pop("cursor_fail_closed", None)
            break
    problems = hook_registry.validate_registry(broken)
    assert any("cursor_fail_closed" in problem for problem in problems)


def test_verify_script_fails_on_broken_registry(tmp_path):
    data = _load_registry()
    broken = copy.deepcopy(data)
    for row in broken["hooks"]:
        if row.get("command") == "hook-turn-end-gate.py":
            row.pop("cursor_event", None)
            row["cursor_skip_reason"] = "removed mapping"
            break
    registry_path = tmp_path / "desired.json"
    registry_path.write_text(json.dumps(broken), encoding="utf-8")
    mod = _load_verify_module()
    assert mod.main(["--registry", str(registry_path)]) == 1


def test_cursor_install_expectations_groups_timeouts():
    rows = hook_registry.cursor_install_expectations()
    stop_rows = [row for row in rows if row["event"] == "stop" and row.get("matcher") is None]
    assert len(stop_rows) == 1
    assert stop_rows[0]["timeout"] >= 57


def test_check_cursor_installation_detects_missing_managed_hook(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({"version": 1, "hooks": {}}), encoding="utf-8")
    problems = hook_registry.check_cursor_installation(hooks_path)
    assert problems
    assert any("missing managed entry" in problem for problem in problems)


def test_check_cursor_installation_passes_after_install(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_cursor_hooks", SCRIPTS_DIR / "install-cursor-hooks.py"
    )
    install_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install_mod)

    hooks_path = tmp_path / "hooks.json"
    install_mod.install(hooks_path, dry_run=False)
    problems = hook_registry.check_cursor_installation(hooks_path)
    assert problems == []
