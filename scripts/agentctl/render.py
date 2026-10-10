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
import errno
import json
import os
import shutil
import tempfile
from pathlib import Path

from lib import kind_baselines

from . import grants as _grants
from .directive import Directive
from .plan import (
    CONDITION_MARKERS,
    PLAN_DIGEST_MARKER,
    RESTATES_PREFIX,
    REVIEW_MARKER,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    VERDICT_MARKER,
    PAIR_BASE_NODE,
    PlanDoc,
    PlanError,
    _sha256_hex,
    _venue_for,
    consumers,
    grants_sha256,
    interface_empty,
    load_plan,
    parse_pair,
    parse_unit,
    plan_coverage_entries,
    reliance_set,
    review_pairs,
)


def _stage_declared_and_derived_grants(s, venue: str):
    """A stage's declared grants (an empty `StageGrants` when the stage
    authors none) plus its derived grants and dropped-entry list — the one
    place that pairs a stage with `grants.derive_stage_grants`'s
    venue-scoped output, shared by `render_stage_brief`, `render_plan_md`,
    `render_plan_grants`, and `cmd_plan_grants` so no projection can drift
    from another on how a stage's effective grant set is computed."""
    declared = s.grants if getattr(s, "grants", None) else _grants.StageGrants()
    derived, dropped = _grants.derive_stage_grants(s, venue=venue)
    return declared, derived, dropped


def _grants_lines(s, venue: str) -> list[str]:
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
    if not (declared_rules or declared_dirs or derived_rules or derived_dirs):
        return []
    out = ["- **Grants (file-access scope):**"]
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
                lines.append(f"- {label}`{fc.command}` (expected exit {fc.expected_exit})")
        lines.append("")
    return lines


def render_plan_md(doc: PlanDoc) -> str:
    """Pure: a PlanDoc -> a markdown prose view. Renders every stage in order."""
    lines: list[str] = []
    lines.extend(render_meta_md(doc))
    lines.extend(render_order_md(doc))

    for s in doc.stages:
        lines.append(f"## Stage {s.index}: {s.title}")
        lines.append("")
        lines.append(f"- **Executor:** {s.actor.executor}")
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
        lines.append("")

    lines.extend(render_final_checks_md(doc))

    return "\n".join(lines).rstrip() + "\n"


def render_stages_md(doc: PlanDoc, stage_indices) -> str:
    """The plan projected onto a SUBSET of its stages, in index order — the reading an
    advisor is given when only those stages have moved. Each stage carries the plan's
    meta with it (see `render_stage_brief`), so a question about a stage's fit to the
    goal is still answerable from the projection alone."""
    return "\n".join(render_stage_brief(doc, index) for index in sorted(stage_indices))


def render_stage_brief(doc: PlanDoc, stage_index: int) -> str:
    """Pure: a PlanDoc + one stage index -> a markdown brief of JUST that stage.

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
    duration_ms/spawn_count/delivered_head) is deliberately never rendered:
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
            "commands; see the full plan file for those)"
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
    return "\n".join(_stage_field_lines(doc, stage, dependency_blocks=False)).rstrip() + "\n"


def _stage_field_lines(doc: PlanDoc, s, *, dependency_blocks: bool) -> list[str]:
    """The stage section of `render_stage_brief`: heading and every non-empty field.
    `dependency_blocks=False` lists the direct dependencies by index only."""
    lines: list[str] = []
    lines.append(f"## Stage {s.index}: {s.title}")
    lines.append("")
    lines.append(f"- **Executor:** {s.actor.executor}")
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
    lines.extend(_grants_lines(s, _venue_for(doc)))
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
    """A materialized `<root>/<plan_sha256>/` topo tree's on-disk content
    does not match what `topo_node_files`/`topo_pair_view` would produce for
    its plan right now — tampered with after materialization, partially
    deleted, or (should the plan_sha256 partitioning ever be bypassed)
    inherited from a different plan version. Raised by `verify_topo_units`,
    which names the offending path and the remedy (delete the tree and
    re-run `materialize_topo_units`, which re-renders it from the plan
    bytes)."""


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
    return _lines_text(
        render_meta_md(doc) + render_order_md(doc) + render_order_coverage_md(doc)
        + render_final_checks_md(doc)
    )


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


def topo_pair_view_dirname(pair_id: str) -> str:
    """The view directory's bare name for `pair_id` — e.g. `view-3-1` or
    `view-base-7`. Matches the exact `view-<pair>` shape
    `spawn-specialist.py --review-topo` grants `Read(//.../view-<pair>/**)`
    against."""
    return f"view-{pair_id}"


def topo_pair_view(doc: PlanDoc, pair_id: str) -> list[str]:
    """The file names making up `pair_id`'s view directory: exactly one, the
    service's node file — never the base's, whose full text is inlined in
    the bundle. Raises ValueError for a pair the plan does not have."""
    _, service = parse_pair(doc, pair_id)
    return [node_file_name(service)]


def topo_node_files(doc: PlanDoc) -> dict[str, str]:
    """Every node file for the WHOLE plan, keyed by filename: `stage-<n>.md`
    for every stage and `base.md` when the plan declares an order. A pure
    function of the plan bytes — `materialize_topo_units` writes these
    verbatim once per plan sha256, and every pair's view directory is a
    copy of one of them."""
    files = {node_file_name(s.index): node_file_text(doc, s.index) for s in doc.stages}
    if doc.meta.order is not None:
        files[node_file_name(PAIR_BASE_NODE)] = node_file_text(doc, PAIR_BASE_NODE)
    return files


def materialize_topo_units(doc: PlanDoc, plan_sha256: str, root: "Path | str") -> Path:
    """Write the WHOLE per-plan-version topo tree under
    `root/plan_sha256/`: every node file (`topo_node_files`), a
    `MANIFEST.json` mapping each root file and each `view-<pair>/` copy to
    its sha256, and one `view-<pair>/` per reliance edge (`review_pairs`)
    holding a byte-identical COPY — never a hardlink — of exactly that
    pair's service file. A pure function of the plan bytes: idempotent, so a
    later call for the same `plan_sha256` is a re-verification, never a
    re-render.

    Built under a process-unique temporary sibling of `root/plan_sha256/`
    (never a fixed name, so two concurrent materializations can never
    collide), `MANIFEST.json` written inside it before the rename, then
    `os.replace`d into place in one step so a reader can never observe a
    partial tree. `os.replace` onto an existing non-empty destination
    raises `OSError` (ENOTEMPTY/EEXIST) — the signal a concurrent writer
    won the race first; the loser discards its own temp directory and
    re-verifies the winner's tree via `verify_topo_units` instead of
    retrying. Any other `OSError` propagates.

    Returns the materialized `root/plan_sha256/` directory."""
    root = Path(root)
    version_root = root / plan_sha256
    if version_root.is_dir():
        verify_topo_units(version_root, doc)
        return version_root

    root.mkdir(parents=True, exist_ok=True)
    node_files = topo_node_files(doc)
    manifest: dict[str, str] = {}
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{plan_sha256}.{os.getpid()}.", dir=root))
    try:
        for filename, content in node_files.items():
            (tmp_dir / filename).write_text(content, encoding="utf-8")
            manifest[filename] = _sha256_hex(content)
        for pair in review_pairs(doc):
            view_name = topo_pair_view_dirname(pair)
            view_dir = tmp_dir / view_name
            view_dir.mkdir()
            for filename in topo_pair_view(doc, pair):
                content = node_files[filename]
                (view_dir / filename).write_text(content, encoding="utf-8")
                manifest[f"{view_name}/{filename}"] = _sha256_hex(content)
        (tmp_dir / "MANIFEST.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
        )
        try:
            os.replace(tmp_dir, version_root)
        except OSError as exc:
            lost_race = exc.errno in (errno.ENOTEMPTY, errno.EEXIST) and version_root.is_dir()
            if not lost_race:
                raise
            shutil.rmtree(tmp_dir, ignore_errors=True)
            verify_topo_units(version_root, doc)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return version_root


def _verify_topo_file(path: Path, expected_content: str, expected_sha256: str, remedy: str) -> None:
    if not path.is_file():
        raise TopoUnitsCorrupt(f"{path} is missing; {remedy}")
    actual = path.read_text(encoding="utf-8")
    if _sha256_hex(actual) != expected_sha256 or actual != expected_content:
        raise TopoUnitsCorrupt(f"{path} content does not match its MANIFEST.json sha256; {remedy}")


def verify_topo_units(unit_dir: "Path | str", doc: PlanDoc) -> None:
    """Re-check an ALREADY-MATERIALIZED `root/plan_sha256/` tree
    (`unit_dir` — the WHOLE version-root directory, not a single view) for
    `doc` right now: every root file and every `view-<pair>/` copy against
    `MANIFEST.json`, AND the file SET at both levels — the root holds
    exactly the expected node files, `MANIFEST.json`, and one
    `view-<pair>/` per reliance edge, and each `view-<pair>/` holds exactly
    that pair's `topo_pair_view`, nothing extra and nothing missing.

    Raises `TopoUnitsCorrupt` naming the offending path and the remedy
    (delete `unit_dir` and re-run `materialize_topo_units`, which
    re-materializes it) on any mismatch."""
    unit_dir = Path(unit_dir)
    remedy = f"delete {unit_dir} and re-run to re-materialize it"
    if not unit_dir.is_dir():
        raise TopoUnitsCorrupt(f"{unit_dir} is missing; {remedy}")

    node_files = topo_node_files(doc)
    expected_manifest: dict[str, str] = {
        filename: _sha256_hex(content) for filename, content in node_files.items()
    }
    expected_view_files: dict[str, set[str]] = {}
    for pair in review_pairs(doc):
        view_name = topo_pair_view_dirname(pair)
        names = set(topo_pair_view(doc, pair))
        expected_view_files[view_name] = names
        for filename in names:
            expected_manifest[f"{view_name}/{filename}"] = _sha256_hex(node_files[filename])

    manifest_path = unit_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise TopoUnitsCorrupt(f"{manifest_path} is missing; {remedy}")
    try:
        actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TopoUnitsCorrupt(f"{manifest_path} is not valid JSON ({exc}); {remedy}") from exc
    if actual_manifest != expected_manifest:
        raise TopoUnitsCorrupt(f"{manifest_path} does not match the expected manifest; {remedy}")

    root_files = {p.name for p in unit_dir.iterdir() if p.is_file()}
    expected_root_files = set(node_files.keys()) | {"MANIFEST.json"}
    if root_files != expected_root_files:
        extra = [str(unit_dir / n) for n in sorted(root_files - expected_root_files)]
        missing = [str(unit_dir / n) for n in sorted(expected_root_files - root_files)]
        raise TopoUnitsCorrupt(f"{unit_dir} file set mismatch — extra: {extra}, missing: {missing}; {remedy}")

    root_dirs = {p.name for p in unit_dir.iterdir() if p.is_dir()}
    expected_root_dirs = set(expected_view_files.keys())
    if root_dirs != expected_root_dirs:
        extra = [str(unit_dir / n) for n in sorted(root_dirs - expected_root_dirs)]
        missing = [str(unit_dir / n) for n in sorted(expected_root_dirs - root_dirs)]
        raise TopoUnitsCorrupt(
            f"{unit_dir} view directory set mismatch — extra: {extra}, missing: {missing}; {remedy}"
        )

    for filename, content in node_files.items():
        _verify_topo_file(unit_dir / filename, content, expected_manifest[filename], remedy)

    for view_name, names in expected_view_files.items():
        view_dir = unit_dir / view_name
        actual_names = {p.name for p in view_dir.iterdir() if p.is_file()}
        if actual_names != names:
            extra = [str(view_dir / n) for n in sorted(actual_names - names)]
            missing = [str(view_dir / n) for n in sorted(names - actual_names)]
            raise TopoUnitsCorrupt(f"{view_dir} file set mismatch — extra: {extra}, missing: {missing}; {remedy}")
        for filename in names:
            _verify_topo_file(
                view_dir / filename, node_files[filename], expected_manifest[f"{view_name}/{filename}"], remedy
            )


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


def _base_edge_section(doc: PlanDoc, service: int) -> str:
    """The base side of a `base-<s>` pair: only the requirements the edge
    supplies — id, text, derivation — and never the goal, done criterion, final
    checks, other requirements or the rest of the order."""
    lines = ["- **Requirements named by this edge:**"]
    for r in _base_edge_requirements(doc, service):
        label = f"**{r.id}**" if r.id else "*(no id)*"
        lines.append(f"  - {label}: {r.text}")
        lines.append(f"    - **Derivation:** {r.derivation or '*(none)*'}")
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
            f"declared product delivers what the requirement asks",
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
            f"only as `{RESTATES_PREFIX}<concern-id>`; do not restate a settled concern as new."
        )
    lines.append(
        f"The {REVIEW_MARKER} block is the last thing in your reply, with no other "
        f"marker (COMPLETED:, REPLAN:, etc.) after it."
    )
    lines.append("")
    return lines


def render_pair_review_bundle(
    doc: PlanDoc, pair_id: str, *, plan_sha256: str, view_dir: "Path | str",
    history: "dict | None" = None,
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
    if node == PAIR_BASE_NODE:
        return [
            "the order and the meta are internally consistent — customer, functional "
            "place, requirements and the coverage map do not contradict each other",
            "every requirement follows from the difficulty and the functional place — "
            "none is arbitrary, none is missing a derivation",
            "every requirement has a coverage entry or a final check that decides it — "
            "none is left uncovered",
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


def cmd_plan_render(args, *, store=None, runner=None) -> Directive:
    """Render the declared TOML plan to markdown on demand — a read-only PROJECTION,
    never written to disk by the engine. The markdown is the Directive's detail (the
    `question-list --format md` precedent), with the raw string also under
    data['markdown'] for programmatic capture.

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
    return Directive(True, "(render)", "inspect", md, data={"markdown": md})
