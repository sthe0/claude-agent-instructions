#!/usr/bin/env python3
"""Derive the `small-change-max-lines` bound from the changes the user accepted without weighing a choice.

Difficulty removed: the bound of the inline (small change) class was a number nobody could trace,
so it sat either below every ordinary accepted change or far above them. It is taken from the
hand-written production-line size (scripts/agentctl/inline_class.py) of past accepted changes,
with the p90 rule and the min_n rule of docs/operations/review-cost-calibration.json.

Population A: merged PRs of the repository owner whose head branch is a mandate-cycle or salvage
branch, each measured over its own head (tip = headRefOid, base = merge-base(tip, M^1) for the
merge commit M). Population B: engine sessions that were resolved after one stage, one approval and
no replan, measured over the range their recorded landing pushed. P is A plus B, one landed change
counted once (a B row whose head is an A merge commit, or whose `git patch-id --stable` equals an
A row's, is dropped). Rows carry only source, id, base, tip and production_lines.

Usage:
  inline-size-calibrate.py --snapshot DIR [--calibration PATH] [--owner LOGIN]
                           [--option N --user-value V --decided-on DATE]
      Builds the calibration and writes it to PATH (--out is an alias of --calibration; default
      docs/operations/inline-size-calibration.json). DIR is a copy of the agentctl state directory
      (session JSON files); it is read, never written. The three decision flags apply only when
      n < min_n; OPTIONS lists what each option means.
  inline-size-calibrate.py --check [--calibration PATH]
      Re-derives the figures from the embedded rows, then checks config.md (value and a pointer to
      the JSON) and the config-rationale.md row (value, JSON path, inputs_sha256, formula, the
      per-population counts, the decision and the p90 sensitivity figures) against them. Exit 0 on
      match, 1 on a difference, 2 on unreadable input.
  inline-size-calibrate.py --recompute [--calibration PATH]
      Re-measures every row from git with the shared function, fetching PR heads (pull/N/head)
      that are not in the object store; exit 1 on any difference.
  inline-size-calibrate.py --basis [--calibration PATH]
      Prints the config-rationale.md row text the check expects.
  inline-size-calibrate.py --funnel --snapshot DIR
      Prints how many sessions survive each population-B filter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import inline_class  # noqa: E402
from agentctl.inline_git import git as _git, measure_range, run as _run  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent
REL_PATH = "docs/operations/inline-size-calibration.json"
KEY = inline_class.BOUND_KEY
MIN_N = 20
FORMULA = "p90 = sorted(v)[min(n-1, round(0.9*(n-1)))]; candidate = ceil(p90); n >= min_n else user_decision"
RULE = "p90_ceil_min_n"
PREVIOUS_BOUND = 20
OPTIONS = {
    1: "ceil(p90) of the available rows (A and B)",
    2: "ceil(p90) of the available rows plus the open cycle and salvage PRs",
    3: "keep the previous bound (PREVIOUS_BOUND)",
    4: "a value the user names",
}
ROW_KEYS = ("source", "id", "base", "tip", "production_lines")
HEAD_RE = re.compile(r"^(?:mandate/|fix/[0-9]+-salvage$)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OUTSIDE_REFERENCE = {
    "source": "Google engineering-practices guide, small CLs",
    "lines_usually_reasonable": 100,
    "use": "outside reference only; the bound comes from this repository's own history",
}


def p90(values: "list[int]"):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))] if ordered else None


def candidate(values: "list[int]") -> "int | None":
    value = p90(values)
    return None if value is None else math.ceil(value)


def rows_digest(rows: "list[dict]") -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def commit_exists(repo: Path, sha: str) -> bool:
    return bool(sha) and _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo).returncode == 0


def ensure_commit(repo: Path, sha: str, number: "int | None") -> bool:
    if commit_exists(repo, sha):
        return True
    if number is None:
        return False
    _run(["git", "fetch", "--no-tags", "-q", "origin", f"pull/{number}/head"], repo)
    return commit_exists(repo, sha)


def merge_base(repo: Path, a: str, b: str) -> "str | None":
    done = _run(["git", "merge-base", a, b], repo)
    return done.stdout.strip() or None if done.returncode == 0 else None


def patch_id(repo: Path, base: str, tip: str) -> "str | None":
    diff = _git(repo, ["git", "diff", base, tip])
    if not diff:
        return None
    out = _git(repo, ["git", "patch-id", "--stable"], diff).split()
    return out[0] if out else None


def _row(source: str, ident, base: str, tip: str, lines: int) -> dict:
    return {"source": source, "id": ident, "base": base, "tip": tip, "production_lines": lines}


def _measured_row(repo: Path, source: str, ident, base: str, tip: str):
    """(row, skip reason): a row whose lines the count does not capture is left out."""
    measured = measure_range(repo, base, tip)
    if measured.binary_paths:
        return None, "binary path counted"
    if measured.production_lines == 0:
        return None, "no counted lines"
    return _row(source, ident, base, tip, measured.production_lines), None


def _is_owner_merge(pr: dict, owner: "str | None") -> bool:
    merged_by = (pr.get("mergedBy") or {}).get("login")
    if not merged_by:
        return False
    return merged_by == (owner or (pr.get("author") or {}).get("login"))


def population_a(repo: Path, prs: "list[dict]", owner: "str | None"):
    """(rows, merge commit oids, patch-ids, skipped reasons) of the merged cycle and salvage PRs."""
    rows, merge_oids, patch_ids, skipped = [], set(), set(), []
    for pr in prs:
        if not HEAD_RE.match(pr.get("headRefName") or "") or not _is_owner_merge(pr, owner):
            continue
        number, tip = pr["number"], pr.get("headRefOid") or ""
        merge_oid = (pr.get("mergeCommit") or {}).get("oid")
        if not merge_oid or not commit_exists(repo, merge_oid):
            skipped.append("merge commit not in the object store")
            continue
        if not ensure_commit(repo, tip, number):
            skipped.append("head not in the object store or fetchable")
            continue
        parents = _git(repo, ["git", "rev-list", "--parents", "-n", "1", merge_oid]).split()[1:]
        base = merge_base(repo, tip, parents[0]) if parents else None
        if base is None:
            skipped.append("no merge base")
            continue
        row, reason = _measured_row(repo, "A", number, base, tip)
        if row is None:
            skipped.append(reason)
            continue
        rows.append(row)
        merge_oids.add(merge_oid)
        pid = patch_id(repo, base, tip)
        if pid:
            patch_ids.add(pid)
    return rows, merge_oids, patch_ids, skipped


def population_open(repo: Path, prs: "list[dict]", trunk: str = "origin/main") -> "list[dict]":
    rows = []
    for pr in prs:
        if not HEAD_RE.match(pr.get("headRefName") or ""):
            continue
        tip = pr.get("headRefOid") or ""
        if not ensure_commit(repo, tip, pr["number"]):
            continue
        base = merge_base(repo, tip, trunk)
        if base is None:
            continue
        row, _ = _measured_row(repo, "open", pr["number"], base, tip)
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: r["id"])


def session_landing(state: dict) -> "dict | None":
    """The recorded landing of a session resolved after one stage, one approval and no replan
    after it; None for any other session."""
    if state.get("node") != "RESOLVED" or len(state.get("stages") or ()) != 1:
        return None
    history = [h for h in state.get("history") or () if isinstance(h, dict)]
    approvals = [i for i, h in enumerate(history) if h.get("event") == "approve"]
    if len(approvals) != 1 or any(h.get("event") == "replan" for h in history[approvals[0] + 1:]):
        return None
    landings = [h for h in history if h.get("event") == "record_landing"]
    return landings[-1] if landings else None


def population_b(repo: Path, snapshot: Path, a_merge_oids: "set[str]", a_patch_ids: "set[str]"):
    """(rows, skipped reasons, dropped as duplicates of A) of the one-stage resolved sessions."""
    rows, skipped, dropped = [], [], 0
    for path in sorted(snapshot.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("session_id") != path.stem:
            continue
        landing = session_landing(state)
        head, base = (landing or {}).get("head"), (landing or {}).get("base")
        if not (head and base and commit_exists(repo, head) and commit_exists(repo, base)):
            continue
        if head in a_merge_oids:
            dropped += 1
            continue
        pid = patch_id(repo, base, head)
        if pid and pid in a_patch_ids:
            dropped += 1
            continue
        row, reason = _measured_row(repo, "B", path.stem, base, head)
        if row is None:
            skipped.append(reason)
        else:
            rows.append(row)
    return sorted(rows, key=lambda r: r["id"]), skipped, dropped


def b_funnel(repo: Path, snapshot: Path) -> "dict[str, int]":
    """How many session files survive each filter of population B, for the --funnel digest."""
    funnel = dict.fromkeys(("files", "sessions", "any_record_landing_event", "resolved", "one_stage", "one_approve_no_replan_after",
                            "has_landing", "commits_present"), 0)
    for path in sorted(snapshot.glob("*.json")):
        funnel["files"] += 1
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("session_id") != path.stem:
            continue
        funnel["sessions"] += 1
        funnel["any_record_landing_event"] += any(
            isinstance(h, dict) and h.get("event") == "record_landing" for h in state.get("history") or ())
        if state.get("node") != "RESOLVED":
            continue
        funnel["resolved"] += 1
        if len(state.get("stages") or ()) != 1:
            continue
        funnel["one_stage"] += 1
        history = [h for h in state.get("history") or () if isinstance(h, dict)]
        approvals = [i for i, h in enumerate(history) if h.get("event") == "approve"]
        if len(approvals) != 1 or any(h.get("event") == "replan" for h in history[approvals[0] + 1:]):
            continue
        funnel["one_approve_no_replan_after"] += 1
        landing = session_landing(state)
        if landing is None:
            continue
        funnel["has_landing"] += 1
        if commit_exists(repo, landing.get("head")) and commit_exists(repo, landing.get("base")):
            funnel["commits_present"] += 1
    return funnel


def _skipped_block(reasons: "list[str]") -> dict:
    counts: "dict[str, int]" = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return {"count": len(reasons), "reasons": counts}


def _values(rows: "list[dict]", source: "str | None" = None) -> "list[int]":
    return [r["production_lines"] for r in rows if source is None or r["source"] == source]


def derived_figures(rows: "list[dict]", open_rows: "list[dict]") -> "tuple[dict, dict]":
    """(populations, sensitivity-without-open_rows-digest): every figure --check re-derives."""
    n_a, n_b = len(_values(rows, "A")), len(_values(rows, "B"))
    populations = {
        "A": {"n": n_a, "p90": p90(_values(rows, "A"))},
        "B": {"n": n_b, "p90": p90(_values(rows, "B"))},
        "P": {"n": len(rows), "p90": p90(_values(rows))},
    }
    sensitivity = {
        "p90_A": populations["A"]["p90"],
        "p90_B": populations["B"]["p90"],
        "p90_A_plus_open_rows": p90(_values(rows, "A") + _values(open_rows)),
        "largest_source_share": round(max(n_a, n_b) / len(rows), 4) if rows else None,
        "outside_reference": OUTSIDE_REFERENCE,
    }
    return populations, sensitivity


def assemble(rows: "list[dict]", open_rows: "list[dict]", skipped: dict, dropped_b: int,
             decision: "dict | None" = None) -> dict:
    rows = sorted(rows, key=lambda r: (r["source"], str(r["id"])))
    populations, sensitivity = derived_figures(rows, open_rows)
    sensitivity["open_rows"] = {"rows": open_rows, "sha256": rows_digest(open_rows)}
    doc = {
        "rule": RULE,
        "formula": FORMULA,
        "min_n": MIN_N,
        "n": len(rows),
        "rows": rows,
        "inputs_sha256": rows_digest(rows),
        "populations": populations,
        "skipped": skipped,
        "deduplicated_b_rows": dropped_b,
        "sensitivity": sensitivity,
    }
    if len(rows) >= MIN_N:
        doc.update(decision="derived", bound_value=candidate(_values(rows)))
    else:
        decision = decision or {}
        doc.update(decision="user", bound_value=decision.get("user_value"),
                   user_value=decision.get("user_value"), decided_on=decision.get("decided_on"),
                   option=decision.get("option"))
    return doc


def build(repo: Path, snapshot: Path, merged_prs: "list[dict]", open_prs: "list[dict]",
          owner: "str | None", decision: "dict | None") -> dict:
    a_rows, merge_oids, patch_ids, a_skipped = population_a(repo, merged_prs, owner)
    b_rows, b_skipped, dropped = population_b(repo, snapshot, merge_oids, patch_ids)
    skipped = {"A": _skipped_block(a_skipped), "B": _skipped_block(b_skipped)}
    return assemble(a_rows + b_rows, population_open(repo, open_prs), skipped, dropped, decision)


class Malformed(Exception):
    pass


def _config_row(text: str, key: str) -> "str | None":
    match = re.search(rf"^\| `{re.escape(key)}` \|.*$", text, re.M)
    return match.group(0) if match else None


def _row_defects(rows, sources: "tuple[str, ...]", label: str) -> "list[str]":
    defects = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != set(ROW_KEYS):
            defects.append(f"{label}[{i}] keys differ from {list(ROW_KEYS)}")
            continue
        if row["source"] not in sources:
            defects.append(f"{label}[{i}].source {row['source']!r} not in {list(sources)}")
        if not isinstance(row["id"], (int, str)) or isinstance(row["id"], bool):
            defects.append(f"{label}[{i}].id is neither a PR number nor a session id")
        if not all(isinstance(row[k], str) and SHA_RE.match(row[k]) for k in ("base", "tip")):
            defects.append(f"{label}[{i}] base or tip is not a 40-hex commit")
        lines = row["production_lines"]
        if not isinstance(lines, int) or isinstance(lines, bool) or lines <= 0:
            defects.append(f"{label}[{i}].production_lines is not a positive integer")
    return defects


def _decision_defects(doc: dict, rows: "list[dict]", open_rows: "list[dict]") -> "list[str]":
    n = len(rows)
    if n >= MIN_N:
        if doc.get("decision") != "derived":
            return [f"decision is {doc.get('decision')!r} but n={n} >= min_n={MIN_N} requires 'derived'"]
        want = candidate(_values(rows))
        return [] if doc.get("bound_value") == want else [
            f"bound_value {doc.get('bound_value')!r}, rows give {want!r}"]
    if doc.get("decision") != "user":
        return [f"decision is {doc.get('decision')!r} but n={n} < min_n={MIN_N} requires 'user'"]
    option, value, decided_on = doc.get("option"), doc.get("user_value"), doc.get("decided_on")
    if value is None and option is None:
        return ["user decision pending: no option recorded"]
    if option not in OPTIONS or isinstance(option, bool):
        return [f"option {option!r} is outside {list(OPTIONS)}"]
    defects = []
    try:
        dt.date.fromisoformat(decided_on)
    except (TypeError, ValueError):
        defects.append(f"decided_on {decided_on!r} is not an ISO date")
    want = {1: candidate(_values(rows)),
            2: candidate(_values(rows) + _values(open_rows)),
            3: PREVIOUS_BOUND}.get(option)
    if option == 4:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            defects.append(f"user_value {value!r} is not a positive integer for option 4")
    elif value != want:
        defects.append(f"user_value {value!r}, option {option} gives {want!r}")
    if doc.get("bound_value") != value:
        defects.append(f"bound_value {doc.get('bound_value')!r} is not user_value {value!r}")
    return defects


def _shown(value) -> str:
    return "none" if value is None else str(value)


def _decision_text(doc: dict) -> str:
    return "derived" if doc["decision"] == "derived" else f"user decision {doc.get('decided_on')}"


def rationale_words(doc: dict) -> "tuple[str, ...]":
    pops, sens = doc["populations"], doc["sensitivity"]
    return (REL_PATH, doc["inputs_sha256"], doc["formula"],
            f"A n={pops['A']['n']}", f"B n={pops['B']['n']}", f"P n={pops['P']['n']}", _decision_text(doc),
            f"p90(A) {_shown(sens['p90_A'])}", f"p90(B) {_shown(sens['p90_B'])}",
            f"p90(A plus open PRs) {_shown(sens['p90_A_plus_open_rows'])}")


def basis_text(doc: dict) -> str:
    pops, sens = doc["populations"], doc["sensitivity"]
    return (f"Basis: {doc['formula']} over the hand-written production-line size of past accepted changes in "
            f"`{REL_PATH}` (A n={pops['A']['n']}, B n={pops['B']['n']}, P n={pops['P']['n']}; decision "
            f"{_decision_text(doc)}; value {doc['bound_value']}; inputs_sha256 `{doc['inputs_sha256']}`; "
            f"p90(A) {_shown(sens['p90_A'])}, p90(B) {_shown(sens['p90_B'])}, "
            f"p90(A plus open PRs) {_shown(sens['p90_A_plus_open_rows'])}). "
            f"Regenerate with `python3 scripts/inline-size-calibrate.py --snapshot DIR`; verify with `--check`.")


def check(path: Path, root: Path = REPO_ROOT) -> "list[str]":
    """Differences between the file, a re-derivation from its own rows, config.md and the
    config-rationale.md row; raises Malformed on unreadable input."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        config = (root / "config.md").read_text(encoding="utf-8")
        rationale = (root / "docs" / "operations" / "config-rationale.md").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise Malformed(f"unreadable: {exc}")
    if not isinstance(doc, dict):
        raise Malformed("not an object")
    for key in ("rule", "formula", "min_n", "n", "rows", "inputs_sha256", "populations", "skipped",
                "sensitivity", "decision", "bound_value"):
        if key not in doc:
            raise Malformed(f"missing key {key!r}")
    sens = doc["sensitivity"]
    if not isinstance(doc["rows"], list) or not isinstance(sens, dict) or not isinstance(sens.get("open_rows"), dict) \
            or not isinstance(sens["open_rows"].get("rows"), list):
        raise Malformed("rows or sensitivity.open_rows are not lists")
    rows, open_rows = doc["rows"], sens["open_rows"]["rows"]
    diffs = _row_defects(rows, ("A", "B"), "rows") + _row_defects(open_rows, ("open",), "open_rows")
    if diffs:
        return diffs
    for field, want in (("rule", RULE), ("formula", FORMULA), ("min_n", MIN_N)):
        if doc[field] != want:
            diffs.append(f"{field} {doc[field]!r} differs from the script's {want!r}")
    if doc["n"] != len(rows):
        diffs.append(f"n {doc['n']!r} is not the row count {len(rows)}")
    if doc["inputs_sha256"] != rows_digest(rows):
        diffs.append("inputs_sha256 does not match the rows")
    if sens["open_rows"].get("sha256") != rows_digest(open_rows):
        diffs.append("open_rows sha256 does not match the open rows")
    populations, want_sens = derived_figures(rows, open_rows)
    if doc["populations"] != populations:
        diffs.append("populations differ from the rows")
    for field, want in want_sens.items():
        if sens.get(field) != want:
            diffs.append(f"sensitivity.{field}: file {sens.get(field)!r}, rows give {want!r}")
    for pop in ("A", "B"):
        block = (doc["skipped"] or {}).get(pop)
        if not isinstance(block, dict) or block.get("count") != sum((block.get("reasons") or {}).values()):
            diffs.append(f"skipped[{pop}].count is not the sum of its reasons")
    diffs += _decision_defects(doc, rows, open_rows)
    bound = doc["bound_value"]
    config_row = _config_row(config, KEY)
    if config_row is None or not re.match(rf"^\| `{KEY}` \| `{re.escape(str(bound))}` \|", config_row):
        diffs.append(f"config.md {KEY} value is not {bound!r}")
    elif REL_PATH not in config_row:
        diffs.append(f"config.md {KEY} row does not name {REL_PATH}")
    rationale_row = _config_row(rationale, KEY)
    if rationale_row is None:
        diffs.append(f"config-rationale.md has no {KEY} row")
    else:
        if not re.match(rf"^\| `{KEY}` \| `{re.escape(str(bound))}` \|", rationale_row):
            diffs.append(f"config-rationale.md {KEY} value is not {bound!r}")
        for word in rationale_words(doc):
            if not re.search(rf"(?<![\w.]){re.escape(word)}(?!\d)", rationale_row):
                diffs.append(f"config-rationale.md {KEY} row lacks {word!r}")
    return diffs


def recompute(path: Path, repo: Path = REPO_ROOT) -> "list[str]":
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        groups = [("rows", doc["rows"]), ("open_rows", doc["sensitivity"]["open_rows"]["rows"])]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Malformed(f"unreadable: {exc}")
    diffs = []
    for label, rows in groups:
        for row in rows:
            number = row["id"] if row["source"] in ("A", "open") else None
            if not (ensure_commit(repo, row["tip"], number) and commit_exists(repo, row["base"])):
                diffs.append(f"{label} {row['source']}:{row['id']}: base or tip not in the object store")
                continue
            lines = measure_range(repo, row["base"], row["tip"]).production_lines
            if lines != row["production_lines"]:
                diffs.append(f"{label} {row['source']}:{row['id']}: file {row['production_lines']}, git gives {lines}")
    return diffs


def _gh_prs(state: str, repo: Path) -> "list[dict]":
    done = _run(["gh", "pr", "list", "--state", state, "--limit", "1000", "--json",
                 "number,headRefName,headRefOid,mergeCommit,mergedBy,author"], repo)
    if done.returncode != 0:
        raise SystemExit(f"gh pr list --state {state}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, help="copy of the agentctl state directory")
    parser.add_argument("--calibration", "--out", type=Path, help="the calibration JSON, read or written")
    parser.add_argument("--owner")
    parser.add_argument("--option", type=int)
    parser.add_argument("--user-value", type=int)
    parser.add_argument("--decided-on")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--recompute", action="store_true")
    parser.add_argument("--basis", action="store_true")
    parser.add_argument("--funnel", action="store_true", help="print the population-B filter counts for --snapshot")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repository holding config.md, the git objects and the default calibration path")
    args = parser.parse_args(argv)
    root = args.root
    if args.funnel:
        if not args.snapshot:
            parser.error("--funnel needs --snapshot DIR")
        print(json.dumps(b_funnel(root, args.snapshot), sort_keys=True))
        return 0
    path = args.calibration or root / REL_PATH
    if args.check or args.recompute or args.basis:
        try:
            if args.basis:
                print(basis_text(json.loads(path.read_text(encoding="utf-8"))))
                return 0
            diffs = recompute(path, root) if args.recompute else check(path, root)
        except (Malformed, OSError, ValueError, KeyError) as exc:
            print(f"malformed: {exc}", file=sys.stderr)
            return 2
        for diff in diffs:
            print(diff, file=sys.stderr)
        if not diffs:
            print("ok")
        return 1 if diffs else 0
    if not args.snapshot:
        parser.error("--snapshot DIR, --check, --recompute or --basis is required")
    decision = None
    if args.option is not None:
        decision = {"option": args.option, "user_value": args.user_value, "decided_on": args.decided_on}
    doc = build(root, args.snapshot, _gh_prs("merged", root), _gh_prs("open", root),
                args.owner, decision)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pops = doc["populations"]
    print(f"n={doc['n']} A={pops['A']['n']} B={pops['B']['n']} p90={pops['P']['p90']} "
          f"decision={doc['decision']} bound_value={doc['bound_value']} skipped={json.dumps(doc['skipped'])} "
          f"dedup={doc['deduplicated_b_rows']} open_rows={len(doc['sensitivity']['open_rows']['rows'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
