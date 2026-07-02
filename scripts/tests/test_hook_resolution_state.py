"""hook-resolution-reminder.py: state-aware resolution gate nudge."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-resolution-reminder.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_resolution_reminder", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _point_roots_at(monkeypatch, tmp_path: Path) -> Path:
    """The hook resolves its state file via config_root at call time from env:
    CLAUDE_AGENT_HOME is the current root, HOME the legacy fallback — point
    both into tmp so no real machine state leaks in. Returns the current
    root's state dir."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "root" / "agentctl" / "state"


def _write_state(state_dir: Path, session_id: str, node: str, resolution_passed: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "node": node,
        "resolution": {"passed": resolution_passed},
    }
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


def test_gate_open_when_resolution_node_not_passed(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-res", "RESOLUTION", resolution_passed=False)
    assert mod.resolution_gate_open("sess-res") is True


def test_gate_closed_when_no_state_file(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    assert mod.resolution_gate_open("nonexistent") is False


def test_gate_closed_when_resolution_already_passed(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-done", "RESOLUTION", resolution_passed=True)
    assert mod.resolution_gate_open("sess-done") is False


def test_gate_closed_when_node_is_not_resolution(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-exec", "EXECUTING", resolution_passed=False)
    assert mod.resolution_gate_open("sess-exec") is False


def test_gate_open_via_legacy_root_fallback(tmp_path, monkeypatch):
    """A session whose state predates migration lives only under ~/.claude —
    the gate must still find it there (fail closed on a half-migrated machine)."""
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    legacy_state = tmp_path / "home" / ".claude" / "agentctl" / "state"
    _write_state(legacy_state, "sess-old", "RESOLUTION", resolution_passed=False)
    assert mod.resolution_gate_open("sess-old") is True
