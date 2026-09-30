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

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import grants as _grants
from . import resources as _resources
from .directive import Directive
from .plan import PlanDoc, _effective_grants_for_stage, _venue_for, load_plan
from .state import CheckKind, CriterionType, Stage
from .tool_contracts import Resolution, load_contract_table, resolve_command


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
            "unresolved", reason_class="contract-unresolved",
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
            "unresolved", reason_class="unknown-program",
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


#: Elements whose text the engine itself executes verbatim; an unresolved command
#: sourced from one of them is a fixed text a reviewer can vouch for once.
ENGINE_EXECUTED_ORIGINS = frozenset({"verify_command", "negative_control", "final_check"})


@dataclass
class BoundaryView:
    """A plan's typed resources and unresolved command identities, each unresolved one
    tagged with the plan element it came from, for `gates.autonomy_boundary`."""
    order_sha256: str
    resources: list = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


def _unresolved_item(identity, origin: str, source: str) -> dict:
    return {"identity": list(identity), "origin": origin, "source": source}


def compute_boundary_view(doc: PlanDoc, order_sha256: str, *, venue: str | None = None) -> BoundaryView:
    """`compute_plan_resources`' resources plus the engine-executed fixed texts
    (negative_control, final_check commands) resolved directly, with every unresolved
    identity carrying its origin: a derived verify-command rule is judged by the
    segment it was derived from, not by its `:*` spelling."""
    v = venue if venue is not None else _venue_for(doc)
    view = BoundaryView(order_sha256=order_sha256)

    def take(res: Resolution, origin: str, source: str, fallback: tuple) -> None:
        if res.status == "resolved":
            view.resources.extend(res.resources)
        else:
            view.unresolved.append(_unresolved_item(res.identity or fallback, origin, source))

    for stage in doc.stages:
        effective = _effective_grants_for_stage(stage, v)
        for rule_grant in effective.allow:
            provenance = rule_grant.provenance
            if provenance == "derived:DR-V":
                parsed = _grants.rule_program_and_arg(rule_grant.rule)
                segment = parsed[1][:-2] if parsed and parsed[1].endswith(":*") else rule_grant.rule
                take(resolve_command(segment, v), "verify_command", segment, ("derived-verify", segment))
                continue
            origin = "declared" if provenance == "declared" else (
                "output_artifact" if provenance == "derived:DR-O" else provenance
            )
            take(resolve_rule_grant(rule_grant.rule, v), origin, rule_grant.rule,
                 ("unresolved", rule_grant.rule))
        for add_dir in effective.add_dirs:
            view.resources.extend(resolve_add_dir_grant(add_dir.path, add_dir.mode).resources)
        view.resources.extend(_stage_spawn_resources(stage))
        view.resources.extend(_stage_landed_resources(stage))
        control = stage.criterion.negative_control
        if control:
            take(resolve_command(control, v), "negative_control", control, ("negative-control", control))
    for check in doc.meta.final_check:
        if check.command:
            take(resolve_command(check.command, v), "final_check", check.command,
                 ("final-check", check.command))
    return view


# --- `agentctl plan-resources` CLI: plan mode + corpus mode ----------------
#
# Plan mode (`--plan P`) renders the SAME per-rule detail `compute_stage_resources`
# computes, but keeps each rule's own Resolution intact (status, reason_class,
# resources, identity) rather than collapsing an unresolved one into the lossy
# `identity or (reason_class, rule)` tuple that aggregation uses for coverage
# checks -- a CLI inspector needs the full picture, a coverage check only needs
# a hashable key.
#
# Corpus mode (`--corpus DIR...` / `--commands-file F`) resolves literal command
# TEXTS directly through `tool_contracts.resolve_command`, the same function
# dispatch/resolve-permission use for a plan's own Bash rules -- so a script's
# effects-registry entry (e.g. land-branch.py) resolves identically whether the
# command reached this module via a plan's declared grant or via a corpus scan.


def _resource_to_dict(res: _resources.Resource) -> dict:
    return dataclasses.asdict(res)


def _resolution_to_dict(res: Resolution, *, label: str, label_key: str = "rule") -> dict:
    return {
        label_key: label,
        "status": res.status,
        "resources": [_resource_to_dict(r) for r in res.resources],
        "reason_class": res.reason_class,
        "reason": res.reason,
        "identity": list(res.identity) if res.identity is not None else None,
    }


def _plan_resources_json(doc: PlanDoc, venue: str) -> dict:
    stages = {}
    for s in doc.stages:
        effective = _effective_grants_for_stage(s, venue)
        rules = [
            _resolution_to_dict(resolve_rule_grant(rg.rule, venue), label=rg.rule)
            for rg in effective.allow
        ]
        add_dirs = [
            _resolution_to_dict(
                resolve_add_dir_grant(ad.path, ad.mode), label=ad.path, label_key="path"
            )
            for ad in effective.add_dirs
        ]
        stages[str(s.index)] = {
            "rules": rules,
            "add_dirs": add_dirs,
            "spawn": [_resource_to_dict(r) for r in _stage_spawn_resources(s)],
            "landed": [_resource_to_dict(r) for r in _stage_landed_resources(s)],
            "stage_effects": [dataclasses.asdict(e) for e in (getattr(s, "effects", []) or [])],
        }
    return {"stages": stages}


def _render_plan_resources_text(data: dict) -> str:
    lines = ["# Plan resources"]
    for idx, stage in sorted(data["stages"].items(), key=lambda kv: int(kv[0])):
        lines.append(f"\n## Stage {idx}")
        for r in stage["rules"]:
            if r["status"] == "resolved":
                kinds = ", ".join(res["kind"] for res in r["resources"]) or "(none)"
                lines.append(f"- resolved: `{r['rule']}` -> {kinds}")
            else:
                lines.append(f"- unresolved ({r['reason_class']}): `{r['rule']}`")
        for r in stage["spawn"]:
            lines.append(f"- spawn -> specialist:{r['role']}")
        for r in stage["landed"]:
            lines.append(f"- landed -> vcs_ref:{r['remote']}/{r['ref']}/{r['op']}")
    return "\n".join(lines) + "\n"


def _iter_commands_file(path: str) -> list[str]:
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _iter_corpus_commands(corpus_dirs: list[str]) -> list[tuple[str, str, str]]:
    """Best-effort: walk each corpus dir for `*.toml` plan files and yield
    (source-path, venue, command-text) for every LITERAL command text the
    plan actually runs: each stage's `verify_command` and `negative_control`,
    the plan's own `[[final_check]]` commands, and any genuinely-declared
    literal (non-`:*`) Bash rule under `[stage.grants]`. A file that fails to
    load as a plan is skipped outright -- the corpus may hold fixtures that
    are deliberately not full plans, and this is a read-only inspection, not
    a validator.

    Segments come from `grants._verify_command_segments`, the SAME top-level
    splitter DR-V uses to propose a permission rule -- but here WITHOUT that
    function's `:*` wildcard suffix, so the literal command text is what gets
    resolved rather than a permission-rule shape.

    An `acceptance_review`-typed stage's `verify_command`/`negative_control`
    fields are skipped entirely: that criterion type names no shell check at
    all (the field may legitimately hold a human-readable observation to
    confirm on review, e.g. `"user observation: ..."`), so feeding it through
    a shell-command splitter manufactures a bogus, non-program first token
    with no real command behind it -- a corpus-scan-only false positive, not
    a gap DR-V's own derivation needs to close (DR-V's proposed permission
    rule for such a stage is inert either way: nothing will ever invoke a
    program literally named after the observation's first word)."""
    out: list[tuple[str, str, str]] = []
    for root in corpus_dirs:
        for p in sorted(Path(root).rglob("*.toml")):
            try:
                doc = load_plan(str(p), strict=False)
            except Exception:
                continue
            venue = _venue_for(doc) or str(Path.cwd())
            for s in doc.stages:
                # plan.py stores this field as a raw, unvalidated string (plan.py
                # crit_type = str(s.get("criterion_type", ...))), and the corpus
                # itself carries both "acceptance_review" and "acceptance-review"
                # spellings -- normalize before compare or the hyphenated form
                # silently falls through this skip.
                if s.criterion.criterion_type.replace("-", "_") == CriterionType.ACCEPTANCE_REVIEW.value:
                    continue
                for text in (s.criterion.verify_command, s.criterion.negative_control):
                    if text:
                        for seg in _grants._verify_command_segments(text):
                            out.append((str(p), venue, seg))
                declared = s.grants if getattr(s, "grants", None) else None
                if declared:
                    for rg in declared.allow:
                        parsed = _grants.rule_program_and_arg(rg.rule)
                        if parsed and parsed[0] == "Bash" and not parsed[1].endswith(":*"):
                            out.append((str(p), venue, parsed[1]))
            for fc in doc.meta.final_check:
                if fc.command:
                    for seg in _grants._verify_command_segments(fc.command):
                        out.append((str(p), venue, seg))
    return out


def _cmd_plan_resources_corpus(args) -> Directive:
    venue = str(Path.cwd())
    contract_path = os.environ.get("AGENTCTL_TOOL_CONTRACTS")
    table = load_contract_table(contract_path)

    commands: list[tuple[str, str, str]] = []
    commands_file = getattr(args, "commands_file", None)
    if commands_file:
        commands.extend(
            ("(commands-file)", venue, c) for c in _iter_commands_file(commands_file)
        )
    corpus = getattr(args, "corpus", None) or []
    if corpus:
        commands.extend(_iter_corpus_commands(corpus))

    dump_path = getattr(args, "dump_commands", None)
    if dump_path:
        Path(dump_path).write_text(
            "".join(f"{cmd}\n" for _src, _v, cmd in commands), encoding="utf-8"
        )

    results = []
    unknown_programs: set[str] = set()
    for source, cmd_venue, cmd in commands:
        res = resolve_command(cmd, cmd_venue, contract_table=table)
        entry = _resolution_to_dict(res, label=cmd, label_key="command")
        entry["source"] = source
        results.append(entry)
        if res.status == "unresolved" and res.reason_class == "unknown-program":
            unknown_programs.add(cmd)

    data = {"results": results, "unknown_programs": sorted(unknown_programs)}
    report_path = getattr(args, "report_json", None)
    if report_path:
        Path(report_path).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    ok = not (getattr(args, "require_no_unknown_program", False) and unknown_programs)
    text = json.dumps(data, indent=2, sort_keys=True)
    return Directive(ok, "(render)", "inspect", text, data=data)


def cmd_plan_resources(args, *, store=None, runner=None) -> Directive:
    if getattr(args, "commands_file", None) or getattr(args, "corpus", None):
        return _cmd_plan_resources_corpus(args)

    doc = load_plan(args.plan)
    venue = _venue_for(doc)
    data = _plan_resources_json(doc, venue)
    fmt = getattr(args, "format", "compact") or "compact"
    text = (
        json.dumps(data, indent=2, sort_keys=True)
        if fmt == "json"
        else _render_plan_resources_text(data)
    )
    return Directive(True, "(render)", "inspect", text, data=data)
