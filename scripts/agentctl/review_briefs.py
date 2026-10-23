"""Which projection of a plan a reviewer reads, chosen in code rather than by prompt wording.

Under risk-shaped planning (phase A) the first review is the axis-R pass over a skeleton:
the reviewer sees exactly the fields `plan.skeleton_digests` freezes, so what is judged and
what is later held fixed are the same text. Every other session reads the full plan.
"""
from __future__ import annotations

from .plan import SKELETON_PHASE, PlanDoc, first_hop
from .risk import NEIGHBOURS, tag_instruction

FULL = "full"
SKELETON = "skeleton"


def brief_kind(risk_planning: bool | None, plan_phase: str) -> str:
    """The brief a session's reviewer gets: the skeleton one only for a bound session whose
    current plan declares the skeleton phase."""
    return SKELETON if risk_planning and plan_phase == SKELETON_PHASE else FULL


def neighbours_brief(doc: PlanDoc, n: int) -> str:
    """Markdown of what stage `n` hands over and takes in, for a reviewer who owes the
    neighbours depth: each first-hop neighbour's declared product and the supplies that
    join it to `n`."""
    stages = {s.index: s for s in doc.stages}
    out = [f"## Neighbours of stage {n}", ""]
    for m in sorted(first_hop(doc, n)):
        s = stages[m]
        out += [f"### Stage {m}: {s.title}", ""]
        out += _lines("result", [s.subject.result])
        out += _lines("output_artifacts", sorted(s.output_artifacts))
        out += _lines("supplies", sorted(f"{x.on}:{x.element}={x.artifact}" for x in s.supplies))
        out.append("")
    if len(out) == 2:
        out.append("(none)")
    return "\n".join(out).rstrip("\n") + "\n"


def bound_addendum(doc: PlanDoc, risk_planning: bool | None, plan_phase: str, depth: str = "",
                   n: int | None = None) -> str:
    """What a bound session's reviewer reads beyond the full brief: the tagging
    instruction, and at the neighbours depth the neighbours of stage `n`. Empty for an
    unbound session or a skeleton plan, whose brief is unchanged."""
    if not risk_planning or brief_kind(risk_planning, plan_phase) == SKELETON:
        return ""
    parts = [tag_instruction(True)]
    if depth == NEIGHBOURS and n is not None:
        parts.append(neighbours_brief(doc, n))
    return "\n\n".join(parts) + "\n"


def _lines(label: str, values) -> list[str]:
    values = [str(v) for v in values if str(v).strip()]
    return [f"- {label}: {', '.join(values)}"] if values else [f"- {label}: (none)"]


def skeleton_brief(doc: PlanDoc) -> str:
    """Markdown of the chain a skeleton settles, for the axis-R reviewer."""
    meta = doc.meta
    order = meta.order
    out = [
        "# Skeleton review (axis R: reachability)",
        "",
        "Judge only whether the chain below reaches the goal: every stage feeds `base_stage`, "
        "each hand-off is real, nothing the goal needs is missing. Write a concern about this "
        "as `blocking: axis:R <what>` so the engine can tell a chain defect from any other.",
        "",
        f"## Goal\n\n{meta.goal.strip()}",
        "",
        f"## Base stage\n\n{meta.base_stage}",
    ]
    if order is not None:
        out += ["", f"## Functional place\n\n{order.functional_place.strip()}"]
        if order.requirements:
            out += ["", "## Requirements", ""]
            out += [f"- {r.id}: {r.text}" for r in order.requirements]
    out += ["", "## Alternatives", ""]
    if meta.alternatives:
        out += [f"- {a}" for a in meta.alternatives]
    else:
        out.append(f"- none weighed: {meta.alternatives_waiver or '(no waiver given)'}")
    for s in sorted(doc.stages, key=lambda x: x.index):
        out += ["", f"## Stage {s.index}: {s.title}", ""]
        out += _lines("executor", [s.actor.executor])
        out += _lines("depends_on", sorted(s.depends_on))
        out += _lines("supplies", sorted(f"{x.on}:{x.element}={x.artifact}" for x in s.supplies))
        out += _lines("result", [s.subject.result])
        out += _lines("output_artifacts", sorted(s.output_artifacts))
        out += _lines("material", [s.subject.material])
        out += _lines("means", [s.means.means])
        out += _lines("method", [s.means.method])
        out += _lines("principle", [s.principle.statement if s.principle else ""])
    return "\n".join(out) + "\n"
