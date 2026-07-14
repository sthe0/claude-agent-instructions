"""agentctl.ledger: the pure claim-provenance closure validator.

Mirrors plan._validate_graph's DFS-3-colour + dangling-edge form, applied to a
claim bag instead of a stage bag. Covers: valid axiom/derivation/assumption
closure; axiom without source; derivation with a dangling premise; a derivation
cycle; an empty ledger; a non-load-bearing claim ignored as a gate target; a
load-bearing derivation resting on a non-load-bearing premise (closure hole); and
a ledger whose claims are ALL non-load-bearing (the vacuous-closure bypass).
"""
from __future__ import annotations

from agentctl.ledger import Claim, claims_from_dicts, claims_to_dicts, validate_ledger
from ast_purity import impure_names


def test_valid_axiom_derivation_assumption_close():
    claims = [
        Claim(id="a1", status="axiom", statement="measured latency is 40ms", source="ticket:ABC-1"),
        Claim(id="d1", status="derivation", statement="p99 exceeds SLO", premises=["a1"]),
        Claim(id="s1", status="assumption", statement="traffic pattern stays steady", basis="last quarter's trend"),
    ]
    assert validate_ledger(claims) == []


def test_axiom_without_source_blocks():
    claims = [Claim(id="a1", status="axiom", statement="x is true", source="")]
    blockers = validate_ledger(claims)
    assert len(blockers) == 1
    assert "a1" in blockers[0] and "source" in blockers[0]


def test_assumption_without_basis_blocks():
    claims = [Claim(id="s1", status="assumption", statement="x", basis="")]
    blockers = validate_ledger(claims)
    assert len(blockers) == 1
    assert "s1" in blockers[0] and "basis" in blockers[0]


def test_derivation_with_dangling_premise_blocks():
    claims = [Claim(id="d1", status="derivation", statement="x", premises=["missing"])]
    blockers = validate_ledger(claims)
    assert any("dangling edge" in b for b in blockers)


def test_derivation_without_premises_blocks():
    claims = [Claim(id="d1", status="derivation", statement="x", premises=[])]
    blockers = validate_ledger(claims)
    assert any("d1" in b and "no premises" in b for b in blockers)


def test_derivation_cycle_blocks():
    claims = [
        Claim(id="d1", status="derivation", statement="x", premises=["d2"]),
        Claim(id="d2", status="derivation", statement="y", premises=["d1"]),
    ]
    blockers = validate_ledger(claims)
    assert any("cycle" in b for b in blockers)


def test_empty_ledger_blocks():
    blockers = validate_ledger([])
    assert blockers == ["no load-bearing claims enumerated"]


def test_non_load_bearing_claim_ignored_as_target():
    claims = [
        Claim(id="n1", status="axiom", statement="irrelevant aside", source="", load_bearing=False),
        Claim(id="a1", status="axiom", statement="the real claim", source="ticket:ABC-1"),
    ]
    assert validate_ledger(claims) == []


def test_load_bearing_derivation_on_non_load_bearing_premise_blocks():
    claims = [
        Claim(id="n1", status="axiom", statement="unmarked aside", source="", load_bearing=False),
        Claim(id="d1", status="derivation", statement="conclusion", premises=["n1"]),
    ]
    blockers = validate_ledger(claims)
    assert any("d1" in b and "non-load-bearing premise" in b for b in blockers)


def test_all_non_load_bearing_bypass_blocks():
    claims = [
        Claim(id="a1", status="axiom", statement="x", source="", load_bearing=False),
        Claim(id="d1", status="derivation", statement="y", premises=["a1"], load_bearing=False),
    ]
    assert validate_ledger(claims) == ["no load-bearing claims enumerated"]


def test_unknown_status_blocks():
    claims = [Claim(id="c1", status="guess", statement="x")]
    blockers = validate_ledger(claims)
    assert any("c1" in b and "unknown status" in b for b in blockers)


def test_dicts_roundtrip():
    claims = [
        Claim(id="a1", status="axiom", statement="x", source="src", premises=[], basis="", load_bearing=True),
        Claim(id="d1", status="derivation", statement="y", premises=["a1"]),
    ]
    assert claims_from_dicts(claims_to_dicts(claims)) == claims


def test_validator_is_pure():
    assert impure_names(validate_ledger) == set()
