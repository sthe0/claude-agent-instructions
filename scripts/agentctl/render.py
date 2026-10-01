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
import hashlib
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
    REVIEW_MARKER,
    VERDICT_MARKER,
    PlanDoc,
    PlanError,
    _venue_for,
    consumers,
    first_hop,
    grants_sha256,
    interface_empty,
    load_plan,
    reliance_closure,
    reliance_set,
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
                    f"delivered commit must be contained in `{ls.target}` and "
                    f"`{ls.remote}/{ls.target}`"
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
                f"commit must be contained in `{ls.target}` and "
                f"`{ls.remote}/{ls.target}`"
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

    s = stage
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
            f"commit must be contained in `{ls.target}` and "
            f"`{ls.remote}/{ls.target}`"
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
    if s.depends_on:
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


# --- Topological review: one plan "unit" (a stage, or the virtual order
# node) reviewed at a time, from a small starting prompt whose first-hop
# neighbours are reachable by exactly one `Read` in a per-unit view
# directory, rather than inlined. See `render_topo_review_bundle`. -------

class TopoUnitsCorrupt(Exception):
    """A materialized `<root>/<plan_sha256>/` topo-unit tree's on-disk
    content does not match what `topo_unit_files`/`topo_unit_view` would
    produce for its plan right now — tampered with after materialization,
    partially deleted, or (should the plan_sha256 partitioning ever be
    bypassed) inherited from a different plan version. Raised by
    `verify_topo_units`, which names the offending path and the remedy
    (delete the tree and re-run `materialize_topo_units`, which
    re-renders it from the plan bytes)."""


def _unit_label(unit: "int | str") -> str:
    """Canonical string label for a topo unit selector: `"order"` for the
    order node, or the stage's decimal index for anything else."""
    if isinstance(unit, str) and unit == "order":
        return "order"
    return str(int(unit))


def _all_units(doc: PlanDoc) -> list["int | str"]:
    """Every topo unit `doc` has: each stage's index, plus the virtual
    `"order"` node when the plan declares a `[meta.order]` block."""
    units: list["int | str"] = [s.index for s in doc.stages]
    if doc.meta.order is not None:
        units.append("order")
    return units


def _order_brief(doc: PlanDoc) -> str:
    """The order node's own full brief: meta + order + order-coverage +
    final-checks, concatenated — the exact `order.md` content
    `topo_unit_files` materializes, so the bundle's own-brief section and
    the materialized file can never drift apart."""
    lines = (
        render_meta_md(doc)
        + render_order_md(doc)
        + render_order_coverage_md(doc)
        + render_final_checks_md(doc)
    )
    return "\n".join(lines).rstrip() + "\n"


def _unit_own_brief(doc: PlanDoc, unit: "int | str") -> str:
    """The unit's own FULL brief: `render_stage_brief` for a stage, or
    `_order_brief` for the order node."""
    if _unit_label(unit) == "order":
        return _order_brief(doc)
    return render_stage_brief(doc, int(unit))


def _unit_first_hop(doc: PlanDoc, unit: "int | str") -> set[int]:
    """The first-hop STAGE neighbours of `unit`: for a stage, `first_hop`
    (its reliance set union its consumers); for the order node, every
    stage in the plan — the order declares no `depends_on`/`supplies` of
    its own to read a narrower neighbour set from, and a reviewer checking
    the order's coverage needs every stage's interface anyway."""
    if _unit_label(unit) == "order":
        return {s.index for s in doc.stages}
    return first_hop(doc, int(unit))


def _unit_relies_on_and_consumers(doc: PlanDoc, unit: "int | str") -> tuple[set[int], set[int]]:
    """`(relies_on, consumed_by)` for `unit` — the two tagged halves of its
    first hop (rendered `supplier` / `customer` respectively). The order
    node relies on every stage's product (it delivers no product of its
    own for a stage to consume), so every stage is reported as
    `relies_on`, never `consumed_by`."""
    if _unit_label(unit) == "order":
        return {s.index for s in doc.stages}, set()
    n = int(unit)
    return reliance_set(doc, n), consumers(doc, n)


def render_stage_interface(doc: PlanDoc, stage_index: int, *, contract: bool = False) -> str:
    """The interface-only projection of one stage: title, expected result
    image, criterion type, done criterion, and output_artifacts — never
    method/means/procedure (the "how", which a consumer relying on this
    stage's result never needs — see conditions 3/4 in
    `render_topo_review_bundle`).

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


def topo_unit_view_dirname(unit: "int | str") -> str:
    """The view directory's bare name for `unit` — e.g. `view-3` or
    `view-order`. Matches the exact `view-<unit>` shape
    `spawn-specialist.py --review-topo` grants `Read(//.../view-<unit>/**)`
    against."""
    return f"view-{_unit_label(unit)}"


def topo_unit_view(doc: PlanDoc, unit: "int | str") -> list[str]:
    """The sorted `stage-<a>.md` filenames making up `unit`'s view
    directory: one per member of its first hop (`_unit_first_hop`) — never
    the unit's own file, never `order.md` for a stage unit, and never a
    transitive-only member. The order node's first hop is every stage, so
    its view holds every stage file."""
    return [f"stage-{n}.md" for n in sorted(_unit_first_hop(doc, unit))]


def topo_unit_files(doc: PlanDoc) -> dict[str, str]:
    """Every topo unit file for the WHOLE plan, keyed by filename:
    `stage-<n>.md` (`render_stage_brief`) for every stage, plus `order.md`
    (`_order_brief`) when the plan declares an order block. A pure
    function of the plan bytes — `materialize_topo_units` writes these
    verbatim once per plan sha256, and every unit's view directory
    (`topo_unit_view`) is a copy of a subset of them."""
    files = {f"stage-{s.index}.md": render_stage_brief(doc, s.index) for s in doc.stages}
    if doc.meta.order is not None:
        files["order.md"] = _order_brief(doc)
    return files


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def materialize_topo_units(doc: PlanDoc, plan_sha256: str, root: "Path | str") -> Path:
    """Write the WHOLE per-plan-version topo unit tree under
    `root/plan_sha256/`: every unit file (`topo_unit_files`), a
    `MANIFEST.json` mapping each root file and each `view-<unit>/` copy to
    its sha256, and one `view-<unit>/` per unit (every stage, plus the
    order node when the plan declares one) holding byte-identical COPIES —
    never hardlinks — of exactly that unit's first-hop files. A pure
    function of the plan bytes: idempotent, so a later call for the same
    `plan_sha256` is a re-verification, never a re-render.

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
    units = _all_units(doc)
    unit_files = topo_unit_files(doc)
    manifest: dict[str, str] = {}
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{plan_sha256}.{os.getpid()}.", dir=root))
    try:
        for filename, content in unit_files.items():
            (tmp_dir / filename).write_text(content, encoding="utf-8")
            manifest[filename] = _sha256_text(content)
        for unit in units:
            view_name = topo_unit_view_dirname(unit)
            view_dir = tmp_dir / view_name
            view_dir.mkdir()
            for filename in topo_unit_view(doc, unit):
                content = unit_files[filename]
                (view_dir / filename).write_text(content, encoding="utf-8")
                manifest[f"{view_name}/{filename}"] = _sha256_text(content)
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
    if _sha256_text(actual) != expected_sha256 or actual != expected_content:
        raise TopoUnitsCorrupt(f"{path} content does not match its MANIFEST.json sha256; {remedy}")


def verify_topo_units(unit_dir: "Path | str", doc: PlanDoc) -> None:
    """Re-check an ALREADY-MATERIALIZED `root/plan_sha256/` tree
    (`unit_dir` — the WHOLE version-root directory, not a single view) for
    `doc` right now: every root file and every `view-<unit>/` copy against
    `MANIFEST.json`, AND the file SET at both levels — the root holds
    exactly the expected unit files, `MANIFEST.json`, and one
    `view-<unit>/` per unit, and each `view-<unit>/` holds exactly that
    unit's `topo_unit_view`, nothing extra and nothing missing.

    Raises `TopoUnitsCorrupt` naming the offending path and the remedy
    (delete `unit_dir` and re-run `materialize_topo_units`, which
    re-materializes it) on any mismatch."""
    unit_dir = Path(unit_dir)
    remedy = f"delete {unit_dir} and re-run to re-materialize it"
    if not unit_dir.is_dir():
        raise TopoUnitsCorrupt(f"{unit_dir} is missing; {remedy}")

    unit_files = topo_unit_files(doc)
    expected_manifest: dict[str, str] = {
        filename: _sha256_text(content) for filename, content in unit_files.items()
    }
    expected_view_files: dict[str, set[str]] = {}
    for unit in _all_units(doc):
        view_name = topo_unit_view_dirname(unit)
        names = set(topo_unit_view(doc, unit))
        expected_view_files[view_name] = names
        for filename in names:
            expected_manifest[f"{view_name}/{filename}"] = _sha256_text(unit_files[filename])

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
    expected_root_files = set(unit_files.keys()) | {"MANIFEST.json"}
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

    for filename, content in unit_files.items():
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
                view_dir / filename, unit_files[filename], expected_manifest[f"{view_name}/{filename}"], remedy
            )


_CONDITION_TEXT = {
    CONDITION_MARKERS[0]: "the unit is organized in a non-arbitrary way",
    CONDITION_MARKERS[1]: "the unit is a genuine derivation from the order",
    CONDITION_MARKERS[2]: "this unit delivers what it declares — its supplier postcondition holds",
    CONDITION_MARKERS[3]: (
        "what this unit relies on is declared, completely, precisely, and jointly "
        "with its suppliers — its consumer precondition holds"
    ),
}

_ENGINE_ORDERED = "engine-ordered"
_DECLARED_ONLY_ORDERING = "declared-only (supplies-wins collapse; not dispatch-ordered)"
_ORDER_UNIT_ORDERING = "none"
_ORDER_UNIT_EDGE = "order-node reliance (no declared supply edge)"
_RAW_DEPENDS_ON_ONLY_EDGE = "depends_on-only"
_WHOLE_PRODUCT = "whole product"


def _engine_dispatch_closure(doc: PlanDoc, n: int) -> frozenset[int]:
    """The transitive closure of the DERIVED, supplies-only `Stage.depends_on`
    graph upstream from stage `n`, excluding `n` itself -- the engine's OWN
    dispatch-ordering view. Narrower than `plan.reliance_closure`, which
    closes over the raw `depends_on ∪ supplies.on` union `_build_supplies`'s
    supplies-wins collapse may have silently widened away from what the
    engine actually dispatches on. Used only to compute a first-hop edge's
    ORDERING tag."""
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
    union `reliance_set` reads. This is the exact drift the supplies-wins
    collapse can introduce: a plan author's TOML `depends_on` edge that
    `_build_supplies` then drops from what the engine actually dispatches
    on, once that stage also declares `[[stage.supplies]]`."""
    if supplier in _engine_dispatch_closure(doc, consumer):
        return _ENGINE_ORDERED
    return _DECLARED_ONLY_ORDERING


def _supply_edge_label(doc: PlanDoc, consumer: int, supplier: int) -> str:
    """Every supply edge from `supplier` to `consumer`, as declared on
    `consumer`'s own `[[stage.supplies]] on = supplier` entries, in
    declaration order and joined with `; ` — each its element (`whole
    product` when the supply names none) and artifact, when named. A stage
    declaring no supplies gets one whole-product supply per `depends_on`
    edge at parse time, so `depends_on-only` marks exactly a raw TOML
    `depends_on` edge the supplies-wins collapse dropped."""
    consumer_stage = next(s for s in doc.stages if s.index == consumer)
    edges = []
    for supply in consumer_stage.supplies:
        if supply.on != supplier:
            continue
        element = f"`{supply.element}`" if supply.element is not None else _WHOLE_PRODUCT
        edge = f"supplies {element}"
        if supply.artifact:
            edge += f" (artifact: `{supply.artifact}`)"
        edges.append(edge)
    return "; ".join(edges) if edges else _RAW_DEPENDS_ON_ONLY_EDGE


def render_topo_review_bundle(doc: PlanDoc, unit: "int | str", *, plan_sha256: str, view_dir: "Path | str") -> str:
    """The `--review-topo` starting prompt for `unit`: order context + own
    full brief + a tagged first-hop neighbour list + first-hop neighbour
    INTERFACES (short, `render_stage_interface`) + transitive-only
    reliance interfaces + `view_dir` and its file list (where each
    first-hop neighbour's FULL brief is reachable via exactly one `Read`)
    + the reconciliation procedure + the plan digest line + the review
    protocol/checklist.

    Every marker string in the protocol section is sourced from
    `plan.REVIEW_MARKER` / `plan.VERDICT_MARKER` / `plan.PLAN_DIGEST_MARKER`
    / `plan.CONDITION_MARKERS` — never duplicated here as a string literal,
    so a reviewer's reply and this bundle's own checklist can never drift
    onto different marker spellings.

    Raises ValueError for a unit the plan does not have (an unknown stage
    index, or `order` on a plan without an order block), and PlanError
    for a dangling or cyclic raw reliance graph."""
    label = _unit_label(unit)
    if label == "order":
        if doc.meta.order is None:
            raise ValueError(f"plan {doc.meta.task_id!r} declares no order block to review")
    elif not any(s.index == int(unit) for s in doc.stages):
        raise ValueError(f"no stage with index {unit} in plan {doc.meta.task_id!r}")
    relies_on, consumed_by = _unit_relies_on_and_consumers(doc, unit)
    first_hop_all = relies_on | consumed_by

    if label == "order":
        # The order node's view already holds every stage, so nothing is
        # transitive-only; the closures still run so a dangling or cyclic
        # raw graph fails this unit exactly as it fails any stage unit.
        for s in doc.stages:
            reliance_closure(doc, s.index)
        transitive: frozenset[int] = frozenset()
    else:
        transitive = reliance_closure(doc, int(unit)) - relies_on
    titles = {s.index: s.title for s in doc.stages}

    lines: list[str] = [f"# Topological review unit: {label}", ""]
    lines.extend(render_order_md(doc))
    lines.append("## Own brief")
    lines.append("")
    lines.append(_unit_own_brief(doc, unit))

    lines.append("## First-hop neighbours")
    lines.append("")
    if first_hop_all:
        for m in sorted(relies_on):
            if label == "order":
                edge, tag = _ORDER_UNIT_EDGE, _ORDER_UNIT_ORDERING
            else:
                edge, tag = _supply_edge_label(doc, int(unit), m), _ordering_tag(doc, int(unit), m)
            lines.append(f"- stage {m} ({titles[m]}): supplier — {edge} — {tag}")
        for m in sorted(consumed_by):
            edge = _supply_edge_label(doc, m, int(unit))
            tag = _ordering_tag(doc, m, int(unit))
            lines.append(f"- stage {m} ({titles[m]}): customer — {edge} — {tag}")
    else:
        lines.append("- *(none — this unit has no declared reliance edges)*")
    lines.append("")

    lines.append("## Neighbour interfaces")
    lines.append("")
    if first_hop_all:
        for m in sorted(first_hop_all):
            lines.append(render_stage_interface(doc, m, contract=True))
    else:
        lines.append("*(none)*")
        lines.append("")

    lines.append("## Transitive reliances (interface only, not in the view directory)")
    lines.append("")
    if transitive:
        for m in sorted(transitive):
            lines.append(render_stage_interface(doc, m, contract=True))
    else:
        lines.append("*(none beyond the first hop)*")
        lines.append("")

    view_files = topo_unit_view(doc, unit)
    lines.append("## Full neighbour briefs")
    lines.append("")
    lines.append(
        f"Each first-hop neighbour's full brief (method and procedure included) is "
        f"one `Read` away in the view directory `{view_dir}`, which holds exactly:"
    )
    if view_files:
        for filename in view_files:
            lines.append(f"- `{Path(view_dir) / filename}`")
    else:
        lines.append("- *(no files — this unit has no first-hop neighbours)*")
    lines.append("")

    concern_markers = ", ".join(f"`{marker}`" for marker in CONDITION_MARKERS)
    gap_marker = CONDITION_MARKERS[3]
    lines.append("## Reconciliation procedure")
    lines.append("")
    lines.append("1. Take the first-hop pairs listed above one at a time, in the listed order.")
    lines.append(
        f"2. For each pair, decide {CONDITION_MARKERS[2].rstrip(':')} (the supplier "
        f"delivers its declared product) and {gap_marker.rstrip(':')} (the customer's "
        f"reliance on it is covered) from the neighbour's interface above first."
    )
    lines.append(
        "3. Only when the interface cannot decide it, `Read` that neighbour's file "
        "from the view directory. This unit's own brief is inlined above in full "
        "and is not in the view directory."
    )
    lines.append(
        f"4. Report any gap a pair reveals as a condition-4 concern: one `{gap_marker}` line."
    )
    lines.append("5. Check the remaining conditions below for this unit, then reply per the protocol.")
    lines.append("")

    lines.append(f"{PLAN_DIGEST_MARKER} {plan_sha256}")
    lines.append("")

    lines.append("## Review protocol")
    lines.append("")
    lines.append("Reply with these lines, in this order:")
    lines.append(f"- `{REVIEW_MARKER}` on a line of its own;")
    lines.append(f"- `{VERDICT_MARKER} <pass|revise>`;")
    lines.append(
        f"- `{PLAN_DIGEST_MARKER} <sha256>` — echo the `{PLAN_DIGEST_MARKER}` line above "
        f"verbatim; do not compute it;"
    )
    lines.append(
        f"- one concern per line, each prefixed by the marker of the condition it "
        f"concerns ({concern_markers}); a condition-4 gap is a `{gap_marker}` line."
    )
    lines.append("")
    lines.append("Conditions:")
    if label == "order":
        joint = (
            "evaluated jointly over all stages against each Order.coverage "
            "requirement, not per-stage"
        )
        lines.append(f"- `{CONDITION_MARKERS[0]}` {_CONDITION_TEXT[CONDITION_MARKERS[0]]}")
        lines.append(f"- `{CONDITION_MARKERS[1]}` {_CONDITION_TEXT[CONDITION_MARKERS[1]]} — {joint}")
        lines.append(
            f"- `{CONDITION_MARKERS[2]}` not applicable — the order node delivers no product "
            f"of its own for a consumer to rely on"
        )
        lines.append(f"- `{CONDITION_MARKERS[3]}` {_CONDITION_TEXT[CONDITION_MARKERS[3]]} — {joint}")
    else:
        for marker in CONDITION_MARKERS:
            lines.append(f"- `{marker}` {_CONDITION_TEXT[marker]}")
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
    rather than read prose."""
    doc = load_plan(args.plan)
    fmt = getattr(args, "format", "compact") or "compact"
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
        data = {"grants_sha256": grants_sha256(doc), "stages": stages}
        text = json.dumps(data, indent=2, sort_keys=True)
        return Directive(True, "(render)", "inspect", text, data=data)
    text = render_plan_grants(doc, fmt=fmt)
    return Directive(True, "(render)", "inspect", text, data={"markdown": text})


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
