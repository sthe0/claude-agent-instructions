"""Tenet definition + runner for the semantic-conflict behavioural eval.

A *tenet* is a named invariant the instruction system must satisfy, expressed as a behavioural
assertion over a candidate edit. A candidate that engages a tenet's protected behaviour with the
wrong polarity is a class-2 (semantic) conflict — invisible to git, caught here.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"\w+", text)}


@dataclass(frozen=True)
class Candidate:
    """A proposed edit / synthesized invariant under evaluation."""

    directive: str        # the behaviour the candidate asserts
    affirms: bool = True   # True = "do X"; False = "do NOT X" (the polarity that flips meaning)


@dataclass(frozen=True)
class Tenet:
    """A protected behavioural invariant. ``must_affirm`` is the polarity the system requires."""

    name: str
    description: str
    protected_terms: frozenset[str]
    must_affirm: bool = True
    min_overlap: int = 2  # how many protected terms must appear before the tenet is engaged

    def engaged_by(self, candidate: Candidate) -> bool:
        """Is the candidate about this tenet's protected behaviour?"""
        return len(self.protected_terms & _tokens(candidate.directive)) >= self.min_overlap

    def holds(self, candidate: Candidate) -> bool:
        """True if the candidate preserves the tenet (or does not engage it)."""
        if not self.engaged_by(candidate):
            return True  # tenet not engaged -> trivially preserved
        return candidate.affirms == self.must_affirm


@dataclass
class TenetResult:
    tenet: Tenet
    engaged: bool
    passed: bool


@dataclass
class EvalReport:
    results: list[TenetResult] = field(default_factory=list)

    @property
    def conflicts(self) -> list[Tenet]:
        return [r.tenet for r in self.results if r.engaged and not r.passed]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


def load_tenets() -> list[Tenet]:
    """Discover every TENET exported by a module in the ``tenets`` subpackage."""
    from . import tenets as tenets_pkg

    found: list[Tenet] = []
    for mod in pkgutil.iter_modules(tenets_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{tenets_pkg.__name__}.{mod.name}")
        tenet = getattr(module, "TENET", None)
        if isinstance(tenet, Tenet):
            found.append(tenet)
    return found


def evaluate(candidate: Candidate, tenets: list[Tenet] | None = None) -> EvalReport:
    tenets = tenets if tenets is not None else load_tenets()
    return EvalReport(results=[
        TenetResult(tenet=t, engaged=t.engaged_by(candidate), passed=t.holds(candidate))
        for t in tenets
    ])


def has_semantic_conflict(candidate: Candidate, tenets: list[Tenet] | None = None) -> bool:
    return evaluate(candidate, tenets).has_conflict
