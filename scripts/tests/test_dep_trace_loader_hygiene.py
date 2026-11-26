"""The suite's own loaders must not widen what the dependency tracer records for every test.

The tracer attributes a module body to the nearest repository frame on the stack. A repository
function that wraps module loading process-wide (a replaced `exec_module`) is that frame for every
module body, so every test inherits every module the suite loads, plus a `dynamic-import` reason from
the loader's own code, and no stored result is ever reused.

The check runs a child pytest on a copy of this repository's `scripts/` (the copy is made from the
checkout this file sits in, so a base checkout is judged by its own code), with the real
`scripts/tests/conftest.py` and the tracer switched on as the suite gate switches it on, over
three tests: A and B are unrelated, C loads a module that imports by a computed name.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import dep_tracer

SENTINEL = "DEP-TRACE-WIDENED"
SCRIPTS = Path(__file__).resolve().parents[1]

DYN_MODULE = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
TEST_A = "def test_a():\n    assert 1 + 1 == 2\n"
TEST_B = "def test_b():\n    assert 'b'.upper() == 'B'\n"
TEST_C = "import hygiene_dyn\n\n\ndef test_c():\n    assert hygiene_dyn.load('json').dumps([]) == '[]'\n"


def _child_env(trace_dir: Path, root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_XDIST_", "CORE_DEP_TRACER")) and k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in env.get("PYTHONPATH", "").split(os.pathsep)
                                        if p and "dep_tracer_site" not in p)
    env.update({"CORE_DEP_TRACER": "1", "CORE_DEP_TRACER_DIR": str(trace_dir), "CORE_DEP_TRACER_ROOT": str(root)})
    return env


def _record_for(records: dict[str, dict], name: str) -> dict | None:
    return next((r for node, r in records.items() if node.endswith(f"::{name}")), None)


def test_loaders_do_not_widen_unrelated_tests(tmp_path):
    root = tmp_path / "core"
    scripts = root / "scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__", "test_*.py", ".pytest_cache"))
    tests = scripts / "tests"
    (tests / "hygiene_dyn.py").write_text(DYN_MODULE)
    for name, body in (("test_hygiene_a.py", TEST_A), ("test_hygiene_b.py", TEST_B), ("test_hygiene_c.py", TEST_C)):
        (tests / name).write_text(body)
    trace_dir = tmp_path / "trace"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-q",
         "tests/test_hygiene_a.py", "tests/test_hygiene_b.py", "tests/test_hygiene_c.py"],
        cwd=scripts, env=_child_env(trace_dir, root), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"the child pytest failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}"

    records = dep_tracer.load_records(trace_dir)
    a, b, c = (_record_for(records, f"test_{n}") for n in "abc")
    assert a and b and c, f"no record for every test: {sorted(records)}"
    own = {"scripts/tests/test_hygiene_a.py", "scripts/tests/conftest.py", "scripts/tests/spawner_launcher.py"}
    assert own <= set(a["files"]), f"the record of A is not a real trace: {sorted(a['files'])[:20]}"
    assert "dynamic-import" in c["always_rerun"], f"the record of C lacks its dynamic-import: {c['always_rerun']}"

    widened = {
        "A lists a module of B or C": sorted(set(a["files"]) & {
            "scripts/tests/test_hygiene_b.py", "scripts/tests/test_hygiene_c.py", "scripts/tests/hygiene_dyn.py"}),
        "A carries dynamic-import": [r for r in a["always_rerun"] if r == "dynamic-import"],
        "B carries dynamic-import": [r for r in b["always_rerun"] if r == "dynamic-import"],
    }
    widened = {why: found for why, found in widened.items() if found}
    if widened:
        print(SENTINEL, widened)
    assert not widened, f"{SENTINEL}: {widened}"
