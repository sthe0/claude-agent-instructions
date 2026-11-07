"""The negative control's harness and catalogue: exit codes, mutant discovery, the no-skip
option the verify command relies on, and that every declared mutant is applicable."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parent
ROOT = SCRIPTS.parent
HARNESS = TESTS_DIR / "telemetry_mutation_control.py"

_spec = importlib.util.spec_from_file_location("telemetry_mutation_control", HARNESS)
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)


def _child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTEST_ADDOPTS")}


def _synthetic_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "scripts" / "tests" / "telemetry_mutants").mkdir(parents=True)
    (root / "scripts" / "unit.py").write_text("VALUE = 1\nTWICE = 2\nTWICE_AGAIN = 2\n")
    (root / "scripts" / "tests" / "test_unit.py").write_text(textwrap.dedent("""\
        import sys
        from pathlib import Path
        import pytest
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import unit

        def test_value():
            assert unit.VALUE == 1

        def test_skipped():
            pytest.skip("not today")
    """))
    (root / "scripts" / "tests" / "test_collect_error.py").write_text("import module_that_does_not_exist\n")
    (root / "scripts" / "tests" / "telemetry_mutants" / "__init__.py").write_text("")
    (root / "scripts" / "tests" / "telemetry_mutants" / "area.py").write_text(textwrap.dedent("""\
        MUTANTS = [
            {"name": "killed", "file": "scripts/unit.py", "anchor": "VALUE = 1",
             "replacement": "VALUE = 2", "tests": ["scripts/tests/test_unit.py::test_value"]},
            {"name": "survives", "file": "scripts/unit.py", "anchor": "TWICE = 2\\n",
             "replacement": "TWICE = 3\\n", "tests": ["scripts/tests/test_unit.py::test_value"]},
            {"name": "anchor_absent", "file": "scripts/unit.py", "anchor": "NOPE",
             "replacement": "X", "tests": ["scripts/tests/test_unit.py::test_value"]},
            {"name": "anchor_twice", "file": "scripts/unit.py", "anchor": "= 2",
             "replacement": "= 3", "tests": ["scripts/tests/test_unit.py::test_value"]},
            {"name": "skipped", "file": "scripts/unit.py", "anchor": "VALUE = 1",
             "replacement": "VALUE = 2", "tests": ["scripts/tests/test_unit.py::test_skipped"]},
            {"name": "collect_error", "file": "scripts/unit.py", "anchor": "VALUE = 1",
             "replacement": "VALUE = 2", "tests": ["scripts/tests/test_collect_error.py"]},
        ]
    """))
    return root


def test_harness_exit_codes_and_mutant_discovery(tmp_path):
    root = _synthetic_root(tmp_path)
    names = [m["name"] for m in control.discover(root)]
    assert names == ["killed", "survives", "anchor_absent", "anchor_twice", "skipped", "collect_error"]

    def run(*args):
        return subprocess.run([sys.executable, str(HARNESS), "--root", str(root), *args],
                              capture_output=True, text=True, env=_child_env(), timeout=300).returncode

    assert run("--mutant", "killed") == 3
    assert run("--mutant", "survives") == 0
    assert run("--mutant", "nosuch") == 4
    assert run("--mutant", "anchor_absent") == 5
    assert run("--mutant", "anchor_twice") == 5
    assert run("--mutant", "skipped") == 6
    assert run("--mutant", "collect_error") == 6
    assert run() == 2
    assert run("--control") == 7  # the control runs every named test, the skipped one included
    assert (root / "scripts" / "unit.py").read_text().startswith("VALUE = 1\n"), "the real tree stays untouched"

    listed = subprocess.run([sys.executable, str(HARNESS), "--root", str(root), "--list"],
                            capture_output=True, text=True, env=_child_env(), timeout=60)
    assert listed.returncode == 0 and listed.stdout.split() == names


def test_control_exits_zero_when_every_named_test_is_green(tmp_path):
    root = _synthetic_root(tmp_path)
    area = root / "scripts" / "tests" / "telemetry_mutants" / "area.py"
    area.write_text(textwrap.dedent("""\
        MUTANTS = [{"name": "only", "file": "scripts/unit.py", "anchor": "VALUE = 1",
                    "replacement": "VALUE = 2", "tests": ["scripts/tests/test_unit.py::test_value"]}]
    """))
    result = subprocess.run([sys.executable, str(HARNESS), "--root", str(root), "--control"],
                            capture_output=True, text=True, env=_child_env(), timeout=300)
    assert result.returncode == 0


def test_no_skip_option_fails_a_run_with_a_skipped_test(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n\ndef test_ok():\n    pass\n\ndef test_skipped():\n    pytest.skip('x')\n")
    env = _child_env()
    env["PYTHONPATH"] = str(TESTS_DIR)

    def run(*extra):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
             "--noconftest", "-p", "conftest", *extra, "test_sample.py"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)

    with_option = run("--telemetry-no-skip")
    without_option = run()
    assert with_option.returncode != 0, with_option.stdout[-500:]
    assert without_option.returncode == 0, without_option.stdout[-500:]


def test_every_declared_mutant_is_applicable_and_names_existing_tests():
    mutants = control.discover(ROOT)
    assert len(mutants) == len({m["name"] for m in mutants}) >= 26
    problems = []
    for mutant in mutants:
        target = ROOT / mutant["file"]
        if not target.is_file():
            problems.append(f"{mutant['name']}: no file {mutant['file']}")
        elif target.read_text(encoding="utf-8").count(mutant["anchor"]) != 1:
            problems.append(f"{mutant['name']}: anchor not exactly once in {mutant['file']}")
        if mutant["anchor"] == mutant["replacement"]:
            problems.append(f"{mutant['name']}: replacement equals anchor")
        for node in mutant["tests"]:
            path, _, test = node.partition("::")
            source = (ROOT / path)
            if not source.is_file() or f"def {test}(" not in source.read_text(encoding="utf-8"):
                problems.append(f"{mutant['name']}: no test {node}")
    assert problems == []
