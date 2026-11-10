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


def _autostart_task(mod, monkeypatch, session_id: str, prompt: str) -> str:
    seen: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: seen.append(argv) or _Done())
    assert mod._autostart(session_id, prompt)
    argv = seen[0]
    return argv[argv.index("--task") + 1]


def test_a_spawned_specialist_session_gets_a_task_id_of_its_own(monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    task = _autostart_task(
        mod, monkeypatch, "322a4d32-932d-4fe3-a363-3d762229d382", "Agent recursion depth 1 working plan"
    )
    assert task == "agent-recursion-depth-1-working-plan-322a4d32"


def test_a_root_session_keeps_the_prompt_slug_task_id(monkeypatch):
    mod = _load_module()
    for depth in (None, "", "0", "x1"):
        if depth is None:
            monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
        else:
            monkeypatch.setenv("AGENT_RECURSION_DEPTH", depth)
        task = _autostart_task(mod, monkeypatch, "322a4d32-932d", "Agent recursion depth 1 working plan")
        assert task == "agent-recursion-depth-1-working-plan", depth


def test_two_spawned_sessions_with_one_prompt_do_not_share_an_accumulator(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "2")
    first = _autostart_task(mod, monkeypatch, "aaaaaaaa-1111", "same prompt")
    second = _autostart_task(mod, monkeypatch, "bbbbbbbb-2222", "same prompt")
    assert first != second
    monkeypatch.setattr(mod, "_autostart", lambda sid, prompt: True)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": "cccccccc-3333", "prompt": "same prompt"})
    ))
    assert mod.main() == 0
    assert "task=same-prompt-cccccccc" in capsys.readouterr().out


def _prompt(monkeypatch, session_id: str, prompt: str) -> None:
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": session_id, "prompt": prompt})
    ))


def test_a_linked_child_whose_state_holds_the_spawners_norm_handoff_gets_a_task_id_of_its_own(
        monkeypatch, tmp_path):
    from agentctl import norm_handoff
    from agentctl.plan import load_plan
    from agentctl.stage_norm import StageNorm
    from conftest import spawn_handoff

    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    doc = load_plan(str(Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"))
    record = norm_handoff.build_record(
        parent_session="parent-1", stage_index=1, kind="developer",
        norm=StageNorm.from_stage(doc.stages[0], {}, doc=doc), grants=[], depth=1, parent_lineage=None,
    )
    session_id = "dddddddd-4444-4444-4444-444444444444"
    monkeypatch.setenv(norm_handoff.NORM_HANDOFF_ENV, spawn_handoff(state_dir, record, session_id))
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    real_run = mod.subprocess.run
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: real_run(argv, **{**kw, "timeout": 120}))
    _prompt(monkeypatch, session_id, "same prompt")
    assert mod.main() == 0
    state = json.loads((state_dir / f"{session_id}.json").read_text(encoding="utf-8"))
    assert state["task_id"] == "same-prompt-dddddddd"
    events = [entry.get("event") for entry in state["history"]]
    assert events.index("norm_handoff") < events.index("start")


def test_a_spawned_session_already_holding_a_start_event_keeps_its_task_id(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    session_id = "eeeeeeee-5555"
    _write_state(state_dir, session_id, node="EXECUTING", weight_class="SMALL_CHANGE",
                 task_id="spawner-task", history=[{"event": "start"}])

    def no_autostart(sid, prompt):
        raise AssertionError("a started session must not be started again")

    monkeypatch.setattr(mod, "_autostart", no_autostart)
    _prompt(monkeypatch, session_id, "same prompt")
    assert mod.main() == 0
    assert json.loads((state_dir / f"{session_id}.json").read_text(encoding="utf-8"))["task_id"] == "spawner-task"
    assert "task=spawner-task" in capsys.readouterr().out
