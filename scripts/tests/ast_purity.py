"""AST-based purity predicate for the coordination gates.

`impure_names(obj)` returns the set of impure module roots a function or module
*reaches* in its own source — where "impure" means the transport-capable roots
{subprocess, socket, urllib, requests, http}. A gate guardian (gates.py) is allowed
to do file I/O (open, pathlib) — reading recorded state is the whole point — but must
never shell out or touch the network; the semantic cognition that does (the thinker
review, the acceptance judge) lives in the impure cli layer, and a guardian only reads
the RECORD it left behind. This is the single definition of "pure" that both Stage 6's
verify_command and the corresponding final_check import, so the plan cannot assert two
different meanings of the word.

A name is reported impure when its root is in the impure set and it is reached via:
  - `import subprocess` / `import subprocess as sp`      (Import — real module name, not the alias)
  - `from subprocess import run`                          (ImportFrom — the module root)
  - `subprocess.run(...)`                                 (Attribute chain whose leftmost Name is impure)
  - a bare `subprocess` reference                         (a Load-context Name whose id is impure)

Honest limits (stated so no one over-trusts this): it is a SYNTACTIC check over the
object's OWN source only. It does not follow calls into helpers, and it cannot see
dynamic evasion — `__import__('subprocess')`, `importlib.import_module('subprocess')`,
`getattr(os, 'system')`, or an impure reach hidden behind a string fed to eval/exec all
pass. It is a guardrail against the ORDINARY, honest way a guardian would accidentally
grow a subprocess call, not a sandbox against a hostile author.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

# Transport-capable roots. Deliberately NOT {os, pathlib, io, ...}: file I/O is allowed
# in a gate (it reads recorded state); only shelling out / networking is forbidden.
IMPURE_ROOTS = frozenset({"subprocess", "socket", "urllib", "requests", "http"})


def _attr_chain_root(node: ast.Attribute) -> ast.AST:
    """The leftmost node of an attribute chain: for `a.b.c` returns the `a` Name."""
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur


def impure_names(module_or_function) -> set[str]:
    """Return the set of impure module roots reached in the object's own source.

    Accepts a module or a function/method. Parses via inspect.getsource + dedent (a
    nested/indented def would otherwise be un-parseable). Empty set == pure (no
    transport-capable reach), which is the admit condition for a gate guardian."""
    source = textwrap.dedent(inspect.getsource(module_or_function))
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in IMPURE_ROOTS:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in IMPURE_ROOTS:
                    found.add(root)
        elif isinstance(node, ast.Attribute):
            root = _attr_chain_root(node)
            if isinstance(root, ast.Name) and root.id in IMPURE_ROOTS:
                found.add(root.id)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in IMPURE_ROOTS:
                found.add(node.id)

    return found
