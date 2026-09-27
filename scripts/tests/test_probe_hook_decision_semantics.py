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
    " Let's get started.",
    "Paste code here if prompted >",
    "https://claude.com/cai/oauth/authorize?code=true",
    "Accessing untrusted files may pose security risks",
    "No, disable external imports",
])
def test_detect_onboarding_true(pane_text):
    assert probe.detect_onboarding(pane_text)


def test_detect_onboarding_false_on_task_output():
    assert not probe.detect_onboarding("PERM_PROBE_OK_abc123\ndone")


# --- detect_trust_dialog (folder-trust "quick safety check" dialog) -------
# Kept separate from detect_onboarding: `_dismiss_onboarding` sends a blind
# Enter for an onboarding match, and Enter on this dialog accepts its
# observed live default, "No, exit" -- so it must be detected and NEVER
# auto-dismissed.

@pytest.mark.parametrize("pane_text", [
    "Quick safety check: Is this a project you created or one you trust?",
    "  > 1. Yes, I trust this folder\n    2. No, exit",
    "Do you trust the files in this folder?",
    "Is this a project you created or one you trust?",
])
def test_detect_trust_dialog_true(pane_text):
    assert probe.detect_trust_dialog(pane_text)


def test_detect_trust_dialog_false_on_task_output():
    assert not probe.detect_trust_dialog("PERM_PROBE_OK_abc123\ndone")


def test_detect_trust_dialog_false_on_ordinary_onboarding():
    assert not probe.detect_trust_dialog("Choose the text style that looks best with your terminal")


# --- scratch hook / settings builders (file writes only, no subprocess) --

def test_decision_hook_script_writes_executable_ask(tmp_path):
    witness = tmp_path / "witness.log"
    path = probe._decision_hook_script(tmp_path, "ask", witness)
    assert path.exists()
    assert path.stat().st_mode & 0o111  # executable bit set
    content = path.read_text()
    assert "'ask'" in content


def test_decision_hook_script_writes_executable_deny(tmp_path):
    witness = tmp_path / "witness.log"
    path = probe._decision_hook_script(tmp_path, "deny", witness)
    content = path.read_text()
    assert "'deny'" in content


def test_decision_hook_script_references_witness_path(tmp_path):
    witness = tmp_path / "witness.log"
    path = probe._decision_hook_script(tmp_path, "deny", witness)
    assert str(witness) in path.read_text()


def test_settings_with_hook_shape(tmp_path):
    witness = tmp_path / "witness.log"
    hook_path = probe._decision_hook_script(tmp_path, "deny", witness)
    settings = probe._settings_with_hook(hook_path)
    pretooluse = settings["hooks"]["PreToolUse"]
    assert pretooluse[0]["matcher"] == "Bash"
    assert pretooluse[0]["hooks"][0]["command"] == str(hook_path)
    assert "permissions" not in settings


def test_settings_with_hook_includes_extra_perms(tmp_path):
    witness = tmp_path / "witness.log"
    hook_path = probe._decision_hook_script(tmp_path, "ask", witness)
    settings = probe._settings_with_hook(hook_path, extra_perms={"deny": ["Edit(//x/**)"]})
    assert settings["permissions"] == {"deny": ["Edit(//x/**)"]}


# --- _hook_fired_for_bash (the witness rule) ------------------------------
# A cell counts as observed only if the witness shows the hook fired for a
# Bash call -- independent of what decision it returned, and independent of
# whether the sentinel text ever appears anywhere (a session that never
# reached a tool-use attempt has no sentinel and no prompt either, and must
# still be classified not-observed rather than "sentinel_ran=no").

def test_hook_fired_for_bash_false_when_witness_missing(tmp_path):
    assert not probe._hook_fired_for_bash(tmp_path / "never-written.log")


def test_hook_fired_for_bash_false_when_witness_empty(tmp_path):
    witness = tmp_path / "witness.log"
    witness.write_text("", encoding="utf-8")
    assert not probe._hook_fired_for_bash(witness)


def test_hook_fired_for_bash_false_when_only_non_bash_entries(tmp_path):
    witness = tmp_path / "witness.log"
    witness.write_text(json.dumps({"tool_name": "Read", "decision": "allow"}) + "\n", encoding="utf-8")
    assert not probe._hook_fired_for_bash(witness)


def test_hook_fired_for_bash_true_when_bash_entry_present(tmp_path):
    witness = tmp_path / "witness.log"
    witness.write_text(
        json.dumps({"tool_name": "Read", "decision": "allow"}) + "\n"
        + json.dumps({"tool_name": "Bash", "decision": "deny"}) + "\n",
        encoding="utf-8",
    )
    assert probe._hook_fired_for_bash(witness)


def test_hook_fired_for_bash_skips_unparseable_lines(tmp_path):
    witness = tmp_path / "witness.log"
    witness.write_text("not json\n" + json.dumps({"tool_name": "Bash"}) + "\n", encoding="utf-8")
    assert probe._hook_fired_for_bash(witness)


# --- _tail_evidence ---------------------------------------------------------

def test_tail_evidence_empty_text():
    assert probe._tail_evidence("") == "(empty)"


def test_tail_evidence_truncates_and_flattens_newlines():
    text = "line one\nline two\nline three"
    result = probe._tail_evidence(text, max_chars=1000)
    assert "\n" not in result
    assert "line three" in result


# --- _not_observed_result --------------------------------------------------

def test_not_observed_result_all_none_fields():
    r = probe._not_observed_result("cell:x", "auto", "deny", False, "some reason")
    assert r.effective_mode is None
    assert r.sentinel_ran is None
    assert r.prompt_shown is None
    assert r.notes == "some reason"


# --- _build_interactive_cell_result (pure decision logic) -----------------
# Synthetic coverage of the two not-observed paths the fix-round-2 brief
# requires: a pane stuck at the trust dialog, and a witness showing the hook
# never fired -- both must classify not-observed even with no sentinel and
# no prompt text anywhere in the pane.

def test_interactive_result_trust_blocked_is_not_observed():
    pane = "Quick safety check: Is this a project you created or one you trust?"
    r = probe._build_interactive_cell_result("deny", "PERM_PROBE_OK_x", pane, hook_fired=False, trust_blocked=True, timed_out=False)
    assert r.effective_mode is None
    assert r.sentinel_ran is None
    assert r.prompt_shown is None
    assert "trust" in r.notes.lower()


def test_interactive_result_hook_not_fired_is_not_observed_even_with_no_pane_evidence():
    r = probe._build_interactive_cell_result("ask", "PERM_PROBE_OK_x", "", hook_fired=False, trust_blocked=False, timed_out=False)
    assert r.effective_mode is None
    assert r.sentinel_ran is None
    assert r.prompt_shown is None
    assert "hook never fired" in r.notes


def test_interactive_result_hook_fired_reports_observed_fields():
    marker = "PERM_PROBE_OK_x"
    pane = f"some output\n{marker}\ndone"
    r = probe._build_interactive_cell_result("deny", marker, pane, hook_fired=True, trust_blocked=False, timed_out=False)
    assert r.effective_mode == "auto"
    assert r.sentinel_ran is True
    assert r.notes.startswith("hook fired")


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


# --- build_interactive_inner_cmd (ambient interactive command shape) -----

def test_interactive_inner_cmd_has_no_claude_config_dir():
    # The interactive cells run ambient on purpose (see module docstring) --
    # no CLAUDE_CONFIG_DIR override should ever appear in the inner command.
    cmd = probe.build_interactive_inner_cmd("/tmp/some-cwd", {"hooks": {}})
    assert "CLAUDE_CONFIG_DIR" not in cmd


def test_interactive_inner_cmd_includes_settings_flag():
    settings = {"hooks": {"PreToolUse": []}}
    cmd = probe.build_interactive_inner_cmd("/tmp/some-cwd", settings)
    assert "--settings" in cmd


def test_interactive_inner_cmd_cds_into_given_dir():
    cmd = probe.build_interactive_inner_cmd("/tmp/some-cwd", {})
    assert "cd /tmp/some-cwd" in cmd


def test_interactive_inner_cmd_uses_auto_mode():
    cmd = probe.build_interactive_inner_cmd("/tmp/some-cwd", {})
    assert "--permission-mode auto" in cmd


# --- _dump_raw (--raw-dump-dir default-off) -------------------------------

def test_dump_raw_writes_nothing_when_dir_is_none(tmp_path, monkeypatch):
    # default (no --raw-dump-dir): must not touch the filesystem at all.
    monkeypatch.chdir(tmp_path)
    probe._dump_raw(None, "some-cell", "raw output")
    assert list(tmp_path.iterdir()) == []


def test_dump_raw_writes_file_when_dir_given(tmp_path):
    dump_dir = tmp_path / "dump"
    probe._dump_raw(dump_dir, "some-cell", "raw output")
    written = dump_dir / "some-cell.raw.txt"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "raw output"


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
