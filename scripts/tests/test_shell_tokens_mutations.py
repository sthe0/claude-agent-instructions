"""Mutation-testing control for `scripts/lib/shell_tokens.py::_removal_regions`.

`test_shell_tokens_nonwidening.py` (D1/D1a/D1c/D1e/D2/D3/R6/S1) proves the two
appliers agree with each other and with two independent frozen reference
walks over measured corpora -- but every one of those controls is a
DIFFERENTIAL comparison: it can only see a difference if some OTHER thing in
the suite already disagrees with the code under test. A decision that
`_removal_regions` makes which happens to agree with every hand-written
reference and every measured command is invisible to all of them. This module
answers the complementary question directly: does EVERY decision-bearing
node in `_removal_regions`'s own body actually change its output on SOME
command, under SOME consumer set? That is mutation testing, not differential
testing, and it needs its own machinery:

- A MECHANICAL CENSUS (`test_every_narrowing_site_is_observable`) walks
  `_removal_regions`'s AST, forces each decision-bearing node in isolation
  (an `If`/`IfExp` test to each constant boolean, a `While` test to `False`,
  a `For` iterable to empty, a `regions.append(...)` call to a no-op), and
  measures whether the mutant's output differs from the shipped function's on
  at least one command from a PROBE UNIVERSE -- itself asserted to strictly
  contain the smaller POPULATION the count is reported against, so "zero
  blind mutations" is a measured fact about two different-sized sets, not an
  artifact of comparing a set to itself.
- A DOMAIN-CLOSURE proof (`test_domain_is_closed_under_decision_bearing_kinds`)
  is the complement of the census's own node-kind filter: rather than trust
  that a golden SITE COUNT would drop if the filter silently stopped
  recognizing some kind, it separately asserts that every decision-bearing
  AST kind actually present in the function is accounted for by the filter,
  so a missing kind is caught even on a build where the count happens to stay
  the same.
- A hand-authored MUTATION_CATALOGUE (`test_every_catalogued_mutation_is_
  caught`) targets the helper functions `_removal_regions` CALLS
  (`_consumer_ok`, `_body_inert`, `_holds_multiple_statements`) and the
  wrapper-level guard `_recognized` that CALLS `_removal_regions` -- every one
  of these lives outside `_removal_regions`'s own AST body, so no census
  scoped to that one function's nodes could ever find them. These are
  INSERTION-class mutations in the sense that matters here: decisions the
  census's domain structurally excludes, not decisions the source lacks.
- `MODULE_CONTRACT_SHA256` pins the three production modules this migration
  touched (replacing an earlier `git diff --quiet <rev>` check that had no
  escape hatch for a legitimate review-driven edit) -- every count and every
  catalogue entry above is measured against ONE fixed shape of
  `_removal_regions`, and a real edit to it must re-derive them, not silently
  invalidate them.
- `test_mutation_module_performs_no_write` proves, by walking THIS module's
  own AST, that every mutation above happens strictly in memory (a
  deep-copied/recompiled AST executed into a throwaway namespace, or a
  string-patched source string compiled the same way) and never writes to
  the tracked `shell_tokens.py` file this module reads.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import signal
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import shell_tokens  # noqa: E402

from test_shell_tokens_nonwidening import CASES, GRID_COMMANDS  # noqa: E402

THIS_FILE = Path(__file__).resolve()
SHELL_TOKENS_PATH = SCRIPTS_DIR / "lib" / "shell_tokens.py"
BASH_WRITE_TARGETS_PATH = SCRIPTS_DIR / "lib" / "bash_write_targets.py"
GUARD_HOOK_PATH = SCRIPTS_DIR / "hook-guard-canon-readonly.py"

# Pins the exact shape `_removal_regions` had when NARROWING_SITES_EXPECTED,
# MUTATIONS_EXPECTED and MUTATION_CATALOGUE's anchor lines were measured. A
# legitimate review-driven edit re-derives all three here rather than the
# suite silently going stale against a moved target -- the escape hatch a
# bare `git diff --quiet <rev>` check does not have.
MODULE_CONTRACT_SHA256 = {
    SHELL_TOKENS_PATH: "38816fc42ec4687f0c779a1dd2540e0c6a4eb6af466502dd5432ad115e1d38ed",
    BASH_WRITE_TARGETS_PATH: "ad24b259c12959974385d8714d9967bdcb50359698a4819c793766cc36ae9f9b",
    GUARD_HOOK_PATH: "dec99146110846fa5c599755b445610ff80d0f52ad17c65b77af3105c7c45a74",
}


def test_production_modules_match_their_pinned_digest():
    for path, digest in MODULE_CONTRACT_SHA256.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest, (
            f"{path} has drifted from its pinned digest ({actual} != {digest}) -- "
            "a legitimate review-driven edit re-pins here, and re-derives "
            "NARROWING_SITES_EXPECTED/MUTATIONS_EXPECTED/MUTATION_CATALOGUE's "
            "anchor lines against the new shape, rather than trusting stale "
            "golden values silently."
        )


_WRITE_CALL_NAMES = frozenset({
    "write_text", "write_bytes", "write", "writelines", "unlink", "rmdir",
    "remove", "rename", "replace", "copy", "copyfile", "move", "chmod",
    "touch", "mkdir",
})

# `_WRITE_CALL_NAMES` overlaps common `str`/`dict` method names ("replace",
# "copy") that carry no filesystem meaning at all -- `base_src.replace(...)`
# (patching a source STRING, see `test_every_catalogued_mutation_is_caught`)
# is exactly such a call. Only a call whose RECEIVER is one of this module's
# known `Path` objects can plausibly be a real write; gate on that receiver
# rather than on the bare method name.
_PATH_LIKE_RECEIVER_NAMES = frozenset({
    "SHELL_TOKENS_PATH", "BASH_WRITE_TARGETS_PATH", "GUARD_HOOK_PATH",
    "THIS_FILE", "path",
})


def test_mutation_module_performs_no_write():
    """Walks THIS module's own source and asserts no `Call` node targets a
    write-capable name on a `Path`-like receiver, and no `open(...)` call
    passes a writable mode -- every mutation this module performs is a
    deep-copied/recompiled AST or a string-patched source executed into a
    throwaway namespace, never a write to the tracked `shell_tokens.py` file
    it reads."""
    tree = ast.parse(THIS_FILE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if (
            name in _WRITE_CALL_NAMES
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _PATH_LIKE_RECEIVER_NAMES
        ):
            pytest.fail(f"write-capable call {name!r} found at line {node.lineno}")
        if name == "open":
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if any(mode in arg.value for mode in ("w", "a", "x")):
                        pytest.fail(f"open() with a writable mode at line {node.lineno}")


# --- census: every decision-bearing node in _removal_regions's own body ---

NARROWING_SITES_EXPECTED = 33
MUTATIONS_EXPECTED = 58

_MUTATIONS_PER_KIND = {"If": 2, "While": 1, "For": 1, "IfExp": 2, "Append": 1}

# Every AST kind the census's filter treats as decision-bearing (FORCED, via
# the site walk below) plus the kinds argued not to need forcing on their own
# (a BoolOp's operands are themselves If/IfExp tests or comparisons the walk
# already reaches -- forcing the BoolOp's short-circuit shape separately would
# duplicate what forcing its operands' owning If/IfExp already covers).
DECISION_BEARING_KINDS = frozenset({
    "If", "While", "For", "IfExp", "BoolOp", "Try", "Match", "comprehension",
})
FORCED_KINDS = frozenset({"If", "While", "For", "IfExp"})
ARGUED_RESIDUAL_KINDS = frozenset({"BoolOp"})


def _load_removal_regions() -> tuple[ast.Module, ast.FunctionDef]:
    tree = ast.parse(SHELL_TOKENS_PATH.read_text(), filename=str(SHELL_TOKENS_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_removal_regions":
            return tree, node
    raise AssertionError("_removal_regions not found in shell_tokens.py")


def _is_regions_append(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "append"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "regions"
    )


def _census_sites(func: ast.FunctionDef) -> list[tuple[str, int, int]]:
    sites: list[tuple[str, int, int]] = []
    for node in ast.walk(func):
        if node is func:
            continue
        if isinstance(node, ast.If):
            sites.append(("If", node.lineno, node.col_offset))
        elif isinstance(node, ast.While):
            sites.append(("While", node.lineno, node.col_offset))
        elif isinstance(node, ast.For):
            sites.append(("For", node.lineno, node.col_offset))
        elif isinstance(node, ast.IfExp):
            sites.append(("IfExp", node.lineno, node.col_offset))
        elif _is_regions_append(node):
            sites.append(("Append", node.lineno, node.col_offset))
    return sites


def assert_domain_closed(func: ast.FunctionDef) -> None:
    """The census's filter recognizes exactly `{If, While, For, IfExp,
    Append}` as decision-bearing. Rather than trust the golden SITE COUNT to
    drop if that filter silently stopped matching some kind, separately
    assert: every kind in `DECISION_BEARING_KINDS` that is actually PRESENT
    in `_removal_regions`'s body is contained in `FORCED_KINDS |
    ARGUED_RESIDUAL_KINDS` -- so a kind the filter quietly stopped covering
    is caught here even on a build where the count happens to stay the same.
    """
    present = {type(n).__name__ for n in ast.walk(func) if n is not func}
    decision_bearing_present = present & DECISION_BEARING_KINDS
    uncovered = decision_bearing_present - (FORCED_KINDS | ARGUED_RESIDUAL_KINDS)
    assert not uncovered, (
        f"decision-bearing AST kinds present in _removal_regions but not "
        f"accounted for by FORCED_KINDS | ARGUED_RESIDUAL_KINDS: {uncovered}"
    )


def test_domain_is_closed_under_decision_bearing_kinds():
    _, func = _load_removal_regions()
    assert_domain_closed(func)


class _ForceMutation(ast.NodeTransformer):
    """Replaces exactly the node identified by `(kind, lineno, col_offset)`
    with its forced mutant, and counts how many nodes it actually matched --
    a mutation that matched zero or more than one node is a bug in the
    census's own site identification, not a real mutation, and must fail
    loudly rather than silently apply to the wrong (or no) node."""

    def __init__(self, kind: str, lineno: int, col_offset: int, variant: str | None):
        self.kind = kind
        self.lineno = lineno
        self.col_offset = col_offset
        self.variant = variant
        self.matches = 0

    def _at(self, node: ast.AST) -> bool:
        return node.lineno == self.lineno and node.col_offset == self.col_offset

    def visit_If(self, node: ast.If):
        self.generic_visit(node)
        if self.kind == "If" and self._at(node):
            self.matches += 1
            node.test = ast.copy_location(ast.Constant(value=self.variant == "true"), node.test)
        return node

    def visit_While(self, node: ast.While):
        self.generic_visit(node)
        if self.kind == "While" and self._at(node):
            self.matches += 1
            node.test = ast.copy_location(ast.Constant(value=False), node.test)
        return node

    def visit_For(self, node: ast.For):
        self.generic_visit(node)
        if self.kind == "For" and self._at(node):
            self.matches += 1
            node.iter = ast.copy_location(ast.List(elts=[], ctx=ast.Load()), node.iter)
        return node

    def visit_IfExp(self, node: ast.IfExp):
        self.generic_visit(node)
        if self.kind == "IfExp" and self._at(node):
            self.matches += 1
            node.test = ast.copy_location(ast.Constant(value=self.variant == "true"), node.test)
        return node

    def visit_Expr(self, node: ast.Expr):
        self.generic_visit(node)
        if self.kind == "Append" and self._at(node) and _is_regions_append(node):
            self.matches += 1
            return ast.copy_location(ast.Pass(), node)
        return node


def _site_variants(kind: str) -> tuple[str | None, ...]:
    return ("true", "false") if kind in ("If", "IfExp") else (None,)


def _all_mutations(sites: list[tuple[str, int, int]]) -> list[tuple[str, int, int, str | None]]:
    return [
        (kind, lineno, col_offset, variant)
        for kind, lineno, col_offset in sites
        for variant in _site_variants(kind)
    ]


def _build_mutant(base_tree: ast.Module, kind: str, lineno: int, col_offset: int, variant: str | None):
    """Deep-copies `base_tree`, applies exactly one forced mutation, compiles
    and `exec`s the result into a throwaway namespace -- the WHOLE module,
    not just the function, so the mutant's `_removal_regions` still resolves
    its own module-level helpers (`_pipeline_consumers_ok`, `_body_inert`,
    ...) from the SAME mutated copy, consistently."""
    tree = copy.deepcopy(base_tree)
    transformer = _ForceMutation(kind, lineno, col_offset, variant)
    tree = transformer.visit(tree)
    ast.fix_missing_locations(tree)
    assert transformer.matches == 1, (
        f"mutation {kind}@{lineno}:{col_offset} variant={variant} matched "
        f"{transformer.matches} nodes, expected exactly 1"
    )
    code = compile(tree, filename=str(SHELL_TOKENS_PATH), mode="exec")
    namespace: dict = {"__name__": "shell_tokens_census_mutant"}
    exec(code, namespace)  # noqa: S102 -- in-memory only, see test_mutation_module_performs_no_write
    return namespace["_removal_regions"]


# POPULATION is every command the differential controls already measure;
# UNIVERSE additionally folds in the D1a grid (234 generated cells covering
# shapes the hand-picked corpus never holds, e.g. multi-construct sequences)
# -- a STRICT superset, asserted below, so "zero blind mutations over the
# universe" is a fact about two differently-sized sets, not the same set
# compared to itself.
# The differential corpora (CASES/GRID_COMMANDS) were measured to agree
# across applier/reimplementation pairs -- they were never selected to force
# each of `_removal_regions`'s OWN decision-bearing nodes individually,  and
# five sites turn out to be genuinely blind to them. Each witness below is
# hand-traced against the specific forced mutant it targets:
#   - backslash-skip `If` (line 321) forced FALSE: a `\"` immediately before
#     a real `<<EOF` -- unskipped, the escaped quote is read as a real quote
#     open, and the walk never finds a close, returning None where the
#     shipped function returns real regions.
#   - final `quote is None` `IfExp` (line 412) forced TRUE: an unterminated
#     bare quote with no heredoc at all -- the shipped function returns None
#     (quote never closed); the mutant always returns `regions` (`[]`).
#   - comment-skip `IfExp` (line 338) forced TRUE: a leading `#`-comment
#     followed by a REAL heredoc on a later line -- the mutant always jumps
#     to end-of-command, skipping the heredoc the shipped function still
#     finds.
#   - comment-skip `IfExp` (line 338) forced FALSE, and here-string
#     unterminated-quote `If` (line 350) forced FALSE: both leave an index
#     variable unclamped (-1) after a `.find()` miss, producing a genuine
#     infinite loop in the mutant rather than a wrong-but-terminating
#     result -- `_call_bounded`'s timeout is itself the observable
#     difference from the shipped function's clean, immediate return.
_RESIDUAL_SITE_WITNESSES = (
    'cat \\"<<EOF\nbody\nEOF',
    "echo 'abc",
    "# comment\ncat <<EOF\nbody\nEOF",
    "cat file.txt # comment",
    ';cat <<<"abc',
)

POPULATION = tuple(command for _, command in CASES)
UNIVERSE = tuple(dict.fromkeys(
    POPULATION + tuple(GRID_COMMANDS) + _RESIDUAL_SITE_WITNESSES
))

CONSUMER_SETS = (
    shell_tokens.CONSUMERS,
    shell_tokens.CONSUMERS | shell_tokens.NON_SHELL_CONSUMERS,
)

# A mutant that alters loop-control flow (e.g. forcing the comment-skip
# IfExp's else branch, which can leave `i` at -1 forever on a trailing
# unterminated `#`) can genuinely hang instead of returning. That hang is
# itself the observable difference -- the shipped function always
# terminates on this bounded, non-adversarial corpus -- so it is caught via
# a wall-clock guard rather than left to wedge the whole test run.
_MUTANT_TIMEOUT_S = 2


class _MutantTimedOut(Exception):
    pass


def _call_bounded(fn, command: str, consumers: frozenset[str]):
    def _on_alarm(signum, frame):
        raise _MutantTimedOut()

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, _MUTANT_TIMEOUT_S)
    try:
        return ("ok", fn(command, consumers))
    except _MutantTimedOut:
        return ("timeout", None)
    except Exception as exc:  # noqa: BLE001 -- either side may legitimately raise
        return ("raise", type(exc).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_every_narrowing_site_is_observable():
    base_tree, func = _load_removal_regions()

    sites = _census_sites(func)
    assert len(sites) == NARROWING_SITES_EXPECTED, len(sites)

    mutations = _all_mutations(sites)
    assert len(mutations) == MUTATIONS_EXPECTED, len(mutations)
    assert sum(_MUTATIONS_PER_KIND[kind] for kind, *_ in sites) == MUTATIONS_EXPECTED

    assert set(POPULATION) < set(UNIVERSE), (
        "POPULATION must be a STRICT subset of UNIVERSE, or 'zero blind "
        "mutations over the universe' would be measured against a set "
        "compared to itself"
    )

    inert: list[tuple[str, int, int, str | None]] = []
    for kind, lineno, col_offset, variant in mutations:
        mutant_fn = _build_mutant(base_tree, kind, lineno, col_offset, variant)
        caught = False
        for consumers in CONSUMER_SETS:
            for command in UNIVERSE:
                original = _call_bounded(shell_tokens._removal_regions, command, consumers)
                mutant = _call_bounded(mutant_fn, command, consumers)
                if original != mutant:
                    caught = True
                    break
            if caught:
                break
        if not caught:
            inert.append((kind, lineno, col_offset, variant))

    assert inert == [], (
        f"mutations with NO observable effect anywhere in UNIVERSE, under "
        f"either consumer set -- these decisions are structurally present "
        f"but never exercised: {inert}"
    )


# --- hand-authored catalogue: decisions outside the census's own domain ---

# Every entry targets a helper `_removal_regions` CALLS, or the wrapper-level
# `_recognized` that CALLS `_removal_regions` -- none of these live inside
# `_removal_regions`'s own AST body, so `_census_sites` structurally cannot
# find them; only a hand-authored catalogue can.
MUTATION_CATALOGUE = (
    {
        "name": "M-A",
        "description": (
            "Bypass `_consumer_ok`'s allowlist membership check entirely. "
            "`_removal_regions` never tests allowlist membership itself -- it "
            "only calls `_pipeline_consumers_ok`, which delegates here -- so "
            "this decision is outside the census's domain (`_consumer_ok` is "
            "not a node inside `_removal_regions`'s body at all)."
        ),
        "old_line": "    return os.path.basename(words[i]) in consumers",
        "new_line": "    return True  # MUTATION M-A: bypass clause (iv) consumer allowlist",
        "observe": "removal_regions",
        "consumers": "narrow",
        "witness": "myunknowncmd <<'D'\nbody\nD",
        "named_control": "D2 (test_nine_enumerated_constructions_still_deny) / D1a's ACTED_FLOOR_UNKNOWN == 0",
    },
    {
        "name": "M-B",
        "description": (
            "Bypass `_body_inert`'s expansion-trigger check (clause vi) "
            "entirely. `_removal_regions` calls this helper but the helper's "
            "own body is a separate function, outside `_removal_regions`'s "
            "own AST -- the census cannot reach into it."
        ),
        "old_line": "    return delimiter_quoted or not any(ch in text for ch in _EXPANSION_TRIGGERS)",
        "new_line": "    return True  # MUTATION M-B: bypass clause (vi) expansion-inert check",
        "observe": "removal_regions",
        "consumers": "narrow",
        "witness": "cat <<EOF\n$(true)\nEOF",
        "named_control": "D2 (test_body_removal_never_turns_a_real_write_from_deny_into_allow)",
    },
    {
        "name": "M-C",
        "description": (
            "Bypass `_holds_multiple_statements` (clause v) entirely. This "
            "helper is consulted only by the WRAPPER `strip_heredoc_bodies`, "
            "never by `_removal_regions` -- observed via the wrapper, not via "
            "`_removal_regions` directly, since a direct call cannot see it "
            "at all."
        ),
        "old_line": "    return statements > 1",
        "new_line": "    return False  # MUTATION M-C: bypass clause (v) multi-statement check",
        "observe": "strip_heredoc_bodies",
        "consumers": None,
        "witness": "cat <<'EOF' > /tmp/s.sh\necho hi\nEOF\nbash /tmp/s.sh",
        "named_control": "docs/decisions/heredoc-body-neutralization.md's 'write then exec' clause-(v) trade",
    },
    {
        "name": "M-D",
        "description": (
            "Bypass `_recognized`'s own final consumer-allowlist check -- "
            "distinct from `_pipeline_consumers_ok`'s pipeline-local check "
            "inside `_removal_regions` itself, since `_recognized` looks only "
            "at the command's FIRST line. `_recognized` is called only by the "
            "wrapper functions, never by `_removal_regions`. Observed via "
            "`neutralize_heredoc_constructs` rather than `strip_heredoc_"
            "bodies`, because clause (v) (real, unpatched by M-D) would "
            "otherwise mask the flip on the strip path."
        ),
        "old_line": '    return all(_consumer_ok(part, consumers) for part in head.split("|"))',
        "new_line": "    return True  # MUTATION M-D: bypass _recognized's own consumer check",
        "observe": "neutralize_heredoc_constructs",
        "consumers": None,
        "witness": "myunknowncmd\ncat <<'D'\nbody\nD",
        "named_control": "D1a's ACTED_FLOOR_UNKNOWN == 0 / test_widened_consumer_body_introduces_no_new_spurious_deny",
    },
)


def _exec_patched_source(patched_src: str) -> dict:
    tree = ast.parse(patched_src, filename=str(SHELL_TOKENS_PATH))
    code = compile(tree, filename=str(SHELL_TOKENS_PATH), mode="exec")
    namespace: dict = {"__name__": "shell_tokens_catalogue_mutant"}
    exec(code, namespace)  # noqa: S102 -- in-memory only, see test_mutation_module_performs_no_write
    return namespace


def test_every_catalogued_mutation_is_caught():
    base_src = SHELL_TOKENS_PATH.read_text()
    for entry in MUTATION_CATALOGUE:
        occurrences = base_src.count(entry["old_line"])
        assert occurrences == 1, (
            f"{entry['name']}: anchor line must appear exactly once in "
            f"{SHELL_TOKENS_PATH} (found {occurrences}) -- {entry['old_line']!r}"
        )
        patched_src = base_src.replace(entry["old_line"], entry["new_line"], 1)
        namespace = _exec_patched_source(patched_src)
        witness = entry["witness"]

        if entry["observe"] == "removal_regions":
            consumers = (
                shell_tokens.CONSUMERS if entry["consumers"] == "narrow"
                else shell_tokens.CONSUMERS | shell_tokens.NON_SHELL_CONSUMERS
            )
            original = shell_tokens._removal_regions(witness, consumers)
            mutant = namespace["_removal_regions"](witness, consumers)
        else:
            original = getattr(shell_tokens, entry["observe"])(witness)
            mutant = namespace[entry["observe"]](witness)

        assert original != mutant, (
            f"{entry['name']} produced NO observable difference on its own "
            f"witness -- this catalogue entry is not actually catching what "
            f"it claims to (witness={witness!r}, original={original!r}, "
            f"mutant={mutant!r})"
        )


# --- mechanical check on the "narrowed producer" claim's own qualification -

_NARROWING_CLAIM_TARGET = "narrowed producer"
_QUALIFIED_CONSUMER_SET_NAMES = ("CONSUMERS", "NON_SHELL_CONSUMERS", "narrow", "widened")
_TRACKED_TEXT_FILES = (
    SHELL_TOKENS_PATH,
    BASH_WRITE_TARGETS_PATH,
    GUARD_HOOK_PATH,
    SCRIPTS_DIR / "tests" / "test_shell_tokens_nonwidening.py",
    SCRIPTS_DIR / "tests" / "test_shell_tokens_mutations.py",
    SCRIPTS_DIR.parent / "docs" / "decisions" / "heredoc-body-neutralization.md",
)


def test_no_unqualified_narrowing_claim():
    """Every mention of a 'narrowed producer' on the tracked surface must
    name WHICH consumer set (CONSUMERS / NON_SHELL_CONSUMERS / narrow /
    widened) it travels through, within a small window of that line --
    D1's own docstring is explicit that narrowing behaves differently for
    the two appliers (D1 cannot see it narrowed on the shared-producer axis
    at all; D1c can), so an unqualified claim reads as though it were one
    fact instead of two.
    """
    for path in _TRACKED_TEXT_FILES:
        if not path.exists():
            continue
        lines = path.read_text().splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _NARROWING_CLAIM_TARGET not in line:
                continue
            window = "\n".join(lines[max(0, lineno - 3):lineno + 2])
            assert any(name in window for name in _QUALIFIED_CONSUMER_SET_NAMES), (
                f"{path}:{lineno}: unqualified 'narrowed producer' claim -- "
                f"name which consumer set (CONSUMERS/NON_SHELL_CONSUMERS/"
                f"narrow/widened) nearby.\n{line}"
            )
