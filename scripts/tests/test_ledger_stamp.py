"""agentctl.edit_ledger.stamp + agentctl.exempt_paths.{scratch_roots,is_ledger_noise}
+ edit-ledger.py's `stamp` subcommand — the stage-1 primitive that lets a
direct-IO canon writer (one that bypasses the Edit/Write hook chokepoint)
record its own attribution, and the scratch-only predicate that decides what
the ledger observes (distinct from is_engine_exempt, which decides what the
engine gate permits).

Every test that drives is_ledger_noise / scratch_roots pins
$AGENTCTL_SCRATCH_ROOTS explicitly rather than relying on the ambient
$TMPDIR — this suite must hold on any machine, not just one where scratch
happens to be /tmp.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentctl import edit_ledger
from agentctl.exempt_paths import is_engine_exempt, is_ledger_noise, scratch_roots
from session_scope import registry

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLI_PATH = SCRIPTS_DIR / "edit-ledger.py"
HOOK_PATH = SCRIPTS_DIR / "hook-scope-track.py"


# ---------------------------------------------------------------------------
# edit_ledger.stamp
# ---------------------------------------------------------------------------

def test_stamp_writes_one_well_formed_row(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    ledger_path = tmp_path / "edit-log.jsonl"
    target = tmp_path / "leaf.md"
    target.write_text("x", encoding="utf-8")

    edit_ledger.stamp(str(target), "record-experience:new", path=ledger_path)

    rows = list(edit_ledger.read_records(ledger_path))
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {"ts", "session_id", "env_session_id", "tool", "file", "cwd"}
    assert row["file"] == str(target.resolve())
    assert row["tool"] == "record-experience:new"
    assert isinstance(row["ts"], float)
    assert row["cwd"] == os.getcwd()


def test_stamp_session_precedence(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    target = tmp_path / "a.md"
    target.write_text("x", encoding="utf-8")

    # explicit session= wins over env for session_id; env_session_id keeps env.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")
    edit_ledger.stamp(str(target), "t", session="explicit-session", path=ledger_path)
    row = list(edit_ledger.read_records(ledger_path))[-1]
    assert row["session_id"] == "explicit-session"
    assert row["env_session_id"] == "env-session"

    # no explicit session -> both ids equal the env value.
    edit_ledger.stamp(str(target), "t", path=ledger_path)
    row = list(edit_ledger.read_records(ledger_path))[-1]
    assert row["session_id"] == "env-session"
    assert row["env_session_id"] == "env-session"

    # neither explicit session nor env -> both "".
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    edit_ledger.stamp(str(target), "t", path=ledger_path)
    row = list(edit_ledger.read_records(ledger_path))[-1]
    assert row["session_id"] == ""
    assert row["env_session_id"] == ""


def test_stamp_never_raises_on_unwritable_ledger(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    unwritable = blocker / "nested" / "edit-log.jsonl"
    target = tmp_path / "a.md"
    target.write_text("x", encoding="utf-8")

    assert edit_ledger.stamp(str(target), "t", path=unwritable) is None  # must not raise


# ---------------------------------------------------------------------------
# scratch_roots() — pure, no filesystem dependence, every branch states the
# environment explicitly rather than inheriting the machine's ambient one.
# ---------------------------------------------------------------------------

def test_scratch_roots_default_includes_tmp(monkeypatch):
    monkeypatch.delenv("AGENTCTL_SCRATCH_ROOTS", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    roots = scratch_roots()
    assert "/tmp" in roots


def test_scratch_roots_includes_tmpdir_and_its_realpath(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_SCRATCH_ROOTS", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    roots = scratch_roots()
    assert "/tmp" in roots
    assert os.path.normpath(str(tmp_path)) in roots
    assert os.path.realpath(str(tmp_path)) in roots


def test_scratch_roots_env_override_replaces_defaults(tmp_path, monkeypatch):
    custom = tmp_path / "custom-scratch"
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(custom))
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    roots = scratch_roots()
    assert "/tmp" not in roots
    assert "/somewhere/else" not in roots
    assert os.path.normpath(str(custom)) in roots


# ---------------------------------------------------------------------------
# is_ledger_noise boundary behaviour — root containment, not substring.
# ---------------------------------------------------------------------------

def test_is_ledger_noise_root_containment_and_prefix_boundary(tmp_path, monkeypatch):
    root = tmp_path / "scratchroot"
    root.mkdir()
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(root))

    assert is_ledger_noise(str(root)) is True
    assert is_ledger_noise(str(root / "x.py")) is True

    evil = Path(str(root) + "-evil")
    assert is_ledger_noise(str(evil / "x.py")) is False

    nested_tmp = tmp_path / "project" / "tmp" / "x.py"
    assert is_ledger_noise(str(nested_tmp)) is False


def test_is_ledger_noise_false_for_memory_paths_which_stay_gate_exempt(tmp_path, monkeypatch):
    root = tmp_path / "scratchroot"
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(root))

    for frag in ("memory-global", "memory", "agent-memory"):
        p = str(tmp_path / frag / "leaves" / "x.md")
        assert is_ledger_noise(p) is False
        assert is_engine_exempt(p) is True


# ---------------------------------------------------------------------------
# edit-ledger.py `stamp` subcommand — real subprocess, no mocking.
# ---------------------------------------------------------------------------

def test_cli_stamp_writes_a_row(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    target = tmp_path / "leaf.md"
    target.write_text("x", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "--ledger", str(ledger_path),
         "stamp", "--file", str(target), "--tool", "script:apply-settings"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0

    rows = list(edit_ledger.read_records(ledger_path))
    assert len(rows) == 1
    assert rows[0]["file"] == str(target.resolve())
    assert rows[0]["tool"] == "script:apply-settings"


def test_cli_stamp_exits_zero_on_unwritable_ledger(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    unwritable = blocker / "nested" / "edit-log.jsonl"
    target = tmp_path / "leaf.md"
    target.write_text("x", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "--ledger", str(unwritable),
         "stamp", "--file", str(target), "--tool", "script:apply-settings"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Coverage regression: the real hook now ledgers a memory write while the
# registry's touched-scope filter is unchanged. This is the control the whole
# stage exists for. Driven via subprocess (not in-process monkeypatch of
# registry.DEFAULT_SCOPES_DIR) because record_touch/set_context/heartbeat bind
# their scopes_dir default at module-def time, so a post-import monkeypatch of
# the module attribute never reaches them — mirrors test_hook_scope_track.py's
# run_hook technique, the only reliable way to redirect where they write.
# ---------------------------------------------------------------------------

def _edit_payload(session_id: str, cwd: str, file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": {"file_path": file_path},
    }


def _run_hook(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_hook_now_ledgers_memory_write_while_touched_paths_stays_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    mem_dir = repo / "memory-global" / "leaves"
    mem_dir.mkdir(parents=True)
    mem_file = mem_dir / "note.md"
    mem_file.write_text("x", encoding="utf-8")

    ledger_path = tmp_path / "edit-log.jsonl"
    scratch = tmp_path / "scratchroot"
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",  # no git/arc stub needed: repo isn't a VCS root either way
        "AGENTCTL_EDIT_LEDGER": str(ledger_path),
        "AGENTCTL_SCRATCH_ROOTS": str(scratch),
        "CLAUDE_CODE_SESSION_ID": "root-session",
    }

    proc = _run_hook(_edit_payload("s1", str(repo), str(mem_file)), env)
    assert proc.returncode == 0

    rows = list(edit_ledger.read_records(ledger_path))
    assert len(rows) == 1
    assert rows[0]["file"] == str(mem_file.resolve())
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["env_session_id"] == "root-session"

    rec = registry.load(home / ".claude" / "agentctl" / "scopes", "s1")
    assert rec is not None
    assert rec.touched_paths == []

    # a scratch path still produces no ledger row, under the same pinned root.
    scratch.mkdir()
    scratch_file = scratch / "x.py"
    scratch_file.write_text("x", encoding="utf-8")
    proc2 = _run_hook(_edit_payload("s1", str(repo), str(scratch_file)), env)
    assert proc2.returncode == 0
    assert len(list(edit_ledger.read_records(ledger_path))) == 1
