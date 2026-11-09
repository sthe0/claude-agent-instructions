"""arc_launcher_recorder against a planted fake launcher, so no real arc is needed."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import arc_launcher_recorder as rec
from conftest import path_with_arc_stub

RECORDER = Path(rec.__file__).resolve()


@pytest.fixture
def fake_launcher(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    path = bindir / "arc"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    if rec._can_attach(str(path)):
        pytest.skip(f"cannot attach here: {rec._can_attach(str(path))}")
    return path


def _run(launcher, log, inner, *flags):
    return subprocess.run(
        [sys.executable, str(RECORDER), "--log", str(log), "--launcher", str(launcher), *flags, "--", *inner],
        capture_output=True, text=True)


def _env(launcher, *shell):
    return ["env", "-i", f"PATH={path_with_arc_stub(str(launcher.parent))}", "HOME=/nonexistent", "sh", "-c", *shell]


def test_records_through_literal_path_and_absolute_path(fake_launcher, tmp_path):
    log = tmp_path / "log"
    proc = _run(fake_launcher, log, _env(fake_launcher, f"arc one; {fake_launcher} two three"))
    assert proc.returncode == 0, proc.stderr
    assert [r[2] for r in rec.read_records(log)] == ["one", "two three"]


def test_records_two_levels_down_and_carries_test_id(fake_launcher, tmp_path):
    log = tmp_path / "log"
    inner = ["env", "-i", f"PATH={path_with_arc_stub(str(fake_launcher.parent))}", "PYTEST_CURRENT_TEST=mod.py::t (call)",
             "sh", "-c", "sh -c 'sh -c \"arc deep\"'"]
    assert _run(fake_launcher, log, inner).returncode == 0
    (record,) = rec.read_records(log)
    assert record[1] == "mod.py::t (call)" and record[2] == "deep"


def test_test_id_is_empty_outside_pytest(fake_launcher, tmp_path):
    log = tmp_path / "log"
    _run(fake_launcher, log, _env(fake_launcher, "arc x"))
    (record,) = rec.read_records(log)
    assert record[1] == ""


def test_exit_status_is_the_real_launchers(fake_launcher, tmp_path):
    fake_launcher.write_text("#!/bin/sh\nexit 7\n")
    proc = _run(fake_launcher, tmp_path / "log", _env(fake_launcher, "arc"))
    assert proc.returncode == 7


def test_no_attach_records_nothing(fake_launcher, tmp_path):
    log = tmp_path / "log"
    assert _run(fake_launcher, log, _env(fake_launcher, "arc one"), "--no-attach").returncode == 0
    assert rec.read_records(log) == []


def test_real_file_is_untouched_outside_the_namespace(fake_launcher, tmp_path):
    before = fake_launcher.read_text()
    _run(fake_launcher, tmp_path / "log", _env(fake_launcher, "arc"))
    assert fake_launcher.read_text() == before


def test_selfcheck_passes_attached_and_fails_unattached(fake_launcher):
    base = [sys.executable, str(RECORDER), "--selfcheck", "--launcher", str(fake_launcher)]
    # the fake answers every call with exit 0, so the selfcheck's two calls both succeed
    assert subprocess.run(base).returncode == 0
    assert subprocess.run([*base, "--no-attach"]).returncode == 1


def test_missing_launcher_skips_unless_required(tmp_path):
    base = [sys.executable, str(RECORDER), "--selfcheck", "--launcher", str(tmp_path / "absent")]
    assert subprocess.run(base).returncode == 0
    assert subprocess.run([*base, "--require-launcher"]).returncode == 1
