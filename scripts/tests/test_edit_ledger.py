"""agentctl.edit_ledger: append-only writer/reader + hook-scope-track.py wiring.

Covers the durable session->edit ledger in isolation (append/read roundtrip,
malformed-line skipping, never-raises-on-unwritable-path) and the A/B join it
exists to serve: a real hook fire records BOTH the hook-stdin session_id (the
actual editing agent) and the env CLAUDE_CODE_SESSION_ID (the root session a
commit trailer keys on), and an is_engine_exempt path is never ledgered.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

from agentctl import edit_ledger

HOOK = Path(__file__).resolve().parent.parent / "hook-scope-track.py"

_SPEC = importlib.util.spec_from_file_location("hook_scope_track_edl", str(HOOK))
track_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = track_mod
_SPEC.loader.exec_module(track_mod)


# ---------------------------------------------------------------------------
# append / read_records roundtrip
# ---------------------------------------------------------------------------

def test_append_and_read_roundtrip(tmp_path):
    path = tmp_path / "edit-log.jsonl"
    edit_ledger.append("s1", "root-s1", "/repo/a.py", "Edit", "/repo", 123.0, path=path)
    edit_ledger.append("s2", "root-s1", "/repo/b.py", "Write", "/repo", 124.0, path=path)

    rows = list(edit_ledger.read_records(path))
    assert len(rows) == 2
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["env_session_id"] == "root-s1"
    assert rows[0]["file"] == "/repo/a.py"
    assert rows[0]["tool"] == "Edit"
    assert rows[0]["cwd"] == "/repo"
    assert rows[0]["ts"] == 123.0
    assert rows[1]["session_id"] == "s2"
    assert rows[1]["env_session_id"] == "root-s1"


def test_read_records_skips_malformed_lines(tmp_path):
    path = tmp_path / "edit-log.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"session_id": "s1", "env_session_id": "s1", "file": "/a", "tool": "Edit", "cwd": "/", "ts": 1.0})
        + "\n"
        + "\n"
        + "[1, 2, 3]\n",
        encoding="utf-8",
    )
    rows = list(edit_ledger.read_records(path))
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"


def test_read_records_missing_file_yields_nothing(tmp_path):
    assert list(edit_ledger.read_records(tmp_path / "nonexistent.jsonl")) == []


def test_append_never_raises_on_unwritable_path(tmp_path):
    # A regular file used as the "directory" component makes mkdir(parents=True)
    # raise inside append(); the call must swallow it, not propagate.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    unwritable = blocker / "nested" / "edit-log.jsonl"
    edit_ledger.append("s1", "s1", "/a", "Edit", "/", 1.0, path=unwritable)  # must not raise


# ---------------------------------------------------------------------------
# hook-scope-track.py wiring: the A/B join + exempt-path filter
# ---------------------------------------------------------------------------

def _payload(session_id: str, cwd: str, file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": {"file_path": file_path},
    }


def test_track_ledgers_both_session_ids_when_they_diverge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(track_mod.registry, "DEFAULT_SCOPES_DIR", home / ".claude" / "agentctl" / "scopes")
    monkeypatch.setattr(track_mod.registry, "resolve_repo_root_vcs", lambda cwd: (None, "none"), raising=False)
    monkeypatch.setattr(track_mod, "resolve_repo_root_vcs", lambda cwd: (None, "none"))
    monkeypatch.setattr(track_mod, "session_pid", lambda: None)

    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "root-session")

    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "a.py"
    target.write_text("x", encoding="utf-8")

    track_mod.track(_payload("subagent-session", str(repo), str(target)), time.time())

    rows = list(edit_ledger.read_records(ledger_path))
    assert len(rows) == 1
    assert rows[0]["session_id"] == "subagent-session"
    assert rows[0]["env_session_id"] == "root-session"
    assert rows[0]["file"] == str(target.resolve())
    assert rows[0]["tool"] == "Edit"


def test_track_does_not_ledger_exempt_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(track_mod.registry, "DEFAULT_SCOPES_DIR", home / ".claude" / "agentctl" / "scopes")
    monkeypatch.setattr(track_mod, "resolve_repo_root_vcs", lambda cwd: (None, "none"))
    monkeypatch.setattr(track_mod, "session_pid", lambda: None)

    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "root-session")

    scratch = tmp_path / "tmp" / "scratch.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("x", encoding="utf-8")

    track_mod.track(_payload("s1", str(tmp_path), str(scratch)), time.time())

    assert list(edit_ledger.read_records(ledger_path)) == []
