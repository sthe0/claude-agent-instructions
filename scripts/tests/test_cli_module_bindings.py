"""Every name a cli module reads is bound in that module, and no cli module defines
a name another one defines.

`agentctl.cli` is split into `cli.py` plus per-group `cli_<group>.py` modules. A
function that moved keeps its source byte for byte, so a global it reads resolves
in the NEW module's namespace: a helper, constant or import that stayed behind (or
moved elsewhere) turns into a NameError at the first call, long after the import
succeeded. Import success and `--help` do not exercise it; the suite exercises only
the paths it happens to run. This check covers the whole class by reading each
module's symbol table instead of running its code.

Two properties, both decidable from the source alone:

* every global a module reads (from any scope, annotations included) is bound at
  that module's top level (def, class, assignment, import) or is a builtin;
* no module-level name is DEFINED (def, class, assignment; imports excluded, so the
  re-imports in cli.py are fine) in two of these modules. `register` is the one name
  every `cli_<group>.py` defines by design.

The checker works on source text so its own red direction is pinned below on
synthetic modules; the real-package tests then run it over the files on disk.
"""
from __future__ import annotations

import ast
import builtins
import symtable
from collections import defaultdict
from pathlib import Path

import agentctl

PACKAGE_DIR = Path(agentctl.__file__).parent
MODULE_ATTRS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__",
    "__annotations__", "__loader__",
}
PER_MODULE_INTERFACE = {"register"}


def cli_module_paths() -> list[Path]:
    return [PACKAGE_DIR / "cli.py", *sorted(PACKAGE_DIR.glob("cli_*.py"))]


def _annotation_names(tree: ast.AST) -> set[str]:
    anns = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    anns.append(arg.annotation)
            if n.returns is not None:
                anns.append(n.returns)
        elif isinstance(n, ast.AnnAssign):
            anns.append(n.annotation)
    return {x.id for a in anns for x in ast.walk(a) if isinstance(x, ast.Name)}


def unbound_names(text: str) -> set[str]:
    """Globals `text` reads that its top level never binds and no builtin supplies."""
    top = symtable.symtable(text, "<module>", "exec")
    bound: set[str] = set()
    reads: set[str] = set()

    def walk(table: symtable.SymbolTable, is_top: bool) -> None:
        for s in table.get_symbols():
            name = s.get_name()
            if is_top:
                if s.is_assigned() or s.is_imported() or s.is_namespace():
                    bound.add(name)
                if s.is_referenced():
                    reads.add(name)
            else:
                if s.is_declared_global() and s.is_assigned():
                    bound.add(name)
                if s.is_global() and s.is_referenced():
                    reads.add(name)
        for child in table.get_children():
            walk(child, False)

    walk(top, True)
    reads |= _annotation_names(ast.parse(text))
    return reads - bound - set(dir(builtins)) - MODULE_ATTRS


def defined_names(text: str) -> set[str]:
    """Names the module's top level defines (def, class, assignment), imports excluded."""
    out: set[str] = set()
    for stmt in ast.parse(text).body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                out |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
    return out


def duplicate_definitions(texts: dict[str, str]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = defaultdict(list)
    for module, text in texts.items():
        for name in defined_names(text) - PER_MODULE_INTERFACE:
            owners[name].append(module)
    return {name: sorted(mods) for name, mods in owners.items() if len(mods) > 1}


CLEAN = "import os\nLIMIT = 3\n\ndef f(x):\n    return os.path.join(str(LIMIT), x)\n"


def test_checker_accepts_a_self_contained_module():
    assert unbound_names(CLEAN) == set()


def test_checker_flags_a_dropped_import():
    assert unbound_names(CLEAN.replace("import os\n", "")) == {"os"}


def test_checker_flags_a_constant_left_in_another_module():
    assert unbound_names(CLEAN.replace("LIMIT = 3\n", "")) == {"LIMIT"}


def test_checker_flags_an_annotation_only_name():
    assert unbound_names("def f(x: Missing) -> None:\n    pass\n") == {"Missing"}


def test_checker_accepts_names_bound_by_global_statements():
    text = "def init():\n    global CACHE\n    CACHE = {}\n\ndef use():\n    return CACHE\n"
    assert unbound_names(text) == set()


def test_checker_flags_a_constant_duplicated_across_modules():
    texts = {"cli_a": "LIMIT = 3\n", "cli_b": "LIMIT = 3\n", "cli_c": "OTHER = 1\n"}
    assert duplicate_definitions(texts) == {"LIMIT": ["cli_a", "cli_b"]}


def test_checker_exempts_the_per_module_register_and_re_imports():
    texts = {
        "cli": "from .cli_a import helper\n",
        "cli_a": "def helper():\n    pass\n\ndef register(sub, add):\n    pass\n",
        "cli_b": "from .cli_a import helper\n\ndef register(sub, add):\n    pass\n",
    }
    assert duplicate_definitions(texts) == {}


def test_every_cli_module_binds_every_global_it_reads():
    problems = {
        path.name: sorted(missing)
        for path in cli_module_paths()
        if (missing := unbound_names(path.read_text()))
    }
    assert problems == {}


def test_no_global_is_defined_in_two_cli_modules():
    texts = {path.name: path.read_text() for path in cli_module_paths()}
    assert duplicate_definitions(texts) == {}
