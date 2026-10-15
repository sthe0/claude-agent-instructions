#!/usr/bin/env python3
"""Report how a plan fits the model-facing size limits, without launching anything.

Difficulty removed: a plan node or a review prompt over its size limit is found only when
the review or the dispatch is spawned and refused, after the plan was approved, and an
author splitting a plan has no measure of how far each node is from the limit. This script
builds every prompt the plan would produce -- the stage briefs, the whole-plan prompts, the
topological pair and unit reviews -- and measures it; nothing is spawned.

Usage: plan-fit-report.py --plan PATH --out PATH [--source PATH]
Writes one JSON object to --out:
  ceiling_chars, plan_stage_max_chars, plan_field_inline_max_chars
  node_chars        {"meta"|"<stage index>": rendered characters, references resolved}
  oversize_nodes    the nodes over plan-stage-max-chars
  lint              {node: "ok"|"warn"|"block"}, as `agentctl submit-plan` would report
  topo              {pair or unit id: "ok"|"refused-before-spawn"}
  sites             one entry per prompt: id, shown_nodes, chars (path-normalized: the
                    view root, repo root, cwd, plans and agent directories and the home
                    directory are placeholders, so a prompt measures the same on any
                    machine) and argv (`<view-root>` stands for the view directory)
  content_digest_equal   with --source: whether both files resolve to the same plan content
A topological verdict is taken on the normalized prompt, so it is the machine-independent
one; a prompt within the path allowance of the ceiling may still be refused at launch.
The only files written are --out and a temporary view directory that is removed.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentctl import dispatch, plan, plan_refs, plan_store, render  # noqa: E402
from agentctl.config import PlanLimitError, Thresholds  # noqa: E402
from agentctl.submission import plan_size_findings  # noqa: E402

VIEW_ROOT = "<view-root>"
OK, WARN, BLOCK = "ok", "warn", "block"
REFUSED = "refused-before-spawn"
SEVERITY_ORDER = (OK, WARN, BLOCK)


def load_script(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def with_view_root(argv: "list[str]", view_root: Path) -> "list[str]":
    return [str(view_root) if part == VIEW_ROOT else part for part in argv]


def load_resolved(path: Path) -> "plan.PlanDoc":
    return plan.load_plan(path, root=plan_refs.default_root(path))


def spawn_stage(stage):
    """`stage` as a spawn stage, so a stage run in the thread still has the brief a
    dispatch would give it."""
    if stage.is_spawn():
        return stage
    return dataclasses.replace(
        stage, actor=dataclasses.replace(stage.actor, executor="spawn:developer"))


def plan_argv(kind: str, plan_path: Path, doc) -> "list[str]":
    return [
        "--kind", kind, "--plan", str(plan_path),
        "--done-criterion", doc.meta.done_criterion or "done",
        "--criterion-type", str(getattr(doc.meta.criterion_type, "value", doc.meta.criterion_type)),
        "--complexity", "medium", "--effort", "medium", "--view-root", VIEW_ROOT,
    ]


def spawn_sites(plan_path: Path, doc) -> "list[tuple[str, str, list[str], dict]]":
    """`(site id, site kind, spawner argv, build_site_prompt selector)` of every
    non-topological prompt the plan produces."""
    sites = []
    for stage in sorted(doc.stages, key=lambda s: s.index):
        argv = dispatch.build_argv(spawn_stage(stage), str(plan_path))[2:]
        sites.append((f"dispatch-stage-brief {stage.index}", "dispatch-stage-brief",
                      [*argv, "--view-root", VIEW_ROOT], {"stage": stage.index}))
    sites.append(("dispatch-no-stage", "dispatch-no-stage", plan_argv("developer", plan_path, doc), {}))
    sites.append(("whole-plan-review", "whole-plan-review", plan_argv("thinker", plan_path, doc), {}))
    return sites


def topo_nodes(doc, review_id: str) -> tuple:
    return (plan.parse_unit(doc, review_id),) if plan.is_unit_id(review_id) else plan.parse_pair(doc, review_id)


def node_name(node) -> str:
    return str(node)


def site_entry(site_id: str, shown, text: str, view_root: Path, argv: "list[str]") -> dict:
    return {
        "id": site_id,
        "shown_nodes": sorted(node_name(n) for n in shown),
        "chars": len(render.normalize_prompt_paths(text, view_root)),
        "argv": argv,
    }


def lint_by_node(doc, positions) -> "dict[str, str]":
    verdict = {"meta": OK, **{str(s.index): OK for s in doc.stages}}
    for finding in plan_size_findings(doc, positions):
        node = node_name(finding["node"])
        if SEVERITY_ORDER.index(finding["severity"]) > SEVERITY_ORDER.index(verdict[node]):
            verdict[node] = finding["severity"]
    return verdict


def build_report(plan_path: Path, source: "Path | None") -> dict:
    topo = load_script("plan-review-topological.py", "plan_review_topological_for_fit_report")
    spawner = load_script("spawn-specialist.py", "spawn_specialist_for_fit_report")
    doc = load_resolved(plan_path)
    limits = Thresholds()
    venue = plan._venue_for(doc)
    node_chars = {"meta": plan.meta_rendered_size(doc)}
    node_chars.update({str(s.index): plan.stage_rendered_size(s, venue) for s in doc.stages})
    report: dict = {
        "plan": str(plan_path),
        "ceiling_chars": render.dispatch_ceiling_chars(),
        "plan_stage_max_chars": limits.plan_stage_max_chars,
        "plan_field_inline_max_chars": limits.plan_field_inline_max_chars,
        "node_chars": node_chars,
        "oversize_nodes": [n for n, chars in node_chars.items() if chars > limits.plan_stage_max_chars],
        "lint": lint_by_node(doc, plan_store.visited_positions(plan_path)),
        "topo": {},
        "sites": [],
    }
    with tempfile.TemporaryDirectory(prefix="plan-fit-") as tmp:
        view_root = Path(tmp)
        for site_id, kind, argv, selector in spawn_sites(plan_path, doc):
            text = spawner.assembled_prompt(with_view_root(argv, view_root))
            shown = render.build_site_prompt(
                kind, plan_path, view_root=view_root, materialize=False, **selector).shown_nodes
            report["sites"].append(site_entry(site_id, shown, text, view_root, argv))
        for review_id in (*plan.review_pairs(doc), *plan.review_units(doc)):
            nodes = topo_nodes(doc, review_id)
            text = topo.reviewer_prompt(str(plan_path), review_id, view_root=view_root)
            normalized = render.normalize_prompt_paths(text, view_root)
            refusal = render.topo_unit_refusal(doc, ("meta", *nodes), normalized)
            report["topo"][review_id] = REFUSED if refusal else OK
            kind = "topo-unit" if plan.is_unit_id(review_id) else "topo-pair"
            argv = topo.build_spawn_argv(
                argparse.Namespace(model=None, complexity=None), review_id, str(plan_path),
                dry_run=False, view_root=VIEW_ROOT)
            shown = nodes if kind == "topo-unit" else ("meta", *nodes)
            report["sites"].append(site_entry(f"{kind} {review_id}", shown, text, view_root, argv))
    if source is not None:
        report["content_digest_equal"] = (
            plan.plan_content_digest(doc) == plan.plan_content_digest(load_resolved(source)))
    return report


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=None,
                        help="the plan this one was externalized from or into: report whether "
                             "both resolve to the same content")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.plan, args.source)
    except (plan.PlanError, PlanLimitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(report['sites'])} sites, oversize nodes: {report['oversize_nodes'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
