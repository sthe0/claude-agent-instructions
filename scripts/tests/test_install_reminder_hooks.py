"""Shared registry drives the Claude installer without changing wiring or hook bodies."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"
REGISTRY = SCRIPTS_DIR / "hooks" / "desired.json"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import hook_registry  # noqa: E402

# Frozen Claude corpus captured from the pre-refactor inline DESIRED table.
FROZEN_CLAUDE_ROWS = [
    ("UserPromptSubmit", None, "hook-context-growth-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-burn-rate-guard.py", 5),
    ("UserPromptSubmit", None, "hook-engine-start.py", 5),
    ("UserPromptSubmit", None, "hook-effort-divergence-watch.py", 10),
    ("UserPromptSubmit", None, "hook-resolution-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-self-improvement-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-tracker-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-tracker-publish-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-ticket-plan-sync.py", 5),
    ("UserPromptSubmit", None, "hook-experience-record-reminder.py", 5),
    ("PreToolUse", "Bash", "hook-push-confirmation-reminder.py", 5),
    ("PreToolUse", "Bash", "hook-readme-currency-reminder.py", 5),
    ("PreToolUse", "Edit|Write", "hook-memory-consistency.py", 5),
    ("PreToolUse", "Edit|Write", "hook-prewrite-plan-check.py", 5),
    ("PreToolUse", "Edit|Write", "hook-state-gate.py", 5),
    ("PreToolUse", "Edit|Write", "hook-term-neutrality.py", 5),
    ("PreToolUse", "AskUserQuestion", "hook-plan-delivery-gate.py", 35),
    ("PreToolUse", "AskUserQuestion", "hook-escalation-diagnosis-gate.py", 35),
    ("PreToolUse", "AskUserQuestion", "hook-deferring-disposition-gate.py", 50),
    ("PreToolUse", "Edit|Write", "hook-scope-conflict.py", 5),
    ("PreToolUse", "Bash", "hook-retry-detector.py", 5),
    ("PreToolUse", "Bash", "hook-long-job-arm.py", 5),
    ("PreToolUse", "Bash", "hook-skill-first.py", 5),
    ("UserPromptSubmit", None, "hook-language-reminder.py", 5),
    ("UserPromptSubmit", None, "hook-instructions-refresh-due.py", 10),
    ("UserPromptSubmit", None, "hook-instruction-grooming-due.py", 5),
    ("PreToolUse", "Bash|Grep|Glob", "hook-multi-mount-search-guard.py", 5),
    ("PreToolUse", "Bash", "hook-guard-destructive-rm.py", 5),
    ("PreToolUse", "Edit|Write", "hook-guard-canon-readonly.py", 5),
    ("PreToolUse", "Bash", "hook-guard-canon-readonly.py", 5),
    ("PostToolUse", "Write", "hook-self-critique-reminder.py", 5),
    ("PostToolUse", "AskUserQuestion", "hook-si-freetext-answer.py", 5),
    ("PostToolUse", "Edit|Write", "hook-scope-track.py", 5),
    ("PostToolUse", "Bash", "hook-scope-track.py", 5),
    ("PostToolUse", "Bash", "hook-review-monitor-arm.py", 5),
    ("SessionStart", None, "hook-policy-scorecard-due.py", 5),
    ("SessionStart", None, "hook-budget-calibration-due.py", 10),
    ("SessionStart", None, "hook-sigma-sentinel-due.py", 5),
    ("SessionStart", None, "hook-self-diagnose-due.py", 5),
    ("SessionStart", None, "hook-canon-guard-wired-check.py", 5),
    ("SessionStart", None, "hook-phase3-due.py", 10),
    ("Stop", None, "hook-turn-end-gate.py", 57),
    ("Stop", None, "hook-run-url-surfaced-reminder.py", 5),
    ("Stop", None, "hook-review-mergeable-guardian.py", 5),
    ("PreToolUse", "Write", "verify-leaf-structure.py --hook", 5),
    ("PreToolUse", "Write", "verify-experience-leaf.py --hook", 5),
    ("PreToolUse", "Write", "verify-no-conflict-markers.py --hook", 5),
]


def _normalize_hooks(hooks: dict, scripts_dir: str) -> dict:
    prefix = str(Path(scripts_dir).resolve()) + os.sep
    normalized = json.loads(json.dumps(hooks))
    for groups in normalized.values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if command.startswith(prefix):
                    hook["command"] = command[len(prefix) :]
    return json.loads(json.dumps(normalized, sort_keys=True))


def _shell_env(tmp_path) -> dict:
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


def test_claude_desired_set_equals_frozen_corpus():
    assert hook_registry.claude_desired_tuples() == FROZEN_CLAUDE_ROWS


def test_cursor_only_rows_do_not_change_claude_tuples(tmp_path):
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    data["hooks"].append(
        {
            "claude_event": None,
            "claude_matcher": None,
            "command": "hook-cursor-memory-context.py",
            "timeout": 5,
            "role": "advisory",
            "cursor_event": "sessionStart",
            "cursor_skip_reason": None,
            "cursor_fail_closed": False,
        }
    )
    clone = tmp_path / "desired.json"
    clone.write_text(json.dumps(data), encoding="utf-8")
    assert hook_registry.claude_desired_tuples(clone) == FROZEN_CLAUDE_ROWS


def test_installer_normalized_wiring_matches_frozen_corpus(tmp_path):
    env = _shell_env(tmp_path)
    proc = subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    settings = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    actual = _normalize_hooks(hooks, str(SCRIPTS_DIR))
    expected_rows = []
    for event, matcher, command, timeout in FROZEN_CLAUDE_ROWS:
        expected_rows.append((event, matcher, command, timeout))
    observed = []
    for event, groups in actual.items():
        for group in groups:
            matcher = group.get("matcher")
            for hook in group.get("hooks", []):
                observed.append(
                    (event, matcher, hook["command"], hook["timeout"])
                )
    assert sorted(observed, key=str) == sorted(expected_rows, key=str)


def test_origin_main_hook_script_bodies_are_unchanged():
    listed = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "origin/main", "scripts"],
        cwd=REPO_ROOT,
        text=True,
    )
    hook_paths = [
        line
        for line in listed.splitlines()
        if line.startswith("scripts/hook-") and line.endswith(".py")
    ]
    assert hook_paths
    proc = subprocess.run(
        ["git", "diff", "--exit-code", "origin/main", "--", *hook_paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
