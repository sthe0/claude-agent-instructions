"""Behavioural eval substrate for semantic (class-2) conflicts (ADR-0001 driver D3, open Q#3).

Git sees text conflicts (class 1); it is blind to *meaning* conflicts — an edit whose words
don't collide with another but whose behaviour contradicts a rule the system must uphold. This
substrate encodes such rules as **tenets**: named, executable behavioural assertions. The runner
evaluates a candidate synthesized invariant (from the synthesizer, stage 11) against the tenet
set; a candidate that violates a tenet is a semantic conflict.

This is a SUBSTRATE/scaffold — seed tenets, not exhaustive coverage (scope stated, not silently
capped). It gates nothing automatically; it informs the synthesizer's ranked menu and the human.
"""

from .runner import (
    Candidate,
    Tenet,
    evaluate,
    has_semantic_conflict,
    load_tenets,
)

__all__ = ["Candidate", "Tenet", "evaluate", "has_semantic_conflict", "load_tenets"]
