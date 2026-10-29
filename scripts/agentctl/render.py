"""Render a typed PlanDoc back to the markdown prose surface, on demand.

Difficulty removed: the planner's deliverable is the TOML plan the engine tracks
(agentctl.plan), not a hand-authored markdown twin. A human reviewer still wants a
readable prose view, but keeping a second hand-written `.md` file was the two-surface
disease — the prose drifted from the typed plan and nothing kept them in sync. This
module GENERATES the prose from the one source (the TOML) on demand, so there is exactly
one source of truth and the view can never drift. The engine never writes the result to
disk; it is a projection, exactly like `agentctl question-list --format md`.

`render_plan_md` is pure (PlanDoc -> str, no filesystem). It renders EVERY stage — the
one invariant a render must never violate is dropping a stage, so the rendered text
carries every stage's index and title.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import difflib
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from lib import kind_baselines
from lib.spawn_ceiling import dispatch_prompt_ceiling_chars

from . import plan_refs, plan_store, protected_actions
from .config import PlanLimitError, plan_field_inline_limit, plan_size_limits
from .directive import Directive
from .plan import (
    CONDITION_MARKERS,
    CUSTOMER_QUESTION_MARKER,
    CUSTOMER_QUESTIONS_MARKER,
    CUSTOMER_QUESTIONS_NONE,
    PLAN_DIGEST_MARKER,
    RESTATES_PREFIX,
    REVIEW_MARKER,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    VERDICT_MARKER,
    PAIR_BASE_NODE,
    PlanDoc,
    PlanError,
    _build_supplies,
    _stage_declared_and_derived_grants,
    _venue_for,
    consumers,
    grants_sha256,
    interface_empty,
    is_unit_id,
    load_order_draft,
    load_plan,
    parse_pair,
    parse_plan,
    parse_unit,
    plan_coverage_entries,
    reliance_set,
    review_pairs,
    split_pair_id,
    stage_closure_digest,
    understanding_digest,
    unit_node,
)


def _protected_hit_text(rule: str, entry_id: str) -> str:
    entry = protected_actions.entry_by_id(entry_id)
    return (f"PROTECTED ACTION {entry_id}: {rule} covers a protected action — "
            f"\"{entry.quote}\" (the user accepts it with accept-protected-grant --id {entry_id})")


NO_VENUE_GRANTS_NOTE = (
    "in-venue grants not derived: the plan declares no absolute repo_root or delivery_worktree"
)


def _no_venue_note(s, venue: str | None) -> str | None:
    """`NO_VENUE_GRANTS_NOTE` when `venue` is None and the stage has a relative
    output_artifacts / material_refs entry whose grant that gap withholds, else None."""
    if venue is not None:
        return None
    relative = [r for r in [*s.output_artifacts, *s.subject.material_refs] if not os.path.isabs(r)]
    return NO_VENUE_GRANTS_NOTE if relative else None


def _grants_lines(s, venue: str | None) -> list[str]:
    """The `- **Grants (file-access scope):**` block for one stage, or `[]`
    when the stage has neither a declared nor a derived grant to show —
    shared by `render_stage_brief` and `render_plan_md`'s per-stage loop so
    the whole-plan view can no longer omit the one detail (a stage's actual
    file-access scope) that a plan reviewer needs to audit without opening
    the single-stage brief for every stage in turn."""
    declared_grants, derived_grants, _dropped = _stage_declared_and_derived_grants(s, venue)
    declared_rules = [r.rule for r in declared_grants.allow]
    declared_dirs = [f"{a.path}:{a.mode}" for a in declared_grants.add_dirs]
    derived_rules = [r.rule for r in derived_grants.allow]
    derived_dirs = [f"{a.path}:{a.mode}" for a in derived_grants.add_dirs]
    note = _no_venue_note(s, venue)
    if not (declared_rules or declared_dirs or derived_rules or derived_dirs or note):
        return []
    out = ["- **Grants (file-access scope):**"]
    if note:
        out.append(f"  - {note}")
    if declared_rules:
        out.append(f"  - declared allow: {', '.join(declared_rules)}")
    if declared_dirs:
        out.append(f"  - declared add_dirs: {', '.join(declared_dirs)}")
    if derived_rules:
        out.append(f"  - derived allow: {', '.join(derived_rules)}")
    if derived_dirs:
        out.append(f"  - derived add_dirs: {', '.join(derived_dirs)}")
    return out


def _effects_lines(s) -> list[str]:
    """The `- **Effects (trusted script identities):**` block for one
    stage, or `[]` when the stage declares no `[[stage.effects]]` entries —
    the stage's own claim about which scripts resolve its calls, distinct
    from `_grants_lines`'s file-access scope."""
    if not s.effects:
        return []
    out = ["- **Effects (trusted script identities):**"]
    for e in s.effects:
        out.append(f"  - {e.path} (resolver: {e.resolver}, sha256: {e.sha256})")
    return out


def _negative_control_lines(crit) -> list[str]:
    """The one-line rendering of a criterion's negative control or its waiver,
    shared between `render_plan_md` and `render_stage_brief` so the two views
    can never drift apart on this field."""
    if crit.negative_control:
        return [f"- **Negative control:** `{crit.negative_control}`"]
    if crit.negative_control_waiver:
        return [f"- **Negative control waived:** {crit.negative_control_waiver}"]
    return []


def _verify_reads_lines(crit) -> list[str]:
    if crit.verify_reads is None:
        return []
    return [f"- **Verify reads:** {', '.join(f'`{p}`' for p in crit.verify_reads) or 'none'}"]


def render_meta_md(doc: PlanDoc) -> list[str]:
    """The plan-level header block (task id / weight class / done criterion /
    criterion type / repo root / external research), as raw lines — factored
    verbatim out of `render_plan_md` so a topo unit's bundle can reuse
    exactly the same projection. `render_plan_md`'s own output is unchanged
    (it just composes this with the other factored pieces)."""
    m = doc.meta
    lines: list[str] = [f"# Plan: {m.goal or m.task_id}", ""]
    lines.append(f"- **Task id:** {m.task_id}")
    if m.weight_class:
        lines.append(f"- **Weight class:** {m.weight_class}")
    if m.done_criterion:
        lines.append(f"- **Done criterion:** {m.done_criterion}")
    lines.append(f"- **Criterion type:** {m.criterion_type}")
    if m.repo_root:
        lines.append(f"- **Repo root:** {m.repo_root}")
    if m.landing_waiver:
        lines.append(f"- **Landing waiver:** {m.landing_waiver}")
    if m.external_research:
        lines.append(f"- **External research:** {m.external_research}")
    lines.append("")
    return lines


def render_order_md(doc: PlanDoc) -> list[str]:
    """The `## Order` block (customer / functional_place / requirements
    only — NOT `coverage`/`requires_traceability`; see
    `render_order_coverage_md`), as raw lines. `[]` when the plan declares
    no order. Factored verbatim out of `render_plan_md`."""
    m = doc.meta
    lines: list[str] = []
    if m.order is not None:
        o = m.order
        lines.append("## Order")
        lines.append("")
        if o.customer_id or o.customer:
            lines.append(f"- **Customer:** {o.customer} (`{o.customer_id}`)")
        if o.functional_place:
            lines.append(f"- **Functional place:** {o.functional_place}")
        if o.requirements:
            lines.append("- **Requirements:**")
            for r in o.requirements:
                label = f"**{r.id}**" if r.id else "*(no id)*"
                lines.append(f"  - {label}: {r.text}")
                lines.append(f"    - **Derivation:** {r.derivation or '*(none)*'}")
        lines.append("")
    return lines


def _proposal_lines(proposals, indent: str = "  ") -> list[str]:
    """One entry per proposal — id, verdict, text, critique and the requirements the
    critique stands against."""
    lines: list[str] = []
    for p in proposals:
        lines.append(f"{indent}- **{p.id or '(no id)'}** ({p.verdict or 'no verdict'}): {p.text}")
        lines.append(f"{indent}  - **Critique:** {p.critique or '*(none)*'}")
        lines.append(f"{indent}  - **Against:** {', '.join(p.against) if p.against else '*(none)*'}")
    return lines


def render_order_understanding_md(doc: PlanDoc) -> list[str]:
    """The order's activity, difficulty, proposals and constraints — the part of the
    understanding `render_order_md` leaves out (it already prints the customer, the
    functional place and the requirements with derivations) — as raw lines. Each field
    appears only when the order states it, so an order without them renders as before;
    `[]` when it states none."""
    order = doc.meta.order
    if order is None:
        return []
    lines: list[str] = []
    if order.activity:
        lines.append(f"- **Activity:** {order.activity}")
    if order.difficulty:
        lines.append(f"- **Difficulty:** {order.difficulty}")
    if order.proposals:
        lines.append("- **Proposals:**")
        lines.extend(_proposal_lines(order.proposals))
    if order.constraints:
        lines.append("- **Constraints:**")
        lines.extend(f"  - **{c.id or '(no id)'}**: {c.text}" for c in order.constraints)
    return ["## Order understanding", "", *lines, ""] if lines else []


def render_order_coverage_md(doc: PlanDoc) -> list[str]:
    """The order's `coverage` map and `requires_traceability` flag, as raw
    lines. Kept as its own function rather than folded into
    `render_order_md` so `render_plan_md`'s output stays byte-identical.
    `[]` when the plan declares no order."""
    m = doc.meta
    if m.order is None:
        return []
    o = m.order
    lines: list[str] = ["## Order coverage", ""]
    lines.append(f"- **Requires traceability:** {o.requires_traceability}")
    if o.coverage:
        lines.append("- **Coverage:**")
        for req_id, stage_refs in o.coverage.items():
            refs = ", ".join(stage_refs) if stage_refs else "*(none)*"
            lines.append(f"  - {req_id}: {refs}")
    else:
        lines.append("- **Coverage:** *(none declared)*")
    lines.append("")
    return lines


def render_final_checks_md(doc: PlanDoc) -> list[str]:
    """The `## Final verification` block, as raw lines. `[]` when the plan
    declares no `final_check`. Factored verbatim out of `render_plan_md`."""
    m = doc.meta
    lines: list[str] = []
    if m.final_check:
        lines.append("## Final verification")
        lines.append("")
        for fc in m.final_check:
            label = f"{fc.label}: " if fc.label else ""
            if fc.kind == "landed" and fc.landed is not None:
                ls = fc.landed
                lines.append(
                    f"- {label}**landed check:** stage {ls.delivered_stage}'s "
                    f"delivered commit must be contained in {ls.refs_phrase()}"
                )
            else:
                cached = ", cacheable" if fc.cacheable else ""
                if fc.verify_reads is not None:
                    cached += f", verify_reads: {', '.join(fc.verify_reads) or 'none'}"
                lines.append(f"- {label}`{fc.command}` (expected exit {fc.expected_exit}{cached})")
        lines.append("")
    return lines


def _optional_lines(s) -> list[str]:
    """The optional-stage marker, empty for every stage that is not optional so a plan
    without optional stages renders byte-identical to before the field existed."""
    if not s.optional:
        return []
    return [
        f"- **Optional:** yes — traces to backlog item {s.backlog_issue}; the customer may "
        f"decline it at approval (`approve --skip-optional {s.index}`)"
    ]


def render_plan_md(doc: PlanDoc) -> str:
    """Pure: a PlanDoc -> a markdown prose view. Renders every stage in order."""
    lines: list[str] = []
    lines.extend(render_meta_md(doc))
    lines.extend(render_order_md(doc))

    for s in doc.stages:
        lines.append(f"## Stage {s.index}: {s.title}")
        lines.append("")
        lines.append(f"- **Executor:** {s.actor.executor}")
        lines.extend(_optional_lines(s))
        if s.actor.capability_required:
            lines.append(f"- **Capability required:** {s.actor.capability_required}")
        if s.subject.material:
            lines.append(f"- **Material:** {s.subject.material}")
        lines.append(f"- **Expected result image:** {s.subject.result}")
        if s.subject.invariants:
            lines.append(f"- **Invariants:** {s.subject.invariants}")
        if s.means.means:
            lines.append(f"- **Means:** {s.means.means}")
        if s.means.method:
            lines.append(f"- **Method:** {s.means.method}")
        if s.means.procedure:
            lines.append(f"- **Procedure:** {s.means.procedure}")
        if s.conditions:
            lines.append(f"- **Conditions:** {s.conditions}")
        lines.append(f"- **Criterion type:** {s.criterion.criterion_type}")
        lines.append(f"- **Done criterion:** {s.criterion.done_criterion}")
        if s.criterion.verify_kind == "landed" and s.criterion.landed is not None:
            ls = s.criterion.landed
            lines.append(
                f"- **Landed check:** stage {ls.delivered_stage}'s delivered "
                f"commit must be contained in {ls.refs_phrase()}"
            )
        elif s.criterion.verify_command:
            lines.append(f"- **Verify command:** `{s.criterion.verify_command}`")
            # Only rendered when the stage opts into the schema-24 lifecycle —
            # a plan declaring no `verify_venue_at_final` renders byte-identical
            # to before this field existed (V4's identity, made visible here too).
            if s.criterion.verify_venue_at_final is not None:
                lines.append(
                    f"- **Verified in:** {s.criterion.verify_venue}; "
                    f"re-verified at resolution in "
                    f"{s.criterion.verify_venue_at_final}"
                )
            lines.extend(_negative_control_lines(s.criterion))
            lines.extend(_verify_reads_lines(s.criterion))
        if s.depends_on:
            lines.append(f"- **Depends on:** {', '.join(str(d) for d in sorted(s.depends_on))}")
        if s.principle is not None:
            p = s.principle
            lines.append(
                f"- **Principle:** {p.statement} "
                f"(source: {p.source}; derivation: {p.derivation}; "
                f"confidence: {p.confidence}; refutation: {p.refutation})"
            )
        lines.extend(_grants_lines(s, _venue_for(doc)))
        lines.extend(_amendment_lines(s))
        lines.append("")

    lines.extend(render_final_checks_md(doc))

    return "\n".join(lines).rstrip() + "\n"


def _amendment_lines(s) -> list[str]:
    """The stage's retrospective log, only in the whole-plan view: no per-stage
    projection a review or a child reads carries it, so an amendment cannot reach a
    decision. Empty for a stage without amendments, so such a plan renders byte-identical."""
    return [f"- **Amendment ({a.date}):** {a.text} — {a.reason}" for a in s.amendments]


def render_stages_md(doc: PlanDoc, stage_indices) -> str:
    """The plan projected onto a SUBSET of its stages, in index order — the reading an
    advisor is given when only those stages have moved. Each stage carries the plan's
    meta with it (see `render_stage_brief`), so a question about a stage's fit to the
    goal is still answerable from the projection alone."""
    return "\n".join(render_stage_brief(doc, index) for index in sorted(stage_indices))


STAGE_BRIEF_FINAL_CHECK_NOTE = "see the full plan file for those"
STAGE_BRIEF_FINAL_CHECK_NOTE_IN_VIEW = "the coordinator runs them"


def render_stage_brief(
    doc: PlanDoc, stage_index: int, *, closure_digest: "str | None" = None,
    final_check_note: str = STAGE_BRIEF_FINAL_CHECK_NOTE,
) -> str:
    """Pure: a PlanDoc + one stage index -> a markdown brief of JUST that stage.

    `final_check_note` ends the Final verification heading; a site whose reader holds no
    plan file passes a note that names nothing to look for.

    A given `closure_digest` adds the stage's closure digest line; the caller computes it
    with `stage_closure_digest` on the plan as authored (a site view renders a copy whose
    long fields are pointers, and that copy's digest would name a different plan). The
    digests the topo pair bundle binds and the origin/main renderer pins are of the brief
    without it.

    Difficulty removed: a dispatched specialist executes exactly one stage, but
    the spawn prompt has been embedding `render_plan_md`'s WHOLE-plan rendering
    (every stage) — dead weight that scales with plan size until a large plan's
    prompt exceeds a child's context window outright. This projects only the
    active stage, so prompt size stops scaling with plan size.

    Unlike `render_plan_md`, this renders EVERY non-empty field on the target
    stage, including ones the whole-plan view omits for brevity across many
    stages (`output_artifacts`, `control`, `criterion.observation`,
    `criterion.expected_exit`) — a single-stage view has no size budget excuse
    to silently drop a field the executor might need. Each direct dependency
    (`depends_on`, derived from `supplies`) is rendered as its own block
    (title, expected result image, output artifacts) distinct from this
    stage's own fields; transitive dependencies are not carried. The raw
    `supplies` edges (on/element/artifact) are rendered separately from the
    resolved dependency blocks. `meta.final_check` entries are carried by
    label only (an unlabeled check by 1-based position + kind) — never their
    command/venue/kind detail, which belongs to the full plan file. Meta's
    `delivery_worktree` is carried alongside the plan's other meta fields.
    The mutable `Outcome` record (status/actual/fail_digests/cost_usd/
    duration_ms/spawn_count/delivered_head/landed_head) is deliberately never rendered:
    it is the engine's execution HISTORY of the stage, not an input to it.

    Raises ValueError if no stage in `doc` carries `stage_index`.
    """
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    if stage is None:
        raise ValueError(f"no stage with index {stage_index} in plan {doc.meta.task_id!r}")

    m = doc.meta
    lines: list[str] = [f"# Plan: {m.goal or m.task_id}", ""]
    lines.append(f"- **Task id:** {m.task_id}")
    if m.weight_class:
        lines.append(f"- **Weight class:** {m.weight_class}")
    if m.done_criterion:
        lines.append(f"- **Overall done criterion:** {m.done_criterion}")
    lines.append(f"- **Overall criterion type:** {m.criterion_type}")
    if m.repo_root:
        lines.append(f"- **Repo root:** {m.repo_root}")
    if m.delivery_worktree:
        lines.append(f"- **Delivery worktree:** {m.delivery_worktree}")
    if m.external_research:
        lines.append(f"- **External research:** {m.external_research}")
    if closure_digest is not None:
        lines.append(f"- **Stage closure digest:** {closure_digest}")
    lines.append("")
    lines.append(
        f"This is a PROJECTED BRIEF of stage {stage.index} only, out of "
        f"{len(doc.stages)} stage(s) in the plan — the other stages are not "
        f"shown and are not this step's concern."
    )
    lines.append("")

    lines.extend(_stage_field_lines(doc, stage, dependency_blocks=True))

    if m.final_check:
        lines.append(
            "## Final verification (labels only — this stage does not need the "
            f"commands; {final_check_note})"
        )
        lines.append("")
        for i, fc in enumerate(m.final_check, start=1):
            if fc.label:
                lines.append(f"- {fc.label}")
            else:
                lines.append(f"- check {i} ({fc.kind})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_stage_fields(doc: PlanDoc, stage_index: int) -> str:
    """Pure: every field of ONE stage and nothing else — the stage's own heading
    and declared fields as `render_stage_brief` shows them, without the plan header
    (goal, done criterion, repo root, research), the final-check labels and the
    supplier blocks under `Depends on` (a supplier's title, result image and output
    artifacts, which belong to the supplier). A review of the stage alone is shown
    exactly the content its currency keys on; the direct dependencies appear as
    stage indices.

    Raises ValueError if no stage in `doc` carries `stage_index`."""
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    if stage is None:
        raise ValueError(f"no stage with index {stage_index} in plan {doc.meta.task_id!r}")
    return render_node(stage, venue=_venue_for(doc))


def _stage_field_lines(
    doc: "PlanDoc | None", s, *, dependency_blocks: bool, venue: "str | None" = None,
) -> list[str]:
    """The stage section of `render_stage_brief`: heading and every non-empty field.
    `dependency_blocks=False` lists the direct dependencies by index only. A caller with
    no `doc` names the grant `venue` itself (None: no venue declared) and cannot ask for
    dependency blocks."""
    if doc is not None:
        if venue is not None:
            raise ValueError("a stage is rendered against its doc's venue or an explicit one, not both")
        venue = _venue_for(doc)
    lines: list[str] = []
    lines.append(f"## Stage {s.index}: {s.title}")
    lines.append("")
    lines.append(f"- **Executor:** {s.actor.executor}")
    lines.extend(_optional_lines(s))
    if s.actor.capability_required:
        lines.append(f"- **Capability required:** {s.actor.capability_required}")
    if s.actor.cost_tier:
        lines.append(f"- **Cost tier:** {s.actor.cost_tier}")
    if s.subject.material:
        lines.append(f"- **Material:** {s.subject.material}")
    if s.subject.material_refs:
        lines.append(f"- **Material refs:** {', '.join(s.subject.material_refs)}")
    if s.knowledge:
        lines.append(f"- **Knowledge:** {s.knowledge}")
    if s.subject.knowledge_refs:
        lines.append(f"- **Knowledge refs:** {', '.join(s.subject.knowledge_refs)}")
    lines.append(f"- **Expected result image:** {s.subject.result}")
    if s.subject.invariants:
        lines.append(f"- **Invariants:** {s.subject.invariants}")
    if s.means.means:
        lines.append(f"- **Means:** {s.means.means}")
    if s.means.method:
        lines.append(f"- **Method:** {s.means.method}")
    if s.means.procedure:
        lines.append(f"- **Procedure:** {s.means.procedure}")
    if s.preconditions:
        lines.append(f"- **Preconditions:** {s.preconditions}")
    if s.conditions:
        lines.append(f"- **Conditions:** {s.conditions}")
    lines.append(f"- **Criterion type:** {s.criterion.criterion_type}")
    lines.append(f"- **Done criterion:** {s.criterion.done_criterion}")
    if s.criterion.verify_kind == "landed" and s.criterion.landed is not None:
        ls = s.criterion.landed
        lines.append(
            f"- **Landed check:** stage {ls.delivered_stage}'s delivered "
            f"commit must be contained in {ls.refs_phrase()}"
        )
    elif s.criterion.verify_command:
        lines.append(f"- **Verify command:** `{s.criterion.verify_command}`")
        if s.criterion.expected_exit:
            lines.append(f"- **Expected exit:** {s.criterion.expected_exit}")
        lines.append(f"- **Verify venue:** {s.criterion.verify_venue}")
        if s.criterion.verify_venue_at_final is not None:
            lines.append(
                f"- **Verified in:** {s.criterion.verify_venue}; "
                f"re-verified at resolution in "
                f"{s.criterion.verify_venue_at_final}"
            )
        lines.extend(_negative_control_lines(s.criterion))
        lines.extend(_verify_reads_lines(s.criterion))
    if s.criterion.observation:
        lines.append(f"- **Prior observation:** {s.criterion.observation}")
    if s.output_artifacts:
        lines.append(f"- **Output artifacts:** {', '.join(s.output_artifacts)}")
    if s.ephemeral_artifacts_waiver:
        lines.append(f"- **Ephemeral artifacts waived:** {s.ephemeral_artifacts_waiver}")
    if s.depends_on and not dependency_blocks:
        lines.append(f"- **Depends on:** {', '.join(f'stage {d}' for d in sorted(s.depends_on))}")
    elif s.depends_on:
        lines.append("- **Depends on** (direct dependencies only; see their own stage for detail):")
        for dep_index in sorted(s.depends_on):
            dep = next((d for d in doc.stages if d.index == dep_index), None)
            if dep is None:
                continue
            lines.append(f"  - Stage {dep.index}: {dep.title}")
            lines.append(f"    - **Its expected result image:** {dep.subject.result}")
            if dep.output_artifacts:
                lines.append(f"    - **Its output artifacts:** {', '.join(dep.output_artifacts)}")
    if s.supplies:
        lines.append("- **Supplies** (raw provision edges this stage declares):")
        for sup in s.supplies:
            edge = f"on stage {sup.on}"
            if sup.element:
                edge += f", element: {sup.element}"
            if sup.artifact:
                edge += f", artifact: {sup.artifact}"
            if sup.delivery:
                edge += f", delivery: {sup.delivery}"
            lines.append(f"  - {edge}")
    if s.control:
        lines.append(f"- **Control (prior attestation):** {s.control}")
    lines.extend(_grants_lines(s, venue))
    lines.extend(_effects_lines(s))
    if s.principle is not None:
        p = s.principle
        lines.append(
            f"- **Principle:** {p.statement} "
            f"(source: {p.source}; derivation: {p.derivation}; "
            f"confidence: {p.confidence}; refutation: {p.refutation})"
        )
    lines.append("")
    return lines


# --- Topological review: one reliance edge ("pair") reviewed per spawn,
# from a small starting prompt whose single service file is reachable by
# exactly one `Read` in a per-pair view directory. A pair id is `<b>-<s>`
# (`b` relies on `s`); nodes are stage indices plus the synthetic `base`
# node (the reconstructed activity: order, goal, done criterion, final
# checks). See `render_pair_review_bundle`; each node is also reviewed alone
# as a unit (`render_unit_review_bundle`). ---------------------------------

class TopoUnitsCorrupt(Exception):
    """A materialized site view's on-disk content does not match the files it must hold —
    tampered with after materialization, or partially deleted. Raised by
    `verify_site_view`, which names the offending path and the remedy (delete the view
    and re-run, which re-renders it from the plan bytes)."""


def _lines_text(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def node_file_name(node: "int | str") -> str:
    """The view file name for one pair node: `base.md` or `stage-<n>.md`."""
    if node == PAIR_BASE_NODE:
        return "base.md"
    return f"stage-{int(node)}.md"


def node_file_text(doc: PlanDoc, node: "int | str") -> str:
    """One node's own FULL file: the order block for `base` (empty when the
    plan declares no order), `render_stage_brief` for a stage. The bundle's
    inlined text and the materialized file are both this one function's
    output. The `base` unit's own fields (`unit_base_text`) add the meta."""
    if node == PAIR_BASE_NODE:
        lines = render_order_md(doc)
        return _lines_text(lines) if lines else ""
    return render_stage_brief(doc, int(node))


def unit_base_text(doc: PlanDoc) -> str:
    """Every field of the `base` unit: the meta (goal, done criterion), the order
    (customer, functional place, requirements with derivations), its coverage map
    and the final checks."""
    return _lines_text(_meta_node_lines(doc) + render_final_checks_md(doc))


def row_part_text(doc: PlanDoc, review_id: str) -> str:
    """The text of every node a unit or pair review judges — the base unit's own
    fields, the full stage fields of a stage node — for diffing two plan versions; a
    node the plan lacks contributes nothing."""
    nodes = (unit_node(review_id),) if is_unit_id(review_id) else split_pair_id(review_id)
    chunks = []
    for node in nodes:
        if node == PAIR_BASE_NODE:
            chunks.append(unit_base_text(doc))
        elif any(s.index == node for s in doc.stages):
            chunks.append(render_stage_fields(doc, int(node)))
    return "\n".join(chunks)


def row_diff(old: PlanDoc, new: PlanDoc, review_id: str) -> str:
    """A unified diff of the row's parts between two plan versions; empty when they read alike."""
    return "\n".join(difflib.unified_diff(
        row_part_text(old, review_id).splitlines(), row_part_text(new, review_id).splitlines(),
        "prior version", "live plan", n=2, lineterm=""))


def _node_label(doc: PlanDoc, node: "int | str") -> str:
    if node == PAIR_BASE_NODE:
        return "the base activity (the order)"
    stage = next(s for s in doc.stages if s.index == node)
    return f"stage {node} ({stage.title})"


def render_stage_interface(doc: PlanDoc, stage_index: int, *, contract: bool = False) -> str:
    """The interface-only projection of one stage: title, expected result
    image, criterion type, done criterion, and output_artifacts — never
    method/means/procedure (the "how", which a consumer relying on this
    stage's result never needs — see conditions 3/4 in
    `render_pair_review_bundle`).

    When `contract=True` and the stage's interface would carry no concrete
    signal (`plan.interface_empty` — a blank expected result image or
    done criterion), falls back to the full `render_stage_brief` instead:
    a consumer asked to rely on an empty interface has nothing to check
    its reliance against, so the fallback trades brevity for something
    checkable.

    Raises PlanError if no stage in `doc` carries `stage_index`."""
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    if stage is None:
        raise PlanError(f"stage {stage_index} not found in plan {doc.meta.task_id!r}")

    if contract and interface_empty(stage):
        return render_stage_brief(doc, stage_index)

    lines = [f"## Stage {stage.index}: {stage.title}", ""]
    lines.append(f"- **Expected result image:** {stage.subject.result}")
    lines.append(f"- **Criterion type:** {stage.criterion.criterion_type}")
    lines.append(f"- **Done criterion:** {stage.criterion.done_criterion}")
    if stage.output_artifacts:
        lines.append("- **Output artifacts:**")
        for a in stage.output_artifacts:
            lines.append(f"  - `{a}`")
    else:
        lines.append("- **Output artifacts:** *(none declared)*")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _publish_tree(root: Path, final: Path, tmp_prefix: str, populate, verify) -> Path:
    """Build a directory tree under a process-unique temporary sibling of `final`, then
    `os.replace` it into place in one step so a reader never sees a partial tree. A `final`
    that already exists is only verified. Losing the race to a concurrent writer (the
    replace meets a non-empty destination) discards the temporary tree and verifies the
    winner's. Any other `OSError` propagates."""
    if final.is_dir():
        verify(final)
        return final

    root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=tmp_prefix, dir=root))
    try:
        populate(tmp_dir)
        try:
            os.replace(tmp_dir, final)
        except OSError as exc:
            lost_race = exc.errno in (errno.ENOTEMPTY, errno.EEXIST) and final.is_dir()
            if not lost_race:
                raise
            shutil.rmtree(tmp_dir, ignore_errors=True)
            verify(final)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return final


_ENGINE_ORDERED = "engine-ordered"
_DECLARED_ONLY_ORDERING = "declared-only (supplies-wins collapse; not dispatch-ordered)"
_NO_ORDERING = "none"
_ORDER_EDGE = "order"
_RAW_DEPENDS_ON_ONLY_EDGE = "depends_on-only"
_WHOLE_PRODUCT = "whole product"
_SUPPLIES_KIND = "supplies"


def _engine_dispatch_closure(doc: PlanDoc, n: int) -> frozenset[int]:
    """The transitive closure of the DERIVED, supplies-only `Stage.depends_on`
    graph upstream from stage `n`, excluding `n` itself -- the engine's OWN
    dispatch-ordering view. Narrower than `plan.reliance_closure`, which
    closes over the raw `depends_on ∪ supplies.on` union, which differs only
    for a PlanDoc whose `raw_depends_on` was edited after parsing. Used only
    to compute an edge's ORDERING tag."""
    stage_by_index = {s.index: s for s in doc.stages}
    closure: set[int] = set()
    stack = [n]
    while stack:
        current = stack.pop()
        for dep in stage_by_index[current].depends_on:
            if dep not in closure:
                closure.add(dep)
                stack.append(dep)
    return frozenset(closure)


def _ordering_tag(doc: PlanDoc, consumer: int, supplier: int) -> str:
    """Whether the edge "`consumer` relies on `supplier`" is reflected in
    the engine's own dispatch order (`supplier` in the transitive closure of
    `consumer`'s derived `Stage.depends_on`) or only in the raw declared
    union `reliance_set` reads. For a parsed plan the two agree
    (`_build_supplies` merges `depends_on` into the supplies); the
    declared-only tag appears only when `raw_depends_on` was edited after
    parsing."""
    if supplier in _engine_dispatch_closure(doc, consumer):
        return _ENGINE_ORDERED
    return _DECLARED_ONLY_ORDERING


def _supply_edge_label(doc: PlanDoc, consumer: int, supplier: int) -> str:
    """Every supply edge from `supplier` to `consumer`, as declared on
    `consumer`'s own `[[stage.supplies]] on = supplier` entries, in
    declaration order and joined with `; ` — each its element (`whole
    product` when the supply names none) and artifact, when named. A stage
    declaring no supplies gets one whole-product supply per `depends_on`
    edge at parse time, so `depends_on-only` appears only for a raw
    `depends_on` edge added to the PlanDoc after parsing."""
    consumer_stage = next(s for s in doc.stages if s.index == consumer)
    edges = []
    for supply in consumer_stage.supplies:
        if supply.on != supplier:
            continue
        element = f"`{supply.element}`" if supply.element is not None else _WHOLE_PRODUCT
        edge = f"supplies {element}"
        if supply.artifact:
            edge += f" (artifact: `{supply.artifact}`)"
        if supply.delivery:
            edge += f" (delivery: `{supply.delivery}`)"
        edges.append(edge)
    return "; ".join(edges) if edges else _RAW_DEPENDS_ON_ONLY_EDGE


def _is_source_stage(doc: PlanDoc, node: "int | str") -> bool:
    return node != PAIR_BASE_NODE and not reliance_set(doc, int(node))


def _control_text(doc: PlanDoc, control: str) -> str:
    """What the stage-addressed coverage control `control` names, in the
    plan's own words (`controls` grammars: a stage's verify_command or landed
    assertion); the control's own spelling when no grammar describes it."""
    from .controls import STAGE_LANDED_ASSERTION, STAGE_VERIFY_COMMAND
    for grammar in (STAGE_VERIFY_COMMAND, STAGE_LANDED_ASSERTION):
        match = grammar.pattern.match(control)
        if match is not None:
            return grammar.describe(match, doc) or control
    return control


def _base_edge_requirements(doc: PlanDoc, service: int) -> list:
    """The order's requirements the coverage entries on stage `service` name,
    in the order's own sequence."""
    order = doc.meta.order
    wanted = {req_id for req_id, _ in plan_coverage_entries(doc).get(service, ())}
    return [r for r in (order.requirements if order is not None else ()) if r.id in wanted]


def _base_edge_proposals(doc: PlanDoc, service: int) -> list:
    """The proposals that bear on stage `service`: every rejected one, which no stage may
    realize, and the adopted or adapted ones standing against a requirement this edge
    names, which the stage may realize only as the verdict says."""
    order = doc.meta.order
    if order is None:
        return []
    edge_ids = {r.id for r in _base_edge_requirements(doc, service)}
    return [
        p for p in order.proposals
        if p.verdict == "reject" or (p.verdict in ("adopt", "adapt") and edge_ids & set(p.against))
    ]


def _base_edge_section(doc: PlanDoc, service: int) -> str:
    """The base side of a `base-<s>` pair: only the requirements the edge
    supplies — id, text, derivation — and the proposals bearing on it
    (`_base_edge_proposals`), and never the goal, done criterion, final
    checks, other requirements or the rest of the order."""
    lines = ["- **Requirements named by this edge:**"]
    for r in _base_edge_requirements(doc, service):
        label = f"**{r.id}**" if r.id else "*(no id)*"
        lines.append(f"  - {label}: {r.text}")
        lines.append(f"    - **Derivation:** {r.derivation or '*(none)*'}")
    proposals = _base_edge_proposals(doc, service)
    if proposals:
        lines.append("- **Proposals bearing on this edge:**")
        lines.extend(_proposal_lines(proposals))
    return "\n".join(lines) + "\n"


def pair_service_text(doc: PlanDoc, service: "int | str", base: "int | str | None" = None) -> str:
    """Section 3 of a pair bundle: the service's declared product. A stage
    service shows its contract interface — or, when it relies on nothing (a
    source stage), its full brief, so its own construction can be judged in
    this pair. For a `base-<s>` pair (`base` is `PAIR_BASE_NODE`) it is the
    stage's declared product — for a blank-interface stage its own fields
    (`render_stage_fields`), never the plan header or a supplier's block — and,
    under it, the control each coverage entry names, in the plan's own words."""
    if base == PAIR_BASE_NODE:
        if interface_empty(next(s for s in doc.stages if s.index == int(service))):
            product = render_stage_fields(doc, int(service))
        else:
            product = render_stage_interface(doc, int(service))
        lines = [product.rstrip("\n"), ""]
        lines.append("**Controls named by the coverage entries:**")
        for req_id, control in plan_coverage_entries(doc).get(int(service), ()):
            lines.append(f"- {req_id} → `{control}`: {_control_text(doc, control)}")
        return "\n".join(lines) + "\n"
    if _is_source_stage(doc, service):
        return render_stage_brief(doc, int(service))
    return render_stage_interface(doc, int(service), contract=True)


def pair_edge_text(doc: PlanDoc, base: "int | str", service: "int | str") -> str:
    """Section 4 of a pair bundle: the edge's label, (for a stage base) the
    base's full reliance set with each edge's kind, and the ORDERING tag. For a
    `base-<s>` pair the edge is the set of coverage entries on stage `s`, each
    requirement with the control it names."""
    if base == PAIR_BASE_NODE:
        lines = [
            f"- Edge: the base activity relies on stage {service} — {_ORDER_EDGE} coverage",
        ]
        lines.extend(
            f"  - {req_id} → {control}"
            for req_id, control in plan_coverage_entries(doc).get(int(service), ())
        )
        lines.append(f"- Ordering: {_NO_ORDERING}")
    else:
        stage = next(s for s in doc.stages if s.index == base)
        supplied = {sup.on for sup in stage.supplies}
        reliance = ", ".join(
            f"stage {m} ({_SUPPLIES_KIND if m in supplied else _RAW_DEPENDS_ON_ONLY_EDGE})"
            for m in sorted(reliance_set(doc, int(base)))
        )
        lines = [
            f"- Edge: stage {base} relies on stage {service} — "
            f"{_supply_edge_label(doc, int(base), int(service))}",
            f"- Reliance set of stage {base}: {reliance}",
            f"- Ordering: {_ordering_tag(doc, int(base), int(service))}",
        ]
    return "\n".join(lines) + "\n"


def _pair_conditions(doc: PlanDoc, base: "int | str", service: "int | str") -> list[str]:
    """The four condition texts (without markers), read for this pair's
    kind. C3 is `not applicable` for the order pair, whose base delivers no
    product of its own."""
    base_label = _node_label(doc, base)
    service_label = _node_label(doc, service)
    if base == PAIR_BASE_NODE:
        return [
            "each requirement this edge names is stated unambiguously and attributed to "
            f"{service_label} in a non-arbitrary way",
            "each named requirement is genuinely derived from the difficulty and the "
            "functional place",
            f"every named requirement is discharged through this edge — {service_label}'s "
            f"declared product delivers what the requirement asks; {service_label} realizes no "
            f"rejected proposal listed above and realizes an adopted or adapted one only as "
            f"its verdict says",
            f"the control each coverage entry names, in {service_label}, genuinely decides "
            f"its requirement and is checkable from that stage's declared product",
        ]
    return [
        f"every need of {base_label} is attributed to a declared edge — it is organized "
        f"in a non-arbitrary way",
        f"{base_label} is a genuine derivation from the order through this edge",
        f"{base_label} delivers its FULL declared product — the part that depends on "
        f"{service_label}'s product measured against it, the rest standing on its own",
        f"{service_label}'s declared product covers the part of {base_label} attributed "
        f"to this edge",
    ]


def _agreement_matches(doc: PlanDoc, agreed: "str | None") -> bool:
    return agreed is not None and agreed == understanding_digest(doc)


def _agreement_label_lines(doc: PlanDoc, agreed: "str | None") -> list[str]:
    """The line telling a reviewer the order understanding it reads is the one the user
    agreed — present only when `agreed` is the digest of this plan's own understanding, so
    a digest of an earlier understanding never vouches for the current one. `doc` is the
    plan itself, never a pointerized view of it, whose digest differs."""
    if not _agreement_matches(doc, agreed):
        return []
    return [
        f"User-agreed order understanding (digest {understanding_digest(doc)[:12]}): judge the "
        "stage against it; a defect in it is an `understanding:` concern, not a rewrite.",
        "",
    ]


def _prior_review_section(history: dict, noun: str = "pair") -> list[str]:
    """The `## Prior review of this <noun>` and `## Changed since the prior verdict`
    sections of a re-review bundle, from the `plan-review-pair-history` payload."""
    lines = [f"## Prior review of this {noun}", ""]
    for position, record in enumerate(history["records"], start=1):
        reviewer, recorded = record["reviewer_verdict"], record["effective_verdict"]
        verdicts = f"`{reviewer}`" if reviewer == recorded else f"`{reviewer}` (recorded as `{recorded}`)"
        lines.append(f"Review {position} (record {record['record_seq']}): verdict {verdicts}")
        for concern in record["concerns"]:
            if not concern["id"]:
                lines.append(f"- (not in the concern ledger) {concern['text']}")
                continue
            marks = f"{concern['severity']} (effective: {concern['effective_severity']})"
            if concern["unresolved"]:
                marks += " — UNRESOLVED BLOCKER"
            lines.append(f"- `{concern['id']}` — {marks} — parts: {', '.join(concern['parts'])}")
            lines.append(f"  {concern['text']}")
        if not record["concerns"]:
            lines.append("- no concerns")
        lines.append("")
    lines.append("## Changed since the prior verdict")
    lines.append("")
    changed = history["changed_parts_since_last"]
    lines.append(
        f"Parts of this {noun} changed since the last review: " + ", ".join(f"`{p}`" for p in changed)
        if changed else "Nothing changed since the prior verdict."
    )
    lines.append("")
    diff = history.get("prior_diff")
    if diff is not None:
        lines.append(f"## What changed in this {noun} since the prior verdict")
        lines.append("")
        if not diff["available"]:
            lines.append(
                f"The prior version is unavailable, so there is no diff: judge the {noun} whole."
            )
        elif not diff["text"]:
            lines.append(f"The text of this {noun}'s parts reads the same as in the prior version.")
        else:
            lines.extend(["```diff", diff["text"], "```"])
        lines.append("")
    return lines


def _protocol_lines(history: "dict | None", noun: str) -> list[str]:
    """The `## Review protocol` section and the closing marker line shared by
    the pair and the unit bundle; `noun` is `pair` or `unit`. Every marker is
    sourced from `plan` (`REVIEW_MARKER`, `VERDICT_MARKER`, `PLAN_DIGEST_MARKER`,
    `CONDITION_MARKERS`) — never a string literal here."""
    concern_markers = ", ".join(f"`{marker}`" for marker in CONDITION_MARKERS)
    gap_marker = CONDITION_MARKERS[3]
    lines = ["## Review protocol", ""]
    lines.append("Reply with these lines, in this order:")
    lines.append(f"- `{REVIEW_MARKER}` on a line of its own;")
    lines.append(f"- `{VERDICT_MARKER} <pass|revise>`;")
    lines.append(
        f"- `{PLAN_DIGEST_MARKER} <sha256>` — echo the `{PLAN_DIGEST_MARKER}` line above "
        f"verbatim; do not compute it;"
    )
    lines.append(
        f"- one concern per line, written `{SEVERITY_BLOCKING}: [{RESTATES_PREFIX}<concern-id>] "
        f"<marker> <concern>` or `{SEVERITY_NOTE}: ...`, where <marker> is the condition the "
        f"concern concerns ({concern_markers}); a condition-4 gap is a `{gap_marker}` line. "
        f"An untagged concern line is refused."
    )
    lines.append(
        f"  - `{SEVERITY_BLOCKING}:` keeps the plan from passing; `{SEVERITY_NOTE}:` is "
        f"recorded and does not. A `{VERDICT_MARKER} pass` carries `{SEVERITY_NOTE}:` lines only."
    )
    lines.append(
        f"  - Block only on a part that changed since the last review of this {noun}, or on a "
        f"part that still carries an unresolved blocker, re-raised as "
        f"`{RESTATES_PREFIX}<concern-id>` (the stable id of the earlier concern). A blocking "
        f"concern on an unchanged part is recorded as advisory."
    )
    if history is not None:
        lines.append(
            f"  - This is a re-review. Block only on a part listed under `## Changed since the "
            f"prior verdict`, or on a part that still carries an unresolved blocker listed "
            f"under `## Prior review of this {noun}` — raise such a blocker again, if it is still "
            f"unfixed, as `{RESTATES_PREFIX}<concern-id>`. Raise any other prior concern again "
            f"only as `{RESTATES_PREFIX}<concern-id>`; do not restate a settled concern as new. "
            f"After a prior `pass`, a blocking concern that names no changed part by its part "
            f"token and re-raises no open blocker is recorded as advisory."
        )
        lines.append(
            "  - Name the part a concern is about with a part token (`stage:<n>`, `meta:`, "
            f"`order:`) leading the concern or right after its marker: `{SEVERITY_BLOCKING}: "
            f"{CONDITION_MARKERS[0]} stage:3 <concern>`."
        )
    lines.append(
        f"- after the concerns, a line `{CUSTOMER_QUESTIONS_MARKER}` followed by one "
        f"`{CUSTOMER_QUESTION_MARKER} <question>` line per question only the customer can "
        f"decide (a choice or a fact this {noun} depends on and the plan does not settle), or "
        f"`{CUSTOMER_QUESTIONS_MARKER} {CUSTOMER_QUESTIONS_NONE}`. A question is not a plan "
        f"remark: a flaw the coordinator can fix is a concern, not a question."
    )
    lines.append(
        f"The {REVIEW_MARKER} block is the last thing in your reply, with no other "
        f"marker (COMPLETED:, REPLAN:, etc.) after it."
    )
    lines.append("")
    return lines


def render_pair_review_bundle(
    doc: PlanDoc, pair_id: str, *, plan_sha256: str, view_dir: "Path | str",
    history: "dict | None" = None, agreed_understanding: "str | None" = None,
    understanding_doc: "PlanDoc | None" = None,
) -> str:
    """The `--review-topo` starting prompt for one reliance edge: the order
    context, the base's full node file, the service's declared product
    (`pair_service_text`), the edge (`pair_edge_text`), the one service
    file in `view_dir`, the per-pair procedure, (for a re-review) the prior
    review and the parts changed since it, the plan digest line and the
    review protocol/checklist. `history` is the `plan-review-pair-history`
    data payload; without it the bundle is the first-review rendering.

    Every marker string in the protocol section is sourced from
    `plan.REVIEW_MARKER` / `plan.VERDICT_MARKER` / `plan.PLAN_DIGEST_MARKER`
    / `plan.CONDITION_MARKERS` — never duplicated here as a string literal,
    so a reviewer's reply and this bundle's own checklist can never drift
    onto different marker spellings.

    Raises ValueError for a pair the plan does not have, and PlanError for
    a dangling or cyclic raw reliance graph."""
    base, service = parse_pair(doc, pair_id)

    lines: list[str] = [f"# Topological review pair: {pair_id}", ""]
    lines.append(f"Base: {_node_label(doc, base)}. Service: {_node_label(doc, service)}.")
    lines.append("")

    if base == PAIR_BASE_NODE:
        lines.append(f"## Base: {_node_label(doc, base)} — the part this edge supplies")
        lines.append("")
        lines.extend(_agreement_label_lines(understanding_doc or doc, agreed_understanding))
        lines.append(_base_edge_section(doc, int(service)).rstrip("\n"))
        lines.append("")
    else:
        base_text = node_file_text(doc, PAIR_BASE_NODE)
        lines.append("## Order context")
        lines.append("")
        lines.append(base_text.rstrip("\n") if base_text else "*(the plan declares no order)*")
        lines.append("")

        lines.append(f"## Base: {_node_label(doc, base)}")
        lines.append("")
        lines.append(node_file_text(doc, base).rstrip("\n"))
        lines.append("")

    lines.append(f"## Service declared product: {_node_label(doc, service)}")
    lines.append("")
    if base != PAIR_BASE_NODE and _is_source_stage(doc, service):
        lines.append(
            "*(this stage relies on nothing, so its full brief is shown and its own "
            "construction is judged in this pair)*"
        )
        lines.append("")
    lines.append(pair_service_text(doc, service, base).rstrip("\n"))
    lines.append("")

    lines.append("## Edge")
    lines.append("")
    lines.append(pair_edge_text(doc, base, service).rstrip("\n"))
    lines.append("")

    lines.append("## Service file")
    lines.append("")
    lines.append(
        f"The service's full node file is one `Read` away in the view directory `{view_dir}`:"
    )
    lines.append(f"- `{Path(view_dir) / node_file_name(service)}`")
    lines.append("")

    gap_marker = CONDITION_MARKERS[3]
    lines.append("## Per-pair procedure")
    lines.append("")
    lines.append(
        f"1. Decide `{gap_marker}` (the service's declared product covers the part of the "
        f"base attributed to this edge) from the service's declared product above first."
    )
    base_inlined = (
        "The base part this edge supplies is inlined above in full."
        if base == PAIR_BASE_NODE else "The base is inlined above in full."
    )
    lines.append(
        "2. Only when that section cannot decide it, `Read` the one file listed in the "
        f"service file section. {base_inlined}"
    )
    lines.append(f"3. Report any gap as one `{gap_marker}` line.")
    lines.append(
        "4. Check the remaining conditions below for this pair only, then reply per the "
        "protocol. Judge nothing about other stages."
    )
    lines.append("")

    if history is not None:
        lines.extend(_prior_review_section(history))

    lines.append(f"{PLAN_DIGEST_MARKER} {plan_sha256}")
    lines.append("")

    lines.extend(_protocol_lines(history, "pair"))
    lines.append("Conditions:")
    for marker, text in zip(CONDITION_MARKERS, _pair_conditions(doc, base, service)):
        lines.append(f"- `{marker}` {text}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _unit_conditions(doc: PlanDoc, node: "int | str") -> list[str]:
    """The four condition texts of a unit review, read for its kind. A unit is
    judged alone, from its own fields and its declared edges."""
    if node == PAIR_BASE_NODE and not doc.stages:
        return [
            "the activity is reconstructed and the difficulty is placed in it — customer, "
            "activity, difficulty, functional place, requirements, proposals and "
            "constraints do not contradict each other",
            "every requirement follows from the difficulty and the functional place — "
            "none is arbitrary, none is missing a derivation, and no requirement merely "
            "restates a proposal; each proposal's critique holds against the requirements "
            "it names",
            "every constraint is user-stated and offered, not decided by the author, and "
            "binds the solution as stated; no requirement or adopted or adapted proposal "
            "contradicts one; every requirement is decidable by a check a later stage "
            "can carry",
            "the goal and the done criterion, where stated, match the requirements and "
            "are checkable",
        ]
    if node == PAIR_BASE_NODE:
        constraints = doc.meta.order is not None and bool(doc.meta.order.constraints)
        return [
            "the order and the meta are internally consistent — customer, functional "
            "place, requirements and the coverage map do not contradict each other",
            "every requirement follows from the difficulty and the functional place — "
            "none is arbitrary, none is missing a derivation; every proposal's verdict "
            "follows from its critique and the requirements it stands against",
            "every requirement has a coverage entry or a final check that decides it — "
            "none is left uncovered"
            + ("; every constraint is respected by the coverage entries and the final "
               "checks" if constraints else ""),
            "the goal, the done criterion and the final checks match the requirements "
            "and are checkable",
        ]
    return [
        "the stage's own fields are internally consistent — result image, done "
        "criterion, criterion, means and method do not contradict each other",
        "the stage relies only on its own fields and the edges declared below — it "
        "assumes nothing a declared edge does not supply",
        "the stage is consumed — the inbound edge set is not empty (an empty inbound "
        "set is an orphan: no stage relies on it and no requirement names it)",
        "the done criterion is checkable from the stage's own controls",
    ]


def _unit_edge_lines(doc: PlanDoc, node: "int | str") -> list[str]:
    """The `## Edges` body of a unit bundle: the unit's full edge set, an empty
    set stated explicitly (an empty inbound set is the orphan signal)."""
    lines: list[str] = []
    if node == PAIR_BASE_NODE and not doc.stages:
        lines.append(
            "Outbound — none at the order phase: coverage entries are written with the stages."
        )
        lines.append("")
        lines.append("Inbound — what relies on the base: nothing (the base is the root).")
        return lines
    if node == PAIR_BASE_NODE:
        entries = plan_coverage_entries(doc)
        known = {s.index for s in doc.stages}
        lines.append("Outbound — the coverage entries, each a typed edge of the base on a stage:")
        rows = [
            f"- {req_id} → stage {s}: `{control}`"
            for s in sorted(entries) if s in known for req_id, control in entries[s]
        ]
        lines.extend(rows or ["- none"])
        lines.append("")
        lines.append("Inbound — what relies on the base: nothing (the base is the root).")
        return lines
    n = int(node)
    lines.append("Outbound — what this stage relies on:")
    outbound = sorted(reliance_set(doc, n))
    lines.extend(
        [f"- stage {m} — {_supply_edge_label(doc, n, m)}" for m in outbound] or ["- none"]
    )
    lines.append("")
    lines.append("Inbound — what relies on this stage:")
    inbound = [f"- stage {c} — {_supply_edge_label(doc, c, n)}" for c in sorted(consumers(doc, n))]
    for req_id, control in plan_coverage_entries(doc).get(n, ()):
        inbound.append(f"- the base: requirement {req_id} → `{control}`")
    lines.extend(inbound or [
        "- none — ORPHAN: no stage relies on this stage and no requirement names it"
    ])
    return lines


def render_unit_review_bundle(
    doc: PlanDoc, unit: str, *, plan_sha256: str, history: "dict | None" = None,
    agreed_understanding: "str | None" = None,
) -> str:
    """The `--review-topo` starting prompt for one UNIT: every field of the
    node, inlined, and its full edge set; nothing else is read. `unit:base`
    holds the order and the meta's goal, done criterion and final checks;
    `unit:<n>` the whole of stage `n`. The same marker protocol as a pair
    bundle (`_protocol_lines`), with unit-specific conditions
    (`_unit_conditions`). Dispatchers key on the `Topological review unit:`
    heading, never the pair heading.

    Raises ValueError for a unit the plan does not have."""
    node = parse_unit(doc, unit)
    lines: list[str] = [f"# Topological review unit: {unit}", ""]
    lines.append(f"Unit: {_node_label(doc, node)}.")
    lines.append("")
    if node == PAIR_BASE_NODE:
        lines.extend(_agreement_label_lines(doc, agreed_understanding))

    lines.append("## Fields")
    lines.append("")
    fields = unit_base_text(doc) if node == PAIR_BASE_NODE else render_stage_fields(doc, int(node))
    lines.append(fields.rstrip("\n"))
    lines.append("")

    lines.append("## Edges")
    lines.append("")
    lines.extend(_unit_edge_lines(doc, node))
    lines.append("")

    lines.append("## Per-unit procedure")
    lines.append("")
    lines.append(
        "1. Judge this unit alone, from the fields and the edges above; there is no "
        "other file to read and no other node to judge."
    )
    lines.append(f"2. Report any gap as one `{CONDITION_MARKERS[3]}` line.")
    lines.append("3. Check the other conditions below, then reply per the protocol.")
    lines.append("")

    if history is not None:
        lines.extend(_prior_review_section(history, "unit"))

    lines.append(f"{PLAN_DIGEST_MARKER} {plan_sha256}")
    lines.append("")

    lines.extend(_protocol_lines(history, "unit"))
    lines.append("Conditions:")
    for marker, text in zip(CONDITION_MARKERS, _unit_conditions(doc, node)):
        lines.append(f"- `{marker}` {text}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- Site prompts: the one builder every model-facing site takes its plan content from.
# A site shows the nodes (the meta, or a stage) it needs in full and the rest of the plan
# only through PROJECTED_FIELDS; whatever else it may need is a file in a view directory
# granted to the model, so the plan content a prompt carries is bounded by the site, not
# by the size of the plan. ------------------------------------------------------------

# --- Node renderer and size limits: one node (the plan-level node or a stage) printed with
# every field, the text both the base role of a site and the size measure are taken from. ---

_STAGE_SCHEMA_KEYS = (
    plan_refs.EXTERNALIZABLE_FIELDS["stage"] | plan_refs.NON_STRING_FIELDS["stage"]
    | {"observation", "control"}
)
_META_SCHEMA_KEYS = plan_refs.EXTERNALIZABLE_FIELDS["meta"] | plan_refs.NON_STRING_FIELDS["meta"]
_ORDER_SCHEMA_KEYS = plan_refs.EXTERNALIZABLE_FIELDS["order"] | plan_refs.NON_STRING_FIELDS["order"]
_WRAPPER_STAGE = {
    "title": "-", "executor": "in_thread", "expected_result_image": "-", "done_criterion": "-",
}


_DRAFT_COVERAGE_LINE = (
    "Coverage: not applicable to an order draft; it maps requirements to stages "
    "written after agreement"
)


def _meta_node_lines(doc: PlanDoc) -> list[str]:
    if not doc.stages:
        return (render_meta_md(doc) + render_order_md(doc) + render_order_understanding_md(doc)
                + [_DRAFT_COVERAGE_LINE, ""])
    return (render_meta_md(doc) + render_order_md(doc) + render_order_understanding_md(doc)
            + render_order_coverage_md(doc))


def _meta_extra_lines(doc: PlanDoc) -> list[str]:
    """The meta fields `render_meta_md` and `render_order_md` leave out."""
    lines: list[str] = []
    if doc.meta.delivery_worktree:
        lines.append(f"- **Delivery worktree:** {doc.meta.delivery_worktree}")
    if doc.meta.order is not None and doc.meta.order.parent_norm:
        lines.append(f"- **Parent norm:** {doc.meta.order.parent_norm}")
    return lines


def _undefined_lines(raw: dict, schema: frozenset) -> list[str]:
    return [
        f"- **{key}:** {_raw_value_text(raw[key])}" for key in sorted(k for k in raw if k not in schema)
    ]


def _raw_value_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def _generic_node_lines(heading: str, raw: dict, schema: frozenset) -> list[str]:
    """A raw node the loader cannot type: every key it holds, the schema's first and then
    the undefined ones sorted."""
    lines = [heading, ""]
    lines.extend(f"- **{key}:** {_raw_value_text(raw[key])}" for key in sorted(k for k in raw if k in schema))
    lines.extend(_undefined_lines(raw, schema))
    return lines


def _typed_stage_from_raw(raw: dict):
    """The Stage `parse_plan` makes of a raw stage table, or None when the table does not
    parse on its own (a required key missing, a value the loader refuses)."""
    index = raw.get("index", 1)
    shell = {k: v for k, v in raw.items() if k not in ("depends_on", "supplies")}
    try:
        doc = parse_plan({
            "meta": {"task_id": "node"},
            "stage": [{**_WRAPPER_STAGE, **shell, "index": index}],
        }, strict=False)
        stage = doc.stages[0]
        stage.supplies = _build_supplies(raw, int(index))
    except (PlanError, ValueError, TypeError, AttributeError):
        return None
    return stage


def _raw_stage_node_text(raw: dict, venue: "str | None") -> str:
    stage = _typed_stage_from_raw(raw) if all(raw.get(k) for k in _WRAPPER_STAGE) else None
    if stage is None:
        index = raw.get("index", "?")
        lines = _generic_node_lines(f"## Stage {index}: {raw.get('title', '')}", raw, _STAGE_SCHEMA_KEYS)
        return _lines_text(lines)
    lines = _stage_field_lines(None, stage, dependency_blocks=False, venue=venue)
    return _lines_text([*lines, *_undefined_lines(raw, _STAGE_SCHEMA_KEYS)])


def _raw_meta_node_text(raw: dict) -> str:
    meta = raw["meta"] if isinstance(raw.get("meta"), dict) else raw
    try:
        doc = parse_plan({"meta": meta, "stage": [dict(_WRAPPER_STAGE)]}, strict=False)
    except (PlanError, ValueError, TypeError, AttributeError):
        doc = None
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    if doc is None:
        lines = _generic_node_lines("# Plan", meta, _META_SCHEMA_KEYS)
        lines.extend(_generic_node_lines("## Order", order, _ORDER_SCHEMA_KEYS) if order else [])
        return _lines_text(lines)
    lines = [*_meta_node_lines(doc), *_meta_extra_lines(doc), *_undefined_lines(meta, _META_SCHEMA_KEYS),
             *_undefined_lines(order, _ORDER_SCHEMA_KEYS)]
    return _lines_text(lines)


def render_node(
    node, *, role: str = "base", inline_all: bool = True, venue: "str | None" = None,
    kind: "str | None" = None,
) -> str:
    """The text of one plan node with every field printed in full, references resolved:
    a stage (`state.Stage`, or a raw stage table), or the plan-level node — `[meta]` and
    `[meta.order]` — given a `PlanDoc` or a raw plan (or `[meta]`) dict.

    The `base` role prints each field of the node itself and nothing of its neighbours: no
    direct-dependency summaries, no final-verification labels, no coverage control text. A
    raw table is rendered whether or not the loader would accept it — what it holds is
    typed when it parses, and every key it holds is printed either way, the undefined ones
    last — so a node the loader refuses can still be measured. `venue` is the grant venue
    of a stage given without its plan; `kind` ("meta" or "stage") says what a raw table is
    when its keys do not (a bare `[meta]` table).

    This is the size measure of a node (`plan.stage_rendered_size` / `meta_rendered_size`),
    not the text `build_site_prompt` prints: that builder renders its nodes through the
    per-site bundle renderers (with dependency summaries, coverage control text and
    pointer-ised long fields), which the node limit does not cover and the prompt-level
    refusal against the spawn ceiling does. The top-level `[[final_check]]` list belongs to
    no node, so no node limit bounds it either — only that same refusal does."""
    if role != "base" or not inline_all:
        raise ValueError("render_node prints the base role with every field inline")
    if isinstance(node, PlanDoc):
        return _lines_text([*_meta_node_lines(node), *_meta_extra_lines(node)])
    if isinstance(node, dict):
        if (kind or ("meta" if "meta" in node else "stage")) == "meta":
            return _raw_meta_node_text(node)
        return _raw_stage_node_text(node, venue)
    return _lines_text(_stage_field_lines(None, node, dependency_blocks=False, venue=venue))


PATH_PLACEHOLDERS = ("<view-root>", "<repo>", "<cwd>", "<plans-dir>", "<agent-home>", "<home>")


def named_prompt_paths(view_root: "str | Path") -> "list[tuple[Path, str]]":
    """The machine-specific directories a prompt can name, each with its placeholder."""
    from lib import config_root

    paths = (
        Path(view_root), Path(__file__).resolve().parents[2], Path.cwd(),
        config_root.plans_dir(), config_root.agent_home(), Path.home(),
    )
    return list(zip(paths, PATH_PLACEHOLDERS))


def normalize_prompt_paths(text: str, view_root: "str | Path") -> str:
    """`text` with the machine-specific directories it names replaced by placeholders —
    the view root by `<view-root>`; the repo root, the working directory, the plans
    directory, the agent home and the user's home by `<repo>`, `<cwd>`, `<plans-dir>`,
    `<agent-home>` and `<home>` — longest path first, so a prompt measures the same on
    any machine."""
    paths: dict[str, str] = {}
    for path, placeholder in named_prompt_paths(view_root):
        for form in {str(path), str(Path(path).resolve())}:
            if len(form) > 1:
                paths[form] = placeholder
    for form in sorted(paths, key=len, reverse=True):
        text = text.replace(form, paths[form])
    return text


def dispatch_ceiling_chars(model: "str | None" = None) -> int:
    """The prompt ceiling a spawn is refused above (`lib.spawn_ceiling`'s
    `dispatch_prompt_ceiling_chars`, the one the launcher applies)."""
    return dispatch_prompt_ceiling_chars(model)


def topo_unit_refusal(
    doc: PlanDoc, unit_nodes, prompt_text: "str | None" = None, *, model: "str | None" = None,
) -> "str | None":
    """Why a topological review of `unit_nodes` (node ids; "meta" or "base" stands for the
    plan-level node) must not be spawned, or None when it may. A node over
    `plan-stage-max-chars` is refused with the message the submission lint blocks with; a
    `prompt_text` over the spawn ceiling (of `model`, the launcher's) is refused on its own
    size. A stage limit that cannot be read refuses with its own message."""
    from .plan import oversize_node_message

    try:
        limit, _ = plan_size_limits()
    except PlanLimitError as exc:
        return str(exc)
    venue = _venue_for(doc)
    seen: set = set()
    for node in unit_nodes:
        label = "meta" if node in ("meta", PAIR_BASE_NODE) else int(node)
        if label in seen:
            continue
        seen.add(label)
        if label == "meta":
            text = render_node(doc)
        else:
            stage = next((s for s in doc.stages if s.index == label), None)
            if stage is None:
                continue
            text = render_node(stage, venue=venue)
        if len(text) > limit:
            return oversize_node_message(label, len(text), limit)
    if prompt_text is not None:
        ceiling = dispatch_ceiling_chars(model)
        if len(prompt_text) > ceiling:
            return (
                f"the review prompt for {', '.join(str(n) for n in unit_nodes)} is "
                f"{len(prompt_text)} chars, over the spawn ceiling {ceiling}: the nodes fit "
                f"plan-stage-max-chars {limit} but the prompt around them does not — shrink "
                "the nodes it shows, their dependency summaries or the review history"
            )
    return None


NON_PLAN_BRIEF_MAX_CHARS = 20000


def non_plan_brief_text(path: "str | Path") -> str:
    """The text of a `--plan` file that does not load as a typed plan — a plain brief
    (the difficulty-escape manager's, a work-item brief) — to be passed inline. A file
    longer than `NON_PLAN_BRIEF_MAX_CHARS` is refused (ValueError), so no unbounded file
    reaches a prompt by the way of a failed plan load."""
    text = Path(path).read_text(encoding="utf-8")
    if len(text) > NON_PLAN_BRIEF_MAX_CHARS:
        raise ValueError(
            f"{path} does not load as a typed plan and is longer than "
            f"{NON_PLAN_BRIEF_MAX_CHARS} chars; a brief that long belongs in a typed plan "
            "or a context dossier"
        )
    return text


SITE_KINDS = (
    "dispatch-stage-brief", "dispatch-no-stage", "topo-pair", "topo-unit",
    "whole-plan-review", "enumerate-node",
)
_NO_VIEW_KINDS = ("dispatch-stage-brief", "dispatch-no-stage")

# The plan keys another node's summary may print, per node kind: what a consumer relies on
# (a stage's declared product) and what orients any reader (the meta's goal and checks by
# label). Method, procedure, knowledge and invariants of a stage, and the meta's context,
# are never in it.
PROJECTED_FIELDS: dict[str, frozenset[str]] = {
    "stage": frozenset({
        "title", "expected_result_image", "criterion_type", "done_criterion",
        "output_artifacts",
    }),
    "meta": frozenset({
        "goal", "task_id", "weight_class", "done_criterion", "criterion_type", "repo_root",
        "delivery_worktree", "external_research", "final_check",
    }),
}

_POINTER_PREFIX = "see objects/"

# The prose attributes of the typed plan that a view prints as a pointer when over the
# inline limit; commands, paths and identifiers are never pointers.
_POINTER_ATTRS = frozenset({
    "knowledge", "material", "result", "invariants", "means", "method", "procedure",
    "preconditions", "conditions", "done_criterion", "observation", "control", "goal",
    "external_research", "customer", "functional_place", "text", "derivation",
})


@dataclass(frozen=True)
class SitePrompt:
    text: str
    add_dirs: tuple[str, ...]
    shown_nodes: frozenset
    view_dir: "Path | None" = None
    view_files: tuple[str, ...] = ()


@dataclass
class _SiteSource:
    doc: PlanDoc
    index_bytes: bytes
    plan_sha256: str
    visited: list
    files: dict


def _load_site_source(plan_path: "str | Path") -> _SiteSource:
    """The plan's typed doc together with every byte it was made of: the index and each
    referenced file as `resolve_refs` read them, so the objects a view holds are the
    verified bytes behind the doc and not a second read of the file system."""
    path = Path(plan_path)
    try:
        data = path.read_bytes()
        raw = tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"cannot read plan file {path}: {exc}") from exc
    files: dict[str, bytes] = {}

    def collect(ref_path: Path, sha256: str) -> bytes:
        content = plan_refs.read_referenced_file(ref_path, sha256)
        files[hashlib.sha256(content).hexdigest()] = content
        return content

    resolved, visited = plan_refs.resolve_refs(raw, path.parent, None, read=collect, index_path=path)
    if not resolved.get("stage") and (resolved.get("meta") or {}).get("order") is not None:
        doc, _ = load_order_draft(path)
    else:
        doc = parse_plan(resolved)
    return _SiteSource(doc, data, hashlib.sha256(data).hexdigest(), visited, files)


def _visited_nodes(visited: list) -> dict[str, set]:
    """sha256 of each referenced file -> the nodes ("meta" or a stage index) whose
    references lead to it."""
    nodes: dict[str, set] = {}
    for position, _path, sha256 in visited:
        head = position[0]
        nodes.setdefault(sha256, set()).add(head if isinstance(head, int) else "meta")
    return nodes


def _pointerize(doc: PlanDoc, limit: int) -> tuple[PlanDoc, dict[str, bytes]]:
    """A copy of `doc` whose prose fields longer than `limit` characters read
    `see objects/<sha256>`, and the bytes of each such field by that hash."""
    view = copy.deepcopy(doc)
    objects: dict[str, bytes] = {}

    def walk(node) -> None:
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for item in dataclasses.fields(node):
                value = getattr(node, item.name)
                if isinstance(value, str):
                    if item.name in _POINTER_ATTRS and len(value) > limit:
                        data = value.encode("utf-8")
                        sha256 = hashlib.sha256(data).hexdigest()
                        objects[sha256] = data
                        setattr(node, item.name, f"{_POINTER_PREFIX}{sha256}")
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for inner in node:
                walk(inner)
        elif isinstance(node, dict):
            for inner in node.values():
                walk(inner)

    walk(view)
    return view, objects


def _site_files(text_files: dict[str, str], objects: dict[str, bytes]) -> dict[str, bytes]:
    files = {name: text.encode("utf-8") for name, text in text_files.items()}
    files.update({f"objects/{sha256}": data for sha256, data in objects.items()})
    return files


def _site_manifest(files: dict[str, bytes]) -> str:
    return json.dumps(
        {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        sort_keys=True, indent=2,
    )


def verify_site_view(view_dir: "Path | str", files: dict[str, bytes]) -> None:
    """Re-check an already-materialized site view against the files it must hold: the exact
    file set, each file's bytes, and `MANIFEST.json`. Raises `TopoUnitsCorrupt` naming the
    offending path."""
    view_dir = Path(view_dir)
    remedy = f"delete {view_dir} and re-run to re-materialize it"
    if not view_dir.is_dir():
        raise TopoUnitsCorrupt(f"{view_dir} is missing; {remedy}")
    expected = {**files, "MANIFEST.json": _site_manifest(files).encode("utf-8")}
    actual = {p.relative_to(view_dir).as_posix() for p in view_dir.rglob("*") if p.is_file()}
    if actual != set(expected):
        extra = sorted(actual - set(expected))
        missing = sorted(set(expected) - actual)
        raise TopoUnitsCorrupt(f"{view_dir} file set mismatch — extra: {extra}, missing: {missing}; {remedy}")
    for name, data in expected.items():
        if (view_dir / name).read_bytes() != data:
            raise TopoUnitsCorrupt(f"{view_dir / name} content does not match its expected bytes; {remedy}")


def _materialize_site_view(
    root: Path, name: str, files: dict[str, bytes],
) -> Path:
    def populate(tmp_dir: Path) -> None:
        for rel, data in files.items():
            target = tmp_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (tmp_dir / "MANIFEST.json").write_text(_site_manifest(files), encoding="utf-8")

    return _publish_tree(
        root, root / name, f".{name}.{os.getpid()}.", populate,
        lambda tree: verify_site_view(tree, files),
    )


def _view_header(view_dir: Path) -> list[str]:
    return [
        f"Plan view: `{view_dir}` — a field too long to print here reads "
        f"`{_POINTER_PREFIX}<sha256>`, a file under that directory.",
        "",
    ]


def plan_full_text(doc: PlanDoc) -> str:
    """Every field of the meta and of every stage, references resolved — the reading of a
    site that has no directory to read the stages from."""
    parts = [unit_base_text(doc).rstrip("\n")]
    parts.extend(render_stage_fields(doc, stage.index).rstrip("\n")
                 for stage in sorted(doc.stages, key=lambda s: s.index))
    return "\n\n".join(parts) + "\n"


def plan_summary_text(doc: PlanDoc) -> str:
    """The whole-plan reading a site without a stage gets: the meta in full, then every
    stage by its declared product only."""
    lines = unit_base_text(doc).rstrip("\n").split("\n")
    lines.extend(["", "## Stages (declared product only)", ""])
    for stage in sorted(doc.stages, key=lambda s: s.index):
        lines.extend(render_stage_interface(doc, stage.index).rstrip("\n").split("\n"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _referenced_files_section(visited: list) -> list[str]:
    """What a site that shows only the plan summary offers beside it: each file the plan
    was assembled from, by the position of the reference that led to it and by its object
    in the view. Empty for a plan without references."""
    if not visited:
        return []
    lines = ["## Referenced files", "",
             "The plan was assembled from these files, each a file of this view:", ""]
    lines.extend(
        f"- `{'.'.join(str(part) for part in position)}` — `objects/{sha256}`"
        for position, _path, sha256 in visited)
    return [*lines, ""]


def _pair_shown_nodes(doc: PlanDoc, pair_id: str) -> frozenset:
    base, service = parse_pair(doc, pair_id)
    shown: set = {"meta"}
    stage = next(s for s in doc.stages if s.index == int(service))
    if base == PAIR_BASE_NODE:
        if interface_empty(stage):
            shown.add(int(service))
    else:
        shown.add(int(base))
        if _is_source_stage(doc, service) or interface_empty(stage):
            shown.add(int(service))
    return frozenset(shown)


def build_site_prompt(
    kind: str, plan_path: "str | Path", *, stage: "int | None" = None,
    pair: "str | None" = None, unit: "str | None" = None,
    view_root: "str | Path | None" = None, history: "dict | None" = None,
    materialize: bool = True, inline_max_chars: "int | None" = None, with_view: bool = True,
    agreed_understanding: "str | None" = None,
) -> SitePrompt:
    """The plan content one model-facing site passes, and the directories the model is
    granted to read the rest from (`add_dirs`). `shown_nodes` names the nodes — "meta" or a
    stage index — whose fields the text prints beyond `PROJECTED_FIELDS`.

    Every kind but `topo-unit` and `enumerate-node` (which print every field of one node
    and read nothing) builds a view directory under `view_root` (default: the topo units
    root): the node files, `objects/<sha256>` for every file the shown nodes were
    resolved from and for every field printed as a pointer, and `MANIFEST.json`.
    `materialize=False` computes the same prompt and view path without writing.
    `with_view=False` is for a caller that grants the model no directory and has no prompt
    ceiling (the two dispatch kinds only): the text carries no view header, no file listing
    and no `see objects/` pointer, nothing is written, and `add_dirs` is empty. A
    `dispatch-no-stage` site then inlines the whole rendered plan, references resolved —
    the named exception to "never the inlined plan" (ADR-0012) — since there is no view
    for its model to read the stages from.

    Raises PlanError for a plan that does not load and ValueError for a stage, pair or
    unit the plan does not have, or for `with_view=False` on any other kind."""
    if kind not in SITE_KINDS:
        raise ValueError(f"unknown site kind {kind!r}; known kinds: {', '.join(SITE_KINDS)}")
    if not with_view and kind not in _NO_VIEW_KINDS:
        raise ValueError(f"with_view=False is for {', '.join(_NO_VIEW_KINDS)} only, not {kind!r}")
    source = _load_site_source(plan_path)
    doc = source.doc

    if kind == "topo-unit":
        node = parse_unit(doc, unit or "")
        text = render_unit_review_bundle(
            doc, unit, plan_sha256=source.plan_sha256, history=history,
            agreed_understanding=agreed_understanding)
        return SitePrompt(text, (), frozenset({"meta" if node == PAIR_BASE_NODE else int(node)}))
    if kind == "enumerate-node":
        if stage is None:
            return SitePrompt(unit_base_text(doc), (), frozenset({"meta"}))
        return SitePrompt(render_stage_fields(doc, stage), (), frozenset({int(stage)}))

    limit = plan_field_inline_limit() if inline_max_chars is None else inline_max_chars
    if not with_view:
        limit = sys.maxsize
    shown_doc, pointer_objects = _pointerize(doc, limit)
    index_object = kind in ("whole-plan-review", "dispatch-no-stage")
    label = kind

    def header(view_dir: Path) -> list[str]:
        return _view_header(view_dir) if with_view else []

    if kind == "dispatch-stage-brief":
        if stage is None:
            raise ValueError("a dispatch-stage-brief site needs a stage")
        label = f"{kind}-{stage}"
        shown = frozenset({int(stage)})
        view_nodes = shown
        try:
            closure = stage_closure_digest(doc, int(stage))
        except PlanError:
            closure = None

        def render(view_dir: Path) -> tuple[str, dict[str, str]]:
            brief = render_stage_brief(
                shown_doc, int(stage), closure_digest=closure,
                final_check_note=STAGE_BRIEF_FINAL_CHECK_NOTE_IN_VIEW)
            return "\n".join([*header(view_dir), brief]), {node_file_name(int(stage)): brief}
    elif kind == "topo-pair":
        base, service = parse_pair(doc, pair or "")
        label = f"{kind}-{pair}"
        shown = _pair_shown_nodes(doc, pair)
        view_nodes = shown | {int(service)}

        def render(view_dir: Path) -> tuple[str, dict[str, str]]:
            bundle = render_pair_review_bundle(
                shown_doc, pair, plan_sha256=source.plan_sha256, view_dir=view_dir,
                history=history, agreed_understanding=agreed_understanding,
                understanding_doc=doc)
            return ("\n".join([*header(view_dir), bundle]),
                    {node_file_name(service): node_file_text(shown_doc, service)})
    else:
        shown = frozenset({"meta"})
        view_nodes = shown | {stage_node.index for stage_node in doc.stages}
        whole_plan_inline = not with_view and kind == "dispatch-no-stage"
        if whole_plan_inline:
            shown = view_nodes

        def render(view_dir: Path) -> tuple[str, dict[str, str]]:
            if whole_plan_inline:
                return plan_full_text(shown_doc), {}
            summary = plan_summary_text(shown_doc)
            parts = [*header(view_dir), summary]
            if with_view:
                parts.extend(_referenced_files_section(source.visited))
            if kind == "dispatch-no-stage" and with_view:
                parts.extend([
                    "## Plan file", "",
                    f"The plan file itself is `{_POINTER_PREFIX}{source.plan_sha256}` in the view "
                    f"directory: read it for the full text of any stage listed above.",
                    "",
                ])
            if kind == "whole-plan-review":
                parts.extend([
                    "## Plan digest", "",
                    f"The plan file itself is `{_POINTER_PREFIX}{source.plan_sha256}` in the view "
                    f"directory: its sha256 is the plan digest.",
                    "",
                    f"{PLAN_DIGEST_MARKER} {source.plan_sha256}",
                ])
            return "\n".join(parts).rstrip() + "\n", {"meta.md": unit_base_text(shown_doc)}

    def view_files(text: str, node_files: dict[str, str]) -> dict[str, bytes]:
        printed = "\n".join([text, *node_files.values()])
        objects = {
            sha256: data for sha256, data in pointer_objects.items()
            if f"{_POINTER_PREFIX}{sha256}" in printed
        }
        owners = _visited_nodes(source.visited)
        objects.update({
            sha256: source.files[sha256] for sha256, nodes in owners.items()
            if nodes & view_nodes and sha256 in source.files
        })
        if index_object:
            objects[source.plan_sha256] = source.index_bytes
        return _site_files(node_files, objects)

    placeholder = Path("<view>")
    if not with_view:
        return SitePrompt(render(placeholder)[0], (), shown)
    first_text, node_files = render(placeholder)
    files = view_files(first_text, node_files)
    root = Path(view_root) if view_root is not None else _default_view_root()
    name = (f"site-{source.plan_sha256[:16]}-{label}-"
            f"{hashlib.sha256(_site_manifest(files).encode('utf-8')).hexdigest()[:12]}")
    view_dir = root / name
    if materialize:
        _materialize_site_view(root, name, files)
    text, _ = render(view_dir)
    return SitePrompt(text, (str(view_dir),), shown, view_dir, tuple(sorted(files)))


def _default_view_root() -> Path:
    from lib import config_root

    return config_root.topo_units_root()


def render_plan_grants(doc: PlanDoc, fmt: str = "compact") -> str:
    """Render the plan's per-stage grant set — declared (plan-authored, in
    `[stage.grants]`) plus derived (mechanically proposed by
    `grants.derive_stage_grants` from the stage's OTHER fields) — as a
    projection, exactly like `render_plan_md`: never written to disk, the typed
    plan (+ the pure derivation function) is the one source of truth.

    `fmt="compact"`: one line per stage — every DECLARED allow rule and
    add_dir (with its mode) verbatim, every DR-O and DR-R DERIVED entry
    verbatim (a reader needs to see exactly what output-artifact/outside-venue
    grant a stage is about to receive), DR-V and DR-E entries only COUNTED (a
    verify_command can carry many segments and a developer/tech-writer stage
    many in-venue refs — spelling every one out would defeat "compact"), plus
    a dropped-count for whatever the validator refused. A spawn stage also
    gets a `kind_baseline=<kind>:<digest>…` segment (`kind_baselines.
    kind_baseline_sha256`, truncated to 12 hex chars) naming the fleet-wide
    baseline its child receives, so a reviewer can confirm which baseline
    applied without diffing `KIND_BASELINES` by eye. `fmt="full"`: every
    entry verbatim, including DR-V/DR-E, plus each dropped entry with its
    refusal reason. A machine-readable form (`--format json`) is not produced
    here — `cmd_plan_grants` builds that directly from `StageGrants.to_dict()`."""
    venue = _venue_for(doc)
    lines: list[str] = []
    for s in doc.stages:
        declared, derived, dropped = _stage_declared_and_derived_grants(s, venue)
        declared_rules = [r.rule for r in declared.allow]
        declared_dirs = [f"{a.path}:{a.mode}" for a in declared.add_dirs]
        dr_v = [r.rule for r in derived.allow if r.provenance == "derived:DR-V"]
        dr_o = [r.rule for r in derived.allow if r.provenance == "derived:DR-O"]
        dr_e = [r.rule for r in derived.allow if r.provenance == "derived:DR-E"]
        dr_r = [f"{a.path}:{a.mode}" for a in derived.add_dirs if a.provenance == "derived:DR-R"]
        note = _no_venue_note(s, venue)
        protected = [_protected_hit_text(rule, entry_id)
                     for rule, entry_id in protected_actions.stage_protected_hits(declared, derived)]
        if fmt == "full":
            lines.append(f"Stage {s.index} ({s.title}):")
            if declared_rules:
                lines.append(f"  declared allow: {', '.join(declared_rules)}")
            if declared_dirs:
                lines.append(f"  declared add_dirs: {', '.join(declared_dirs)}")
            if dr_v:
                lines.append(f"  DR-V (verify_command): {', '.join(dr_v)}")
            if dr_o:
                lines.append(f"  DR-O (output artifacts): {', '.join(dr_o)}")
            if dr_e:
                lines.append(f"  DR-E (in-venue edit): {', '.join(dr_e)}")
            if dr_r:
                lines.append(f"  DR-R (outside-venue read): {', '.join(dr_r)}")
            for d in dropped:
                lines.append(f"  dropped: {d['entry']} ({d['reason']})")
            for text in protected:
                lines.append(f"  {text}")
            if note:
                lines.append(f"  {note}")
            if not (declared_rules or declared_dirs or dr_v or dr_o or dr_e or dr_r):
                lines.append("  (no grants)")
        else:
            parts = [f"Stage {s.index} ({s.title}):"]
            if declared_rules:
                parts.append("declared allow=[" + ", ".join(declared_rules) + "]")
            if declared_dirs:
                parts.append("declared add_dirs=[" + ", ".join(declared_dirs) + "]")
            if dr_o:
                parts.append("DR-O=[" + ", ".join(dr_o) + "]")
            if dr_r:
                parts.append("DR-R=[" + ", ".join(dr_r) + "]")
            if dr_v:
                parts.append(f"DR-V={len(dr_v)} derived")
            if dr_e:
                parts.append(f"DR-E={len(dr_e)} derived")
            if dropped:
                parts.append(f"dropped={len(dropped)}")
            parts.extend(f"[{text}]" for text in protected)
            if note:
                parts.append(f"[{note}]")
            if s.is_spawn():
                kind = s.spawn_kind()
                digest = kind_baselines.kind_baseline_sha256(kind)
                parts.append(f"kind_baseline={kind}:{digest[:12]}…")
            if len(parts) == 1:
                parts.append("(no grants)")
            lines.append(" ".join(parts))
    return "\n".join(lines).rstrip() + "\n"


def cmd_plan_grants(args, *, store=None, runner=None) -> Directive:
    """Render the plan's per-stage grant set on demand — a read-only PROJECTION,
    never written to disk, mirroring `cmd_plan_render`'s pattern exactly. `--format
    json` returns a machine-readable per-stage breakdown (declared/derived/dropped,
    each with provenance) plus the plan's `grants_sha256` — the same digest
    `present-plan`/`approve` bind — for a caller that wants to script against it
    rather than read prose.

    A spawn stage whose child settings the spawn would refuse (see
    `grant_shadow`) makes the projection not-ok — one line per problem,
    worded as the spawn-time refusal — while a mixed-spelling advisory only
    adds an `advisory:` line."""
    from . import grant_shadow

    doc = load_plan(args.plan)
    fmt = getattr(args, "format", "compact") or "compact"
    problems = grant_shadow.plan_grant_shadow_problems(doc)
    advisories = grant_shadow.mixed_spelling_advisories(doc)
    ok = not problems
    if fmt == "json":
        venue = _venue_for(doc)
        stages = {}
        for s in doc.stages:
            declared, derived, dropped = _stage_declared_and_derived_grants(s, venue)
            stages[str(s.index)] = {
                "declared": declared.to_dict(),
                "derived": derived.to_dict(),
                "dropped": dropped,
            }
        data = {
            "grants_sha256": grants_sha256(doc),
            "stages": stages,
            "shadow_problems": problems,
            "advisories": advisories,
        }
        text = json.dumps(data, indent=2, sort_keys=True)
        return Directive(ok, "(render)", "inspect", text, data=data)
    text = render_plan_grants(doc, fmt=fmt)
    tail = problems + [f"advisory: {a}" for a in advisories]
    if tail:
        text = "\n".join([text.rstrip("\n"), "", *tail])
    return Directive(
        ok, "(render)", "inspect", text,
        data={"markdown": text, "shadow_problems": problems, "advisories": advisories},
    )


def plan_render_stage_arg_type(raw: str) -> str:
    """argparse `type=` for `plan-render --stage`: eagerly validates a bare int
    or a comma-separated list of ints (e.g. '3' or '3,5') at PARSE time, so a
    malformed value ('abc') or an empty one ('') fails with a clean argparse
    usage error instead of either a bare ValueError surfacing deep inside
    `_parse_stage_arg`, or (for '', which `_parse_stage_arg` would silently
    treat as an empty index list) a silently-empty render."""
    parts = raw.split(",")
    if not raw or any(not part.strip() for part in parts):
        raise argparse.ArgumentTypeError(
            f"invalid --stage value: {raw!r} (expected an int, or a comma-separated "
            "list of ints, e.g. '3' or '3,5')"
        )
    for part in parts:
        try:
            int(part.strip())
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid --stage value: {raw!r} (expected an int, or a comma-separated "
                "list of ints, e.g. '3' or '3,5')"
            ) from None
    return raw


def _parse_stage_arg(raw) -> "list[int] | None":
    """Parse `--stage` into a sorted list of stage indices, or None when unset.

    Accepts a bare int — direct Namespace construction (tests), or a legacy
    caller that bypasses argparse — or a str, either a single index ('3') or a
    comma-separated list ('3,5'). The int branch is kept even though real CLI
    parsing now always supplies a str (validated by `plan_render_stage_arg_type`
    beforehand): several callers (tests, and any future non-CLI caller) build a
    Namespace directly with a bare int, bypassing argparse entirely."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return [raw]
    return sorted(int(part.strip()) for part in str(raw).split(",") if part.strip())


def _externalized_fields(plan_path: str, stage_indices: list[int] | None) -> list[dict]:
    """Where each externalized field or stage of the plan at `plan_path` lives and which
    bytes the plan pins for it: `{field, path, sha256}` in document order. With
    `stage_indices`, only the references of those stages."""
    index = Path(plan_path)
    entries = []
    for position, path, sha256 in plan_store.visited_refs(index):
        if stage_indices is not None and position[0] not in stage_indices:
            continue
        try:
            shown = os.fspath(path.relative_to(index.resolve().parent))
        except ValueError:
            shown = os.fspath(path)
        entries.append({"field": ".".join(str(part) for part in position), "path": shown, "sha256": sha256})
    return entries


def _externalized_section(entries: list[dict]) -> str:
    lines = ["", "## Externalized fields", ""]
    lines += [f"- `{e['field']}` — `{e['path']}` (sha256 `{e['sha256']}`)" for e in entries]
    return "\n".join(lines) + "\n"


def cmd_plan_render(args, *, store=None, runner=None) -> Directive:
    """Render the declared TOML plan to markdown on demand — a read-only PROJECTION,
    never written to disk by the engine. The markdown is the Directive's detail (the
    `question-list --format md` precedent), with the raw string also under
    data['markdown'] for programmatic capture.

    A hypertext plan renders its resolved text; a trailing "Externalized fields" section
    names the reference path and sha256 of each externalized field (also under
    data['externalized']), so a reader can tell which text lives in which file. A plan
    without references renders exactly as before.

    `--stage N` (schema-independent; reads an already-loaded PlanDoc) renders only
    that stage via `render_stage_brief` instead of the whole plan; `--stage N,M`
    renders each of those stages via `render_stages_md`, byte-identically to
    `render_stage_brief` in the single-stage case."""
    doc = load_plan(args.plan)
    stage_indices = _parse_stage_arg(getattr(args, "stage", None))
    if stage_indices is None:
        md = render_plan_md(doc)
    elif len(stage_indices) == 1:
        try:
            md = render_stage_brief(doc, stage_indices[0])
        except ValueError as exc:
            return Directive(False, "(render)", "error", str(exc), data={})
    else:
        try:
            md = render_stages_md(doc, stage_indices)
        except ValueError as exc:
            return Directive(False, "(render)", "error", str(exc), data={})
    externalized = _externalized_fields(args.plan, stage_indices)
    if externalized:
        md += _externalized_section(externalized)
    data = {"markdown": md}
    if externalized:
        data["externalized"] = externalized
    return Directive(True, "(render)", "inspect", md, data=data)
