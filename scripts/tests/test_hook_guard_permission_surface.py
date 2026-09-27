"""Tests for hook-guard-permission-surface.py — `decide()`/`decide_detailed()`
fire ONLY on positive identification across G1 (edit/bash/state)-G4, never on
UNKNOWN/unreadable/unparsable, and always return `"ask"` (never `"deny"`) on a
fire. Each G-branch gets a positive case, at least one adjacent negative case
that could plausibly be confused for it, and — where the plan calls it out —
the specific pinned shape from round-4/round-5 calibration: a `claude`
invocation elided inside a quoted brief argument to `spawn-specialist.py`
must NOT fire G2 (round-4 F1), and `cat x > <live settings>` DOES fire G1-bash
even though `Bash(cat:*)` is itself a grantable rule (round-5 L7) — a
DELIBERATE fire, not a bug.

Hermetic: G1-edit/G1-bash use a literal `.claude-agent` path component (never
an actual `$VAR` reference — `bash_write_targets` deliberately never expands
one) so `is_live_settings` fires without depending on this machine's real
`$CLAUDE_AGENT_HOME`. G1-state monkeypatches `CLAUDE_AGENT_HOME` to `tmp_path`
so `is_agentctl_state_path` resolves under it. G3's launch-surface case
monkeypatches `HOME` to `tmp_path`, since `is_launch_surface` always resolves
against the real `Path.home()`.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-permission-surface.py"

_SPEC = importlib.util.spec_from_file_location("hook_guard_permission_surface", HOOK_SCRIPT)
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

decide = guard.decide
decide_detailed = guard.decide_detailed


def _read_file_map(mapping: dict[str, str]):
    def _read(path: str) -> str:
        if path not in mapping:
            raise FileNotFoundError(path)
        return mapping[path]
    return _read


# --- baseline: an unrelated tool never fires ---

def test_read_tool_always_allows():
    assert decide("Read", {"file_path": "/etc/hosts"}, "/tmp", "default", lambda p: "") == "allow"


def test_decision_is_independent_of_permission_mode(tmp_path):
    command = f"cat x > {tmp_path}/.claude-agent/settings.json"
    for mode in ("default", "acceptEdits", "bypassPermissions", "plan", None):
        assert decide("Bash", {"command": command}, str(tmp_path), mode, None) == "ask"


# --- G1-edit ---

def test_g1_edit_fires_on_permissions_widening(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    new_text = '{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    decision, branch, message = decide_detailed(
        "Edit", tool_input, str(tmp_path), "default", _read_file_map({str(target): old_text}),
    )
    assert decision == "ask"
    assert branch == "G1-edit"
    assert str(target) in message


def test_g1_edit_fires_on_hooks_key_change_regardless_of_direction(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"hooks": {"PreToolUse": []}}'
    new_text = '{"hooks": {}}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    read_file = _read_file_map({str(target): old_text})
    assert decide("Edit", tool_input, str(tmp_path), "default", read_file) == "ask"


def test_g1_edit_allows_on_narrowing_edit(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": ["Bash(echo:*)", "Bash(ls:*)"]}}'
    new_text = '{"permissions": {"allow": ["Bash(echo:*)"]}}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    read_file = _read_file_map({str(target): old_text})
    assert decide("Edit", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_edit_allows_edit_of_non_security_key(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"autoCompactWindow": 100000}'
    new_text = '{"autoCompactWindow": 150000}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    read_file = _read_file_map({str(target): old_text})
    assert decide("Edit", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_edit_allows_repo_template_settings(tmp_path):
    target = tmp_path / "settings" / "base.json"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    assert decide("Edit", tool_input, str(tmp_path), "default", lambda p: "x") == "allow"


def test_g1_edit_allows_when_old_string_does_not_match_base_file(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    tool_input = {
        "file_path": str(target),
        "old_string": "this text is not present in the base file",
        "new_string": '{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}',
    }
    read_file = _read_file_map({str(target): old_text})
    assert decide("Edit", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_edit_allows_when_base_file_is_not_valid_json(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = "not json at all"
    tool_input = {"file_path": str(target), "old_string": "not json", "new_string": "still not json"}
    read_file = _read_file_map({str(target): old_text})
    assert decide("Edit", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_edit_allows_when_base_file_is_unreadable(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    assert decide("Edit", tool_input, str(tmp_path), "default", _read_file_map({})) == "allow"


# --- G1-bash ---

def test_g1_bash_fires_on_write_into_live_settings_even_though_cat_is_grantable(tmp_path):
    """Round-5 L7, pinned: `Bash(cat:*)` is accepted by `validate_rule` as a
    grantable rule, but the CONCRETE call `cat x > .../settings.json` still
    fires — this is the intended fire the grant/guard disjointness test also
    pins, not a gap."""
    command = f"cat x > {tmp_path}/.claude-agent/settings.json"
    decision, branch, message = decide_detailed(
        "Bash", {"command": command}, str(tmp_path), "default", None,
    )
    assert decision == "ask"
    assert branch == "G1-bash"
    assert "settings.json" in message


def test_g1_bash_allows_a_plain_read_with_no_write_target(tmp_path):
    command = f"cat {tmp_path}/.claude-agent/settings.json"
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "allow"


def test_g1_bash_allows_sed_in_place_over_a_repo_template(tmp_path):
    command = f"sed -i 's/x/y/' {tmp_path}/settings/base.json"
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "allow"


# --- G1-state ---

def _isolate_agent_home(monkeypatch, tmp_path) -> None:
    """`config_root.agent_home()` checks `CLAUDE_CONFIG_DIR` before
    `CLAUDE_AGENT_HOME` — the live harness always sets the former, so a test
    isolating agent_home() must clear/override BOTH or the real value wins."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))


def test_g1_state_fires_on_edit_under_agentctl_state_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "agentctl" / "state" / "sess-1.json"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    decision, branch, _ = decide_detailed("Edit", tool_input, str(tmp_path), "default", lambda p: "x")
    assert decision == "ask"
    assert branch == "G1-state"


def test_g1_state_fires_on_bash_write_under_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    command = f"echo '{{}}' > {tmp_path}/agentctl/state/sess-1.json"
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "ask"


def test_g1_state_allows_write_outside_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    command = f"echo hi > {tmp_path}/scratch.txt"
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "allow"


# --- G2 ---

def test_g2_fires_on_bare_claude_with_dangerously_skip_permissions(tmp_path):
    command = "claude --dangerously-skip-permissions -p 'do the thing'"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert decision == "ask"
    assert branch == "G2"


def test_g2_fires_through_a_timeout_wrapper_with_add_dir():
    command = "timeout -s KILL 5 claude --add-dir /"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


def test_g2_fires_on_install_path_spelling_with_permission_mode(tmp_path):
    command = f"{tmp_path}/.local/share/claude/versions/1.2.3/claude --permission-mode bypassPermissions"
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "ask"


def test_g2_allows_a_plain_claude_help_call():
    assert decide("Bash", {"command": "claude --help"}, "/tmp", "default", None) == "allow"


def test_g2_allows_claude_flags_elided_inside_a_quoted_spawn_specialist_argument():
    """Round-4 F1: the claude invocation with `--permission-mode
    bypassPermissions` sits entirely inside ONE quoted argument to
    `spawn-specialist.py` — never its own program token — so `is_claude_program`
    correctly never matches it and this must not fire."""
    command = (
        "python3 scripts/spawn-specialist.py --kind developer --brief "
        "\"echo perm-live-child && claude -p --permission-mode bypassPermissions --help\""
    )
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "allow"


# --- G3 ---

def test_g3_fires_on_edit_under_launch_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library" / "LaunchAgents" / "com.example.plist"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    decision, branch, _ = decide_detailed("Edit", tool_input, str(tmp_path), "default", lambda p: "x")
    assert decision == "ask"
    assert branch == "G3"


def test_g3_fires_on_crontab_invocation():
    command = "(crontab -l; echo '* * * * * /bin/true') | crontab -"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


def test_g3_allows_a_write_outside_any_launch_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "notes.txt"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    assert decide("Edit", tool_input, str(tmp_path), "default", lambda p: "x") == "allow"


# --- G4 ---

def test_g4_fires_on_resolve_permission_decision_granted():
    command = (
        "python3 -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    decision, branch, message = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G4"
    assert "Bash(rm:*)" in message
    assert "3" in message


def test_g4_allows_decision_denied():
    command = (
        "python3 -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision denied"
    )
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "allow"


def test_g4_allows_a_readonly_agentctl_verb():
    assert decide("Bash", {"command": "python3 -m agentctl status"}, "/tmp", "default", None) == "allow"


# --- main() subprocess: fire logging and always-exit-0 ---

def _run_hook(payload: dict, env_extra: dict) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def test_main_logs_a_fire_with_session_transcript_and_tool_use_id(tmp_path):
    log_path = tmp_path / "guard.jsonl"
    payload = {
        "session_id": "sess-123",
        "transcript_path": "/fake/transcript.jsonl",
        "tool_use_id": "toolu_fixture_1",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 -m agentctl resolve-permission --rule x --stage 1 --decision granted"},
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    result = _run_hook(payload, {"CLAUDE_PERMISSION_GUARD_LOG": str(log_path)})
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["session_id"] == "sess-123"
    assert row["transcript_path"] == "/fake/transcript.jsonl"
    assert row["tool_use_id"] == "toolu_fixture_1"
    assert row["branch"] == "G4"


def test_main_allow_emits_no_stdout_and_no_log_write(tmp_path):
    log_path = tmp_path / "guard.jsonl"
    payload = {
        "session_id": "sess-123",
        "transcript_path": "/fake/transcript.jsonl",
        "tool_use_id": "toolu_fixture_2",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    result = _run_hook(payload, {"CLAUDE_PERMISSION_GUARD_LOG": str(log_path)})
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not log_path.exists()


def test_main_malformed_stdin_exits_zero_without_crashing():
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input="not json", capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
