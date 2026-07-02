"""hook-resolution-reminder.py: branch-hygiene line wired into the
resolution-gate nudge via land-branch.py --check.

Complements test_hook_resolution_state.py (which covers resolution_gate_open()
in isolation) by exercising main() end to end for the branch-hygiene wiring.
"""
from __future__ import annotations

import importlib.util
import io
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


def _run(monkeypatch, capsys, mod, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out


def _arm_gate(mod, monkeypatch, tmp_path, session_id="sess-res"):
    state_dir = tmp_path / "state"
    _write_state(state_dir, session_id, "RESOLUTION", resolution_passed=False)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    return session_id


def test_gate_open_and_landable_appends_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: mod.BRANCH_HYGIENE_HINT)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT in out


def test_gate_open_and_not_landable_no_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: None)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_gate_open_and_land_branch_check_raises_no_crash(monkeypatch, capsys, tmp_path):
    """landable_branch_hint() itself is the boundary that swallows failures
    (see next test for an end-to-end version); here we simulate a caller-side
    surprise by making it raise, and confirm main() still doesn't crash the
    hook's always-exit-0 contract if that boundary is ever weakened."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)

    def _boom(repo_dir):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(mod, "landable_branch_hint", _boom)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": session_id, "cwd": str(tmp_path)}))
    )

    try:
        rc = mod.main()
    except RuntimeError:
        rc = None
    out = capsys.readouterr().out

    assert rc == 0, "main() must not let a landable_branch_hint failure propagate or skip exit 0"
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_land_branch_check_actually_failing_subprocess_no_branch_line(monkeypatch, capsys, tmp_path):
    """End-to-end (no stubbing of landable_branch_hint itself): point
    LAND_BRANCH_SCRIPT at a missing file so the real subprocess call fails,
    and confirm the hook degrades silently."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "LAND_BRANCH_SCRIPT", tmp_path / "does-not-exist.py")

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_non_gate_gratitude_path_unchanged_no_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    # Even if the check would report LANDABLE, the gratitude fallback path
    # must never invoke it.
    monkeypatch.setattr(
        mod,
        "landable_branch_hint",
        lambda repo_dir: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": "no-state", "prompt": "thanks"})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_gate_closed_ordinary_prompt_no_output(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)

    rc, out = _run(
        monkeypatch, capsys, mod, {"session_id": "no-state", "prompt": "please add a test"}
    )

    assert rc == 0
    assert out == ""
