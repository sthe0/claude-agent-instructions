#!/usr/bin/env python3
"""Derive the oversized-leaf thresholds from the measured experience-leaf corpus.

Difficulty removed: a size cap guessed from intuition either flags nothing or
flags a third of the corpus. The two caps self-diagnose.py's `oversized-leaf`
kind uses are taken from the corpus itself: the Tukey far-out fence
(Q3 + 3*IQR) on the natural log of each axis, exponentiated back, because both
axes are heavy-tailed and a raw fence flags a long tail of ordinary leaves.

  memory-leaf-max-bytes                = byte fence, rounded up to the next 1000
  memory-leaf-max-contexts-multiple    = ceil(context fence / principle-promotion-threshold)

The corpus is every `schema: difficulty/v1` leaf under memory-global/leaves/ of
the FROZEN tree --rev, read through `git ls-tree` / `git show`, never from the
working copy, so the measurement stays reproducible after a split shrinks the
leaf that motivated it. Bytes and contexts come from self-diagnose.py's own
measure_leaf, so the scanner and this script cannot disagree.

Usage: memory-leaf-size-calibrate.py --rev <commit> [--out PATH] [--repo PATH]
Writes docs/operations/memory-leaf-size-calibration.json by default.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "operations" / "memory-leaf-size-calibration.json"
LEAVES_PREFIX = "memory-global/leaves/"
METHOD = (
    "Tukey far-out fence Q3 + 3*IQR on the natural log of each axis, "
    "exponentiated back; quartiles are statistics.quantiles(n=4, method='inclusive'); "
    "log axis is log(max(x, 1)); bytes cap rounded up to the next 1000, "
    "contexts multiple = ceil(context fence / principle-promotion-threshold); "
    "the quotient is rounded to 6 decimals before the ceil so exp/log float noise "
    "cannot push an exact integer fence up by one"
)


def _load_self_diagnose():
    spec = importlib.util.spec_from_file_location(
        "self_diagnose", Path(__file__).resolve().parent / "self-diagnose.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["self_diagnose"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def quartiles(values: "list[float]") -> "dict[str, float]":
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"q1": q1, "median": q2, "q3": q3, "iqr": q3 - q1}


def far_out_fence(q: "dict[str, float]") -> float:
    return q["q3"] + 3 * q["iqr"]


def _log(x: int) -> float:
    return math.log(max(x, 1))


def calibrate(repo: Path, rev: str) -> dict:
    sd = _load_self_diagnose()
    names = _git(repo, "ls-tree", "-r", "--name-only", rev, LEAVES_PREFIX).splitlines()
    rows = []
    for name in sorted(n for n in names if n.endswith(".md")):
        text = _git(repo, "show", f"{rev}:{name}")
        if not sd.is_difficulty_leaf(text):
            continue
        size, contexts = sd.measure_leaf(text)
        rows.append({"path": name, "bytes": size, "contexts": contexts})
    if len(rows) < 4:
        raise SystemExit(f"only {len(rows)} difficulty/v1 leaves at {rev}; cannot calibrate")

    b = [r["bytes"] for r in rows]
    c = [r["contexts"] for r in rows]
    qb, qc = quartiles([float(x) for x in b]), quartiles([float(x) for x in c])
    lqb, lqc = quartiles([_log(x) for x in b]), quartiles([_log(x) for x in c])
    fence_bytes_log = math.exp(far_out_fence(lqb))
    fence_ctx_log = math.exp(far_out_fence(lqc))

    try:
        config = _git(repo, "show", f"{rev}:config.md")
        promotion = int(
            next(
                cell
                for line in config.splitlines()
                if "`principle-promotion-threshold`" in line
                for cell in (c_.strip().strip("`") for c_ in line.split("|")[2:])
                if cell.isdigit()
            )
        )
    except (subprocess.CalledProcessError, StopIteration):
        promotion = sd.PRINCIPLE_PROMOTION_DEFAULT

    max_bytes = math.ceil(round(fence_bytes_log, 6) / 1000) * 1000
    multiple = math.ceil(round(fence_ctx_log / promotion, 6))
    for r in rows:
        r["flagged"] = r["bytes"] > max_bytes or r["contexts"] > multiple * promotion

    return {
        "method": METHOD,
        "measured_on": date.today().isoformat(),
        "rev": rev,
        "n_leaves": len(rows),
        "principle_promotion_threshold": promotion,
        "quartiles": {
            "raw": {"bytes": qb, "contexts": qc},
            "log": {"bytes": lqb, "contexts": lqc},
        },
        "fences": {
            "raw": {"bytes": far_out_fence(qb), "contexts": far_out_fence(qc)},
            "log_derived": {"bytes": fence_bytes_log, "contexts": fence_ctx_log},
        },
        "values": {
            "memory-leaf-max-bytes": max_bytes,
            "memory-leaf-max-contexts-multiple": multiple,
        },
        "n_flagged": sum(1 for r in rows if r["flagged"]),
        "leaves": rows,
    }


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rev", required=True, help="frozen commit whose tree is measured")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--repo", default=str(REPO_ROOT))
    args = p.parse_args(argv)
    result = calibrate(Path(args.repo), args.rev)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"n_leaves={result['n_leaves']} n_flagged={result['n_flagged']} "
        f"values={result['values']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
