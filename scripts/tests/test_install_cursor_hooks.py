"""Hermetic tests for install-cursor-hooks.py merge behavior."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_registry  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "install_cursor_hooks", SCRIPTS_DIR / "install-cursor-hooks.py"
)
install_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_mod)
install = install_mod.install
merge_hooks = install_mod.merge_hooks

MANAGED_COMMAND = str((SCRIPTS_DIR / "hook-cursor-adapt.py").resolve())
MEMORY_COMMAND = str((SCRIPTS_DIR / "hook-cursor-memory-context.py").resolve())


def _arcadia_fixture() -> dict:
    return {
        "version": 1,
        "hooks": {
            "beforeShellExecution": [
                {"command": "./hooks/block-arcadia-wide-search.sh"},
                {"command": "./hooks/block-arcadia-wide-ya-style.sh"},
            ]
        },
    }


def test_merge_preserves_arcadia_guards(tmp_path):
    existing = _arcadia_fixture()
    desired_rows = [
        {
            "event": "preToolUse",
            "matcher": "Shell",
            "timeout": 5,
            "failClosed": True,
        }
    ]
    merged = merge_hooks(existing, desired_rows, MANAGED_COMMAND)
    arcadia = merged["hooks"]["beforeShellExecution"]
    assert len(arcadia) == 2
    assert arcadia[0]["command"] == "./hooks/block-arcadia-wide-search.sh"
    assert arcadia[1]["command"] == "./hooks/block-arcadia-wide-ya-style.sh"
    pre_tool = merged["hooks"]["preToolUse"]
    assert any(MANAGED_COMMAND in hook["command"] for hook in pre_tool)


def test_merge_is_idempotent(tmp_path):
    existing = _arcadia_fixture()
    desired_rows = [
        {
            "event": "stop",
            "matcher": None,
            "timeout": 57,
            "failClosed": False,
        },
        {
            "event": "preToolUse",
            "matcher": "Write|StrReplace",
            "timeout": 5,
            "failClosed": True,
        },
    ]
    first = merge_hooks(existing, desired_rows, MANAGED_COMMAND)
    second = merge_hooks(first, desired_rows, MANAGED_COMMAND)
    assert second == first


def test_install_writes_flat_managed_hooks(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps(_arcadia_fixture()), encoding="utf-8")
    hook_registry.REGISTRY_PATH = SCRIPTS_DIR / "hooks" / "desired.json"
    first = install(hooks_path, dry_run=True)
    second = install(hooks_path, dry_run=True)
    assert first == second
    install(hooks_path, dry_run=False)
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert len(data["hooks"]["beforeShellExecution"]) == 2
    managed = [
        hook
        for hook in data["hooks"].get("preToolUse", [])
        if MANAGED_COMMAND in hook.get("command", "")
    ]
    assert managed
    assert managed[0]["type"] == "command"
    assert "hooks" not in managed[0]
    session_start = data["hooks"].get("sessionStart", [])
    memory_hooks = [
        hook for hook in session_start if MEMORY_COMMAND in hook.get("command", "")
    ]
    assert memory_hooks
