"""Claim-provenance ledger: a pure structural closure check over load-bearing claims.

A reasoning/research deliverable makes claims — some grounded in an external
source (axiom), some derived from other claims (derivation), some explicitly
flagged as unverified (assumption). Unsupported speculation presented as fact is a
free variable smuggled into what reads as a closed conclusion; validate_ledger is
the type-check that catches it, in the same DFS-3-colour + dangling-edge form as
plan._validate_graph (reused deliberately, not imported, to keep this module
dependency-free of the plan/session machinery).

WHICH load-bearing decisions/judgments exist in a deliverable is perception, not a
form, so it is never regex-extracted. Instead a semantic pass (advisor.py, stage 5)
RAISES `Candidate`s and `validate_candidates` gates on their DISPOSITION —
structurally, exactly like `validate_ledger` gates on claim closure: a 'recorded'
candidate must point at an existing load-bearing claim (whose own grounding
`validate_ledger` enforces over the same bag), a 'dismissed' one must carry a
reason, and a bare 'raised' candidate blocks. Neither validator judges truth or
content — only field presence and graph closure.

Pure module: no filesystem, subprocess, or network access. The gate that calls
this (plugins_ledger.py) stays pure for the same reason gates.py does.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VALID_STATUSES = frozenset({"axiom", "derivation", "assumption"})
VALID_DISPOSITIONS = frozenset({"raised", "recorded", "dismissed"})


@dataclass
class Claim:
    id: str
    status: str
    statement: str
    source: str = ""
    premises: list[str] = field(default_factory=list)
    basis: str = ""
    load_bearing: bool = True


def claims_from_dicts(raw: list[dict]) -> list[Claim]:
    return [
        Claim(
            id=d["id"],
            status=d["status"],
            statement=d.get("statement", ""),
            source=d.get("source", ""),
            premises=list(d.get("premises", [])),
            basis=d.get("basis", ""),
            load_bearing=d.get("load_bearing", True),
        )
        for d in raw
    ]


def claims_to_dicts(claims: list[Claim]) -> list[dict]:
    return [
        {
            "id": c.id,
            "status": c.status,
            "statement": c.statement,
            "source": c.source,
            "premises": list(c.premises),
            "basis": c.basis,
            "load_bearing": c.load_bearing,
        }
        for c in claims
    ]


def validate_ledger(claims: list[Claim]) -> list[str]:
    """Pure: a claim bag -> a list of blockers (empty iff closed).

    Fail-closed by construction: an empty ledger, or a ledger whose claims are
    ALL non-load-bearing, has no load-bearing claim to gate on and is itself a
    blocker — otherwise marking every claim non-load-bearing (or adding none at
    all) would vacuously close the ledger.
    """
    blockers: list[str] = []
    by_id = {c.id: c for c in claims}
    load_bearing = [c for c in claims if c.load_bearing]

    if not load_bearing:
        blockers.append("no load-bearing claims enumerated")
        return blockers

    for c in load_bearing:
        if c.status not in VALID_STATUSES:
            blockers.append(f"claim {c.id!r} has unknown status {c.status!r}")
            continue
        if c.status == "axiom":
            if not c.source:
                blockers.append(f"axiom claim {c.id!r} has no source")
        elif c.status == "assumption":
            if not c.basis:
                blockers.append(f"assumption claim {c.id!r} has no basis")
        elif c.status == "derivation":
            if not c.premises:
                blockers.append(f"derivation claim {c.id!r} has no premises")
                continue
            for p in c.premises:
                premise = by_id.get(p)
                if premise is None:
                    blockers.append(
                        f"derivation claim {c.id!r} rests on unknown premise {p!r} (dangling edge)"
                    )
                elif not premise.load_bearing:
                    blockers.append(
                        f"derivation claim {c.id!r} rests on non-load-bearing premise {p!r} "
                        "(a load-bearing claim may not rest on an un-gated premise)"
                    )

    # Acyclicity over the premises graph (DFS 3-colour), restricted to
    # load-bearing derivation claims — mirrors plan._validate_graph's form.
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {c.id: WHITE for c in load_bearing}

    def visit(node_id: str, trail: list[str]) -> None:
        colour[node_id] = GRAY
        node = by_id.get(node_id)
        premises = node.premises if node is not None and node.status == "derivation" else []
        for dep in premises:
            if dep not in colour:
                continue  # dangling or non-load-bearing premise already reported above
            if colour[dep] == GRAY:
                cycle = trail[trail.index(dep):] + [dep]
                blockers.append(f"claim premise cycle: {' -> '.join(cycle)}")
                continue
            if colour[dep] == WHITE:
                visit(dep, trail + [dep])
        colour[node_id] = BLACK

    for c in load_bearing:
        if colour[c.id] == WHITE:
            visit(c.id, [c.id])

    return blockers


@dataclass
class Candidate:
    id: str
    statement: str
    disposition: str = "raised"
    reason: str = ""
    claim: str = ""


def candidates_from_dicts(raw: list[dict]) -> list[Candidate]:
    return [
        Candidate(
            id=d["id"],
            statement=d.get("statement", ""),
            disposition=d.get("disposition", "raised"),
            reason=d.get("reason", ""),
            claim=d.get("claim", ""),
        )
        for d in raw
    ]


def candidates_to_dicts(candidates: list[Candidate]) -> list[dict]:
    return [
        {
            "id": c.id,
            "statement": c.statement,
            "disposition": c.disposition,
            "reason": c.reason,
            "claim": c.claim,
        }
        for c in candidates
    ]


def validate_candidates(candidates: list[Candidate], claims: list[Claim]) -> list[str]:
    """Pure: a candidate bag + the claim bag it references -> blockers (empty iff
    every candidate is dispositioned). A 'raised' candidate (the default an
    enumeration pass writes) always blocks — that is what makes the enumeration
    cross-check advisory-BLOCKING rather than merely advisory. 'dismissed' needs a
    reason; 'recorded' needs a --claim id that exists in `claims` AND is
    load_bearing. The referenced claim's own grounding is validate_ledger's job
    over the same bag — this validator only checks that the pointer is real, never
    that the claim's content truly supports the candidate's statement (that
    judgement stays outside this gate's code).
    """
    blockers: list[str] = []
    by_id = {c.id: c for c in claims}

    for cand in candidates:
        if cand.disposition not in VALID_DISPOSITIONS:
            blockers.append(f"candidate {cand.id!r} has unknown disposition {cand.disposition!r}")
        elif cand.disposition == "raised":
            blockers.append(f"undispositioned enumeration candidate {cand.id!r}")
        elif cand.disposition == "dismissed":
            if not cand.reason:
                blockers.append(f"dismissed candidate {cand.id!r} has no reason")
        elif cand.disposition == "recorded":
            if not cand.claim:
                blockers.append(f"recorded candidate {cand.id!r} has no linked claim")
                continue
            claim = by_id.get(cand.claim)
            if claim is None:
                blockers.append(
                    f"recorded candidate {cand.id!r} points at unknown claim {cand.claim!r} (dangling edge)"
                )
            elif not claim.load_bearing:
                blockers.append(
                    f"recorded candidate {cand.id!r} points at non-load-bearing claim {cand.claim!r}"
                )

    return blockers
