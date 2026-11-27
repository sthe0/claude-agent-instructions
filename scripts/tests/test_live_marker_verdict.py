"""The live:core-marker verdict mapping (R3): the CLI's InstructionsLoaded load
record decides alone, the structured reply is a recorded diagnostic that never
gates, and no non-PASS evidence is lost or leaked. Synthetic inputs only — the
live launches live in smoke_marker_control.py and the stage's evidence JSON."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIVE = REPO / "scripts" / "instruction-sandbox-live.py"


def _load_live():
    spec = importlib.util.spec_from_file_location("isb_live_verdict", LIVE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["isb_live_verdict"] = mod  # the dataclasses need the module resolvable
    spec.loader.exec_module(mod)
    return mod


def _reader(files: dict):
    return lambda path: files.get(path)


def _record(*paths: str) -> str:
    return "".join(
        json.dumps({"hook_event_name": "InstructionsLoaded", "file_path": path,
                   "load_reason": "include"}) + "\n"
        for path in paths)


CLAUDE_MD = "/sbx/home/.claude-agent/CLAUDE.md"
CONFIG_MD = "/sbx/home/.claude-agent/config.md"
TOKEN = "tok-abc"


def test_marker_file_loaded_passes():
    live = _load_live()
    read = _reader({
        CLAUDE_MD: "instructions\n@~/.claude-agent/config.md\n",
        CONFIG_MD: f"head\n\nSANDBOX-MARKER: {TOKEN}\n",
    })
    paths = live.parse_load_record(_record(CLAUDE_MD, CONFIG_MD))
    outcome = live.marker_verdict(paths, (TOKEN,), True, {"marker": TOKEN}, "", read)
    assert outcome.verdict == "PASS" and outcome.reason_code == ""
    assert outcome.load_record == [CLAUDE_MD, CONFIG_MD]
    assert outcome.missing == () and outcome.reply_match == {TOKEN: "match"}


def test_marker_file_absent_is_marker_missing():
    live = _load_live()
    # The load record names CLAUDE.md but not the marker file (a broken import);
    # the reply carrying the token must not rescue the FAIL.
    read = _reader({CLAUDE_MD: "instructions without the marker\n"})
    paths = live.parse_load_record(_record(CLAUDE_MD))
    outcome = live.marker_verdict(paths, (TOKEN,), True, {"marker": TOKEN}, "", read)
    assert outcome.verdict == "FAIL" and outcome.reason_code == "marker-missing"
    assert outcome.missing == (TOKEN,) and outcome.reply_match == {TOKEN: "match"}


def test_completed_session_without_load_record_is_load_record_missing():
    live = _load_live()
    read = _reader({})
    # No record at all, and a record that exists but names no file both count
    # as no record: the session completed, so the missing record is the FAIL.
    for paths in ([], live.parse_load_record(_record())):
        outcome = live.marker_verdict(paths, (TOKEN,), True, {"marker": "no marker"}, "", read)
        assert outcome.verdict == "FAIL" and outcome.reason_code == "load-record-missing"
        assert outcome.load_record == []
    # A record of junk lines names no file either.
    assert live.parse_load_record("not json\n{}\n") == []


def test_launch_error_without_load_record_is_unavailable():
    live = _load_live()
    read = _reader({})
    for reason in ("timeout", "launch-error", "launch-exit=7 warming up"):
        outcome = live.marker_verdict([], (TOKEN,), False, None, reason, read)
        assert outcome.verdict == "UNAVAILABLE"
        assert outcome.reason_code == reason.split()[0], reason


def test_load_record_decides_despite_cli_error():
    live = _load_live()
    read = _reader({CONFIG_MD: f"SANDBOX-MARKER: {TOKEN}\n"})
    paths = live.parse_load_record(_record(CONFIG_MD))
    # A present record decides whatever the CLI exit says: PASS with the token
    # in a loaded file, FAIL marker-missing without it — never UNAVAILABLE.
    for completed in (True, False):
        outcome = live.marker_verdict(paths, (TOKEN,), completed, None,
                                       "launch-exit=7 warming up", read)
        assert outcome.verdict == "PASS", completed
    read_empty = _reader({CLAUDE_MD: "no marker here\n"})
    paths_without = live.parse_load_record(_record(CLAUDE_MD))
    outcome = live.marker_verdict(paths_without, (TOKEN,), False, None,
                                  "launch-exit=7 warming up", read_empty)
    assert outcome.verdict == "FAIL" and outcome.reason_code == "marker-missing"


def test_reply_token_wrapped_in_text_is_diagnostic_only():
    live = _load_live()
    read = _reader({CONFIG_MD: f"SANDBOX-MARKER: {TOKEN}\n"})
    paths = live.parse_load_record(_record(CONFIG_MD))
    wrapped = {"marker": f"The marker reads SANDBOX-MARKER: {TOKEN} today"}
    outcome = live.marker_verdict(paths, (TOKEN,), True, wrapped, "", read)
    assert outcome.verdict == "PASS"  # the record, not the reply, decides
    assert outcome.reply_match == {TOKEN: "wrapped"}


def test_reply_no_token_is_diagnostic_only():
    live = _load_live()
    read = _reader({CONFIG_MD: f"SANDBOX-MARKER: {TOKEN}\n"})
    paths = live.parse_load_record(_record(CONFIG_MD))
    for result in ({"marker": "I cannot see any marker"}, {"marker": ""},
                   {"other": TOKEN}, {"marker": 7}, "plain reply", None):
        outcome = live.marker_verdict(paths, (TOKEN,), True, result, "", read)
        assert outcome.verdict == "PASS", result
        assert outcome.reply_match[TOKEN] in ("no-token", "no-field"), result


def test_non_pass_keeps_bounded_redacted_reply():
    live = _load_live()
    read = _reader({})
    secret = "sk-ant-oat01-abcdefghijklmnop0123456789"
    long_result = {"marker": f"{secret} {'x' * 600} {TOKEN} tail"}
    outcome = live.marker_verdict([], (TOKEN,), True, long_result, "", read)
    assert outcome.verdict == "FAIL" and outcome.reason_code == "load-record-missing"
    excerpt = outcome.reply_excerpt
    assert TOKEN not in excerpt and "<MARKER>" in excerpt
    assert secret not in excerpt and "[redacted]" in excerpt
    assert len(excerpt.encode("utf-8")) <= live.REPLY_EXCERPT_LIMIT
    assert outcome.reply_excerpt  # a non-PASS run keeps a non-empty reply


def test_argv_requests_json_schema_and_load_hook(tmp_path):
    live = _load_live()
    argv = live.build_argv("haiku")
    assert "--output-format" in argv and "json" in argv
    assert "--json-schema" in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["marker"]
    assert schema["properties"]["marker"] == {"type": "string"}
    assert argv[argv.index("--tools") + 1] == ""
    # The hook the verdict rests on is registered in the sandbox settings and
    # only there — the record path lives in the hook command, not the child env.
    root = tmp_path / "sbx"
    settings = root / "home" / ".claude-agent" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": []}]}}))
    record_file = root / live.LOAD_RECORD_NAME
    live.register_load_hook(root, record_file)
    live.register_load_hook(root, record_file)  # idempotent
    hooks = json.loads(settings.read_text())["hooks"]
    assert len(hooks["InstructionsLoaded"]) == 1
    assert hooks["UserPromptSubmit"] == [{"hooks": []}]
    command = hooks["InstructionsLoaded"][0]["hooks"][0]["command"]
    assert command == f"cat >> {record_file}"
    assert live.parse_load_record(_record(CONFIG_MD)) == [CONFIG_MD]