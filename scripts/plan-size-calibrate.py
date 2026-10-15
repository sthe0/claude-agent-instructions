#!/usr/bin/env python3
"""Derive the plan size limits from the dispatch prompt ceiling.

Difficulty removed: a plan node larger than a reviewer's prompt can hold is refused only
when the review is launched, after the plan was approved, and a limit guessed from
intuition either refuses nothing or refuses plans that fit. The two limits come from the
ceiling and from the largest prompt a topological pair review builds:

  C = the dispatch prompt ceiling (`spawn-specialist.dispatch_prompt_ceiling_chars`)
  O = the length of the full pair-review prompt on a fixture whose nodes are empty,
      path-normalized (`render.normalize_prompt_paths`), plus n_p * P, the allowance for
      the n_p placeholders turning back into real paths of up to P extra characters each
  S = floor((C - O) / 3), rounded down to a multiple of 1000    (plan-stage-max-chars)
  F = floor(S / k), rounded down to a multiple of 500           (plan-field-inline-max-chars)

A pair prompt shows three nodes (the plan-level node, the base, the service), so three
nodes of S characters plus O fit under C. k is the largest per-node-kind count of fields
a plan may hold in a file of their own (`plan_refs.EXTERNALIZABLE_FIELDS`), so a node whose
every such field is at the inline limit still fits.

Usage: plan-size-calibrate.py [--out PATH]
Writes docs/operations/plan-size-calibration.json by default.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentctl import plan_refs, render  # noqa: E402
from lib import config_root  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "docs" / "operations" / "plan-size-calibration.json"
PAIR_ID = "3-2"
S_STEP = 1000
F_STEP = 500
FORMULA = (
    "S = floor((C - O) / 3) rounded down to a multiple of 1000; "
    "F = floor(S / k) rounded down to a multiple of 500; "
    "O = len(normalized pair-review prompt on the fixture) + n_p * P; "
    "P = (longest real path a placeholder stands for, the view root taken at "
    "config_root.topo_units_root()) - (shortest placeholder length)"
)

FIXTURE_PLAN = """[meta]
task_id = "plan-size-calibration"

[meta.order]
customer_id = "c"
customer = "c"
functional_place = "f"

[[meta.order.requirements]]
id = "R1"
text = "r"
derivation = "d"

[meta.order.coverage]
R1 = ["stage 2 verify_command"]

[[stage]]
index = 1
title = "t"
executor = "in_thread"
expected_result_image = "r"
criterion_type = "measurable"
done_criterion = "d"

[[stage]]
index = 2
title = "t"
executor = "in_thread"
expected_result_image = "r"
criterion_type = "measurable"
done_criterion = "d"
depends_on = [1]

[[stage]]
index = 3
title = "t"
executor = "in_thread"
expected_result_image = "r"
criterion_type = "measurable"
done_criterion = "d"
depends_on = [2]
"""


def load_topo_driver():
    name = "plan_review_topological_for_calibration"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "plan-review-topological.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixture_pair_prompt(directory: Path, *, view_root: "Path | None" = None) -> "tuple[str, Path]":
    """The full pair-review prompt on the fixture plan written into `directory`, and the
    view root it was built for (the plan's directory unless `view_root` is given)."""
    plan = directory / "plan.toml"
    plan.write_text(FIXTURE_PLAN, encoding="utf-8")
    root = view_root if view_root is not None else directory
    return load_topo_driver().reviewer_prompt(str(plan), PAIR_ID, view_root=root), root


def normalized_fixture_prompt(directory: Path) -> str:
    text, root = fixture_pair_prompt(directory)
    return render.normalize_prompt_paths(text, root)


def placeholder_count(text: str) -> int:
    return sum(text.count(p) for p in render.PATH_PLACEHOLDERS)


def path_allowance() -> int:
    """P: the most characters a placeholder can grow by when it is replaced by the real
    path it stands for."""
    longest = max(
        len(form)
        for path, _ in render.named_prompt_paths(config_root.topo_units_root())
        for form in {str(path), str(Path(path).resolve())}
    )
    return longest - min(len(p) for p in render.PATH_PLACEHOLDERS)


def largest_field_count() -> int:
    return max(len(fields) for fields in plan_refs.EXTERNALIZABLE_FIELDS.values())


def calibrate() -> dict:
    ceiling = render.dispatch_ceiling_chars()
    with tempfile.TemporaryDirectory() as tmp:
        text = normalized_fixture_prompt(Path(tmp))
    n_p = placeholder_count(text)
    allowance = path_allowance()
    overhead = len(text) + n_p * allowance
    k = largest_field_count()
    stage_max = (ceiling - overhead) // 3 // S_STEP * S_STEP
    field_max = stage_max // k // F_STEP * F_STEP
    return {
        "C": ceiling, "O": overhead, "n_p": n_p, "P": allowance, "k": k,
        "S": stage_max, "F": field_max,
        "normalized_prompt_chars": len(text),
        "formula": FORMULA,
        "command": "python3 scripts/plan-size-calibrate.py",
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    result = calibrate()
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("C", "O", "n_p", "P", "k", "S", "F")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
