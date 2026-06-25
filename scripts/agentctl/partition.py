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


def render_section(
    m1: bool,
    m2: bool,
    m3: bool,
    m4: bool,
    m3_severe: bool = False,
    m4_severe: bool = False,
    verdict_value: str | None = None,
) -> str:
    """Render the deterministic `## Partition` skeleton; the LLM fills sub-PR
    specifics during cognition."""
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
    return "\n".join(lines)
