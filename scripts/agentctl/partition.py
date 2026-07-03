"""Partition assessment (M1–M4): the pure truth-table + section renderer.

This is the deterministic half of the partition step. The four markers are
cognitive inputs the LLM coordinator supplies (it judges standalone value,
heterogeneity, blocking dependencies, and rollback risk); the verdict that
follows from them, and the rendered `## Partition` skeleton, are mechanical
and live here so the rule cannot drift between sessions.

Markers (see memory-global/leaves/partition-markers.md):
  M1 = independent standalone-value deliverables
  M2 = heterogeneous work (different skills / areas)
  M3 = blocking dependencies between parts
  M4 = differing rollback risk
with optional M3/M4 severity flags that force a split recommendation.
"""
from __future__ import annotations


def verdict(
    m1: bool,
    m2: bool,
    m3: bool,
    m4: bool,
    m3_severe: bool = False,
    m4_severe: bool = False,
) -> str:
    """Map the 4 markers (+2 severities) to "recommended" / "possible" / "not_required".

    M1 ∧ any other         -> recommended (independent deliverables; split per M1)
    severe M3/M4           -> recommended (severity override, even without M1)
    no marker at all       -> not_required (ship as one PR)
    otherwise              -> possible (some marker, not the recommend combo)
    """
    any_other = m2 or m3 or m4
    if m3_severe or m4_severe:
        return "recommended"
    if m1 and any_other:
        return "recommended"
    if not (m1 or m2 or m3 or m4):
        return "not_required"
    return "possible"


_GUIDANCE = {
    "recommended": "split into independent PRs per M1 boundaries before implementation",
    "possible": "consider splitting; coordinator decides with the user",
    "not_required": "ship as one PR",
}


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def unit_delivery_order(units, stage_depends: dict[int, list[int]]) -> list[list[int]]:
    """Derive the inter-unit delivery ORDER from cross-unit stage dependencies.

    A partition unit groups approved-plan stages. When a stage in one unit
    `depends_on` a stage owned by ANOTHER unit, that dependency imposes a delivery
    order — the dependent unit is not independently shippable, it must land AFTER
    the unit it draws from. This is order, NOT rejection (cross-unit edges are
    allowed by design).

    `units` is a sequence of objects exposing a `.stages` list of stage indices;
    `stage_depends` maps each stage index to the stage indices it depends on.
    Returns a list parallel to `units`: for each unit, the sorted 1-based unit
    numbers it must be delivered after."""
    owner: dict[int, int] = {}
    for pos, u in enumerate(units, start=1):
        for s in u.stages:
            owner[s] = pos
    order: list[list[int]] = []
    for pos, u in enumerate(units, start=1):
        afters: set[int] = set()
        for s in u.stages:
            for dep in stage_depends.get(s, []):
                dep_owner = owner.get(dep)
                if dep_owner is not None and dep_owner != pos:
                    afters.add(dep_owner)
        order.append(sorted(afters))
    return order


def render_units(units, stage_depends: dict[int, list[int]] | None = None) -> str:
    """Render the `Units:` block: one numbered line per delivery unit, its mode,
    title, grouped stages, optional materialization ref, and derived delivery order
    ('after unit N') so a dependent unit is never presented as independently
    shippable. Empty string when there are no units."""
    if not units:
        return ""
    order = unit_delivery_order(units, stage_depends or {})
    lines = ["Units:"]
    for pos, (u, afters) in enumerate(zip(units, order), start=1):
        stages_csv = ", ".join(str(s) for s in u.stages)
        suffix = ""
        if u.ref:
            suffix += f" ref={u.ref}"
        if afters:
            suffix += " after unit " + ", ".join(str(a) for a in afters)
        lines.append(f"  {pos}. [{u.mode}] {u.title} (stages: {stages_csv}){suffix}")
    return "\n".join(lines)


def render_section(
    m1: bool,
    m2: bool,
    m3: bool,
    m4: bool,
    m3_severe: bool = False,
    m4_severe: bool = False,
    verdict_value: str | None = None,
    units=None,
    stage_depends: dict[int, list[int]] | None = None,
) -> str:
    """Render the deterministic `## Partition` skeleton; the LLM fills sub-PR
    specifics during cognition. When `units` are recorded, append the `Units:`
    block; with no units the output is byte-identical to before."""
    v = verdict_value or verdict(m1, m2, m3, m4, m3_severe, m4_severe)
    lines = [
        "## Partition",
        "",
        f"Verdict: {v}",
        (
            "Markers: "
            f"M1 independent deliverables: {_yn(m1)}; "
            f"M2 heterogeneous: {_yn(m2)}; "
            f"M3 blocking deps: {_yn(m3)} (severe: {_yn(m3_severe)}); "
            f"M4 rollback risk: {_yn(m4)} (severe: {_yn(m4_severe)})"
        ),
        _GUIDANCE[v],
    ]
    units_block = render_units(units, stage_depends) if units else ""
    if units_block:
        lines.extend(["", units_block])
    return "\n".join(lines)
