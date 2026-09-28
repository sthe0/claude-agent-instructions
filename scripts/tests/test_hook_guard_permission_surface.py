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
import inspect
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


# --- structural invariants ---

def test_denial_arming_module_is_never_imported_by_the_guard():
    """Pins the historical fix: the reverted guard
    (hook-guard-permission-self-grant.py, commit b1d5802) used sticky
    cross-invocation "denial_arming" state and caused false positives; this
    guard's redesign deliberately imports no such module."""
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    assert "denial_arming" not in source
    assert not hasattr(guard, "denial_arming")


def test_decide_and_decide_detailed_never_touch_the_filesystem():
    """Structural: neither function performs I/O beyond the injected
    read_file -- confirmed by reading their own source, which must reference
    no bare filesystem read/write helper."""
    for fn in (guard.decide, guard.decide_detailed):
        src = inspect.getsource(fn)
        assert "open(" not in src
        assert "_real_read_file" not in src
        assert "_log_fire" not in src


def test_decide_detailed_fire_does_not_write_the_guard_log(tmp_path, monkeypatch):
    log_path = tmp_path / "would-be-guard-log.jsonl"
    monkeypatch.setenv("CLAUDE_PERMISSION_GUARD_LOG", str(log_path))
    command = (
        "python3 -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G4")
    assert not log_path.exists()


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


# --- G1-edit: `env`/defaultMode/bypassPermissions/MCP keys (round-2 should-fix
# S2 — `env` used to fire G1-edit on ANY change; a calibration run found 8 of 9
# fires were autocompact-window/AFK-timeout/output-length env edits, none
# security-relevant. Now `env` only fires on a named credential/base-url/proxy
# pattern, and defaultMode/disableBypassPermissionsMode/enableAllProjectMcpServers/
# enabledMcpjsonServers — previously not covered by `permission_surface.widens`
# at all — get their own widening checks.) ---

def _g1_edit_decision(tmp_path, old_text, new_text):
    target = tmp_path / ".claude-agent" / "settings.json"
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    read_file = _read_file_map({str(target): old_text})
    return decide("Edit", tool_input, str(tmp_path), "default", read_file)


def test_g1_edit_allows_autocompact_window_env_change(tmp_path):
    old_text = '{"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}}'
    new_text = '{"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "180000"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_allows_afk_timeout_env_change(tmp_path):
    old_text = '{"env": {"CLAUDE_AFK_TIMEOUT_MS": "60000"}}'
    new_text = '{"env": {"CLAUDE_AFK_TIMEOUT_MS": "120000"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_allows_bash_max_output_length_env_change(tmp_path):
    old_text = '{"env": {"BASH_MAX_OUTPUT_LENGTH": "30000"}}'
    new_text = '{"env": {"BASH_MAX_OUTPUT_LENGTH": "60000"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_fires_on_anthropic_api_key_env_change(tmp_path):
    old_text = '{"env": {"ANTHROPIC_API_KEY": "old"}}'
    new_text = '{"env": {"ANTHROPIC_API_KEY": "new"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_fires_on_anthropic_base_url_env_change(tmp_path):
    old_text = '{"env": {"ANTHROPIC_BASE_URL": "https://a.example"}}'
    new_text = '{"env": {"ANTHROPIC_BASE_URL": "https://b.example"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_fires_on_https_proxy_env_change(tmp_path):
    old_text = '{"env": {"HTTPS_PROXY": "http://a.example:8080"}}'
    new_text = '{"env": {"HTTPS_PROXY": "http://evil.example:8080"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_fires_on_claude_code_use_env_change(tmp_path):
    old_text = '{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}}'
    new_text = '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_allows_default_mode_narrowing(tmp_path):
    old_text = '{"permissions": {"defaultMode": "acceptEdits"}}'
    new_text = '{"permissions": {"defaultMode": "default"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_fires_on_default_mode_widening(tmp_path):
    old_text = '{"permissions": {"defaultMode": "default"}}'
    new_text = '{"permissions": {"defaultMode": "bypassPermissions"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_fires_on_auto_to_bypass_permissions_widening(tmp_path):
    # round-3 should-fix 3: auto and bypassPermissions used to tie at the same
    # rank, so this transition (skip the ask -> skip the rules themselves)
    # went unreported.
    old_text = '{"permissions": {"defaultMode": "auto"}}'
    new_text = '{"permissions": {"defaultMode": "bypassPermissions"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_allows_bypass_permissions_to_auto_narrowing(tmp_path):
    old_text = '{"permissions": {"defaultMode": "bypassPermissions"}}'
    new_text = '{"permissions": {"defaultMode": "auto"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_fires_on_default_mode_widening_to_unranked_value(tmp_path):
    old_text = '{"permissions": {"defaultMode": "default"}}'
    new_text = '{"permissions": {"defaultMode": "someFutureMode"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_allows_setting_disable_bypass_permissions_mode(tmp_path):
    old_text = '{"permissions": {}}'
    new_text = '{"permissions": {"disableBypassPermissionsMode": "disable"}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_fires_on_removing_disable_bypass_permissions_mode(tmp_path):
    old_text = '{"permissions": {"disableBypassPermissionsMode": "disable"}}'
    new_text = '{"permissions": {}}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_fires_on_enable_all_project_mcp_servers_turned_on(tmp_path):
    old_text = '{"enableAllProjectMcpServers": false}'
    new_text = '{"enableAllProjectMcpServers": true}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_allows_enable_all_project_mcp_servers_turned_off(tmp_path):
    old_text = '{"enableAllProjectMcpServers": true}'
    new_text = '{"enableAllProjectMcpServers": false}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


def test_g1_edit_fires_on_enabled_mcpjson_servers_entry_added(tmp_path):
    old_text = '{"enabledMcpjsonServers": ["known-server"]}'
    new_text = '{"enabledMcpjsonServers": ["known-server", "new-server"]}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "ask"


def test_g1_edit_allows_enabled_mcpjson_servers_entry_removed(tmp_path):
    old_text = '{"enabledMcpjsonServers": ["known-server", "old-server"]}'
    new_text = '{"enabledMcpjsonServers": ["known-server"]}'
    assert _g1_edit_decision(tmp_path, old_text, new_text) == "allow"


# --- G1-write / G1-multiedit / G1-notebook (finding B1: Write, MultiEdit and
# NotebookEdit reach the same G1-edit/G1-state/G3 surfaces as Edit — the
# guard was wired to `Edit|Write|MultiEdit|NotebookEdit` but `decide_detailed`
# used to inspect only Edit and Bash, letting the other three through) ---

def test_g1_write_fires_on_permissions_widening(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    new_text = '{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}'
    tool_input = {"file_path": str(target), "content": new_text}
    decision, branch, message = decide_detailed(
        "Write", tool_input, str(tmp_path), "default", _read_file_map({str(target): old_text}),
    )
    assert decision == "ask"
    assert branch == "G1-edit"
    assert str(target) in message


def test_g1_write_allows_on_narrowing(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": ["Bash(echo:*)", "Bash(ls:*)"]}}'
    new_text = '{"permissions": {"allow": ["Bash(echo:*)"]}}'
    tool_input = {"file_path": str(target), "content": new_text}
    read_file = _read_file_map({str(target): old_text})
    assert decide("Write", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_write_allows_when_base_file_is_unreadable(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    tool_input = {"file_path": str(target), "content": '{"permissions": {"allow": ["Bash(rm:*)"]}}'}
    assert decide("Write", tool_input, str(tmp_path), "default", _read_file_map({})) == "allow"


def test_g1_multi_edit_fires_on_permissions_widening(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    tool_input = {
        "file_path": str(target),
        "edits": [{"old_string": '"allow": []', "new_string": '"allow": ["Bash(rm -rf /:*)"]'}],
    }
    decision, branch, message = decide_detailed(
        "MultiEdit", tool_input, str(tmp_path), "default", _read_file_map({str(target): old_text}),
    )
    assert decision == "ask"
    assert branch == "G1-edit"
    assert str(target) in message


def test_g1_multi_edit_allows_on_narrowing(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": ["Bash(echo:*)", "Bash(ls:*)"]}}'
    tool_input = {
        "file_path": str(target),
        "edits": [{"old_string": ', "Bash(ls:*)"', "new_string": ""}],
    }
    read_file = _read_file_map({str(target): old_text})
    assert decide("MultiEdit", tool_input, str(tmp_path), "default", read_file) == "allow"


def test_g1_state_fires_on_write_under_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "agentctl" / "state" / "sess-1.json"
    tool_input = {"file_path": str(target), "content": "{}"}
    decision, branch, _ = decide_detailed("Write", tool_input, str(tmp_path), "default", lambda p: "x")
    assert decision == "ask"
    assert branch == "G1-state"


def test_g1_state_fires_on_multi_edit_under_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "agentctl" / "state" / "sess-1.json"
    tool_input = {"file_path": str(target), "edits": [{"old_string": "x", "new_string": "y"}]}
    decision, branch, _ = decide_detailed("MultiEdit", tool_input, str(tmp_path), "default", lambda p: "x")
    assert decision == "ask"
    assert branch == "G1-state"


def test_g1_state_fires_on_notebook_edit_under_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "agentctl" / "state" / "notebook.ipynb"
    tool_input = {"notebook_path": str(target), "new_source": "print(1)"}
    decision, branch, _ = decide_detailed("NotebookEdit", tool_input, str(tmp_path), "default", None)
    assert decision == "ask"
    assert branch == "G1-state"


def test_g1_write_allows_write_outside_all_g1_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "notes.txt"
    tool_input = {"file_path": str(target), "content": "hello"}
    assert decide("Write", tool_input, str(tmp_path), "default", lambda p: "") == "allow"


def test_g1_state_allows_notebook_edit_outside_agentctl_dir(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "notebooks" / "scratch.ipynb"
    tool_input = {"notebook_path": str(target)}
    assert decide("NotebookEdit", tool_input, str(tmp_path), "default", None) == "allow"


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


def test_g1_bash_fires_on_sed_in_place_over_live_settings(tmp_path):
    command = f"sed -i 's/x/y/' {tmp_path}/.claude-agent/settings.json"
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


@pytest.mark.parametrize(
    "command",
    [
        "sudo --user x claude --dangerously-skip-permissions",
        "env -C /tmp claude --add-dir /",
        "flock /tmp/l claude --add-dir /",
        "taskset 1 claude --add-dir /",
    ],
    ids=["sudo", "env", "flock", "taskset"],
)
def test_g2_fires_through_named_wrapper_shapes(command):
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "npm exec @anthropic-ai/claude-code -- --settings /tmp/x",
        "npx @anthropic-ai/claude-code@latest --add-dir /",
    ],
    ids=["npm-exec", "npx"],
)
def test_g2_fires_on_package_runner_shapes(command):
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


def test_g2_fires_on_node_cli_js_install_path_spelling():
    command = "node /home/u/.local/share/claude/node_modules/@anthropic-ai/claude-code/cli.js --settings /tmp/x"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


def test_g2_fires_on_claude_hidden_inside_a_bash_c_payload():
    """G2 must recurse into `sh|bash|zsh -c PAYLOAD` -- a spawned child could
    otherwise dodge identification by wrapping the widening call in one
    layer of `bash -c '...'`."""
    command = "bash -c 'claude --add-dir /'"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G2"


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


def test_g2_fires_on_allow_dangerously_skip_permissions_flag():
    """Round-2 finding B2: `--allow-dangerously-skip-permissions` widens
    exactly like `--dangerously-skip-permissions` and must fire on its own,
    not only when the older spelling is present."""
    command = "claude --allow-dangerously-skip-permissions -p 'do the thing'"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G2"


@pytest.mark.parametrize(
    "flag",
    ["--allowedTools", "--allowed-tools"],
    ids=["camel", "kebab"],
)
def test_g2_fires_on_allowed_tools_flag_spellings(flag):
    """Round-2 finding B2: both the camelCase and kebab-case spellings of the
    allowed-tools flag widen the permission surface and must fire."""
    command = f"claude {flag} Bash"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


@pytest.mark.parametrize("value", ["default", "plan"], ids=["default", "plan"])
def test_g2_allows_permission_mode_default_and_plan(value):
    """Round-2 finding B2: `--permission-mode default|plan` NARROWS
    permissions relative to the fleet's own default mode, unlike every other
    value (e.g. `bypassPermissions`, `acceptEdits`) — it must not fire."""
    command = f"claude --permission-mode {value}"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "allow"


def test_g2_fires_on_glued_flag_equals_value_form():
    """Round-2 finding B2: a widening flag glued to its value with `=`
    (`--add-dir=/`) must be recognized the same as the separate-token form
    (`--add-dir /`) — the prior bare-token-equality check missed it."""
    command = "claude --add-dir=/"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


def test_g2_allows_glued_permission_mode_equals_default():
    """The glued `=` form of the `--permission-mode` exemption must also be
    recognized — `--permission-mode=default` narrows exactly like the
    separate-token spelling and must not fire."""
    command = "claude --permission-mode=default"
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "allow"


def test_g2_fires_through_a_leading_bare_env_assignment():
    """Round-2 finding B2: a bare `KEY=VALUE` prefix with no `env` keyword
    (ordinary shell syntax) previously defeated `is_claude_program` —
    `strip_wrappers` trusted the first token as the program name, and that
    token was the assignment itself, hiding the real `claude` invocation."""
    command = "ANTHROPIC_BASE_URL=https://x claude --add-dir /"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G2"


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


@pytest.mark.parametrize(
    "command",
    [
        "crontab /tmp/new-crontab",
        "crontab -",
        "crontab -e",
        "crontab /tmp/new-crontab 2>&1 | tail -1",
        "crontab -r 2>/dev/null",
    ],
    ids=["install-file", "stdin", "edit", "install-file-redirected", "remove-redirected"],
)
def test_g3_fires_on_crontab_write_modes(command):
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "crontab -l",
        "which crontab",
        'echo "crontab"',
        "crontab -l 2>&1 | head -20",
        "crontab -l 2>/dev/null | grep -n tmux",
        'systemctl --user list-timers --all 2>&1 | head -25; echo "=== cron:"; crontab -l 2>&1 | head -20',
    ],
    ids=["list-only", "which", "echoed-word", "list-2>&1", "list-2>devnull", "list-in-sequence"],
)
def test_g3_allows_crontab_list_and_non_program_mentions(command):
    """Round-2 finding B3: the prior bare regex fired on `crontab -l` (a
    read-only list) the same as on a real write, and on the mere word
    "crontab" appearing anywhere in the command."""
    assert decide("Bash", {"command": command}, "/tmp", "default", None) == "allow"


def test_g3_allows_a_write_outside_any_launch_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "notes.txt"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    assert decide("Edit", tool_input, str(tmp_path), "default", lambda p: "x") == "allow"


def test_g3_fires_on_write_under_launch_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library" / "LaunchAgents" / "com.example.plist"
    tool_input = {"file_path": str(target), "content": "<plist/>"}
    decision, branch, _ = decide_detailed("Write", tool_input, str(tmp_path), "default", None)
    assert decision == "ask"
    assert branch == "G3"


def test_g3_fires_on_multi_edit_under_launch_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library" / "LaunchAgents" / "com.example.plist"
    tool_input = {"file_path": str(target), "edits": [{"old_string": "x", "new_string": "y"}]}
    decision, branch, _ = decide_detailed("MultiEdit", tool_input, str(tmp_path), "default", lambda p: "x")
    assert decision == "ask"
    assert branch == "G3"


def test_g3_fires_on_notebook_edit_under_launch_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library" / "LaunchAgents" / "notebook.ipynb"
    tool_input = {"notebook_path": str(target)}
    decision, branch, _ = decide_detailed("NotebookEdit", tool_input, str(tmp_path), "default", None)
    assert decision == "ask"
    assert branch == "G3"


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


def test_g4_fires_on_agentctl_cli_absolute_entry_point_spelling(tmp_path):
    command = (
        f"python3 {tmp_path}/scripts/agentctl-cli.py resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert decision == "ask"
    assert branch == "G4"


def test_g4_fires_on_venv_interpreter_spelling():
    command = (
        "/home/u/.venv/bin/python -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G4"


# --- G4: round-2 should-fix S3 (name the pending request, glued --decision=,
# recurse through bash -c like G2) and S4 (`_shell_c_payloads` coverage) ---

def test_g4_fires_on_glued_decision_equals_granted():
    command = "python3 -m agentctl resolve-permission --session abc123 --decision=granted"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G4"


def test_g4_fires_through_a_bash_c_wrapper():
    command = "bash -c \"python3 -m agentctl resolve-permission --session s1 --decision granted\""
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision == "ask"
    assert branch == "G4"


def test_g4_message_names_the_pending_request_action_from_state(tmp_path):
    session = "sess-42"
    filename = f"{guard.config_root.sanitize_session_id(session)}.json"
    state_path = guard.config_root.agentctl_state_dir() / filename
    state_doc = json.dumps({"permission_request": {"action": "Bash(git push:*)", "stage_index": 2}})
    read_file = _read_file_map({str(state_path): state_doc})
    command = f"python3 -m agentctl resolve-permission --session {session} --decision granted"
    decision, branch, message = decide_detailed("Bash", {"command": command}, "/tmp", "default", read_file)
    assert decision == "ask"
    assert branch == "G4"
    assert "Bash(git push:*)" in message


def test_g4_message_falls_back_to_naming_the_session_when_state_is_unreadable():
    command = "python3 -m agentctl resolve-permission --session sess-99 --decision granted"
    decision, branch, message = decide_detailed(
        "Bash", {"command": command}, "/tmp", "default", _read_file_map({}),
    )
    assert decision == "ask"
    assert branch == "G4"
    assert "sess-99" in message


# --- G4: round-3 nit 2 (name the real --scope, not the nonexistent --stage) ---

def test_g4_message_includes_the_real_scope_value():
    """`resolve-permission` has no `--stage` argument at all -- a real call
    names `--scope` (`once`/`project`/`global`/`stage`). Before this fix, the
    message named only `--stage` (always absent on a real call), reading
    "stage (none named)" on every single real G4 fire."""
    command = "python3 -m agentctl resolve-permission --session sess-1 --decision granted --scope project"
    decision, branch, message = decide_detailed(
        "Bash", {"command": command}, "/tmp", "default", _read_file_map({}),
    )
    assert decision == "ask"
    assert branch == "G4"
    assert "scope project" in message


def test_g4_message_defaults_scope_to_once_and_drops_stage_clause_when_absent():
    """No `--scope` on the command means the CLI itself defaults to `once` --
    the message should say so, not fall silent; and with no `--stage` flag
    (the real, common shape) the old unconditional "stage (none named)"
    clause must be gone entirely, not merely reworded."""
    command = "python3 -m agentctl resolve-permission --session sess-1 --decision granted"
    decision, branch, message = decide_detailed(
        "Bash", {"command": command}, "/tmp", "default", _read_file_map({}),
    )
    assert decision == "ask"
    assert branch == "G4"
    assert "scope once" in message
    assert "none named" not in message


def test_shell_c_payloads_matches_stacked_short_flag_cluster():
    assert guard._shell_c_payloads(["bash", "-lc", "echo hi"]) == ["echo hi"]
    assert guard._shell_c_payloads(["bash", "-xc", "echo hi"]) == ["echo hi"]


def test_shell_c_payloads_matches_shell_behind_a_timeout_wrapper():
    assert guard._shell_c_payloads(["timeout", "5", "bash", "-c", "echo hi"]) == ["echo hi"]


def test_shell_c_payloads_matches_shell_behind_an_env_wrapper():
    assert guard._shell_c_payloads(["env", "X=1", "sh", "-c", "echo hi"]) == ["echo hi"]


def test_shell_c_payloads_matches_eval():
    assert guard._shell_c_payloads(["eval", "echo", "hi"]) == ["echo hi"]


def test_shell_c_payloads_empty_for_a_non_shell_non_eval_program():
    assert guard._shell_c_payloads(["python3", "-c", "print(1)"]) == []


def test_shell_c_payloads_empty_for_a_long_flag_that_merely_contains_c():
    assert guard._shell_c_payloads(["bash", "--rcfile", "x"]) == []


def test_shell_c_payloads_empty_when_dash_c_is_the_scripts_own_argument():
    # round-3 nit 3: `bash script.sh -c x` runs the SCRIPT `script.sh` --
    # the `-c x` that follows is an argument to `script.sh`, not a flag to
    # `bash` itself (a shell only reads its own flags before the first
    # positional operand). Scanning past `script.sh` used to misread its
    # `-c` as bash's own payload flag.
    assert guard._shell_c_payloads(["bash", "script.sh", "-c", "x"]) == []



# Round-3 review blocking 2: stopping at the first non-`-` token also stopped at
# an option's own argument (`-o pipefail`) and at `+`-options, silencing G2/G4.
_SHELL_OPTION_PREFIXES = ["-o pipefail", "+o posix", "-O extglob", "+x", "--rcfile F", "--init-file F", "-e -o pipefail"]


@pytest.mark.parametrize("prefix", _SHELL_OPTION_PREFIXES)
def test_g2_fires_through_a_bash_c_wrapper_after_shell_options(prefix):
    command = f"bash {prefix} -c 'claude --dangerously-skip-permissions -p x'"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert (decision, branch) == ("ask", "G2")


@pytest.mark.parametrize("prefix", _SHELL_OPTION_PREFIXES)
def test_g4_fires_through_a_bash_c_wrapper_after_shell_options(prefix):
    command = f"bash {prefix} -c 'python3 -m agentctl resolve-permission --session s1 --decision granted'"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert (decision, branch) == ("ask", "G4")


def test_shell_c_payloads_stop_at_a_plus_prefixed_script_operand():
    assert guard._shell_c_payloads(["bash", "+weird.sh", "-c", "x"]) == []


def test_shell_c_payloads_stop_at_double_dash():
    assert guard._shell_c_payloads(["bash", "--", "s", "-c", "x"]) == []

# --- negative corpus (shapes that must not fire any branch) ---

@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commit -m 'switch --permission-mode default in the reference doc'",
        "python3 scripts/verify-all.py --staged",
        "python3 -c 'pass'",
        "echo --dangerously-skip-permissions",
        "grep -rn permission-mode scripts/",
        "cat <<'EOF'\n{\"permissions\": {\"allow\": [\"Bash(rm:*)\"]}}\nEOF",
    ],
    ids=[
        "git-status", "git-commit-mentioning-flag", "python3-scripts-call",
        "python3-dash-c-pass", "echo-of-flag", "grep-of-flag", "heredoc-of-settings-json",
    ],
)
def test_negative_corpus_never_fires(tmp_path, command):
    assert decide("Bash", {"command": command}, str(tmp_path), "default", None) == "allow"


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


# --- round-2 should-fix S5: `ts`/`permission_mode`/`target` on a logged row ---
# without these the per-session-day false-positive measurement the plan
# requires is lost, and so is the command match when `tool_use_id` is absent.

def test_main_logged_row_carries_timestamp_mode_and_command_target(tmp_path):
    log_path = tmp_path / "guard.jsonl"
    payload = {
        "session_id": "sess-124",
        "transcript_path": "/fake/transcript.jsonl",
        "tool_use_id": "toolu_fixture_3",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 -m agentctl resolve-permission --rule x --stage 1 --decision granted"},
        "cwd": str(tmp_path),
        "permission_mode": "acceptEdits",
    }
    before = datetime.now(timezone.utc)
    result = _run_hook(payload, {"CLAUDE_PERMISSION_GUARD_LOG": str(log_path)})
    after = datetime.now(timezone.utc)
    assert result.returncode == 0
    row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["permission_mode"] == "acceptEdits"
    assert row["target"] == payload["tool_input"]["command"]
    ts = datetime.fromisoformat(row["ts"])
    assert before <= ts <= after


def test_main_logged_row_target_falls_back_to_file_path_for_edit(tmp_path):
    log_path = tmp_path / "guard.jsonl"
    target = tmp_path / ".claude-agent" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"permissions": {"allow": []}}', encoding="utf-8")
    payload = {
        "session_id": "sess-125",
        "transcript_path": "/fake/transcript.jsonl",
        "tool_use_id": "toolu_fixture_4",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": '{"permissions": {"allow": []}}',
            "new_string": '{"permissions": {"allow": ["Bash(rm:*)"]}}',
        },
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    result = _run_hook(payload, {"CLAUDE_PERMISSION_GUARD_LOG": str(log_path)})
    assert result.returncode == 0
    row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["target"] == str(target)


def test_log_target_prefers_command_over_file_path():
    assert guard._log_target({"command": "echo hi", "file_path": "/some/path"}) == "echo hi"


def test_log_target_reads_notebook_path():
    assert guard._log_target({"notebook_path": "/nb.ipynb"}) == "/nb.ipynb"


def test_log_target_none_when_no_recognized_key():
    assert guard._log_target({}) is None


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


# --- mutation catalogue: each G-branch's positive test is a genuine control,
# not an accidental pass -- neutralize the SPECIFIC predicate the branch
# depends on and confirm the previously-firing scenario now allows. ---

def _g1_edit_widening_case(tmp_path, monkeypatch):
    del monkeypatch
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    new_text = '{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    return "Edit", tool_input, str(tmp_path), _read_file_map({str(target): old_text})


def _g1_edit_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "is_live_settings", lambda p: False)


def _g1_write_widening_case(tmp_path, monkeypatch):
    """Round-2 finding B1: the plan requires Write to reach G1-edit exactly
    like Edit -- add it to the catalogue so this control is itself proven a
    genuine control, not an accidental pass."""
    del monkeypatch
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"permissions": {"allow": []}}'
    new_text = '{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}'
    tool_input = {"file_path": str(target), "content": new_text}
    return "Write", tool_input, str(tmp_path), _read_file_map({str(target): old_text})


def _g1_bash_write_case(tmp_path, monkeypatch):
    del monkeypatch
    command = f"cat x > {tmp_path}/.claude-agent/settings.json"
    return "Bash", {"command": command}, str(tmp_path), None


def _g1_bash_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "is_live_settings", lambda p: False)


def _g1_state_edit_case(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    target = tmp_path / "agentctl" / "state" / "sess-1.json"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    return "Edit", tool_input, str(tmp_path), (lambda p: "x")


def _g1_state_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "is_agentctl_state_path", lambda p: False)


def _g2_case(tmp_path, monkeypatch):
    del monkeypatch
    command = "claude --dangerously-skip-permissions -p 'do the thing'"
    return "Bash", {"command": command}, str(tmp_path), None


def _g2_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "is_claude_program", lambda seg: False)


def _g3_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library" / "LaunchAgents" / "com.example.plist"
    tool_input = {"file_path": str(target), "old_string": "x", "new_string": "y"}
    return "Edit", tool_input, str(tmp_path), (lambda p: "x")


def _g3_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "is_launch_surface", lambda p: False)


def _g4_case(tmp_path, monkeypatch):
    del monkeypatch
    command = (
        "python3 -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    return "Bash", {"command": command}, str(tmp_path), None


def _g4_mutate(monkeypatch):
    monkeypatch.setattr(guard.widening_targets, "agentctl_invocation_verb", lambda seg: (False, None))


_MUTATION_CATALOGUE = [
    ("G1-edit", _g1_edit_widening_case, _g1_edit_mutate),
    ("G1-edit", _g1_write_widening_case, _g1_edit_mutate),
    ("G1-bash", _g1_bash_write_case, _g1_bash_mutate),
    ("G1-state", _g1_state_edit_case, _g1_state_mutate),
    ("G2", _g2_case, _g2_mutate),
    ("G3", _g3_case, _g3_mutate),
    ("G4", _g4_case, _g4_mutate),
]


@pytest.mark.parametrize(
    "branch, build_case, mutate", _MUTATION_CATALOGUE, ids=[c[0] for c in _MUTATION_CATALOGUE],
)
def test_mutation_catalogue_control_goes_red(branch, build_case, mutate, tmp_path, monkeypatch):
    tool_name, tool_input, cwd, read_file = build_case(tmp_path, monkeypatch)
    decision_before, branch_before, _ = decide_detailed(tool_name, tool_input, cwd, "default", read_file)
    assert (decision_before, branch_before) == ("ask", branch)

    mutate(monkeypatch)

    decision_after, branch_after, _ = decide_detailed(tool_name, tool_input, cwd, "default", read_file)
    assert decision_after == "allow", f"{branch} still fired after neutralizing its predicate: {branch_after}"


# --- round-2 should-fix S6: false-positive-direction mutations ---
# `_MUTATION_CATALOGUE` above only proves each branch's POSITIVE control
# depends on a real predicate. Nothing proved the negative corpus is a real
# control rather than a vacuous pass. Each test below takes a case that
# currently, correctly, returns "allow" and shows a named predicate change
# would flip it to a false "ask" -- so a negative-control test really does
# go red if that predicate ever regresses this way.

def test_internal_exception_during_a_benign_bash_command_fails_open_not_closed(monkeypatch):
    """`decide_detailed`'s outer `except Exception: return "allow"` (module
    docstring: "any unexpected shape or internal exception allows") is a
    design choice, not just prose -- if a future edit flipped that fallback
    to "ask", this test (which forces a real internal failure on an
    otherwise-benign command) would go red. `_g1_bash` is patched to raise
    so the exception path is genuinely exercised, not just assumed
    unreachable."""
    def _boom(command, cwd):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "_g1_bash", _boom)
    decision, branch, _ = decide_detailed("Bash", {"command": "git status"}, "/tmp", "default", None)
    assert decision == "allow"
    assert branch is None


def test_negative_control_goes_red_when_is_crontab_target_always_true(monkeypatch):
    command = "git status"
    decision_before, _, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision_before == "allow"

    monkeypatch.setattr(guard.widening_targets, "is_crontab_target", lambda c: True)
    decision_after, branch_after, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision_after == "ask"
    assert branch_after == "G3"


def test_negative_control_goes_red_when_iter_candidate_programs_returns_every_token(monkeypatch):
    # "claude" appears only as an echoed argument, never as the invoked
    # program -- currently allow, same shape as the negative corpus's own
    # "echo-of-flag" case.
    command = "echo claude --dangerously-skip-permissions"
    decision_before, _, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision_before == "allow"

    def _every_token(tokens):
        return [
            (guard.widening_targets.program_name(t).casefold(), [])
            for t in tokens
            if not t.startswith("-")
        ]

    monkeypatch.setattr(guard.widening_targets, "iter_candidate_programs", _every_token)
    decision_after, branch_after, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert decision_after == "ask"
    assert branch_after == "G2"
