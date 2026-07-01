#!/usr/bin/env python3
"""Hermetic tests for the mount-hygiene check in hook-tracker-reminder.py.

Covers mount_mismatches() directly (fire + every silent case) and one
end-to-end run of the hook via subprocess with JSON on stdin. Uses a
tmp dir as the task-mount root; touches no real mount. Exit 0 on all-pass;
also importable by pytest (functions are plain asserts).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-tracker-reminder.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_tracker_reminder", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_root(tmp: Path, *mount_names: str) -> Path:
    root = tmp / "task-mounts"
    for name in mount_names:
        (root / name).mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_fires_on_wrong_mount(monkeypatch=None):
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "main", "DEEPAGENT-440-foo")
        _set_root(mod, root)
        out = mod.mount_mismatches(["DEEPAGENT-440"], str(root / "main"))
        assert out == [("DEEPAGENT-440", "main", ["DEEPAGENT-440-foo"])], out


def test_lists_multiple_matching_mounts():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "main", "DEEPAGENT-440-a", "DEEPAGENT-440-b")
        _set_root(mod, root)
        out = mod.mount_mismatches(["DEEPAGENT-440"], str(root / "main" / "sub"))
        assert out == [("DEEPAGENT-440", "main", ["DEEPAGENT-440-a", "DEEPAGENT-440-b"])], out


def test_silent_when_already_in_ticket_mount():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "DEEPAGENT-440-foo")
        _set_root(mod, root)
        out = mod.mount_mismatches(["DEEPAGENT-440"], str(root / "DEEPAGENT-440-foo" / "robot"))
        assert out == [], out


def test_silent_when_no_matching_mount():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "main", "OTHER-1-x")
        _set_root(mod, root)
        out = mod.mount_mismatches(["DEEPAGENT-440"], str(root / "main"))
        assert out == [], out


def test_silent_when_cwd_outside_root():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "main", "DEEPAGENT-440-foo")
        _set_root(mod, root)
        out = mod.mount_mismatches(["DEEPAGENT-440"], str(tmp / "elsewhere" / "repo"))
        assert out == [], out


def test_noop_when_no_root(monkeypatch=None):
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        # Point env at a non-existent root and ensure ~/task-mounts is not it.
        missing = Path(td) / "does-not-exist"
        import os
        os.environ["CLAUDE_TASK_MOUNT_ROOT"] = str(missing)
        try:
            out = mod.mount_mismatches(["DEEPAGENT-440"], str(missing / "main"))
            assert out == [], out
        finally:
            os.environ.pop("CLAUDE_TASK_MOUNT_ROOT", None)


def test_end_to_end_stdout():
    """Full hook run: tracker-reminder line always, mount-check line on mismatch."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _make_root(tmp, "main", "DEEPAGENT-440-foo")
        payload = {"prompt": "continue DEEPAGENT-440 please", "cwd": str(root / "main")}
        env = {"CLAUDE_TASK_MOUNT_ROOT": str(root)}
        import os
        full_env = {**os.environ, **env}
        res = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps(payload), capture_output=True, text=True, env=full_env,
        )
        assert res.returncode == 0, res.returncode
        assert "[tracker-reminder]" in res.stdout, res.stdout
        assert "[mount-check]" in res.stdout, res.stdout
        assert "DEEPAGENT-440-foo" in res.stdout, res.stdout


def _set_root(mod, root: Path) -> None:
    import os
    os.environ["CLAUDE_TASK_MOUNT_ROOT"] = str(root)


def _run_all() -> int:
    import os
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        finally:
            os.environ.pop("CLAUDE_TASK_MOUNT_ROOT", None)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
