"""Resolve a plan's per-stage effective grants ([stage.grants] declared +
grants.derive_stage_grants derived, plus [[stage.effects]] declarations, plus
a spawn stage's specialist and a landed stage's vcs_ref) into typed Resources
or content-bound unresolved identities.

Difficulty removed: three call sites — `cmd_plan_resources` (a new read-only
CLI surface), `cmd_approve`'s order-approvals ledger stamping, and
`cmd_dispatch`'s Resource:-line self-grant cross-check — each need "what does
this plan/stage touch" in typed-Resource form. Computing it independently at
each site would drift the moment one of them adds a wrinkle (a new wildcard-
tail case, a new resolver) the others don't see; this module is the one
computation all three call.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import grants as _grants
from . import resources as _resources
from .plan import PlanDoc, _effective_grants_for_stage, _venue_for
from .state import CheckKind, Stage
from .tool_contracts import Resolution, resolve_command


def resolve_rule_grant(rule: str, venue: str) -> Resolution:
    """Resolve one `Tool(arg)` rule string to the resource(s) it covers, or
    leave it unresolved with a `reason_class` naming why.

    A Bash rule with a `:*` wildcard tail is unresolved with
    `reason_class="wildcard-tail"`: the argv it admits at materialization
    time is unbounded, so no bounded resource set can be attributed to the
    rule TEXT alone. (A write-capable program can never carry a wildcard
    tail here at all — `grants.validate_rule` refuses that shape outright
    before a rule of that form ever reaches a StageGrants — so this case in
    practice only arises for a readonly or interpreter/script invocation,
    e.g. `Bash(python3 scripts/foo.py:*)`.) An ACTUAL dispatched call
    resolves its own, concrete command line via `tool_contracts.
    resolve_command` instead of this function — see cli.py's dispatch/
    self-grant call sites."""
    parsed = _grants.rule_program_and_arg(rule)
    if parsed is None:
        return Resolution(
            "unresolved", reason_class="unparseable-rule",
            reason=f"rule {rule!r} does not parse as Tool(arg)",
            identity=("unparseable-rule", rule),
        )
    tool, arg = parsed
    if tool == "Bash":
        if arg.endswith(":*"):
            return Resolution(
                "unresolved", reason_class="wildcard-tail",
                reason=(
                    f"rule {rule!r} has a `:*` wildcard tail -- the argv it "
                    "admits at materialization time is not bounded, so the "
                    "resource(s) it could touch cannot be resolved from the "
                    "declared rule text alone"
                ),
                identity=("wildcard-tail", rule),
            )
        return resolve_command(arg, venue)
    if tool not in ("Edit", "Write", "Read", "NotebookEdit"):
        return Resolution(
            "unresolved", reason_class="unknown-tool",
            reason=f"rule {rule!r} names an unrecognized tool {tool!r}",
            identity=("unknown-tool", rule),
        )
    path = _grants.rule_file_path(arg)
    mode = "read" if tool == "Read" else "write"
    return Resolution("resolved", resources=[_resources.FileResource(path, mode)])


def resolve_add_dir_grant(path: str, mode: str) -> Resolution:
    return Resolution("resolved", resources=[_resources.FileResource(path, mode)])


def _stage_spawn_resources(stage: Stage) -> list:
    if not stage.is_spawn():
        return []
    return [_resources.SpecialistResource(stage.spawn_kind())]


def _stage_landed_resources(stage: Stage) -> list:
    crit = stage.criterion
    if crit.verify_kind != CheckKind.LANDED.value or crit.landed is None:
        return []
    return [_resources.VcsRefResource(crit.landed.remote, crit.landed.target, "land")]


@dataclass
class StageResources:
    stage_index: int
    resources: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    stage_effects: list = field(default_factory=list)


def compute_stage_resources(stage: Stage, venue: str) -> StageResources:
    """Compute the union of a stage's typed resources: effective (declared +
    derived) grants, its own spawn/landed shape, and its declared
    [[stage.effects]] (echoed through unresolved -- resolving a
    StageEffectDeclaration against a concrete call is `cli.py`'s job, since
    it needs the actual argv, not just the declaration)."""
    effective = _effective_grants_for_stage(stage, venue)
    out_resources: list = []
    out_unresolved: list = []
    for rule_grant in effective.allow:
        res = resolve_rule_grant(rule_grant.rule, venue)
        if res.status == "resolved":
            out_resources.extend(res.resources)
        else:
            out_unresolved.append(res.identity or (res.reason_class, rule_grant.rule))
    for add_dir in effective.add_dirs:
        res = resolve_add_dir_grant(add_dir.path, add_dir.mode)
        out_resources.extend(res.resources)
    out_resources.extend(_stage_spawn_resources(stage))
    out_resources.extend(_stage_landed_resources(stage))
    return StageResources(
        stage_index=stage.index,
        resources=out_resources,
        unresolved=out_unresolved,
        stage_effects=list(getattr(stage, "effects", []) or []),
    )


def compute_plan_resources(doc: PlanDoc, *, venue: str | None = None) -> list[StageResources]:
    v = venue if venue is not None else _venue_for(doc)
    return [compute_stage_resources(s, v) for s in doc.stages]
