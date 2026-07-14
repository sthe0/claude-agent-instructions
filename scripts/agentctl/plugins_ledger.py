"""The claim-provenance ledger plugin: a skill-less consumer of the plugin layer.

A reasoning/research deliverable is not resolved by "tests pass" — it is resolved
by every load-bearing claim it makes (a number, a DECISION, a JUDGMENT) being
grounded (axiom w/ source), derived (premises established, acyclic), or explicitly
marked (assumption w/ basis). Like `experience`, this discipline has no owning
skill to turn it on, so the ENGINE auto-activates it whenever classify is told the
session's deliverable_kind is 'reasoning' or 'mixed' on a SUBSTANTIVE session — a
'mixed' deliverable still carries load-bearing claims alongside code, so it arms
the gate too.

Division of labour (engine owns the CLOSURE CHECK; the coordinator owns WHICH
claims exist): the gate structurally validates the claim bag (ledger.py's pure
validate_ledger, the DFS-3-colour + dangling-edge form reused from
plan._validate_graph) — it never judges whether a claim's content is true. This
plugin only wires that pure check to the resolution gate; the CLI surface for
recording claims (ledger-add/ledger-check) is stage 3.

The bag also carries `candidates` — load-bearing decision/judgment candidates
raised by an enumeration pass (a coordinator's own read, or ledger-enumerate's
independent advisor pass, stage 5). The resolution gate ANDs claim CLOSURE with
candidate DISPOSITION-COMPLETENESS (ledger.validate_candidates): a candidate left
'raised' blocks resolution until it is recorded (linked to a closed load-bearing
claim, via ledger-candidate/ledger-dispose, stage 4) or dismissed with a reason.
This is what makes the enumeration cross-check advisory-BLOCKING rather than
merely advisory — the engine never judges a candidate's content, only whether it
has been dispositioned.

A third deterministic blocker makes the cross-check MANDATORY: the bag carries an
`enumerated` flag that `ledger-enumerate` (the independent advisor pass, stage 5)
flips once it has read the outgoing deliverable and raised whatever candidates it
detected. While that flag is False the resolution gate blocks even when the
recorded claims/candidates are themselves closed — so a reasoning/mixed deliverable
cannot resolve without the second, independent reading actually having run."""
from __future__ import annotations

from . import ledger
from .plugins import Plugin, PluginDirective, register
from .state import WeightClass


def _auto_activate(state) -> bool:
    return (
        getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value
        and getattr(state, "deliverable_kind", "") in ("reasoning", "mixed")
    )


_ENUMERATE_NOT_RUN = (
    "enumeration cross-check not run — run `agentctl ledger-enumerate --artifact <file>`"
)


def ledger_blockers(bag) -> list[str]:
    """The full resolution-gate blocker set for a ledger bag, so the read-only
    `ledger-check` command and the resolution gate never diverge:
      1. claim CLOSURE (ledger.validate_ledger) — every load-bearing claim
         grounded/derived/marked, premise graph acyclic;
      2. candidate DISPOSITION-COMPLETENESS (ledger.validate_candidates) — every
         enumeration candidate recorded (linked to a load-bearing claim) or
         dismissed (with a reason);
      3. the enumeration cross-check has RUN (bag['enumerated']). This makes the
         independent advisor pass structurally MANDATORY for a reasoning/mixed
         deliverable, not merely available — an un-run cross-check blocks resolution
         even when the recorded claims/candidates are themselves closed.
    Pure: reads only the bag; the non-determinism is confined to candidate
    GENERATION (advisor.enumerate_claims), never this decision."""
    claims = ledger.claims_from_dicts(bag.get("claims", []))
    candidates = ledger.candidates_from_dicts(bag.get("candidates", []))
    blockers = ledger.validate_ledger(claims) + ledger.validate_candidates(candidates, claims)
    if not bag.get("enumerated"):
        blockers.append(_ENUMERATE_NOT_RUN)
    return blockers


def _ledger_gate(state, bag) -> list[str]:
    return ledger_blockers(bag)


def _observe_resolve(state, bag) -> list[PluginDirective]:
    blockers = _ledger_gate(state, bag)
    if not blockers:
        return []
    directives = [PluginDirective(
        "ledger", "close_ledger",
        "run the enumeration cross-check, then ground or label every load-bearing "
        "claim and disposition every enumeration candidate before resolving — "
        f"blockers: {'; '.join(blockers)} (use `agentctl ledger-enumerate ...`, "
        "`agentctl ledger-add ...`, then `agentctl ledger-check` to confirm closure)",
        blocking=True,
    )]
    # Echo already-dispositioned candidates (reason / linked claim) so a dismiss
    # reason is not write-only at the gate while other candidates still block.
    dispositioned = [
        c for c in bag.get("candidates", [])
        if c.get("disposition") in ("recorded", "dismissed")
    ]
    if dispositioned:
        echoes = "; ".join(
            f"{c['id']}: {c['disposition']}"
            + (f" -> claim {c['claim']!r}" if c.get("claim") else "")
            + (f" ({c['reason']!r})" if c.get("reason") else "")
            for c in dispositioned
        )
        directives.append(PluginDirective(
            "ledger", "echo_dispositions", f"dispositioned candidates so far: {echoes}",
        ))
    return directives


def _terminal(state, event: str) -> bool:
    return event == "resolve" and bool(getattr(state.resolution, "passed", False))


register(
    Plugin(
        name="ledger",
        scope="task",
        auto_activate=_auto_activate,
        observers={"resolve": _observe_resolve},
        gates={"resolution": _ledger_gate},
        state_factory=lambda: {"claims": [], "candidates": [], "enumerated": False},
        terminal=_terminal,
    )
)
