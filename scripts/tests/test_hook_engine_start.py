"""hook-engine-start.py: UserPromptSubmit nudge that steers the coordinator onto
the agentctl spine (start/classify when idle, reset when the prior task is closed,
a node-derived status hint when live). importlib-loads the hook module by path and
points config_root's env-driven resolution (CLAUDE_AGENT_HOME + HOME) at a tmp
tree (mirrors test_hook_resolution_state.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-engine-start.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_engine_start", HOOK_SCRIPT)
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


def _write_state(state_dir: Path, session_id: str, **fields) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps(fields), encoding="utf-8")


def test_no_state_emits_start_line(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    msg = mod.build_message("sess-new")
    assert "sess-new" in msg
    assert "--if-absent" in msg


def test_live_substantive_plan_ready_status_hint(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-live", node="PLAN_READY", weight_class="SUBSTANTIVE",
                 task_id="demo")
    msg = mod.build_message("sess-live")
    assert "PLAN_READY" in msg
    assert "approve" in msg.lower()


def test_resolved_prior_emits_reset(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-done", node="RESOLVED", weight_class="SUBSTANTIVE",
                 task_id="demo")
    msg = mod.build_message("sess-done")
    assert "reset" in msg.lower()


def test_chat_routed_emits_reset(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, "sess-chat", node="ROUTED", weight_class="CHAT", task_id="demo")
    msg = mod.build_message("sess-chat")
    assert "reset" in msg.lower()


def test_corrupt_state_behaves_as_no_state(tmp_path, monkeypatch):
    mod = _load_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sess-bad.json").write_text("{not valid json", encoding="utf-8")
    msg = mod.build_message("sess-bad")
    assert "--if-absent" in msg  # falls back to the start line, no crash


def test_main_full_stdin_to_stdout(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": "s-x", "prompt": "build a thing"})
    ))
    rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "s-x" in out


def test_main_empty_prompt_silent(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": "s-x", "prompt": "   "})
    ))
    rc = mod.main()
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
