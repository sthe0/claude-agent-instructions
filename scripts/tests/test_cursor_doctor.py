"""Hermetic tests for cursor-doctor.sh hook installation check."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
DOCTOR_SCRIPT = REPO_ROOT / "cursor" / "scripts" / "cursor-doctor.sh"


def _load_install_module():
    spec = importlib.util.spec_from_file_location(
        "install_cursor_hooks", SCRIPTS_DIR / "install-cursor-hooks.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_doctor(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_doctor_hook_check_passes_with_installed_hooks(tmp_path, monkeypatch):
    hooks_path = tmp_path / "hooks.json"
    install_mod = _load_install_module()
    install_mod.install(hooks_path, dry_run=False)

    env = {
        **{key: value for key, value in __import__("os").environ.items()},
        "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
        "CURSOR_HOOKS_JSON": str(hooks_path),
        "HOME": str(tmp_path),
    }
    result = _run_doctor(env)
    assert "Cursor hooks.json matches registry-derived managed entries" in result.stdout
    assert "missing managed entries" not in result.stdout


def test_doctor_hook_check_fails_when_guardian_missing(tmp_path, monkeypatch):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps({"version": 1, "hooks": {"stop": [], "preToolUse": []}}),
        encoding="utf-8",
    )

    env = {
        **{key: value for key, value in __import__("os").environ.items()},
        "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
        "CURSOR_HOOKS_JSON": str(hooks_path),
        "HOME": str(tmp_path),
    }
    result = _run_doctor(env)
    assert result.returncode != 0
    assert "missing managed entries" in result.stdout


def test_doctor_hook_check_fails_when_memory_context_missing(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_mod = _load_install_module()
    install_mod.install(hooks_path, dry_run=False)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    session_hooks = data.get("hooks", {}).get("sessionStart", [])
    data["hooks"]["sessionStart"] = [
        hook
        for hook in session_hooks
        if "hook-cursor-memory-context.py" not in str(hook.get("command") or "")
    ]
    hooks_path.write_text(json.dumps(data), encoding="utf-8")

    env = {
        **{key: value for key, value in __import__("os").environ.items()},
        "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
        "CURSOR_HOOKS_JSON": str(hooks_path),
        "HOME": str(tmp_path),
    }
    result = _run_doctor(env)
    assert result.returncode != 0
    assert "missing managed entries" in result.stdout
