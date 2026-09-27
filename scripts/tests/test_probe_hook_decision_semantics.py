"""Unit tests for probe-hook-decision-semantics.py's pure result-parsing and
rendering functions -- never launches `claude`, tmux, or any subprocess.
Exercises: the stream-json init-event mode extraction (`find_key` /
`parse_stream_json_effective_mode`) against synthetic fixtures, the pane-text
prompt/denial detectors, the scratch hook/settings builders (file writes
only), and the markdown table renderer.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "probe_hook_decision_semantics", ROOT / "scripts" / "probe-hook-decision-semantics.py"
)
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["probe_hook_decision_semantics"] = probe
_SPEC.loader.exec_module(probe)


# --- find_key -----------------------------------------------------------

def test_find_key_direct_hit():
    assert probe.find_key({"permissionMode": "acceptEdits"}, probe._MODE_KEY_NAMES) == "acceptEdits"


def test_find_key_case_insensitive():
    assert probe.find_key({"PermissionMode": "auto"}, probe._MODE_KEY_NAMES) == "auto"


def test_find_key_nested_dict():
    obj = {"type": "system", "subtype": "init", "data": {"session": {"permission_mode": "bypassPermissions"}}}
    assert probe.find_key(obj, probe._MODE_KEY_NAMES) == "bypassPermissions"


def test_find_key_nested_list():
    obj = [{"foo": "bar"}, {"mode": "default"}]
    assert probe.find_key(obj, probe._MODE_KEY_NAMES) == "default"


def test_find_key_absent_returns_none():
    assert probe.find_key({"foo": "bar"}, probe._MODE_KEY_NAMES) is None


def test_find_key_non_string_value_is_returned_as_is():
    # A caller checking `isinstance(found, str)` filters this out downstream;
    # find_key itself is a raw structural search, not a type-validating one.
    assert probe.find_key({"mode": 42}, probe._MODE_KEY_NAMES) == 42


# --- parse_stream_json_effective_mode ------------------------------------

def test_parse_stream_json_finds_first_mode_line():
    raw = "\n".join([
        "not json at all",
        json.dumps({"type": "system", "subtype": "init", "permissionMode": "acceptEdits", "session_id": "abc"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
    ])
    assert probe.parse_stream_json_effective_mode(raw) == "acceptEdits"


def test_parse_stream_json_no_mode_field_returns_none():
    raw = json.dumps({"type": "assistant", "message": {"content": []}})
    assert probe.parse_stream_json_effective_mode(raw) is None


def test_parse_stream_json_empty_input_returns_none():
    assert probe.parse_stream_json_effective_mode("") is None


def test_parse_stream_json_skips_unparseable_lines():
    raw = "\n".join([
        "{not valid json",
        json.dumps({"mode": "auto"}),
    ])
    assert probe.parse_stream_json_effective_mode(raw) == "auto"


# --- command_executed / permission_denied ---------------------------------
# Fixtures mirror the real observed transcript shape: a denied Bash call's
# marker sits verbatim in the assistant's proposed tool_use.input.command
# regardless of outcome, so only a non-error tool_result -- or the CLI's own
# `permission_denials` field on the final result event -- distinguishes ran
# from denied.

def _tool_use_line(marker: str):
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"echo {marker}"}}]},
    })


def _tool_result_line(content: str, is_error: bool):
    return json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": content, "is_error": is_error}]},
    })


def _result_line(permission_denials):
    obj = {"type": "result", "subtype": "success"}
    if permission_denials:
        obj["permission_denials"] = permission_denials
    return json.dumps(obj)


def test_command_executed_true_on_non_error_tool_result():
    marker = "PERM_PROBE_OK_abc"
    raw = "\n".join([_tool_use_line(marker), _tool_result_line(f"{marker}\n", is_error=False), _result_line(None)])
    assert probe.command_executed(raw, marker)


def test_command_executed_false_when_only_in_tool_use_input():
    # This is the bug this function fixes: sentinel_present would return True
    # here (the marker is in the raw text via tool_use.input.command) even
    # though the call was denied and never produced a tool_result.
    marker = "PERM_PROBE_OK_def"
    raw = "\n".join([_tool_use_line(marker), _result_line([{"tool_name": "Bash"}])])
    assert not probe.command_executed(raw, marker)
    assert probe.sentinel_present(raw, marker)  # documents the old, buggy behaviour


def test_command_executed_false_on_error_tool_result_containing_marker():
    marker = "PERM_PROBE_OK_ghi"
    raw = "\n".join([_tool_use_line(marker), _tool_result_line(f"hook error mentioning {marker}", is_error=True)])
    assert not probe.command_executed(raw, marker)


def test_permission_denied_true_when_result_carries_denials():
    raw = _result_line([{"tool_name": "Bash", "tool_input": {"command": "echo x"}}])
    assert probe.permission_denied(raw)


def test_permission_denied_false_when_result_has_no_denials():
    raw = _result_line(None)
    assert not probe.permission_denied(raw)


def test_permission_denied_false_on_empty_input():
    assert not probe.permission_denied("")


# --- sentinel_present / detect_prompt / detect_denial --------------------

def test_sentinel_present_true():
    assert probe.sentinel_present("output: PERM_PROBE_OK_abc123 done", "PERM_PROBE_OK_abc123")


def test_sentinel_present_false():
    assert not probe.sentinel_present("nothing here", "PERM_PROBE_OK_abc123")


@pytest.mark.parametrize("pane_text", [
    "Do you want to proceed?\n1. Yes\n2. No",
    "Claude needs your permission to use Bash command",
    "  1. Yes, and don't ask again  ",
])
def test_detect_prompt_true(pane_text):
    assert probe.detect_prompt(pane_text)


def test_detect_prompt_false_on_unrelated_text():
    assert not probe.detect_prompt("hello world, running your command now")


def test_detect_denial_true():
    assert probe.detect_denial("Permission denied: hook blocked this tool call")


def test_detect_denial_false():
    assert not probe.detect_denial("everything ran fine")


@pytest.mark.parametrize("pane_text", [
    "Choose the text style that looks best with your terminal",
    "Do you trust the files in this folder?",
    " Let's get started.",
    "Paste code here if prompted >",
    "https://claude.com/cai/oauth/authorize?code=true",
])
def test_detect_onboarding_true(pane_text):
    assert probe.detect_onboarding(pane_text)


def test_detect_onboarding_false_on_task_output():
    assert not probe.detect_onboarding("PERM_PROBE_OK_abc123\ndone")


# --- scratch hook / settings builders (file writes only, no subprocess) --

def test_decision_hook_script_writes_executable_ask(tmp_path):
    path = probe._decision_hook_script(tmp_path, "ask")
    assert path.exists()
    assert path.stat().st_mode & 0o111  # executable bit set
    content = path.read_text()
    assert "'ask'" in content


def test_decision_hook_script_writes_executable_deny(tmp_path):
    path = probe._decision_hook_script(tmp_path, "deny")
    content = path.read_text()
    assert "'deny'" in content


def test_settings_with_hook_shape(tmp_path):
    hook_path = probe._decision_hook_script(tmp_path, "deny")
    settings = probe._settings_with_hook(hook_path)
    pretooluse = settings["hooks"]["PreToolUse"]
    assert pretooluse[0]["matcher"] == "Bash"
    assert pretooluse[0]["hooks"][0]["command"] == str(hook_path)
    assert "permissions" not in settings


def test_settings_with_hook_includes_extra_perms(tmp_path):
    hook_path = probe._decision_hook_script(tmp_path, "ask")
    settings = probe._settings_with_hook(hook_path, extra_perms={"deny": ["Edit(//x/**)"]})
    assert settings["permissions"] == {"deny": ["Edit(//x/**)"]}


# --- render_table ---------------------------------------------------------

def test_render_table_basic_shape():
    results = [
        probe.CellResult(
            cell_id="headless:auto:deny", requested_mode="auto", decision="deny",
            effective_mode="auto", sentinel_ran=False, prompt_shown=False, timed_out=False, notes="",
        ),
        probe.CellResult(
            cell_id="headless:default:ask", requested_mode="default", decision="ask",
            effective_mode=None, sentinel_ran=None, prompt_shown=None, timed_out=True, notes="timed out",
        ),
    ]
    table = probe.render_table(results)
    assert "| Cell | Requested mode |" in table
    assert "`headless:auto:deny`" in table
    assert "| auto | deny | auto | no | no | no |" in table
    assert "not observed" in table
    assert "timed out" in table


def test_render_table_bool_formatting_is_yes_no_not_true_false():
    results = [
        probe.CellResult(
            cell_id="c", requested_mode="m", decision="d",
            effective_mode="m2", sentinel_ran=True, prompt_shown=True, timed_out=False, notes="",
        ),
    ]
    table = probe.render_table(results)
    assert "True" not in table
    assert "False" not in table
    assert "yes" in table
    assert "no" in table


# --- _plan ------------------------------------------------------------

def test_plan_has_twelve_cells():
    assert len(probe._plan()) == 12


def test_plan_cell_ids_unique():
    plan = probe._plan()
    assert len(set(plan)) == len(plan)


# --- main --dry-run (argparse + planning only, no subprocess) -----------

def test_main_dry_run_lists_all_cells(capsys):
    rc = probe.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    for cell_id in probe._plan():
        assert cell_id in out


def test_main_dry_run_only_filters(capsys):
    rc = probe.main(["--dry-run", "--only", "add_dir"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "add_dir:accept_edits_read_add_dir" in out
    assert "headless:" not in out
