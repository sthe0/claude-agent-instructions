"""gw1-gw8: scripts/tests/sweep_grant_surface.sh -- its file list, the
summary-line rule that decides green, and what its `main` runs.

Every case runs with a stub `python3` first on PATH. The sweep's own file
list includes the test file that runs gc_mutation_control.py, which in turn
runs these cases, so a case that let the real `main` reach the real pytest
would recurse into a full sweep. The stub records its cwd and argv to a
file, prints the summary line the case chooses, and exits 0.

Each case sources the script in a fresh `bash -c` and asserts the source
step itself exited 0 before reading anything, so a script that fails to
source fails every case rather than passing one vacuously.
gc_mutation_control.py checks each case goes red under the weakening it
exists to catch.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SWEEP = Path(__file__).resolve().parent / "sweep_grant_surface.sh"
MUTATION_CONTROL_TEST = (
    "scripts/tests/test_spawn_specialist_grants.py:test_mutation_control_is_collected_and_discriminates"
)
LAW_EXTRAS = {
    "scripts/tests/test_stage_grants.py",
    "scripts/tests/test_grant_derivation.py",
    "scripts/tests/test_sweep_grant_surface.py",
}
GREEN_LINE = "3 passed in 0.12s"
SOURCE_FAILED = 97


def _stub_env(tmp_path: Path, last_line: str = GREEN_LINE) -> tuple[dict, Path]:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(parents=True)
    record = tmp_path / "stub-record"
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        '{ pwd -P; printf "%s\\n" "$@"; } >> "$GW_STUB_RECORD"\n'
        'printf "%s\\n" "$GW_STUB_LAST_LINE"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GW_STUB_RECORD"] = str(record)
    env["GW_STUB_LAST_LINE"] = last_line
    return env, record


def _sourced(env: dict, snippet: str, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["bash", "-c", f'. "$1" || exit {SOURCE_FAILED}\nshift\n{snippet}', "_", str(SWEEP), *args],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode != SOURCE_FAILED, f"sourcing {SWEEP} failed: {proc.stderr}"
    return proc


def _root_and_array(env: dict, array: str) -> tuple[Path, list[str]]:
    proc = _sourced(env, f'printf "%s\\n" "$REPO_ROOT"; printf "%s\\n" "${{{array}[@]}}"')
    root, *items = proc.stdout.splitlines()
    return Path(root), [item for item in items if item]


def _accepts(env: dict, line: str) -> bool:
    return _sourced(env, 'summary_line_is_green "$1"', line).returncode == 0


def test_gw1_every_sweep_file_exists(tmp_path):
    env, _ = _stub_env(tmp_path)
    root, files = _root_and_array(env, "SWEEP_FILES")
    missing = [f for f in files if not (root / f).is_file()]
    assert not missing


def test_gw2_sweep_files_nonempty_without_duplicates(tmp_path):
    env, _ = _stub_env(tmp_path)
    _root, files = _root_and_array(env, "SWEEP_FILES")
    assert files
    assert len(files) == len(set(files))


def test_gw3_sweep_files_follow_the_membership_law(tmp_path):
    env, _ = _stub_env(tmp_path)
    root, files = _root_and_array(env, "SWEEP_FILES")
    naming_the_assembler = {
        f"scripts/tests/{path.name}"
        for path in (root / "scripts" / "tests").glob("test_*.py")
        if "build_child_settings" in path.read_text()
    }
    assert set(files) == naming_the_assembler | LAW_EXTRAS


def test_gw4_green_summary_shapes_are_accepted(tmp_path):
    env, _ = _stub_env(tmp_path)
    for line in (
        "3 passed in 0.12s",
        "1 passed, 1 warning in 0.12s",
        "3 passed, 2 warnings in 0.12s",
        "452 passed in 71.20s (0:01:11)",
        "452 passed, 2 warnings in 71.20s (0:01:11)",
    ):
        assert _accepts(env, line), line


def test_gw5_non_green_summary_shapes_are_rejected(tmp_path):
    env, _ = _stub_env(tmp_path)
    for line in (
        "3 passed, 1 skipped in 0.12s",
        "3 passed, 1 xfailed in 0.12s",
        "1 failed, 3 passed in 0.12s",
        "3 passed, 1 error in 0.12s",
        "3 passed, 1 deselected in 0.12s",
        "3 skipped in 0.12s",
        "no tests ran in 0.01s",
        "",
    ):
        assert not _accepts(env, line), line


def test_gw6_sourcing_does_not_run_main(tmp_path):
    env, record = _stub_env(tmp_path)
    proc = _sourced(env, ":")
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert not record.exists()


def test_gw7_required_defs_name_the_mutation_control_test(tmp_path):
    env, _ = _stub_env(tmp_path)
    root, pairs = _root_and_array(env, "REQUIRED_DEFS")
    assert MUTATION_CONTROL_TEST in pairs
    for pair in pairs:
        file, name = pair.split(":", 1)
        assert re.search(rf"^def {re.escape(name)}\(", (root / file).read_text(), re.MULTILINE), pair


def test_gw8_main_runs_one_unfiltered_pytest_and_judges_its_last_line(tmp_path):
    green_env, green_record = _stub_env(tmp_path / "green")
    root, files = _root_and_array(green_env, "SWEEP_FILES")
    assert _sourced(green_env, "main").returncode == 0
    cwd, *argv = green_record.read_text().splitlines()
    assert Path(cwd) == root
    assert argv == ["-m", "pytest", "-q", *files]

    red_env, _ = _stub_env(tmp_path / "red", last_line="1 failed, 2 passed in 0.12s")
    assert _sourced(red_env, "main").returncode != 0

    missing_env, missing_record = _stub_env(tmp_path / "missing")
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    assert _sourced(missing_env, 'REPO_ROOT="$1"; main', str(empty_root)).returncode != 0
    assert not missing_record.exists()
