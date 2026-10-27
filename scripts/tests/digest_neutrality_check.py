#!/usr/bin/env python3
"""Digest-neutrality check: a change to the plan machinery must not move the digests of
plans that already exist.

Difficulty removed: a plan's content digest, its per-part digests and the premise keys a
disposed question is stamped with are persisted (approvals, reviews, enumerations all bind
to them). An engine edit that shifts any of them silently voids every live session's
approval. Reading the diff does not prove neutrality; loading the same plans under the
unchanged base tree and the working tree and comparing the values does.

Usage:
    python3 scripts/tests/digest_neutrality_check.py [--base COMMIT] [--plans-dir DIR]

`--base` defaults to the commit pinned in `fixtures/digest_neutrality_base.txt` (the fork
point of the delivery branch, fixed once so the check never drifts with the moving trunk).
`--plans-dir` defaults to `fixtures/` beside this script.

How it works: `git archive <base> scripts` is extracted into a temp dir; every `*.toml`
under the plans dir is then loaded by the base tree's `agentctl.plan` and by the working
tree's, each in its OWN `python3 -I` subprocess with cwd and sys.path set to that tree, so
both runs can never import the same module. Per plan the worker reports `plan_content_digest`,
`plan_meta_digest`, `order_digest`, the per-part digest map, `_reviewed_stage_keys` and
`stage_closure_digest` of every stage, and the premise keys (`plan_meta_element_keys` and every stage's
`stage_element_keys`, which includes the `stage_question_key` whole-stage entry).

Outcome per plan: loads in both trees with equal values (compared); loads in both with a
different value (changed -> failure); loads under base only (regressed -> failure); loads
under the working tree only, or under neither (skipped: a newer plan or not a plan). A
worker that crashes, prints no result, or raises while describing a plan that loaded is
`broken` -> failure, never a skip. Zero compared plans is a failure too: a check that
compared nothing proves nothing.

Exit codes: 0 neutral, 1 a failure above, 2 could not run (bad base, no git, bad dir).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_BASE_FILE = HERE / "fixtures" / "digest_neutrality_base.txt"
DEFAULT_PLANS_DIR = HERE / "fixtures"


def _closure_digest(P, doc, index: int) -> str:
    try:
        return P.stage_closure_digest(doc, index)
    except P.PlanError as exc:
        return f"PlanError: {exc}"


def _describe(doc) -> dict:
    from agentctl import plan as P

    return {
        "plan_digest": P.plan_content_digest(doc),
        "plan_meta_digest": P.plan_meta_digest(doc),
        "order_digest": P.order_digest(doc),
        "parts": P.part_digest_map(doc),
        "reviewed_stage_keys": {str(s.index): P._reviewed_stage_keys(s) for s in doc.stages},
        "stage_closure_digests": {str(s.index): _closure_digest(P, doc, s.index) for s in doc.stages},
        "premise_keys": {
            "plan": P.plan_meta_element_keys(doc),
            "stages": {str(s.index): P.stage_element_keys(s) for s in doc.stages},
        },
    }


def _worker(tree: str) -> int:
    """Load each plan path read from stdin with `tree`'s agentctl and print the results."""
    os.chdir(tree)
    sys.path.insert(0, str(Path(tree) / "scripts"))
    from agentctl import plan as P

    results = {}
    for path in json.load(sys.stdin):
        try:
            doc = P.load_plan(path)
        except P.PlanError as exc:
            results[path] = {"status": "no-load", "error": str(exc)}
            continue
        except Exception as exc:  # a loader that crashes is broken, not a "not a plan"
            results[path] = {"status": "broken", "error": f"load: {type(exc).__name__}: {exc}"}
            continue
        try:
            results[path] = {"status": "loaded", "values": _describe(doc)}
        except Exception as exc:
            results[path] = {"status": "broken", "error": f"describe: {type(exc).__name__}: {exc}"}
    for entry in results.values():
        entry["loaded_from"] = P.__file__
    json.dump(results, sys.stdout)
    return 0


def load_under(tree: Path, plans: list[str]) -> dict:
    """The per-plan results of `tree`'s loader, run in an isolated interpreter. Raises
    RuntimeError when the worker itself fails: a broken loader must fail the check."""
    proc = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--worker", "--tree", str(tree)],
        input=json.dumps(plans), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"loader under {tree} exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"loader under {tree} printed no result: {exc}") from exc


def compare(base_tree: Path, work_tree: Path, plans: list[str]) -> dict:
    """Classify every plan. Returns {compared, changed, regressed, broken, skipped} where
    `compared` is a count and the rest are lists of `path: reason` strings."""
    try:
        base = load_under(base_tree, plans)
    except RuntimeError as exc:
        return {"compared": 0, "changed": [], "regressed": [], "broken": [f"base: {exc}"], "skipped": []}
    try:
        work = load_under(work_tree, plans)
    except RuntimeError as exc:
        return {"compared": 0, "changed": [], "regressed": [], "broken": [f"working tree: {exc}"], "skipped": []}
    report = {"compared": 0, "changed": [], "regressed": [], "broken": [], "skipped": []}
    for path in plans:
        b, w = base.get(path), work.get(path)
        if b is None or w is None:
            report["broken"].append(f"{path}: loader returned no entry")
        elif "broken" in (b["status"], w["status"]):
            side = "base" if b["status"] == "broken" else "working tree"
            report["broken"].append(f"{path}: {side} {(b if side == 'base' else w)['error']}")
        elif b["status"] == "loaded" and w["status"] == "loaded":
            report["compared"] += 1
            if b["values"] != w["values"]:
                keys = sorted(k for k in b["values"] if b["values"][k] != w["values"].get(k))
                report["changed"].append(f"{path}: differs in {', '.join(keys)}")
        elif b["status"] == "loaded":
            report["regressed"].append(f"{path}: loads under base, not under the working tree: {w['error']}")
        else:
            report["skipped"].append(path)
    return report


def verdict(report: dict) -> int:
    failed = report["changed"] or report["regressed"] or report["broken"] or report["compared"] == 0
    return 1 if failed else 0


def extract_base(commit: str, dest: Path) -> None:
    archive = subprocess.Popen(
        ["git", "-C", str(REPO_ROOT), "archive", commit, "scripts"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout, capture_output=True, check=False)
    archive.stdout.close()
    git_err = archive.stderr.read().decode(errors="replace")
    if archive.wait() != 0 or tar.returncode != 0:
        raise RuntimeError(f"cannot extract base {commit!r}: {git_err.strip() or tar.stderr.decode(errors='replace').strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="base commit (default: the pinned fixture file)")
    ap.add_argument("--plans-dir", default=str(DEFAULT_PLANS_DIR))
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--tree", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        return _worker(args.tree)
    base = args.base or DEFAULT_BASE_FILE.read_text(encoding="utf-8").strip()
    plans_dir = Path(args.plans_dir).resolve()
    if not plans_dir.is_dir():
        print(f"digest-neutrality: plans dir {plans_dir} is not a directory", file=sys.stderr)
        return 2
    plans = sorted(str(p) for p in plans_dir.rglob("*.toml"))
    with tempfile.TemporaryDirectory(prefix="digest-neutrality-base-") as tmp:
        try:
            extract_base(base, Path(tmp))
        except (RuntimeError, OSError) as exc:
            print(f"digest-neutrality: {exc}", file=sys.stderr)
            return 2
        report = compare(Path(tmp), REPO_ROOT, plans)
    for key in ("changed", "regressed", "broken"):
        for line in report[key]:
            print(f"{key.upper()}: {line}")
    code = verdict(report)
    if report["compared"] == 0:
        print("digest-neutrality: FAIL -- no plan was compared, so nothing was proven")
    print(
        f"digest-neutrality: {'OK' if code == 0 else 'FAIL'} -- base {base[:12]}: "
        f"{report['compared']} compared, {len(report['changed'])} changed, "
        f"{len(report['regressed'])} regressed, {len(report['broken'])} broken, "
        f"{len(report['skipped'])} skipped")
    return code


if __name__ == "__main__":
    sys.exit(main())
