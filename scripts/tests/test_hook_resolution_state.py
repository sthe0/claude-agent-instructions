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


def _write_state(state_dir: Path, session_id: str, node: str, resolution_passed: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "node": node,
        "resolution": {"passed": resolution_passed},
    }
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


def test_gate_open_when_resolution_node_not_passed(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = tmp_path / "state"
    _write_state(state_dir, "sess-res", "RESOLUTION", resolution_passed=False)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    assert mod.resolution_gate_open("sess-res") is True


def test_gate_closed_when_no_state_file(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    assert mod.resolution_gate_open("nonexistent") is False


def test_gate_closed_when_resolution_already_passed(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = tmp_path / "state"
    _write_state(state_dir, "sess-done", "RESOLUTION", resolution_passed=True)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    assert mod.resolution_gate_open("sess-done") is False


def test_gate_closed_when_node_is_not_resolution(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = tmp_path / "state"
    _write_state(state_dir, "sess-exec", "EXECUTING", resolution_passed=False)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    assert mod.resolution_gate_open("sess-exec") is False
