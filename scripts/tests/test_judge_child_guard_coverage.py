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

The check that VALIDATES each guard's shape is likewise an ALLOWLIST, not a
denylist. Only one test expression is accepted:

    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0

Every other shape is refused — including semantically-equivalent positive forms
like `JUDGE_CHILD_ENV_VAR in os.environ`, walrus wrappers, and any comparison
against `None` / `""`. This inverts an earlier denylist that enumerated
"negative shapes to reject" (UnaryOp(Not), Compare(NotEq), Compare(NotIn)) and
grew a hole: three further inversions (`== None`, `is None`, `in (None, "")`,
and a walrus around any of them) mean "marker ABSENT" without matching the
rejected node kinds, so a hook written that way would silently disable itself
everywhere EXCEPT inside a judge child — the exact inverse of the guard, and
the shape that would rejoin the recursion this stage exists to stop.
Extending the rejection list by one more entry each time such a shape is
spotted reproduces the very defect this stage was created to repair. An
allowlist closes the class instead of one entry at a time.

The trade-off is explicit: a legitimate positive form outside the allowlist
(e.g. `if JUDGE_CHILD_ENV_VAR in os.environ:`) is refused too, and the
rejection message names the accepted shape so the author can tell "the
predicate has not caught up with a genuinely new form" from "this shape does
not mean what you think it means".

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


_ACCEPTED_GUARD_SOURCE = (
    f"    if os.environ.get({_GUARD_ENV_NAME}):\n"
    f"        return 0"
)


def _guard_shape_reason(main: ast.FunctionDef) -> str | None:
    """Return None if `main` opens with the accepted guard; otherwise a short
    description of the shape that was seen instead. The guard must be `main()`'s
    FIRST statement (a guard placed after the ledger call, the stdin read or the
    judge call has already done the thing it exists to prevent) and its test
    must match the single accepted shape (see `_test_is_accepted_shape`)."""
    first = main.body[0]
    if not isinstance(first, ast.If):
        return f"first statement is {type(first).__name__}, not an `if`"
    if not _test_is_accepted_shape(first.test):
        return f"test is `{ast.unparse(first.test)}` — not the accepted shape"
    if not any(
        isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value == 0
        for stmt in first.body
    ):
        return "guard body does not `return 0`"
    return None


def _guard_is_first_statement(main: ast.FunctionDef) -> bool:
    """Boolean wrapper over `_guard_shape_reason` for callers that only need a
    verdict."""
    return _guard_shape_reason(main) is None


def _test_is_accepted_shape(node: ast.AST) -> bool:
    """ALLOWLIST predicate. The accepted shape is exactly the expression

        os.environ.get(JUDGE_CHILD_ENV_VAR)

    used as an `if` test — a `Call` on `os.environ.get` with one positional
    argument that is the `Name` `JUDGE_CHILD_ENV_VAR`, no keywords. Any other
    node kind is refused: negations (`not …`, `!=`, `not in`), equality/is
    tests against `None` or the empty string, membership in a container of
    falsy values, walrus wrappers around any of those, `bool(…)` calls,
    boolean operators — every shape the denylist could and could not enumerate.
    Refused shapes include forms that are semantically POSITIVE (`in os.environ`,
    a walrus without a comparison); the trade-off is stated in the module
    docstring."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get"):
        return False
    receiver = func.value
    if not (isinstance(receiver, ast.Attribute) and receiver.attr == "environ"):
        return False
    if not (isinstance(receiver.value, ast.Name) and receiver.value.id == "os"):
        return False
    if node.keywords or len(node.args) != 1:
        return False
    arg = node.args[0]
    return isinstance(arg, ast.Name) and arg.id == _GUARD_ENV_NAME


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
    unguarded: list[tuple[str, str]] = []
    for path, tree in _hook_trees():
        if not _calls_a_judge(tree):
            continue
        main = _main_function(tree)
        if main is None:
            unguarded.append((path.name, "no `main()` function"))
            continue
        reason = _guard_shape_reason(main)
        if reason is not None:
            unguarded.append((path.name, reason))

    assert unguarded == [], (
        "judge-calling hook(s) without the accepted judge-child guard as "
        "main()'s first statement:\n"
        + "\n".join(f"  - {name}: {reason}" for name, reason in unguarded)
        + "\nThe ONLY accepted shape is:\n"
        + _ACCEPTED_GUARD_SOURCE
        + "\nThis is an ALLOWLIST — even semantically-equivalent positive forms "
        "(`in os.environ`, walrus, etc.) are refused. Either write the guard in "
        "the accepted shape and add a test that drives the hook with the marker "
        "set, or, if the refused shape is a genuinely new positive form the "
        "predicate has not caught up with, extend `_test_is_accepted_shape` "
        "here to admit it (and add a positive-control test for the new form)."
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


def test_the_guard_check_rejects_every_shape_that_is_not_the_accepted_one():
    """RED arm for the allowlist. Each shape below either INVERTS the guard
    (body reachable when the marker is ABSENT — disables the hook everywhere
    except inside a judge child, the exact defect this stage exists to close)
    or is a plausible non-accepted expression a future author might reach for.
    Every one must be refused; a control that accepts the negation of what it
    checks is not a control.

    The first three shapes are the reason this predicate was rewritten from a
    denylist to an allowlist: `== None`, `is None`, and `in (None, "")` each
    mean "marker ABSENT" without matching the previous rejection list's
    `UnaryOp(Not)` / `Compare(NotEq)` / `Compare(NotIn)` kinds, so they slipped
    through undetected. A walrus wrapping any of them inherits the same gap.
    """
    def _mk(test_line: str) -> ast.FunctionDef:
        src = (
            "def main():\n"
            f"    {test_line}\n"
            "        return 0\n"
            "    return 0\n"
        )
        return _main_function(ast.parse(src))

    # The three inversion shapes that motivated this rewrite — each equivalent
    # in effect to `if not os.environ.get(MARKER):` and each invisible to the
    # previous denylist.
    eq_none = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR) == None:")
    is_none = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR) is None:")
    in_none_or_empty = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR) in (None, ''):")
    walrus_is_none = _mk(
        "if (m := os.environ.get(JUDGE_CHILD_ENV_VAR)) is None:"
    )

    # The three inversion shapes the old denylist did catch — kept here to
    # prove the allowlist keeps rejecting them, not only the new ones.
    unary_not = _mk("if not os.environ.get(JUDGE_CHILD_ENV_VAR):")
    not_equal = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR) != '1':")
    not_in = _mk("if JUDGE_CHILD_ENV_VAR not in os.environ:")

    # Plausible additional shapes an author might write that mean "absent" or
    # simply differ from the accepted expression — all refused by the allowlist.
    len_zero = _mk("if len(os.environ.get(JUDGE_CHILD_ENV_VAR) or '') == 0:")
    not_bool = _mk("if not bool(os.environ.get(JUDGE_CHILD_ENV_VAR)):")
    is_empty_string = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR) == '':")

    # Semantically POSITIVE forms that are still refused because the allowlist
    # names one shape and only one; the rejection message tells the author.
    in_environ = _mk("if JUDGE_CHILD_ENV_VAR in os.environ:")
    walrus_truthy = _mk("if m := os.environ.get(JUDGE_CHILD_ENV_VAR):")
    os_getenv = _mk("if os.getenv(JUDGE_CHILD_ENV_VAR):")
    with_default = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR, ''):")

    for main in (
        eq_none, is_none, in_none_or_empty, walrus_is_none,
        unary_not, not_equal, not_in,
        len_zero, not_bool, is_empty_string,
        in_environ, walrus_truthy, os_getenv, with_default,
    ):
        assert _guard_is_first_statement(main) is False, (
            f"allowlist accepted a non-accepted shape: {ast.unparse(main.body[0].test)}"
        )

    # And the accepted shape must still be accepted.
    positive = _mk("if os.environ.get(JUDGE_CHILD_ENV_VAR):")
    assert _guard_is_first_statement(positive) is True


def test_the_predicate_admits_all_five_real_hook_guards():
    """Positive control. An allowlist is only useful if it admits the shapes
    that actually appear in the tree — otherwise it is a rule that will be
    suppressed on first contact with real code. The five hooks below carry the
    marker guard today (four are judge-callers; `hook-self-improvement-reminder`
    is guarded defensively because its OUTPUT lands inside a judge). The
    predicate must accept every one of them, unmodified."""
    expected = {
        "hook-turn-end-gate.py",
        "hook-escalation-diagnosis-gate.py",
        "hook-self-improvement-reminder.py",
        "hook-plan-delivery-gate.py",
        "hook-deferring-disposition-gate.py",
    }
    seen: set[str] = set()
    for path, tree in _hook_trees():
        if path.name not in expected:
            continue
        seen.add(path.name)
        main = _main_function(tree)
        assert main is not None, f"{path.name}: no main() function"
        reason = _guard_shape_reason(main)
        assert reason is None, (
            f"{path.name}: allowlist refused the real guard shape ({reason}). "
            "Either this hook now writes its guard differently (fix the hook) "
            "or the allowlist has drifted from the accepted shape (fix here)."
        )
    missing = expected - seen
    assert missing == set(), (
        f"positive control found no such hook file(s): {sorted(missing)} — "
        "the file was renamed or removed. Update the expected set here after "
        "verifying the new file still carries the guard."
    )


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
