"""Tests for scripts/onboard.sh — hermetic subprocess harness.

Mirrors the test_detect_backend.py pattern: tmp HOME, env-seam stubs,
no real setup-symlinks/doctor/hooks are called.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
ONBOARD = SCRIPTS / "onboard.sh"


# ── Stub helpers ──────────────────────────────────────────────────────────────


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _make_call_log(tmp: Path) -> Path:
    log = tmp / "call.log"
    log.write_text("")
    return log


def _make_setup_symlinks_stub(tmp: Path, call_log: Path, rc: int = 0) -> Path:
    return _write_exec(
        tmp / "setup-symlinks.sh",
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'setup-symlinks\\n' >> "{call_log}"
            exit {rc}
        """),
    )


def _make_doctor_stub(tmp: Path, call_log: Path, rc: int = 0) -> Path:
    return _write_exec(
        tmp / "doctor.sh",
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'doctor\\n' >> "{call_log}"
            exit {rc}
        """),
    )


def _make_hook(hook_dir: Path, name: str, call_log: Path, rc: int = 0) -> Path:
    return _write_exec(
        hook_dir / name,
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '{name}\\n' >> "{call_log}"
            exit {rc}
        """),
    )


def _run(
    env_extra: dict | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CLAUDE_DRY_RUN", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(ONBOARD)] + (args or []),
        env=env,
        capture_output=True,
        text=True,
    )


def _base_env(tmp: Path, call_log: Path, hook_dir: Path | None = None, *, doctor_rc: int = 0) -> dict:
    setup_stub = _make_setup_symlinks_stub(tmp, call_log)
    doctor_stub = _make_doctor_stub(tmp, call_log, rc=doctor_rc)
    e: dict = {
        "SETUP_SYMLINKS_BIN": str(setup_stub),
        "DOCTOR_BIN": str(doctor_stub),
    }
    if hook_dir is not None:
        e["CLAUDE_ONBOARD_HOOK_DIR"] = str(hook_dir)
    return e


# ── Test 1: setup-symlinks → doctor → hooks in sorted order ──────────────────


def test_runs_steps_in_order(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "onboard.d"
    hook_dir.mkdir()
    _make_hook(hook_dir, "00-a.sh", call_log)
    _make_hook(hook_dir, "10-b.sh", call_log)

    r = _run(_base_env(tmp_path, call_log, hook_dir))
    assert r.returncode == 0, r.stderr
    assert call_log.read_text().splitlines() == [
        "setup-symlinks", "doctor", "00-a.sh", "10-b.sh"
    ]


# ── Test 2: hook failure aborts; later hooks do not run ──────────────────────


def test_hook_failure_aborts_with_status(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "onboard.d"
    hook_dir.mkdir()
    _make_hook(hook_dir, "00-ok.sh", call_log, rc=0)
    _make_hook(hook_dir, "10-fail.sh", call_log, rc=3)
    _make_hook(hook_dir, "20-skipped.sh", call_log, rc=0)

    r = _run(_base_env(tmp_path, call_log, hook_dir))
    assert r.returncode == 3, f"Expected exit 3, got {r.returncode}"
    calls = call_log.read_text().splitlines()
    assert "20-skipped.sh" not in calls, f"Later hook ran after failure: {calls}"
    assert "10-fail.sh" in calls


# ── Test 3: CLAUDE_DRY_RUN performs no calls, prints plan ────────────────────


def test_dry_run_no_calls_prints_plan(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "onboard.d"
    hook_dir.mkdir()
    _make_hook(hook_dir, "00-a.sh", call_log)

    env = _base_env(tmp_path, call_log, hook_dir)
    env["CLAUDE_DRY_RUN"] = "1"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert call_log.read_text().splitlines() == [], \
        f"Dry-run should call nothing, got: {call_log.read_text()}"
    assert "would run" in r.stderr, f"Dry-run should print plan; stderr={r.stderr!r}"


# ── Test 4: empty hook dir is no-op ──────────────────────────────────────────


def test_empty_hook_dir_is_noop(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "onboard.d"
    hook_dir.mkdir()  # no .sh files

    r = _run(_base_env(tmp_path, call_log, hook_dir))
    assert r.returncode == 0, r.stderr
    assert call_log.read_text().splitlines() == ["setup-symlinks", "doctor"]


# ── Test 5: absent hook dir is no-op ─────────────────────────────────────────


def test_absent_hook_dir_is_noop(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "nonexistent-onboard.d"  # not created

    r = _run(_base_env(tmp_path, call_log, hook_dir))
    assert r.returncode == 0, r.stderr
    assert call_log.read_text().splitlines() == ["setup-symlinks", "doctor"]


# ── Test 6: doctor non-zero warns and continues ───────────────────────────────


def test_doctor_failure_warns_and_continues(tmp_path):
    call_log = _make_call_log(tmp_path)
    hook_dir = tmp_path / "onboard.d"
    hook_dir.mkdir()
    _make_hook(hook_dir, "00-a.sh", call_log)

    r = _run(_base_env(tmp_path, call_log, hook_dir, doctor_rc=1))
    assert r.returncode == 0, r.stderr
    calls = call_log.read_text().splitlines()
    assert "00-a.sh" in calls, f"Hook should run after doctor warn: {calls}"
    assert "continuing" in r.stderr, f"Expected warning message, got: {r.stderr!r}"


# ── Test 7: -h/--help prints usage and exits 0 ────────────────────────────────


def test_help_flag_prints_usage():
    r = _run(args=["--help"])
    assert r.returncode == 0
    assert "onboard" in r.stdout


def test_short_help_flag_prints_usage():
    r = _run(args=["-h"])
    assert r.returncode == 0
    assert "onboard" in r.stdout
