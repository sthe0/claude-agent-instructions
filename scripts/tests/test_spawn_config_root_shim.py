"""spawn-specialist refuses a forged config root outside the suite, while the suite's
config_root_shim keeps that check off in the spawners it starts unless a test arms it."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import config_root_shim as shim_source

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
SPECIALIZATIONS = SCRIPT.parent.parent / "skills" / "specializations"
REFUSAL = "refusing to spawn: the child's config root"


def _spawn_dry_run(tmp_path: Path, *, with_shim: bool, extra_path: str = "") -> subprocess.CompletedProcess:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "skills").symlink_to(SPECIALIZATIONS)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(forged), "AGENT_RECURSION_DEPTH": "0"}
    env.pop("CLAUDE_AGENT_HOME", None)
    if not with_shim:
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in env.get("PYTHONPATH", "").split(os.pathsep)
            if p and not os.path.basename(p).startswith(shim_source.SHIM_DIR_PREFIX))
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (extra_path, env.get("PYTHONPATH")) if p)
    cmd = [
        sys.executable, str(SCRIPT), "--dry-run",
        "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "nothing", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def test_a_spawner_started_without_the_shim_refuses_a_forged_config_root(tmp_path):
    result = _spawn_dry_run(tmp_path, with_shim=False)
    assert result.returncode == 2, result.stderr
    assert REFUSAL in result.stderr


def test_a_spawner_the_suite_starts_skips_the_config_root_check(tmp_path):
    result = _spawn_dry_run(tmp_path, with_shim=True)
    assert REFUSAL not in result.stderr
    assert result.returncode == 0, result.stderr


def test_a_spawner_with_the_committed_tests_dir_on_pythonpath_refuses_a_forged_config_root(tmp_path):
    result = _spawn_dry_run(tmp_path, with_shim=False, extra_path=str(Path(__file__).resolve().parent))
    assert result.returncode == 2, result.stderr
    assert REFUSAL in result.stderr


def test_no_committed_sitecustomize_guards_the_config_root_check():
    guards = [p for p in SCRIPT.parent.rglob("sitecustomize.py") if shim_source.CHECK_NAME in p.read_text()]
    assert guards == []


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
