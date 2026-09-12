"""Regression test for the baa1daea cross-session reminder bug (item I).

The hook hook-engine-start.py used config_root.resolve_agentctl_state_file(),
which falls back to the legacy root (~/.claude/agentctl/state/) when the current
root has no state for a session. A pre-migration state file in the legacy root
with task_id="max-32c56a7f-..." was surfaced as "Live session: task=max-32c56a7f-..."
in an unrelated conversation whose session_id matched the legacy file's name.

Fix: _load_state() now only checks agentctl_state_dir() (current root), never the
legacy fallback.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-engine-start.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "baa1daea-cross-session-state.json"

_CROSS_SESSION_TASK_ID = "max-32c56a7f-8da1-43aa-9b29-4dea183b009d"
_SESSION_ID = "baa1daea-e560-4133-8af3-6cae096d0d92"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_engine_start", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _point_roots_at(monkeypatch, tmp_path: Path):
    """Redirect current and legacy roots into tmp so no real state leaks in."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    current_state_dir = tmp_path / "root" / "agentctl" / "state"
    legacy_state_dir = tmp_path / "home" / ".claude" / "agentctl" / "state"
    return current_state_dir, legacy_state_dir


def test_cross_session_state_fixture_is_valid():
    """The fixture is well-formed JSON with the task_id that caused the bug."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["task_id"] == _CROSS_SESSION_TASK_ID
    assert data["session_id"] == _SESSION_ID
    assert data["node"] != "RESOLVED"


def test_baa1daea_legacy_state_not_surfaced_in_current_session(tmp_path, monkeypatch):
    """Legacy-root state for the session_id must NOT produce a 'Live session' message.

    Scenario: pre-migration state with task_id=max-32c56a7f-... lives in the legacy
    root; the current root has no state. The hook should emit the 'No agentctl session'
    start-steering message, not 'Live session: task=max-32c56a7f-...'.
    """
    mod = _load_module()
    current_state_dir, legacy_state_dir = _point_roots_at(monkeypatch, tmp_path)

    # Write the cross-session state ONLY in the legacy root, not the current root.
    legacy_state_dir.mkdir(parents=True, exist_ok=True)
    fixture_data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    (legacy_state_dir / f"{_SESSION_ID}.json").write_text(
        json.dumps(fixture_data), encoding="utf-8"
    )
    assert not (current_state_dir / f"{_SESSION_ID}.json").exists()

    msg = mod.build_message(_SESSION_ID)

    assert _CROSS_SESSION_TASK_ID not in msg, (
        f"Hook surfaced cross-session task_id {_CROSS_SESSION_TASK_ID!r} from legacy root; "
        f"full message: {msg!r}"
    )
    assert "Live session" not in msg, (
        f"Hook emitted 'Live session' from a legacy-only state; full message: {msg!r}"
    )
    assert "--if-absent" in msg, (
        f"Expected the start-steering message (--if-absent) for a session with no "
        f"current-root state; full message: {msg!r}"
    )


def test_current_root_state_still_surfaces_live_session(tmp_path, monkeypatch):
    """A state file in the CURRENT root must still produce the normal 'Live session' msg."""
    mod = _load_module()
    current_state_dir, _ = _point_roots_at(monkeypatch, tmp_path)
    current_state_dir.mkdir(parents=True, exist_ok=True)
    (current_state_dir / f"{_SESSION_ID}.json").write_text(
        json.dumps({
            "session_id": _SESSION_ID,
            "task_id": "current-task-slug",
            "node": "EXECUTING",
            "weight_class": "SUBSTANTIVE",
        }),
        encoding="utf-8",
    )

    msg = mod.build_message(_SESSION_ID)
    assert "Live session" in msg
    assert "current-task-slug" in msg
