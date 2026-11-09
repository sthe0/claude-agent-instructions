"""The suite never starts a real model CLI.

Guards in scripts/tests/stubs stand first on the session PATH under every model CLI name, and
conftest fails the test (and the run) whose process tree reached one. The guards see only
launches that resolve a name through a PATH going through the stubs directory, so the lint
below holds every child environment a test builds to that: no real-CLI directory ahead of the
stubs directory or the inherited PATH, no child env without PATH, no `env -i` without a PATH=
that passes the same rule, no absolute model CLI path as the program to run.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import (
    ARC_STUB_DIR,
    LAUNCH_LOG_ENV,
    LAUNCH_MARKER,
    MODEL_CLI_NAMES,
    path_with_arc_stub,
    path_without_model_cli,
)
from model_cli_path import unattributed_launch_log

TESTS_DIR = Path(__file__).resolve().parent
LINT_EXEMPT = frozenset({Path(__file__).name, "manual_real_cli_mcp.py"})

GUARDED_PATH_HELPERS = frozenset({"path_with_arc_stub", "path_without_model_cli"})
REAL_CLI_DIRS = frozenset({"/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin", "/usr/local/sbin"})
CHILD_LAUNCHERS = frozenset({
    "run", "Popen", "check_output", "check_call", "call", "spawn",
    "create_subprocess_exec", "create_subprocess_shell",
})
EXEC_LAUNCHERS = frozenset({
    "execv", "execve", "execl", "execle", "execvp", "execvpe", "execlp", "execlpe",
    "spawnv", "spawnve", "spawnl", "spawnle", "spawnvp", "spawnvpe", "spawnlp", "spawnlpe",
    "posix_spawn", "posix_spawnp",
})
ENV_IGNORE = re.compile(r"\benv\s+(-i|--ignore-environment)\b")
SHELL_PATH_ASSIGN = re.compile(r"(?:^|[\s;&|(\"'])PATH=([^\s;&|)\"']*)")

# How one PATH entry can resolve a model CLI name: to a real one, to a guard (the stubs
# directory, or the inherited PATH, which starts with it), or to whatever a test put there.
REAL, GUARDED, NEUTRAL = "real", "guarded", "neutral"


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.what}"


def _is_real_dir(entry: str) -> bool:
    entry = entry.rstrip("/") or "/"
    return entry in REAL_CLI_DIRS or entry.endswith("/.local/bin")


def _entry_kind(entry: str) -> str:
    """A literal entry; `$PATH` in a shell string is the inherited PATH."""
    if "$PATH" in entry or "${PATH" in entry:
        return GUARDED
    return REAL if _is_real_dir(entry) else NEUTRAL


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_path_key(node: ast.AST) -> bool:
    return _const_str(node) == "PATH"


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and _const_str(stmt.value) is not None


class _Scope:
    """Simple name bindings of one function body (or the module), for resolving a PATH value
    or an env dict that is built on one line and passed on another."""

    def __init__(self, body_owner: ast.AST, parent: "_Scope | None"):
        self.parent = parent
        self.bindings: dict[str, list[ast.AST]] = {}
        self.path_set: set[str] = set()
        for node in _walk_scope(body_owner):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.bindings.setdefault(target.id, []).append(node.value)
                    elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                            and _is_path_key(target.slice):
                        self.path_set.add(target.value.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                self.bindings.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not body_owner:
                self.bindings.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name):
                receiver, method = node.func.value.id, node.func.attr
                if method == "update" and (any(k.arg in ("PATH", None) for k in node.keywords)
                                           or any(not isinstance(a, ast.Dict) or _dict_carries_path(a)
                                                  for a in node.args)):
                    self.path_set.add(receiver)
                elif method == "setdefault" and node.args and _is_path_key(node.args[0]):
                    self.path_set.add(receiver)

    def lookup(self, name: str) -> list[ast.AST] | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None


def _walk_scope(owner: ast.AST):
    """Every node of `owner`'s body, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(owner))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _dict_carries_path(node: ast.Dict) -> bool:
    return any(k is None or _is_path_key(k) for k in node.keys)


def _env_carries_path(node: ast.AST, scope: _Scope, depth: int = 0) -> bool:
    """False only for an env that is known to hold no PATH: a dict literal, or a `dict(...)`
    of keywords alone, with no PATH key and no `**` spread."""
    if isinstance(node, ast.Dict):
        return _dict_carries_path(node)
    if isinstance(node, ast.Call) and _name_of(node.func) == "dict":
        return bool(node.args) or any(k.arg in ("PATH", None) for k in node.keywords)
    if isinstance(node, ast.Name) and depth < 3:
        if node.id in scope.path_set:
            return True
        bound = scope.bindings.get(node.id)
        if not bound:
            return True
        return any(_env_carries_path(b, scope, depth + 1) for b in bound)
    return True


def _path_kinds(node: ast.AST, scope: _Scope, depth: int = 0) -> list[str]:
    """The kind of each entry of a PATH value, in order (see REAL, GUARDED, NEUTRAL)."""
    if depth > 4:
        return [NEUTRAL]
    text = _const_str(node)
    if text is not None:
        return [_entry_kind(e) for e in text.split(os.pathsep)]
    if isinstance(node, ast.JoinedStr):
        return _concat_kinds(list(node.values), scope, depth)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concat_kinds(_flatten_add(node), scope, depth)
    if isinstance(node, ast.FormattedValue):
        return _path_kinds(node.value, scope, depth + 1)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [k for elt in node.elts for k in _path_kinds(elt, scope, depth + 1)]
    if isinstance(node, ast.Starred):
        return _path_kinds(node.value, scope, depth + 1)
    if isinstance(node, ast.GeneratorExp) and len(node.generators) == 1:
        return _path_kinds(node.generators[0].iter, scope, depth + 1)
    if isinstance(node, ast.IfExp):
        return _path_kinds(node.body, scope, depth + 1) + _path_kinds(node.orelse, scope, depth + 1)
    if isinstance(node, ast.Subscript) and _is_path_key(node.slice):
        return [GUARDED]
    if isinstance(node, ast.Call):
        name = _name_of(node.func)
        if name in GUARDED_PATH_HELPERS:
            return [GUARDED]
        if name in ("get", "getenv") and node.args and _is_path_key(node.args[0]):
            return [GUARDED]
        if name == "join" and node.args:
            return _path_kinds(node.args[0], scope, depth + 1)
        if name in ("str", "fspath") and node.args:
            return _path_kinds(node.args[0], scope, depth + 1)
        return [NEUTRAL]
    name = _name_of(node)
    if name == "ARC_STUB_DIR":
        return [GUARDED]
    if name == "defpath":
        return [REAL]
    if isinstance(node, ast.Name):
        bound = scope.lookup(node.id)
        if bound and len(bound) == 1 and not isinstance(bound[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _path_kinds(bound[0], scope, depth + 1)
    return [NEUTRAL]


def _flatten_add(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_add(node.left) + _flatten_add(node.right)
    return [node]


def _concat_kinds(parts: list[ast.AST], scope: _Scope, depth: int) -> list[str]:
    """Kinds of a PATH value built by concatenation: a literal piece split on the separator;
    the text a literal piece adds to the entry an expression began (`{home}/.local/bin`)
    belongs to that entry."""
    kinds: list[str] = []
    joined_to_previous = False
    for part in parts:
        text = _const_str(part)
        if text is None:
            kinds.extend(_path_kinds(part, scope, depth + 1))
            joined_to_previous = True
            continue
        entries = text.split(os.pathsep)
        if joined_to_previous:
            if entries[0].endswith("/.local/bin"):
                kinds[-1] = REAL
            entries = entries[1:]
        kinds.extend(_entry_kind(e) for e in entries)
        joined_to_previous = False
    return kinds


def _path_assignment_kinds(node: ast.AST, scope: _Scope) -> list[str] | None:
    """Kinds of the value of a `PATH=...` argv element (`env PATH=... cmd`), else None."""
    text = _const_str(node)
    if text is not None:
        return [_entry_kind(e) for e in text[5:].split(os.pathsep)] if text.startswith("PATH=") else None
    if isinstance(node, ast.JoinedStr) and node.values:
        head = _const_str(node.values[0])
        if head is not None and head.startswith("PATH="):
            return _concat_kinds([ast.Constant(head[5:]), *node.values[1:]], scope, 0)
    return None


def _reaches_real_first(kinds: list[str]) -> bool:
    return next((k for k in kinds if k != NEUTRAL), NEUTRAL) == REAL


def _argv0(node: ast.AST, scope: _Scope) -> str | None:
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _const_str(node.elts[0])
    text = _const_str(node)
    if text is not None:
        return text.split()[0] if text.split() else None
    if isinstance(node, ast.Name):
        bound = scope.lookup(node.id)
        if bound and len(bound) == 1 and isinstance(bound[0], (ast.List, ast.Tuple, ast.Constant)):
            return _argv0(bound[0], scope)
    return None


def _is_absolute_model_cli(text: str | None) -> bool:
    return bool(text) and text.startswith(("/", "~")) and Path(text).name in MODEL_CLI_NAMES


class _Linter:
    def __init__(self, filename: str):
        self.filename = filename
        self.findings: list[Finding] = []

    def flag(self, node: ast.AST, what: str) -> None:
        self.findings.append(Finding(self.filename, getattr(node, "lineno", 0), what))

    def lint_scope(self, owner: ast.AST, parent: _Scope | None) -> None:
        scope = _Scope(owner, parent)
        body = owner.body if isinstance(getattr(owner, "body", None), list) else []
        docstrings = {id(s.value) for s in body if _is_docstring(s)}
        for node in _walk_scope(owner):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                self.lint_scope(node, scope)
                continue
            if isinstance(node, ast.Expr) and _is_docstring(node):
                docstrings.add(id(node.value))
            self.check_node(node, scope, docstrings)

    def check_path_value(self, node: ast.AST, value: ast.AST, scope: _Scope) -> None:
        if _reaches_real_first(_path_kinds(value, scope)):
            self.flag(node, "child PATH reaches a real model CLI directory before the stubs directory")

    def check_node(self, node: ast.AST, scope: _Scope, docstrings: set[int]) -> None:
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and _is_path_key(key):
                    self.check_path_value(node, value, scope)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Subscript) and _is_path_key(t.slice) for t in targets):
                self.check_path_value(node, node.value, scope)
        elif isinstance(node, ast.Delete):
            if any(isinstance(t, ast.Subscript) and _is_path_key(t.slice) for t in node.targets):
                self.flag(node, "deletes PATH from a child env")
        elif isinstance(node, ast.Call):
            self.check_call(node, scope)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            self.check_string(node, node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            self.check_argv(node, scope)

    def check_argv(self, node: ast.List | ast.Tuple, scope: _Scope) -> None:
        """`env PATH=... cmd`: the PATH= value is judged like any other; `env -i` passes only
        with one, since without it the child searches the default path. A literal PATH= element
        is judged by check_string; only an f-string one is judged here."""
        assignments = []
        for element in node.elts:
            kinds = _path_assignment_kinds(element, scope)
            if kinds is None:
                continue
            assignments.append(kinds)
            if isinstance(element, ast.JoinedStr) and _reaches_real_first(kinds):
                self.flag(node, "`PATH=` argument reaches a real model CLI directory before the stubs directory")
        texts = [_const_str(e) for e in node.elts]
        if not assignments and any(a == "env" and b in ("-i", "--ignore-environment")
                                   for a, b in zip(texts, texts[1:])):
            self.flag(node, "`env -i` without PATH= starts a child that searches the default path")

    def check_string(self, node: ast.AST, text: str) -> None:
        assignments = [[_entry_kind(e) for e in m.group(1).split(os.pathsep)]
                       for m in SHELL_PATH_ASSIGN.finditer(text)]
        if ENV_IGNORE.search(text) and not assignments:
            self.flag(node, "`env -i` without PATH= starts a child that searches the default path")
        if any(_reaches_real_first(kinds) for kinds in assignments):
            self.flag(node, "`PATH=` reaches a real model CLI directory before the stubs directory")

    def check_call(self, node: ast.Call, scope: _Scope) -> None:
        name = _name_of(node.func)
        args = node.args
        if name in ("pop", "delenv", "unsetenv") and args and _is_path_key(args[0]):
            self.flag(node, f"`{name}(\"PATH\")` leaves a child env without PATH")
        if name in ("setenv", "setdefault", "putenv") and len(args) > 1 and _is_path_key(args[0]):
            self.check_path_value(node, args[1], scope)
        for kw in node.keywords:
            if kw.arg == "PATH":
                self.check_path_value(node, kw.value, scope)
        if name in CHILD_LAUNCHERS or name in EXEC_LAUNCHERS:
            env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
            if env is not None and not _env_carries_path(env, scope):
                self.flag(node, "child env without PATH (the default search path reaches /usr/bin)")
            program = next((kw.value for kw in node.keywords if kw.arg == "executable"), None)
            if program is not None and _is_absolute_model_cli(_const_str(program)):
                self.flag(node, "absolute model CLI path as `executable=`")
            if args and _is_absolute_model_cli(_argv0(args[0], scope)):
                self.flag(node, "absolute model CLI path as the program to run")


def lint_source(source: str, filename: str = "<snippet>") -> list[Finding]:
    linter = _Linter(filename)
    linter.lint_scope(ast.parse(source, filename), None)
    return sorted(linter.findings, key=lambda f: f.line)


def lint_tree(root: Path = TESTS_DIR) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in LINT_EXEMPT or "__pycache__" in path.parts:
            continue
        findings += lint_source(path.read_text(encoding="utf-8"), str(path.relative_to(root.parent.parent)))
    return findings


def test_tree_builds_no_child_env_that_bypasses_the_guards():
    findings = lint_tree()
    assert not findings, "child envs that can reach a real model CLI:\n" + "\n".join(map(str, findings))


REJECTED_SNIPPETS = {
    "literal system PATH": 'subprocess.run(["x"], env={"PATH": "/usr/bin:/bin"})',
    "stubs after a real-CLI directory": 'env["PATH"] = f"/usr/bin:{ARC_STUB_DIR}"',
    "defpath first": 'env = dict(os.environ, PATH=os.pathsep.join([os.defpath, ARC_STUB_DIR]))',
    "inline env without PATH": 'subprocess.run(["x"], env={"HOME": "/h"})',
    "bound env without PATH": 'def f():\n    env = {"HOME": "/h"}\n    subprocess.run(["x"], env=env)',
    "dict() env without PATH": 'def f():\n    env = dict(HOME="/h")\n    subprocess.Popen(["x"], env=env)',
    "popped PATH": 'env.pop("PATH")',
    "deleted PATH": 'del env["PATH"]',
    "monkeypatch.delenv PATH": 'monkeypatch.delenv("PATH")',
    "monkeypatch.setenv system PATH": 'monkeypatch.setenv("PATH", "/usr/local/bin:" + os.environ["PATH"])',
    "env -i in a shell string": 'subprocess.run("env -i claude -p hi", shell=True)',
    "env -i in argv": 'subprocess.run(["env", "-i", "x"])',
    "absolute claude argv[0]": 'subprocess.run(["/usr/bin/claude", "-p", "hi"])',
    "absolute agent in a bound argv": 'def f():\n    cmd = ["/opt/x/agent", "-p"]\n    subprocess.run(cmd)',
    "absolute claude executable=": 'subprocess.run(["x"], executable="/usr/bin/claude")',
    "home .local/bin first": 'env["PATH"] = f"{home}/.local/bin:{os.environ[\'PATH\']}"',
    "shell PATH= literal": 'subprocess.run("PATH=/usr/bin:/bin claude -p hi", shell=True)',
    "env -i with a literal system PATH=": 'subprocess.run(["env", "-i", "PATH=/usr/bin:/bin", "x"])',
    "env -i with a test dir then system PATH=":
        'subprocess.run(["env", "-i", f"PATH={tmp_path}:/usr/bin:/bin", "x"])',
}

ACCEPTED_SNIPPETS = {
    "test stub dir ahead of the inherited PATH": 'env["PATH"] = f"{tmp_path}:{os.environ[\'PATH\']}"',
    "path_with_arc_stub": 'subprocess.run(["x"], env={"PATH": path_with_arc_stub(str(tmp_path))})',
    "path_without_model_cli": 'env["PATH"] = path_without_model_cli()',
    "spread os.environ": 'subprocess.run(["x"], env={**os.environ, "HOME": "/h"})',
    "dict(os.environ, ...)": 'subprocess.run(["x"], env=dict(os.environ, HOME="/h"))',
    "env given PATH later": 'def f():\n    env = {"HOME": "/h"}\n    env["PATH"] = path_with_arc_stub()\n    subprocess.run(["x"], env=env)',
    "mocked which": 'monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")',
    "stubs dir then system": 'env["PATH"] = f"{ARC_STUB_DIR}:/usr/bin:/bin"',
    "test-only dir alone": 'env["PATH"] = str(tmp_path)',
    "env -i with a guarded PATH=": 'subprocess.run(["env", "-i", f"PATH={path_without_model_cli()}", "x"])',
}


@pytest.mark.parametrize("snippet", REJECTED_SNIPPETS.values(), ids=REJECTED_SNIPPETS.keys())
def test_lint_rejects_bypass(snippet):
    assert lint_source(snippet), snippet


@pytest.mark.parametrize("snippet", ACCEPTED_SNIPPETS.values(), ids=ACCEPTED_SNIPPETS.keys())
def test_lint_accepts_guarded_path(snippet):
    assert lint_source(snippet) == [], snippet


@pytest.mark.parametrize("name", MODEL_CLI_NAMES)
def test_every_model_cli_name_resolves_to_its_guard(name):
    assert shutil.which(name, path=path_with_arc_stub()) == os.path.join(ARC_STUB_DIR, name)
    assert shutil.which(name) == os.path.join(ARC_STUB_DIR, name)


@pytest.mark.parametrize("name", MODEL_CLI_NAMES)
def test_guard_free_path_holds_no_model_cli(name):
    path = path_without_model_cli()
    assert shutil.which(name, path=path) is None
    assert shutil.which("sh", path=path) and shutil.which("python3", path=path)
    assert os.path.realpath(shutil.which("arc", path=path)) == os.path.realpath(os.path.join(ARC_STUB_DIR, "arc"))


def test_guard_without_a_launch_log_writes_where_conftest_reads(tmp_path):
    """A child built from scratch carries no launch log variable; its guard falls back to the
    log conftest derives from the stubs directory. A copied guard keeps the real one's log
    out of this run."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    guard = stubs / "claude"
    shutil.copy2(os.path.join(ARC_STUB_DIR, "claude"), guard)
    log = unattributed_launch_log(str(stubs.resolve()))
    try:
        done = subprocess.run([str(guard), "-p", "orphan"], env={"PATH": path_without_model_cli()},
                              capture_output=True, text=True)
        assert done.returncode == 97, done.stderr
        assert log.read_text().strip() == f"{LAUNCH_MARKER}: claude -p orphan [test: ?]"
    finally:
        log.unlink(missing_ok=True)


PROBE_TESTS = """
import os
import subprocess


def test_claude_print():
    subprocess.run(["claude", "-p", "hi"], capture_output=True)


def test_agent_print():
    subprocess.run(["agent", "-p", "hi"], capture_output=True)


def test_untouched():
    pass
"""

UNATTRIBUTED_TEST = """
import os
import subprocess


def test_child_drops_the_test_id():
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    subprocess.run(["claude", "-p", "orphan"], env=env, capture_output=True)
"""


def _nested_pytest(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run `source` as a test file under this suite's conftest, with its own launch log."""
    (tmp_path / "conftest.py").symlink_to(TESTS_DIR / "conftest.py")
    (tmp_path / "test_probe.py").write_text(textwrap.dedent(source), encoding="utf-8")
    log = tmp_path / "launch.log"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS")}
    env[LAUNCH_LOG_ENV] = str(log)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "-p", "no:cacheprovider", "-q", "-rf",
         f"--rootdir={tmp_path}", str(tmp_path / "test_probe.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    return done, log


def test_guard_blocks_and_names_a_path_resolved_launch(tmp_path):
    done, log = _nested_pytest(tmp_path, PROBE_TESTS)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    for test in ("test_claude_print", "test_agent_print"):
        assert f"{LAUNCH_MARKER} in test_probe.py::{test}" in out, out
        assert f"FAILED test_probe.py::{test}" in out, out
    assert "FAILED test_probe.py::test_untouched" not in out, out
    logged = log.read_text()
    assert "claude -p hi [test: test_probe.py::test_claude_print (call)]" in logged
    assert "agent -p hi [test: test_probe.py::test_agent_print (call)]" in logged


def test_unattributed_launch_fails_the_run(tmp_path):
    done, log = _nested_pytest(tmp_path, UNATTRIBUTED_TEST)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "1 passed" in out, out
    assert f"{LAUNCH_MARKER}: 1 launch(es) logged" in out, out
    assert "claude -p orphan [test: ?]" in out, out
    assert log.read_text().strip().endswith("[test: ?]")
