"""Semantic-conflict behavioural eval (ADR-0001 S4 stage 12, open Q#3).

Proves a tenet PASSES for a non-conflicting edit and FAILS for an A-vs-not-A semantic conflict —
the class-2 conflict git is blind to. scripts/ is on sys.path via conftest.
"""
import consensus_eval as ce
from consensus_eval.runner import Candidate, Tenet


def test_at_least_two_seed_tenets_discovered():
    tenets = ce.load_tenets()
    names = {t.name for t in tenets}
    assert len(tenets) >= 2
    assert "side-effect-free-preauthorized" in names
    assert "core-changes-through-human-gate" in names


def test_tenet_passes_for_non_conflicting_edit():
    # an edit about an unrelated topic does not engage the tenets -> no conflict
    cand = Candidate(directive="prefer the skill-first dispatch for known domain operations")
    assert ce.has_semantic_conflict(cand) is False


def test_tenet_passes_for_compatible_affirming_edit():
    # restates the protected behaviour with the SAME polarity -> preserved
    cand = Candidate(
        directive="side-effect-free read actions stay pre-authorized and need no ask",
        affirms=True,
    )
    report = ce.evaluate(cand)
    assert report.has_conflict is False
    # and the relevant tenet WAS engaged (not vacuously passing)
    engaged = [r for r in report.results if r.engaged]
    assert any(r.tenet.name == "side-effect-free-preauthorized" for r in engaged)


def test_tenet_fails_for_a_vs_not_a_semantic_conflict():
    # SAME words as the tenet, OPPOSITE polarity -> meaning conflict git cannot see
    cand = Candidate(
        directive="side-effect-free read actions are pre-authorized",
        affirms=False,  # i.e. "are NOT pre-authorized" — the not-A
    )
    report = ce.evaluate(cand)
    assert report.has_conflict is True
    assert any(t.name == "side-effect-free-preauthorized" for t in report.conflicts)


def test_core_gate_tenet_detects_auto_edit_conflict():
    # negates "Core changes go through the human approval gate" -> propose-not-execute conflict
    conflicting = Candidate(
        directive="Core changes go through the human approval gate before an agent applies them",
        affirms=False,  # the not-A: Core changes do NOT require the human gate
    )
    report = ce.evaluate(conflicting)
    assert any(t.name == "core-changes-through-human-gate" for t in report.conflicts)


def test_custom_tenet_set_isolation():
    t = Tenet(
        name="t", description="d",
        protected_terms=frozenset({"alpha", "beta"}), must_affirm=True, min_overlap=2,
    )
    assert ce.has_semantic_conflict(Candidate("alpha beta", affirms=False), tenets=[t]) is True
    assert ce.has_semantic_conflict(Candidate("alpha beta", affirms=True), tenets=[t]) is False
    assert ce.has_semantic_conflict(Candidate("gamma only", affirms=False), tenets=[t]) is False
