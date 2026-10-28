"""Planted defects for the order-understanding tests: proof that a test goes red.

A test that has only ever passed proves nothing about its own sensitivity. Each mutant
below plants ONE real defect in the production code under test, by monkeypatching, and a
test earns trust only when the suite fails with the mutant active. The mutant is chosen by
the environment variable `ORDER_AGREEMENT_MUTANT`, so a subprocess (or a person at the
shell) can run the same test file twice: once clean, once mutated.

    ORDER_AGREEMENT_MUTANT=verdict_unchecked python3 -m pytest scripts/tests/test_order_understanding.py

The table is closed. An unknown name, or a mutant whose target no longer exists, ends the
run with exit code 4 (a usage error) instead of letting the suite pass unmutated — a
mutant that silently does nothing would read as a test that "caught" nothing and then as
a green control. Exit 1 is reserved for what the mutant is for: a test failed.

To add a mutant, add a row to MUTANTS and a test that goes red under it; the other order-
agreement stages share this harness.
"""

from __future__ import annotations

import os
import sys

ENV = "ORDER_AGREEMENT_MUTANT"
USAGE_ERROR = 4


def _verdict_unchecked(mp):
    """The submission seam stops grading a proposal's verdict against PROPOSAL_VERDICTS."""
    mp.setattr("agentctl.submission._invalid_verdicts", lambda proposals: [], raising=True)


def _digest_ignores_constraints(mp):
    """understanding_digest stops hashing the constraints part."""
    from agentctl import plan

    original = plan.understanding_parts
    mp.setattr(
        plan,
        "understanding_parts",
        lambda order: {k: v for k, v in original(order).items() if k != "constraints"},
        raising=True,
    )


def _completeness_unchecked(mp):
    """The completeness grade reports nothing for an incomplete understanding."""
    mp.setattr(
        "agentctl.submission.order_understanding_completeness_violations",
        lambda meta: [],
        raising=True,
    )


def _place_contributes_when_empty(mp):
    """order_place / order_scope add the understanding even when the order states none,
    which moves the key of every order that predates it."""
    mp.setattr("agentctl.plan._understanding_empty", lambda order: False, raising=True)


MUTANTS = {
    "verdict_unchecked": _verdict_unchecked,
    "digest_ignores_constraints": _digest_ignores_constraints,
    "completeness_unchecked": _completeness_unchecked,
    "place_contributes_when_empty": _place_contributes_when_empty,
}


def active_mutant() -> str | None:
    """The mutant named in the environment, or None when none is requested."""
    return os.environ.get(ENV) or None


def apply_active_mutant(monkeypatch) -> str | None:
    """Plant the requested mutant through `monkeypatch`; None when none is requested.
    Fails closed (exit 4) on a name outside MUTANTS or a target that does not exist."""
    name = active_mutant()
    if name is None:
        return None
    if name not in MUTANTS:
        _usage_error(f"{ENV}={name!r} is not a known mutant; known: {sorted(MUTANTS)}")
    try:
        MUTANTS[name](monkeypatch)
    except (AttributeError, ImportError) as exc:
        _usage_error(f"mutant {name!r} has no target to patch: {exc}")
    return name


def _usage_error(message: str):
    """End the process with USAGE_ERROR. `pytest.exit` is not used: raised from a fixture
    it crashes pytest's own warnings plugin in sessionfinish and the run ends with exit 1,
    the code that means 'a test caught the mutant'; raised at import it is a collection
    error, exit 2. Neither may be mistaken for a kill."""
    sys.stderr.write(f"order_agreement_mutants: {message}\n")
    sys.stderr.flush()
    os._exit(USAGE_ERROR)
