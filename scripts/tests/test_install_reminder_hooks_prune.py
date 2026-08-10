"""install-reminder-hooks.sh's reconcile pass: prune a hook registration
whose script lives under the repo's managed scripts dir and no longer exists
on disk, while leaving DESIRED entries and non-managed (hand-wired) entries
alone — even a dangling non-managed one.

Each test drives the real script via subprocess against a fixture settings
file (never the real ~/.claude-agent/settings.json), following the pattern
in test_canon_writer_stamping.py's `_shell_env`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
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


def _seed(env, hooks):
    settings = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"
    settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return settings


def _run(env):
    return subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30,
    )


def _group(matcher, commands):
    g = {} if matcher is None else {"matcher": matcher}
    g["hooks"] = [{"type": "command", "command": c, "timeout": 5} for c in commands]
    return g


def test_dangling_managed_hook_is_pruned(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    assert not Path(dangling).exists()
    settings = _seed(env, {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert "pruned" in proc.stdout

    data = json.loads(settings.read_text(encoding="utf-8"))
    all_commands = [
        h["command"]
        for grp in data["hooks"].get("PreToolUse", [])
        for h in grp["hooks"]
    ]
    assert dangling not in all_commands


def test_existing_managed_hook_is_kept(tmp_path):
    env = _shell_env(tmp_path)
    real = str(SCRIPTS_DIR / "hook-context-growth-reminder.py")
    assert Path(real).exists()
    settings = _seed(env, {"UserPromptSubmit": [_group(None, [real])]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    all_commands = [
        h["command"]
        for grp in data["hooks"].get("UserPromptSubmit", [])
        for h in grp["hooks"]
    ]
    assert all_commands.count(real) == 1


def test_non_managed_dangling_hook_is_kept(tmp_path):
    env = _shell_env(tmp_path)
    outside = str(tmp_path / "elsewhere" / "hook-outside.py")
    assert not Path(outside).exists()
    settings = _seed(env, {"PreToolUse": [_group("ZZZ-fake-matcher", [outside])]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    all_commands = [
        h["command"]
        for grp in data["hooks"].get("PreToolUse", [])
        for h in grp["hooks"]
    ]
    assert outside in all_commands


def test_group_emptied_by_pruning_is_dropped(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    settings = _seed(env, {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    matchers = [grp.get("matcher") for grp in data["hooks"].get("PreToolUse", [])]
    assert "ZZZ-fake-matcher" not in matchers


def test_run_is_idempotent(tmp_path):
    env = _shell_env(tmp_path)
    dangling = str(SCRIPTS_DIR / "hook-totally-removed-fake.py")
    outside = str(tmp_path / "elsewhere" / "hook-outside.py")
    settings = _seed(env, {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling, outside])]})

    assert _run(env).returncode == 0
    after_first = json.loads(settings.read_text(encoding="utf-8"))
    assert _run(env).returncode == 0
    after_second = json.loads(settings.read_text(encoding="utf-8"))
    assert after_first == after_second


def test_prune_survives_repo_diverging_from_the_installers_own_tree(tmp_path):
    # The two dirs the installer juggles must stay distinct: the managed dir it
    # prunes under follows $CLAUDE_INSTRUCTIONS_REPO, while self-diagnose.py is
    # loaded from the installer's OWN tree. Point the former at a repo that has
    # no self-diagnose.py at all — loading it from there would raise.
    env = _shell_env(tmp_path)
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        SCRIPTS_DIR / "lib" / "config-root.sh",
        fake_repo / "scripts" / "lib" / "config-root.sh",
    )
    assert not (fake_repo / "scripts" / "self-diagnose.py").exists()
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(fake_repo)

    dangling = str(fake_repo / "scripts" / "hook-totally-removed-fake.py")
    settings = _seed(env, {"PreToolUse": [_group("ZZZ-fake-matcher", [dangling])]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    all_commands = [
        h["command"]
        for grp in data["hooks"].get("PreToolUse", [])
        for h in grp["hooks"]
    ]
    assert dangling not in all_commands


def test_review_monitor_arm_is_wired_once_under_posttooluse_bash(tmp_path):
    """The generic tests above cover "every DESIRED entry gets wired" and "a
    re-run changes nothing"; this pins the EVENT and MATCHER, which decide
    whether the auto-arm hook ever sees a Bash call's output at all."""
    env = _shell_env(tmp_path)
    settings = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"

    assert _run(env).returncode == 0
    second = _run(env)
    assert second.returncode == 0
    assert "already wired" in second.stdout

    data = json.loads(settings.read_text(encoding="utf-8"))
    bash_groups = [g for g in data["hooks"]["PostToolUse"] if g.get("matcher") == "Bash"]
    commands = [h["command"] for g in bash_groups for h in g["hooks"]]
    armed = [c for c in commands if os.path.basename(c) == "hook-review-monitor-arm.py"]
    assert len(armed) == 1, commands


def test_no_desired_entry_is_ever_removed(tmp_path):
    env = _shell_env(tmp_path)
    settings = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"

    installer_text = INSTALLER.read_text(encoding="utf-8")
    desired_block = re.search(r"DESIRED = \[(.*?)\n\]", installer_text, re.S).group(1)
    # Strip comments before scanning for quoted script names: a stray quote
    # pair inside a comment (e.g. a "leave as is" aside) would otherwise shift
    # the parser's notion of which quote opens/closes a string, and a later
    # ".py" mention anywhere past that point would parse as a bogus DESIRED
    # entry — this bit a past comment that quoted "leave as is" verbatim.
    desired_block = re.sub(r"#[^\n]*", "", desired_block)
    desired_basenames = {
        tok.split()[0] for tok in re.findall(r'"([^"]*\.py[^"]*)"', desired_block)
    }
    assert desired_basenames  # sanity: the DESIRED table was actually found

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    wired_basenames = {
        os.path.basename(h["command"].split()[0])
        for grp_list in data["hooks"].values()
        for grp in grp_list
        for h in grp["hooks"]
    }
    assert desired_basenames <= wired_basenames
