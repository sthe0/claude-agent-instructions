"""Standing guard over the judge-child guard itself.

The four hooks that consult a judge each carry the same three lines at the top
of `main()`:

    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0

Without them a judge subprocess runs the fleet's own hooks, each of which may
consult a judge, which runs the hooks again — the measured 126-deep recursion
that exhausted a 5-hour quota window in 48 minutes. Config-root isolation is the
structural fix; this marker is the belt for the case where a hook is reached
some other way (a session that exports the ambient root, a future launcher).

The guard set began as a DENYLIST — the two hooks somebody happened to notice —
which is why this file exists: it derives the set that NEEDS a guard from the
tree instead of from memory, so a new judge-calling hook fails here on the
commit that adds it rather than in a quota fire.

Scope, stated honestly: a hook counts as judge-calling when it imports
`agentctl.advisor` (any alias) or reaches a judge seam in `lib.host_llm` /
`lib.marker_extract` directly. TRANSITIVE reach is deliberately not enumerated —
`agentctl.cli` imports the advisor, so a transitive rule would demand the guard
of every hook that touches the engine at all, including ones that never spawn
anything, and a rule that over-demands gets suppressed rather than obeyed. The
residual: a hook that reaches a judge only through a third module is invisible
here. `hook-self-improvement-reminder.py` is the live example of the other
direction — it calls no judge, and is guarded anyway because its OUTPUT lands
inside one; extra guards are never failures here, only missing ones are.
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

_JUDGE_SEAM_MODULES = frozenset({
    "agentctl.advisor",
    "lib.marker_extract",
})
# `lib.host_llm` is NOT in that set: every guarded hook imports it for the marker
# constant itself, so treating the module as a seam would make the rule
# self-fulfilling. These two names are what actually spawns a judge from it.
_JUDGE_SEAM_NAMES = frozenset({"build_prompt_argv", "isolated_run_kwargs"})
_GUARD_ENV_NAME = "JUDGE_CHILD_ENV_VAR"


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module name this file imports, in dotted form — `import a.b` and
    `from a import b` both yield "a.b", so a seam is found whichever way it was
    reached."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _calls_a_judge(tree: ast.Module) -> bool:
    if _imported_modules(tree) & _JUDGE_SEAM_MODULES:
        return True
    return any(
        (isinstance(node, ast.Attribute) and node.attr in _JUDGE_SEAM_NAMES)
        or (isinstance(node, ast.Name) and node.id in _JUDGE_SEAM_NAMES)
        for node in ast.walk(tree)
    )


def _main_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


def _guard_is_first_statement(main: ast.FunctionDef) -> bool:
    """The guard must be `main()`'s FIRST statement, not merely present: a guard
    placed after the ledger call, the stdin read or the judge call has already
    done the thing it exists to prevent. The test must also be POSITIVE
    (`if os.environ.get(MARKER):` — enter the body when the marker is present):
    a negated form like `if not os.environ.get(MARKER): return 0` reads at a
    glance like a guard but disables the hook everywhere EXCEPT inside a judge
    child — the inverse of what the guard exists to enforce."""
    first = main.body[0]
    if not isinstance(first, ast.If):
        return False
    if _GUARD_ENV_NAME not in {n.id for n in ast.walk(first.test) if isinstance(n, ast.Name)}:
        return False
    if _marker_is_under_negation(first.test):
        return False
    return any(
        isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value == 0
        for stmt in first.body
    )


def _marker_is_under_negation(node: ast.AST) -> bool:
    """True if `JUDGE_CHILD_ENV_VAR` appears inside a boolean negation of the
    test — a `not ...` unary op or a `!=` / `not in` comparison. Either shape
    makes the body reachable when the marker is ABSENT, which is exactly what a
    judge-child guard must never do.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _references_marker(node.operand)
    if isinstance(node, ast.Compare):
        if any(isinstance(op, (ast.NotEq, ast.NotIn)) for op in node.ops):
            return _references_marker(node)
    return any(_marker_is_under_negation(child) for child in ast.iter_child_nodes(node))


def _references_marker(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == _GUARD_ENV_NAME
        for n in ast.walk(node)
    )


def _hook_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SCRIPTS_DIR.glob("hook-*.py"))
    ]


def test_the_enumeration_finds_the_hooks_it_is_meant_to_cover():
    """A coverage test over an empty set passes vacuously, so pin the floor: the
    four judge-calling hooks known when this guard was written must still be
    found. A fifth is welcome — a third is a broken enumerator."""
    judge_callers = {path.name for path, tree in _hook_trees() if _calls_a_judge(tree)}

    assert judge_callers >= {
        "hook-turn-end-gate.py",
        "hook-escalation-diagnosis-gate.py",
        "hook-plan-delivery-gate.py",
        "hook-deferring-disposition-gate.py",
    }


def test_every_judge_calling_hook_guards_against_its_own_child():
    unguarded = []
    for path, tree in _hook_trees():
        if not _calls_a_judge(tree):
            continue
        main = _main_function(tree)
        if main is None or not _guard_is_first_statement(main):
            unguarded.append(path.name)

    assert unguarded == [], (
        f"judge-calling hook(s) without the judge-child guard as main()'s first "
        f"statement: {unguarded}. Add:\n"
        f"    if os.environ.get({_GUARD_ENV_NAME}):\n"
        f"        return 0\n"
        f"and a test that drives the hook with the marker set."
    )


def test_the_guard_check_rejects_a_guard_that_runs_too_late():
    """RED arm for the predicate itself, against a mutated world rather than a
    mutated check: the same guard, one statement lower, must not satisfy it."""
    late = ast.parse(
        "def main():\n"
        "    judge_ledger.hook_start('x')\n"
        "    if os.environ.get(JUDGE_CHILD_ENV_VAR):\n"
        "        return 0\n"
        "    return 0\n"
    )
    first = ast.parse(
        "def main():\n"
        "    if os.environ.get(JUDGE_CHILD_ENV_VAR):\n"
        "        return 0\n"
        "    return 0\n"
    )

    assert _guard_is_first_statement(_main_function(late)) is False
    assert _guard_is_first_statement(_main_function(first)) is True


def test_the_guard_check_rejects_a_guard_whose_test_is_inverted():
    """RED arm for the negated shape. `if not os.environ.get(MARKER): return 0`
    passes any check that only asks whether the marker NAME appears in the test,
    but disables the hook everywhere EXCEPT inside a judge child — the inverse
    of what the guard exists to enforce. A control that accepts the negation of
    what it checks is not a control; each shape below is a mutation the
    predicate must reject.
    """
    unary_not = ast.parse(
        "def main():\n"
        "    if not os.environ.get(JUDGE_CHILD_ENV_VAR):\n"
        "        return 0\n"
        "    return 0\n"
    )
    not_equal = ast.parse(
        "def main():\n"
        "    if os.environ.get(JUDGE_CHILD_ENV_VAR) != '1':\n"
        "        return 0\n"
        "    return 0\n"
    )
    not_in = ast.parse(
        "def main():\n"
        "    if JUDGE_CHILD_ENV_VAR not in os.environ:\n"
        "        return 0\n"
        "    return 0\n"
    )
    positive = ast.parse(
        "def main():\n"
        "    if os.environ.get(JUDGE_CHILD_ENV_VAR):\n"
        "        return 0\n"
        "    return 0\n"
    )

    assert _guard_is_first_statement(_main_function(unary_not)) is False
    assert _guard_is_first_statement(_main_function(not_equal)) is False
    assert _guard_is_first_statement(_main_function(not_in)) is False
    assert _guard_is_first_statement(_main_function(positive)) is True


def test_the_seam_check_rejects_a_hook_that_calls_no_judge():
    """RED arm for the other predicate: a hook importing only the engine's state
    must not be dragged into the guarded set, or the rule over-demands and gets
    suppressed."""
    assert _calls_a_judge(ast.parse("from agentctl.state import SessionState")) is False
    assert _calls_a_judge(ast.parse("from lib.host_llm import JUDGE_CHILD_ENV_VAR")) is False, (
        "importing the marker constant must not by itself make a hook a judge caller"
    )
    assert _calls_a_judge(ast.parse("from agentctl import advisor")) is True
    assert _calls_a_judge(ast.parse("from agentctl import advisor as _advisor")) is True
    assert _calls_a_judge(ast.parse("host_llm.isolated_run_kwargs()")) is True
