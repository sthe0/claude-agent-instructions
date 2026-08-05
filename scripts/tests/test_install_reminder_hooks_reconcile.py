"""install-reminder-hooks.sh's RECONCILE direction: an already-registered hook's
`timeout` is brought to the DESIRED value.

Difficulty removed: the script was insert-only. It skipped any row whose script
basename was already in the target group, so a corrected DESIRED timeout could
never reach a machine that already had the hook — the fix lived in the repo and
the live registration kept its old number forever. That is exactly how three
judge-calling hooks stayed pinned at 5s while calling a judge whose fastest
measured run was 10.5s: the harness killed them mid-judge on every call, and a
killed hook and a hook whose judge said NO look identical from outside.

The scope is deliberately narrow, and both boundaries are pinned here:
  - only `timeout` is reconciled, never `command` — an entry with the same
    basename under a FOREIGN directory is a machine-local choice about what
    runs, and silently retargeting it is worse than leaving it slow (the wiring
    probe reports the divergence instead);
  - reconciliation runs over the root the installer owns. The prune-only root's
    immunity is pinned in test_install_reminder_hooks_two_file_prune.py.

Each test drives the real script via subprocess against fixture settings files,
mirroring the `_shell_env` pattern of its sibling prune modules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import hook_wiring  # noqa: E402

# A DESIRED row whose timeout is deliberately generous: the escalation-diagnosis
# gate calls one judge under a 20s whole-invocation budget, so it is registered
# at 25s (that budget plus interpreter-start headroom).
SLOW_HOOK = "hook-escalation-diagnosis-gate.py"
SLOW_EVENT = "PreToolUse"
SLOW_MATCHER = "AskUserQuestion"


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


def _run(env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30,
    )


def _agent_settings(env) -> Path:
    return Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"


def _write(env, hooks: dict) -> Path:
    path = _agent_settings(env)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return path


def _group(matcher, commands, timeout) -> dict:
    g = {} if matcher is None else {"matcher": matcher}
    g["hooks"] = [
        {"type": "command", "command": c, "timeout": timeout} for c in commands
    ]
    return g


def _entries(path: Path, event: str, basename: str) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        h
        for grp in data.get("hooks", {}).get(event, [])
        for h in grp.get("hooks", [])
        if h.get("command", "").endswith(basename)
    ]


def _required_timeout(basename: str) -> int:
    """The floor a registration of `basename` must clear: the hook's own
    whole-invocation judge budget, as declared in hook_wiring's requirements
    table. Asserted as a floor rather than against a literal copied from the
    installer, because the requirement IS "the harness cap must not bind below
    the hook's own deadline" — the DESIRED row adds interpreter-start headroom
    on top, and pinning that exact sum would make raising it a test edit."""
    for name, minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS:
        if name == basename:
            return minimum
    raise AssertionError(f"{basename} has no timeout requirement")


def test_an_existing_registration_gets_the_corrected_timeout(tmp_path):
    """The insert-only defect, pinned: the hook is ALREADY registered, at the old
    5s. A run that leaves it at 5 is the bug this reconciler exists to remove."""
    env = _shell_env(tmp_path)
    stale = str(SCRIPTS_DIR / SLOW_HOOK)
    settings = _write(env, {SLOW_EVENT: [_group(SLOW_MATCHER, [stale], 5)]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    entries = _entries(settings, SLOW_EVENT, SLOW_HOOK)
    assert len(entries) == 1, entries
    assert entries[0]["timeout"] != 5, "the stale registration was left as it was"
    assert entries[0]["timeout"] >= _required_timeout(SLOW_HOOK)


def test_reconciliation_does_not_duplicate_the_entry(tmp_path):
    """Reconcile means EDIT, not add-alongside. A second entry would leave the
    harness with two registrations of one hook and the reconciler with two
    numbers to keep in step forever."""
    env = _shell_env(tmp_path)
    settings = _write(env, {
        SLOW_EVENT: [_group(SLOW_MATCHER, [str(SCRIPTS_DIR / SLOW_HOOK)], 5)],
    })

    assert _run(env).returncode == 0
    assert len(_entries(settings, SLOW_EVENT, SLOW_HOOK)) == 1


def test_reconciliation_is_idempotent(tmp_path):
    """Once the number is right, a further run must not rewrite the file: a
    gratuitous write on every session-start install is churn, and it would make
    "the installer changed something" stop meaning anything."""
    env = _shell_env(tmp_path)
    settings = _write(env, {
        SLOW_EVENT: [_group(SLOW_MATCHER, [str(SCRIPTS_DIR / SLOW_HOOK)], 5)],
    })
    assert _run(env).returncode == 0
    after_first = settings.read_text(encoding="utf-8")

    assert _run(env).returncode == 0
    assert settings.read_text(encoding="utf-8") == after_first


def test_a_foreign_dirname_entry_is_not_retargeted(tmp_path):
    """The boundary that keeps reconciliation honest: same basename, different
    directory. That is a machine-local choice about WHAT runs — a fork, a
    vendored copy, another checkout. Rewriting its `command` would silently
    change which code executes, which is qualitatively worse than leaving the
    timeout wrong; the wiring probe reports the divergence instead."""
    env = _shell_env(tmp_path)
    foreign = str(tmp_path / "elsewhere" / SLOW_HOOK)
    settings = _write(env, {SLOW_EVENT: [_group(SLOW_MATCHER, [foreign], 5)]})

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr

    entries = _entries(settings, SLOW_EVENT, SLOW_HOOK)
    assert len(entries) == 1, "the foreign entry was duplicated or replaced"
    assert entries[0]["command"] == foreign, "the command was retargeted"


def test_reconciliation_never_rewrites_a_command(tmp_path):
    """The same boundary stated as a whole-file property rather than per entry:
    across every hook the installer touched, no `command` string that was there
    before the run is gone after it."""
    env = _shell_env(tmp_path)
    foreign = str(tmp_path / "elsewhere" / SLOW_HOOK)
    other = str(tmp_path / "elsewhere" / "hook-context-growth-reminder.py")
    settings = _write(env, {
        SLOW_EVENT: [_group(SLOW_MATCHER, [foreign], 5)],
        "UserPromptSubmit": [_group(None, [other], 5)],
    })

    assert _run(env).returncode == 0
    after = settings.read_text(encoding="utf-8")

    assert foreign in after and other in after


def test_a_hook_absent_from_the_root_is_still_added(tmp_path):
    """Reconciliation is added ALONGSIDE the insert pass, not in place of it: an
    empty root must still come out fully wired."""
    env = _shell_env(tmp_path)
    settings = _write(env, {})

    assert _run(env).returncode == 0

    entries = _entries(settings, SLOW_EVENT, SLOW_HOOK)
    assert len(entries) == 1
    assert entries[0]["timeout"] >= _required_timeout(SLOW_HOOK)
