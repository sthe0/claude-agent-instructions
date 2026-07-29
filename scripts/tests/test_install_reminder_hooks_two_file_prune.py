"""install-reminder-hooks.sh's PRUNE-ONLY scope: beyond $CLAUDE_AGENT_HOME/settings.json,
the script also reconciles the files named in its PRUNE_ONLY_SETTINGS array
(today: $HOME/.claude/settings.json) — but only in the delete direction. A
DESIRED entry is never added there, and a missing or unparseable file is
skipped rather than created or truncated.

Each test drives the real script via subprocess against fixture settings
files, mirroring test_install_reminder_hooks_prune.py's `_shell_env` pattern,
except HOME here is a real directory the script itself resolves
PRUNE_ONLY_SETTINGS against (bash `$HOME/.claude/settings.json`), so tests
seed `<HOME>/.claude/settings.json` directly rather than via `CLAUDE_AGENT_HOME`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"


def _shell_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_AGENT_HOME": str(agent_home),
        "AGENTCTL_EDIT_LEDGER": str(tmp_path / "edit-log.jsonl"),
        "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
    }


def _run(env):
    return subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30,
    )


def _group(matcher, commands):
    g = {} if matcher is None else {"matcher": matcher}
    g["hooks"] = [{"type": "command", "command": c, "timeout": 5} for c in commands]
    return g


def _dot_claude_settings(env):
    d = Path(env["HOME"]) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def _all_commands(data, event):
    return [
        h["command"]
        for grp in data.get("hooks", {}).get(event, [])
        for h in grp["hooks"]
    ]


def test_owned_dangling_entry_in_dot_claude_settings_is_deleted(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    settings = _dot_claude_settings(env)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]}}),
        encoding="utf-8",
    )

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert dangling not in _all_commands(data, "PreToolUse")


def test_unowned_dangling_entry_in_dot_claude_settings_is_kept(tmp_path):
    env = _shell_env(tmp_path)
    outside = str(tmp_path / "elsewhere" / "hook-outside.py")
    settings = _dot_claude_settings(env)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("ZZZ-fake-matcher", [outside])]}}),
        encoding="utf-8",
    )

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert outside in _all_commands(data, "PreToolUse")


def test_no_desired_entry_is_ever_added_to_dot_claude_settings(tmp_path):
    env = _shell_env(tmp_path)
    settings = _dot_claude_settings(env)
    original = {"hooks": {"PreToolUse": [_group("ZZZ-fake-matcher", [])]}}
    settings.write_text(json.dumps(original), encoding="utf-8")

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    wired_basenames = {
        os.path.basename(h["command"].split()[0])
        for grp_list in data.get("hooks", {}).values()
        for grp in grp_list
        for h in grp["hooks"]
    }
    # None of the canonical reminder hooks (added to CLAUDE_AGENT_HOME only)
    # should ever land in the prune-only file.
    assert not any(name.startswith("hook-") and "context-growth" in name for name in wired_basenames)
    assert "hook-context-growth-reminder.py" not in wired_basenames


def test_interpreter_prefixed_dangling_entry_in_dot_claude_settings_is_deleted(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    settings = _dot_claude_settings(env)
    settings.write_text(
        json.dumps({"hooks": {"PostToolUse": [_group("AskUserQuestion", [f"python3 {dangling}"])]}}),
        encoding="utf-8",
    )

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _all_commands(data, "PostToolUse") == []


def test_backup_is_written_before_mutating_dot_claude_settings(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    settings = _dot_claude_settings(env)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]}}),
        encoding="utf-8",
    )

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    backup = Path(str(settings) + ".bak")
    assert backup.is_file()
    backup_data = json.loads(backup.read_text(encoding="utf-8"))
    assert dangling in _all_commands(backup_data, "PreToolUse")


def test_group_emptied_by_pruning_in_dot_claude_settings_is_removed(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    settings = _dot_claude_settings(env)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]}}),
        encoding="utf-8",
    )

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    matchers = [grp.get("matcher") for grp in data.get("hooks", {}).get("PreToolUse", [])]
    assert "ZZZ-fake-matcher" not in matchers


def test_missing_dot_claude_settings_is_skipped_not_created(tmp_path):
    env = _shell_env(tmp_path)
    settings = _dot_claude_settings(env)
    assert not settings.exists()
    # Remove the directory entirely too, so nothing pre-creates the parent.
    settings.parent.rmdir()

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert not settings.exists()
    assert not settings.parent.exists()


def test_unparseable_dot_claude_settings_is_skipped_not_truncated(tmp_path):
    env = _shell_env(tmp_path)
    settings = _dot_claude_settings(env)
    settings.write_text("{ not json", encoding="utf-8")

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert settings.read_text(encoding="utf-8") == "{ not json"


def test_dot_claude_settings_untouched_when_nothing_to_prune(tmp_path):
    env = _shell_env(tmp_path)
    real = str(SCRIPTS_DIR / "hook-context-growth-reminder.py")
    assert Path(real).exists()
    settings = _dot_claude_settings(env)
    original = {"hooks": {"UserPromptSubmit": [_group(None, [real])]}}
    settings.write_text(json.dumps(original), encoding="utf-8")

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    assert not Path(str(settings) + ".bak").exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == original
