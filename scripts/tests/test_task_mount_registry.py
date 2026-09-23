"""Persisted ticket -> mount-name binding (task_mount_registry.py).

Covers the difficulty enter-task.sh's --key case now closes: a title change on
an already-mounted ticket must not change the mount name a second `--key` call
resolves to.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from project_entry.task_mount_registry import _default_root, get_name, set_name

SCRIPT = Path(__file__).resolve().parents[1] / "project_entry" / "task_mount_registry.py"


def _fake_fs(tmp_path: Path):
    def read(path: str) -> "str | None":
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write(path: str, text: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    return read, write


# ── pure get_name/set_name ──────────────────────────────────────────────────

def test_unbound_key_returns_none(tmp_path):
    read, _write = _fake_fs(tmp_path)
    assert get_name(str(tmp_path), "PROJ-440", read) is None


def test_set_then_get_round_trips(tmp_path):
    read, write = _fake_fs(tmp_path)
    path = set_name(str(tmp_path), "PROJ-440", "PROJ-440-judge-calibration", write)
    assert Path(path).is_file()
    assert get_name(str(tmp_path), "PROJ-440", read) == "PROJ-440-judge-calibration"


def test_second_set_overwrites(tmp_path):
    read, write = _fake_fs(tmp_path)
    set_name(str(tmp_path), "PROJ-440", "first-name", write)
    set_name(str(tmp_path), "PROJ-440", "second-name", write)
    assert get_name(str(tmp_path), "PROJ-440", read) == "second-name"


def test_title_drift_does_not_change_a_bound_name(tmp_path):
    """The exact scenario this module exists to close: derive+persist once,
    then a later resolve for the SAME ticket key ignores a new slug entirely
    (the caller in enter-task.sh never re-derives once a binding exists)."""
    read, write = _fake_fs(tmp_path)
    set_name(str(tmp_path), "PROJ-440", "PROJ-440-judge-calibration", write)
    # A title-driven re-derivation would have produced a different slug here;
    # get_name must still return the originally bound name.
    assert get_name(str(tmp_path), "PROJ-440", read) == "PROJ-440-judge-calibration"


def test_malformed_record_is_treated_as_unbound(tmp_path):
    read, _write = _fake_fs(tmp_path)
    (tmp_path / "PROJ-440.json").write_text("not json", encoding="utf-8")
    assert get_name(str(tmp_path), "PROJ-440", read) is None


def test_key_is_sanitized_into_a_safe_filename(tmp_path):
    read, write = _fake_fs(tmp_path)
    set_name(str(tmp_path), "weird/key with spaces", "some-name", write)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name
    assert get_name(str(tmp_path), "weird/key with spaces", read) == "some-name"


# ── _default_root env resolution ────────────────────────────────────────────

def test_default_root_explicit_override_wins(tmp_path):
    got = _default_root({"CLAUDE_TASK_MOUNTS_DIR": "/custom/root", "HOME": str(tmp_path)}.get)
    assert got == "/custom/root"


def test_default_root_falls_back_to_agent_home(tmp_path):
    got = _default_root({"CLAUDE_AGENT_HOME": str(tmp_path / "cfg"), "HOME": str(tmp_path)}.get)
    assert got == str(tmp_path / "cfg" / "task-mounts.d")


def test_default_root_falls_back_to_isolated_home(tmp_path):
    (tmp_path / ".claude-agent").mkdir()
    got = _default_root({"HOME": str(tmp_path)}.get)
    assert got == str(tmp_path / ".claude-agent" / "task-mounts.d")


def test_default_root_falls_back_to_legacy_home(tmp_path):
    got = _default_root({"HOME": str(tmp_path)}.get)
    assert got == str(tmp_path / ".claude" / "task-mounts.d")


# ── CLI (get/set subcommands) ────────────────────────────────────────────────

def _run(*args: str, env: "dict[str, str]") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


def test_cli_get_missing_key_exits_1(tmp_path):
    r = _run("get", str(tmp_path), "PROJ-440", env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 1
    assert r.stdout == ""


def test_cli_set_then_get_round_trips(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    r_set = _run("set", str(tmp_path), "PROJ-440", "PROJ-440-judge-calibration", env=env)
    assert r_set.returncode == 0
    assert r_set.stdout.strip().endswith("PROJ-440.json")

    r_get = _run("get", str(tmp_path), "PROJ-440", env=env)
    assert r_get.returncode == 0
    assert r_get.stdout.strip() == "PROJ-440-judge-calibration"


def test_cli_unknown_command_exits_2(tmp_path):
    r = _run("bogus", env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2
