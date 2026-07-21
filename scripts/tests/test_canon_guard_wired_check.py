"""Tests for hook-canon-guard-wired-check.py — the SessionStart detector that
warns when the canon read-only guard is not wired into BOTH live PreToolUse
chains (Edit|Write + Bash) or is wired to a missing script path.

Hermetic: the hook reads live settings from $CLAUDE_CANON_GUARD_SETTINGS (test
seam); each test writes a crafted settings.json there and asserts on stderr.
Non-blocking and fail-open: the hook always exits 0 and only ever warns.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-canon-guard-wired-check.py"
# A real, existing script path so "wired path exists" cases pass the os.path.exists check.
GUARD_PATH = str(SCRIPTS_DIR / "hook-guard-canon-readonly.py")


def _group(matcher: str, command: str) -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _run(tmp_path: Path, settings: dict) -> subprocess.CompletedProcess:
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps(settings), encoding="utf-8")
    env = {**os.environ, "CLAUDE_CANON_GUARD_SETTINGS": str(sp)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=env, capture_output=True, text=True,
    )


def _wired_both(edit_cmd: str, bash_cmd: str) -> dict:
    return {"hooks": {"PreToolUse": [
        _group("Edit|Write", edit_cmd),
        _group("Bash", bash_cmd),
    ]}}


def test_silent_when_wired_in_both_chains(tmp_path):
    proc = _run(tmp_path, _wired_both(GUARD_PATH, GUARD_PATH))
    assert proc.returncode == 0
    assert proc.stderr.strip() == "", proc.stderr


def test_warns_when_absent_from_edit_chain(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Bash", GUARD_PATH),  # only Bash wired
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Edit|Write chain" in proc.stderr


def test_warns_when_absent_from_bash_chain(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH),  # only Edit|Write wired
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Bash chain" in proc.stderr


def test_warns_when_absent_from_both_chains(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Edit|Write", "/some/other/hook.py"),
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Edit|Write chain" in proc.stderr
    assert "Bash chain" in proc.stderr


def test_warns_when_wired_path_missing(tmp_path):
    missing = "/nonexistent/dir/hook-guard-canon-readonly.py"
    proc = _run(tmp_path, _wired_both(missing, missing))
    assert proc.returncode == 0
    assert "missing script path" in proc.stderr


def test_wired_command_with_args_path_exists_is_silent(tmp_path):
    """The script path is the first token; trailing args must not break the
    exists() check."""
    cmd = f"{GUARD_PATH} --some-arg"
    proc = _run(tmp_path, _wired_both(cmd, cmd))
    assert proc.returncode == 0
    assert proc.stderr.strip() == "", proc.stderr


def test_missing_settings_file_fails_open(tmp_path):
    env = {**os.environ, "CLAUDE_CANON_GUARD_SETTINGS": str(tmp_path / "nope.json")}
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr.strip() == ""


def test_malformed_settings_fails_open(tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text("not json", encoding="utf-8")
    env = {**os.environ, "CLAUDE_CANON_GUARD_SETTINGS": str(sp)}
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr.strip() == ""
