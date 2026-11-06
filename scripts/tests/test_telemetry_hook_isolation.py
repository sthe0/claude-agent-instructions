"""Hooks stay off the database: no hook's import closure reaches sqlite3 or the store, and
only the store module opens the record."""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FORBIDDEN = ("sqlite3", "lib.telemetry.store")
ALLOWED_RECORD_PATH_USERS = {
    "lib/telemetry/store.py", "telemetry.py", "lib/config_root.py"}


def _hook_scripts(scripts: Path) -> list:
    installer = (scripts / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    body = installer.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    desired = None
    for node in ast.parse(body).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "DESIRED" for t in node.targets):
            desired = ast.literal_eval(node.value)
    assert desired, "DESIRED table not found in install-reminder-hooks.sh"
    names = {row[2].split()[0] for row in desired}
    names |= {p.name for p in scripts.glob("hook-*.py")}
    return sorted(scripts / name for name in names if name.endswith(".py") and (scripts / name).exists())


def _imported_modules(path: Path) -> set:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _module_files(scripts: Path, dotted: str) -> list:
    parts = dotted.split(".")
    files = []
    for end in range(1, len(parts) + 1):
        base = scripts.joinpath(*parts[:end])
        if (base / "__init__.py").exists():
            files.append(base / "__init__.py")
        elif end == len(parts) and base.with_suffix(".py").exists():
            files.append(base.with_suffix(".py"))
    return files


def closure_violations(scripts: Path, entry: Path) -> list:
    """Modules in `entry`'s static import closure (inside `scripts`) that are forbidden."""
    seen, reached, queue = set(), set(), [entry]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for module in _imported_modules(path):
            reached.add(module)
            queue.extend(_module_files(scripts, module))
    return sorted(m for m in reached if m in FORBIDDEN or m.startswith("sqlite3."))


def test_no_hook_entry_point_imports_sqlite3_or_the_writer():
    hooks = _hook_scripts(SCRIPTS)
    assert len(hooks) > 10
    offenders = {h.name: closure_violations(SCRIPTS, h) for h in hooks}
    assert {name: found for name, found in offenders.items() if found} == {}


def test_closure_analysis_sees_a_forbidden_import_behind_a_helper(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "helper.py").write_text("def go():\n    import sqlite3\n    return sqlite3\n")
    (tmp_path / "lib" / "viastore.py").write_text("from lib.telemetry import store\n")
    (tmp_path / "lib" / "telemetry").mkdir()
    (tmp_path / "lib" / "telemetry" / "__init__.py").write_text("")
    (tmp_path / "lib" / "telemetry" / "store.py").write_text("X = 1\n")
    (tmp_path / "hook-a.py").write_text("from lib import helper\n")
    (tmp_path / "hook-b.py").write_text("from lib import viastore\n")
    (tmp_path / "hook-c.py").write_text("import json\nfrom lib.telemetry import emit\n")
    assert closure_violations(tmp_path, tmp_path / "hook-a.py") == ["sqlite3"]
    assert closure_violations(tmp_path, tmp_path / "hook-b.py") == ["lib.telemetry.store"]
    assert closure_violations(tmp_path, tmp_path / "hook-c.py") == []


def test_only_store_module_opens_the_record():
    importers, opening = [], []
    for path in sorted(SCRIPTS.rglob("*.py")):
        relative = path.relative_to(SCRIPTS).as_posix()
        if relative.startswith("tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "sqlite3" or n.startswith("sqlite3.") for n in names):
                importers.append(relative)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "telemetry_db":
                opening.append(relative)
    assert set(importers) == {"lib/telemetry/store.py"}
    assert set(opening) <= ALLOWED_RECORD_PATH_USERS
