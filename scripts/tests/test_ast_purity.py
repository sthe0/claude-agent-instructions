"""The single AST purity predicate (tests/ast_purity.py) that both Stage 6's
verify_command and its final_check import, so "pure guardian" has ONE meaning.

Pins the two directions that matter: a real gate guardian (file I/O, no transport)
reads as pure; the three ordinary ways a guardian could accidentally grow a
subprocess reach — `import subprocess`, `from subprocess import run`, and a bare
`subprocess` name — each read as impure."""
from __future__ import annotations

import subprocess  # noqa: F401  (referenced by the impure sample below)

from agentctl import gates
from ast_purity import IMPURE_ROOTS, impure_names


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
