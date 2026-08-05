"""install-reminder-hooks.sh's PRUNE-ONLY scope: beyond $CLAUDE_AGENT_HOME/settings.json,
the script also reconciles the files named in its PRUNE_ONLY_SETTINGS array
(today: $HOME/.claude/settings.json) — in the delete direction, plus exactly the
one named ADD exemption. Every DESIRED entry other than PRUNE_ONLY_ALSO_ADD's
is kept out, and a missing or unparseable file is skipped rather than created or
truncated.

"Prune-only" is thus a claim about ENFORCEMENT, not about bytes: a DETECTOR
denies nothing, and registering it only in the root that is always correctly
wired would leave the one root where the gap is real unable to report it. The
positive half of that split — the detector IS added, no gate-bearing hook ever
is — lives in test_installer_detector_in_personal_root.py; this module holds the
prune-direction and file-safety guarantees the exemption must not erode.

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


def test_an_unexempted_desired_entry_is_never_added_to_dot_claude_settings(tmp_path):
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
    # An advisory reminder is added to CLAUDE_AGENT_HOME only: it is neither the
    # named exemption nor gate-bearing, so it stands for the ordinary DESIRED row.
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


def test_dot_claude_settings_untouched_when_nothing_left_to_reconcile(tmp_path):
    """No gratuitous write. The fixture is already in the state the installer
    wants — nothing dangling to prune AND the one ADD exemption already wired —
    so a second run must not rewrite the file or leave a .bak behind. This is
    also the exemption's idempotence: it adds by basename, once."""
    env = _shell_env(tmp_path)
    real = str(SCRIPTS_DIR / "hook-context-growth-reminder.py")
    detector = str(SCRIPTS_DIR / "hook-canon-guard-wired-check.py")
    assert Path(real).exists() and Path(detector).exists()
    settings = _dot_claude_settings(env)
    original = {"hooks": {
        "UserPromptSubmit": [_group(None, [real])],
        "SessionStart": [_group(None, [detector])],
    }}
    settings.write_text(json.dumps(original), encoding="utf-8")

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    assert not Path(str(settings) + ".bak").exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == original
