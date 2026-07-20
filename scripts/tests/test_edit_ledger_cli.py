"""scripts/edit-ledger.py: read-only by-session / by-file query CLI over the
stage-1 durable ledger.

Covers the CLI in isolation (seeded records, the by-session A/B join, by-file
realpath matching, --json output, absent-ledger tolerance) and a live
end-to-end path: a real PostToolUse Edit payload piped through the actual
hook-scope-track.py, then found by this CLI — exercising the real hook +
CLI wiring together (backs the plan's final_check 2).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from agentctl import edit_ledger

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLI_PATH = SCRIPTS_DIR / "edit-ledger.py"
HOOK_PATH = SCRIPTS_DIR / "hook-scope-track.py"

_SPEC = importlib.util.spec_from_file_location("edit_ledger_cli", str(CLI_PATH))
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)


def _seed(path: Path) -> None:
    edit_ledger.append("subagent-1", "root-A", "/repo/a.py", "Edit", "/repo", 100.0, path=path)
    edit_ledger.append("s2", "root-B", "/repo/b.py", "Write", "/repo", 200.0, path=path)
    edit_ledger.append("s3", "root-B", "/repo/c.py", "Edit", "/repo", 300.0, path=path)


# ---------------------------------------------------------------------------
# by-session / by-file over seeded records (importable main())
# ---------------------------------------------------------------------------

def test_by_session_matches_hook_session_id(tmp_path, capsys):
    path = tmp_path / "edit-log.jsonl"
    _seed(path)
    assert cli.main(["--ledger", str(path), "by-session", "subagent-1"]) == 0
    out = capsys.readouterr().out
    assert "/repo/a.py" in out
    assert "/repo/b.py" not in out


def test_by_session_matches_env_session_id_ab_join(tmp_path, capsys):
    # Querying by the ROOT env session id (root-B) must surface both records
    # made under it, even though their hook-stdin session_id differs (s2/s3).
    path = tmp_path / "edit-log.jsonl"
    _seed(path)
    assert cli.main(["--ledger", str(path), "by-session", "root-B"]) == 0
    out = capsys.readouterr().out
    assert "/repo/b.py" in out
    assert "/repo/c.py" in out
    assert "/repo/a.py" not in out


def test_by_file_matches_realpath(tmp_path, capsys):
    path = tmp_path / "edit-log.jsonl"
    real_target = tmp_path / "target.py"
    real_target.write_text("x", encoding="utf-8")
    edit_ledger.append("s1", "root-A", str(real_target.resolve()), "Edit", str(tmp_path), 50.0, path=path)

    symlink = tmp_path / "alias.py"
    try:
        symlink.symlink_to(real_target)
        query = str(symlink)
    except OSError:
        query = str(real_target)

    assert cli.main(["--ledger", str(path), "by-file", query]) == 0
    out = capsys.readouterr().out
    assert "target.py" in out


def test_json_output_is_parseable(tmp_path, capsys):
    path = tmp_path / "edit-log.jsonl"
    _seed(path)
    assert cli.main(["--ledger", str(path), "--json", "by-session", "subagent-1"]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["file"] == "/repo/a.py"


def test_absent_ledger_yields_no_rows_exit_zero(tmp_path, capsys):
    path = tmp_path / "does-not-exist.jsonl"
    assert cli.main(["--ledger", str(path), "by-session", "anything"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# LIVE end-to-end: real hook fire -> real CLI query
# ---------------------------------------------------------------------------

def test_live_hook_then_cli_end_to_end(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    target = tmp_path / "finalcheck.py"
    target.write_text("x", encoding="utf-8")

    env = {
        **os.environ,
        "AGENTCTL_EDIT_LEDGER": str(ledger_path),
        "CLAUDE_CODE_SESSION_ID": "FINALCHECK-root",
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)

    payload = {
        "tool_name": "Edit",
        "session_id": "FINALCHECK",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(target)},
    }

    hook_result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert hook_result.returncode == 0

    cli_result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--ledger", str(ledger_path), "by-session", "FINALCHECK"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert cli_result.returncode == 0
    assert "finalcheck.py" in cli_result.stdout
