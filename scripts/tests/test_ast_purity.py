"""The single AST purity predicate (tests/ast_purity.py) that both Stage 6's
verify_command and its final_check import, so "pure guardian" has ONE meaning.

Pins the two directions that matter: a real gate guardian (file I/O, no transport)
reads as pure; the three ordinary ways a guardian could accidentally grow a
subprocess reach — `import subprocess`, `from subprocess import run`, and a bare
`subprocess` name — each read as impure."""
from __future__ import annotations

import subprocess  # noqa: F401  (referenced by the impure sample below)
from unittest.mock import patch

import pytest

from agentctl import gates
from ast_purity import IMPURE_ROOTS, _LINECACHE_HINT, impure_names


# --- the real guardians are pure --------------------------------------------

def test_gates_module_is_pure():
    assert impure_names(gates) == set()


def test_acceptance_review_blockers_is_pure():
    assert impure_names(gates.acceptance_review_blockers) == set()


def test_plan_review_blockers_is_pure():
    # The sibling guardian this one mirrors — also file-I/O-only, also pure.
    assert impure_names(gates.plan_review_blockers) == set()


# --- a pure sample: file I/O is explicitly allowed ---------------------------

def test_file_io_sample_reads_pure():
    def reads_a_file(path):
        with open(path) as fh:
            return fh.read()

    assert impure_names(reads_a_file) == set()


# --- the three ordinary impure reaches are each caught -----------------------

def test_import_subprocess_is_impure():
    def shells_out():
        import subprocess
        return subprocess.run(["true"])

    assert "subprocess" in impure_names(shells_out)


def test_from_subprocess_import_is_impure():
    def shells_out():
        from subprocess import run
        return run(["true"])

    assert "subprocess" in impure_names(shells_out)


def test_bare_subprocess_name_is_impure():
    def shells_out():
        return subprocess.run(["true"])  # module-level import, bare attribute chain

    assert "subprocess" in impure_names(shells_out)


def test_impure_roots_are_transport_only():
    # File-I/O roots are deliberately NOT impure — a guardian reads recorded state.
    assert "subprocess" in IMPURE_ROOTS and "socket" in IMPURE_ROOTS
    assert "os" not in IMPURE_ROOTS and "pathlib" not in IMPURE_ROOTS


# --- concurrent-edit / linecache desync hint ---------------------------------

def test_oserror_from_getsource_carries_linecache_hint():
    """OSError from inspect.getsource (source unlocatable) re-raises with a hint
    about linecache desync so the failure is not mistaken for a code regression."""
    with patch("ast_purity.inspect.getsource", side_effect=OSError("could not get source")):
        with pytest.raises(OSError, match="linecache"):
            impure_names(lambda: None)


def test_syntaxerror_from_ast_parse_carries_linecache_hint():
    """SyntaxError from ast.parse (e.g. garbled source from a concurrent edit)
    re-raises with a hint about linecache desync."""
    with patch("ast_purity.inspect.getsource", return_value="def f(\n  # truncated"):
        with pytest.raises(SyntaxError, match="linecache"):
            impure_names(lambda: None)


def test_linecache_hint_text_present():
    """The hint constant is non-empty and mentions the key diagnostic terms."""
    assert "linecache" in _LINECACHE_HINT
    assert "concurrent" in _LINECACHE_HINT
    assert "Re-run" in _LINECACHE_HINT
