"""spawn-specialist refuses a forged config root when started directly; the suite's test-time
launcher (spawner_launcher) starts it with that check answered "no refusal", and nothing
committed disables the check."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import spawner_launcher

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
SPECIALIZATIONS = SCRIPT.parent.parent / "skills" / "specializations"
REFUSAL = "refusing to spawn: the child's config root"


def _spawn_dry_run(tmp_path: Path, entry: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "skills").symlink_to(SPECIALIZATIONS)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(forged), "AGENT_RECURSION_DEPTH": "0", **(env_extra or {})}
    env.pop("CLAUDE_AGENT_HOME", None)
    cmd = [
        sys.executable, entry, "--dry-run",
        "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "nothing", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def test_a_direct_spawn_refuses_a_forged_config_root(tmp_path):
    result = _spawn_dry_run(tmp_path, str(SCRIPT))
    assert result.returncode == 2, result.stderr
    assert REFUSAL in result.stderr


def test_a_direct_spawn_with_the_tests_dir_on_pythonpath_refuses_a_forged_config_root(tmp_path):
    result = _spawn_dry_run(tmp_path, str(SCRIPT), {"PYTHONPATH": str(Path(__file__).resolve().parent)})
    assert result.returncode == 2, result.stderr
    assert REFUSAL in result.stderr


def test_a_spawn_through_the_launcher_skips_the_config_root_check(tmp_path):
    result = _spawn_dry_run(tmp_path, spawner_launcher.launcher())
    assert REFUSAL not in result.stderr
    assert result.returncode == 0, result.stderr


def test_the_launcher_exists_only_outside_the_committed_tree():
    assert not Path(spawner_launcher.launcher()).resolve().is_relative_to(SCRIPT.parent.parent)


def test_no_committed_file_disables_the_config_root_check():
    hooks = [
        p for name in ("sitecustomize.py", "usercustomize.py") for p in SCRIPT.parent.rglob(name)
        if spawner_launcher.CHECK_NAME in p.read_text()
    ]
    assert hooks == []


def _load_spawner():
    spec = importlib.util.spec_from_file_location("spawn_specialist_config_root_shim", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_in_process_spawner_runs_the_real_check_only_when_armed(tmp_path, monkeypatch, request):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "forged"))
    spawner = _load_spawner()
    assert spawner.config_root_refusal() is None
    request.getfixturevalue("config_root_check_armed")
    refusal = spawner.config_root_refusal()
    assert refusal is not None and str(tmp_path / "forged") in refusal
