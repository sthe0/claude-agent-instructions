"""Evaluate the method-statement rule against labelled sets: `run` judges a set N times and records
the replies; `check` turns a recorded result into a pass/fail against thresholds.

Difficulty removed: a reviewer rule is only as good as the share of planted defects it finds and the
share of clean statements it leaves alone, and a recorded result is only evidence for the set, the
commit and the instructions it was produced with. `check` therefore refuses a result whose pins differ
from the ones asked for, before it reads a single verdict.

    method-statement-eval.py run   --kind orders|stage-briefs|plan-reviews|small-tasks|length
                                   --set SET.json --runs N --out RESULT.json [--config-root DIR] [--commit SHA]
    method-statement-eval.py check --kind orders|stage-briefs|plan-reviews|small-tasks|link-reading|length
                                   --results RESULT.json [thresholds] [pins]

Labelled set (orders, stage-briefs, plan-reviews): {"items": [{"id", "label": method|how_built|neither,
"text"}]}. Each item is judged in the place of its kind (customer side, brief, stage description).
`check` reads {"results": [{"id", "label", "reported": [verdict per run]}]}; a trial is found only when
the reported verdict is the item's own label, and a neither item is flagged on any other verdict.

small-tasks: items {"id", "text", "expected": small|substantive}; `run` asks the host model, with the
CLAUDE.md under --config-root in the prompt, and `check` compares the share that agree with the expected
class and refuses a result recorded under another root or other instruction digests (those of CLAUDE.md
and of every file under the root that it imports, transitively).
link-reading: {"results": [{"id", "required", "read"}]}; every item that required reading a linked record
must have read it. Recorded from a reviewer session, so there is no `run` for it.
length: {"runs": [{"orders": [{"id", "included", "old_chars", "new_chars"}]}]}; an order whose new rendering
is longer than the old by more than --tolerance-chars passes only when --excess-readings holds an
`irreducible` reading of it.

Pins, accepted by every `check`: --set (the result's recorded set sha256 must equal the file's, and its
rows must be the set's items with the set's labels), --set-digest-file (sha256sum format, looked up by
the set's base name), --commit. A result whose recorded `kind` is not the `--kind` asked for is refused.

Exit codes: 0 met; 1 below a threshold, a pin differs, or an excess is unread or removable; 2 usage
error or unreadable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import method_statement  # noqa: E402

LABELLED_KINDS = {"orders": method_statement.CUSTOMER_SIDE, "stage-briefs": method_statement.BRIEF,
                  "plan-reviews": method_statement.STAGE_DESCRIPTION}
DEFECT_LABELS = (method_statement.METHOD, method_statement.HOW_BUILT)
INSTRUCTION_FILES = ("CLAUDE.md",)
AGENT_HOME_PREFIX = "~/.claude-agent/"
IMPORT_TOKEN = re.compile(r"(?:^|(?<=\s))@(\S+)")
SMALL, SUBSTANTIVE = "small", "substantive"
REPO_ROOT = SCRIPTS_DIR.parent


class Usage(Exception):
    """Exit 2: a bad invocation or an input that cannot be read."""


class Unmet(Exception):
    """Exit 1: the result does not meet what was asked of it."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Usage(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise Usage(f"{path}: expected a JSON object")
    return data


def head_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Usage(f"cannot read the HEAD commit (pass --commit): {exc}") from exc
    return out.stdout.strip()


def _imported_files(config_root: Path, text: str) -> list[str]:
    """The files under `config_root` that `text` imports with `@<path>` (a path relative to the root, or
    under the agent home `~/.claude-agent/`); an `@` token that names no file there is not an import."""
    found = []
    for token in IMPORT_TOKEN.findall(text):
        relative = token[len(AGENT_HOME_PREFIX):] if token.startswith(AGENT_HOME_PREFIX) else token
        path = (config_root / relative).resolve()
        if path.is_file() and path.is_relative_to(config_root.resolve()):
            found.append(str(path.relative_to(config_root.resolve())))
    return found


def instruction_digests(config_root: Path) -> dict:
    """The digest of each instruction file under `config_root`: `CLAUDE.md` and, transitively, every
    file it imports there."""
    digests: dict = {}
    pending = [name for name in INSTRUCTION_FILES if (config_root / name).is_file()]
    while pending:
        name = pending.pop()
        if name in digests:
            continue
        digests[name] = sha256_file(config_root / name)
        text = (config_root / name).read_text(encoding="utf-8", errors="replace")
        pending.extend(_imported_files(config_root, text))
    return dict(sorted(digests.items()))


# --- run -------------------------------------------------------------------------------------------


def _items(path) -> list[dict]:
    items = load_json(path).get("items")
    if not isinstance(items, list) or not items:
        raise Usage(f"{path}: no items")
    return items


def _small_task_class(text: str, claude_md: str, runner) -> str:
    prompt = (
        "Below are an agent's global instructions, then a task. Classify the task by the instructions' "
        'weight classes as "small" (a small change) or "substantive". Answer with one JSON object '
        '{"class": "small" | "substantive"} and nothing else.\n\nInstructions:\n'
        f"{claude_md}\n\nTask:\n{text}\n")
    from agentctl import grounding_judge as GJ

    def parse(reply: str) -> str:
        value = GJ._reply_object(reply).get("class")
        if value not in (SMALL, SUBSTANTIVE):
            raise ValueError(f"class {value!r} is neither {SMALL} nor {SUBSTANTIVE}")
        return value

    try:
        return GJ._ask("small_task_class", prompt, runner, parse)
    except GJ.JudgeUnavailable:
        return method_statement.COULD_NOT_READ


def run_kind(args, runner) -> dict:
    set_path = Path(args.set)
    items = _items(set_path)
    runner = runner if runner is not None else method_statement.default_runner()
    config_root = Path(args.config_root) if args.config_root else None
    result = {"kind": args.kind, "set_name": set_path.name, "set_sha256": sha256_file(set_path),
              "commit": args.commit or head_commit(), "runs": args.runs}
    if config_root is not None:
        digests = instruction_digests(config_root)
        if not digests:
            raise Usage(f"no instruction file ({', '.join(INSTRUCTION_FILES)}) under {config_root}")
        result["config_root"] = str(config_root)
        result["instruction_digests"] = digests
    if args.kind in LABELLED_KINDS:
        place = LABELLED_KINDS[args.kind]
        result["results"] = [
            {"id": it["id"], "label": it["label"],
             "reported": [method_statement.judge(it["text"], place, runner=runner)["verdict"]
                          for _ in range(args.runs)]}
            for it in items]
    elif args.kind == "small-tasks":
        if config_root is None:
            raise Usage("run --kind small-tasks needs --config-root")
        claude_md = (config_root / "CLAUDE.md").read_text(encoding="utf-8")
        result["results"] = [
            {"id": it["id"], "expected": it["expected"],
             "reported": [_small_task_class(it["text"], claude_md, runner) for _ in range(args.runs)]}
            for it in items]
    elif args.kind == "length":
        orders = [{"id": it["id"], "included": bool(it.get("included", True)),
                   "old_chars": len(it["old"]), "new_chars": len(it["new"])} for it in items]
        result["runs"] = [{"orders": orders} for _ in range(args.runs)]
    else:
        raise Usage(f"--kind {args.kind} has no run mode; its results are recorded from a reviewer session")
    return result


# --- check -----------------------------------------------------------------------------------------


def _digest_entry(digest_file: Path, name: str) -> str:
    try:
        lines = Path(digest_file).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise Usage(f"cannot read {digest_file}: {exc}") from exc
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == name:
            return parts[0]
    raise Unmet(f"{digest_file} has no digest line for {name}")


def check_pins(results: dict, args) -> None:
    """Refuse a result recorded for another set, another set file or another commit."""
    recorded = results.get("set_sha256")
    if args.set:
        actual = sha256_file(Path(args.set))
        if recorded != actual:
            raise Unmet(f"the result's recorded set digest {recorded} is not the sha256 {actual} of {args.set}")
    if args.set_digest_file:
        name = Path(args.set).name if args.set else results.get("set_name")
        if not name:
            raise Usage("--set-digest-file needs --set or a result that records its set name")
        entry = _digest_entry(Path(args.set_digest_file), name)
        if recorded != entry:
            raise Unmet(f"the result's recorded set digest {recorded} differs from {entry} in {args.set_digest_file}")
        if args.set and sha256_file(Path(args.set)) != entry:
            raise Unmet(f"{args.set} no longer has the digest {entry} listed in {args.set_digest_file}")
    if args.commit:
        got = results.get("commit") or ""
        if not (got == args.commit or (len(args.commit) >= 7 and got.startswith(args.commit))):
            raise Unmet(f"the result was recorded at commit {got or 'none'}, not {args.commit}")


def _share(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def _runs_floor(rows: list, min_runs: int) -> None:
    short = [r["id"] for r in rows if len(r["reported"]) < min_runs]
    if short:
        raise Unmet(f"fewer than {min_runs} runs for: {', '.join(map(str, short))}")


def check_labelled(results: dict, args, out: list) -> None:
    if args.min_found is None or args.max_flagged is None:
        raise Usage(f"--kind {args.kind} needs --min-found and --max-flagged")
    rows = results.get("results")
    if not isinstance(rows, list) or not rows:
        raise Usage("the result holds no items")
    allowed = (*DEFECT_LABELS, method_statement.NEITHER)
    for r in rows:
        if r.get("label") not in allowed or not isinstance(r.get("reported"), list):
            raise Usage(f"item {r.get('id')!r}: label must be one of {', '.join(allowed)} with a reported list")
    _runs_floor(rows, args.min_runs)
    problems = []
    for label in DEFECT_LABELS:
        trials = [v for r in rows if r["label"] == label for v in r["reported"]]
        if not trials:
            problems.append(f"no items labelled {label}")
            continue
        share = _share(sum(v == label for v in trials), len(trials))
        out.append(f"found {label}: {share:.3f} of {len(trials)} trials (min {args.min_found})")
        if share < args.min_found:
            problems.append(f"found share of {label} {share:.3f} is below {args.min_found}")
    clean = [v for r in rows if r["label"] == method_statement.NEITHER for v in r["reported"]]
    if not clean:
        problems.append("no items labelled neither")
    else:
        share = _share(sum(v != method_statement.NEITHER for v in clean), len(clean))
        out.append(f"flagged neither: {share:.3f} of {len(clean)} trials (max {args.max_flagged})")
        if share > args.max_flagged:
            problems.append(f"flagged share {share:.3f} is above {args.max_flagged}")
    if problems:
        raise Unmet("; ".join(problems))


def check_small_tasks(results: dict, args, out: list) -> None:
    if args.config_root is None or args.min_share is None or args.min_items is None:
        raise Usage("--kind small-tasks needs --config-root, --min-share and --min-items")
    root = Path(args.config_root)
    recorded_root = results.get("config_root")
    if not recorded_root or Path(recorded_root).resolve() != root.resolve():
        raise Unmet(f"the result was recorded under config root {recorded_root}, not {args.config_root}")
    recorded = results.get("instruction_digests")
    if not isinstance(recorded, dict) or not recorded:
        raise Unmet("the result records no instruction digests")
    current = instruction_digests(root)
    differing = sorted(name for name in {*recorded, *current} if recorded.get(name) != current.get(name))
    if differing:
        raise Unmet(f"instruction file {', '.join(differing)} under {args.config_root} differs from the one "
                    "the result was recorded with")
    rows = results.get("results")
    if not isinstance(rows, list):
        raise Usage("the result holds no items")
    if len(rows) < args.min_items:
        raise Unmet(f"{len(rows)} items, fewer than {args.min_items}")
    _runs_floor(rows, args.min_runs)
    trials = [(r["expected"], v) for r in rows for v in r["reported"]]
    share = _share(sum(e == v for e, v in trials), len(trials))
    out.append(f"small-task class agreement: {share:.3f} of {len(trials)} trials (min {args.min_share})")
    if share < args.min_share:
        raise Unmet(f"agreement {share:.3f} is below {args.min_share}")


def check_link_reading(results: dict, args, out: list) -> None:
    rows = results.get("results")
    if not isinstance(rows, list) or not rows:
        raise Usage("the result holds no items")
    required = [r for r in rows if r.get("required")]
    if not required:
        raise Unmet("no item required reading a linked record")
    unread = [str(r.get("id")) for r in required if not r.get("read")]
    out.append(f"link reading: {len(required) - len(unread)} of {len(required)} required links read")
    if unread:
        raise Unmet(f"required link not read: {', '.join(unread)}")


def check_length(results: dict, args, out: list) -> None:
    if args.tolerance_chars is None or args.excess_readings is None or args.min_orders is None:
        raise Usage("--kind length needs --tolerance-chars, --excess-readings and --min-orders")
    runs = results.get("runs")
    if not isinstance(runs, list) or not runs:
        raise Usage("the result holds no runs")
    if len(runs) < args.min_runs:
        raise Unmet(f"{len(runs)} runs, fewer than {args.min_runs}")
    readings = {r.get("id"): r for r in load_json(args.excess_readings).get("readings", [])}
    included, excess = {}, {}
    for run in runs:
        for o in run.get("orders", []):
            included[o["id"]] = included.get(o["id"], True) and bool(o.get("included"))
            over = o["new_chars"] - o["old_chars"]
            if over > args.tolerance_chars:
                excess[o["id"]] = max(excess.get(o["id"], 0), over)
    count = sum(1 for v in included.values() if v)
    out.append(f"length: {count} orders included, {len(excess)} longer than the old rendering "
               f"by more than {args.tolerance_chars} chars")
    problems = []
    if count < args.min_orders:
        problems.append(f"{count} orders included, fewer than {args.min_orders}")
    for order_id, over in sorted(excess.items()):
        reading = readings.get(order_id)
        if reading is None:
            problems.append(f"excess of {over} chars in {order_id} has no reading")
        elif reading.get("reading") != "irreducible":
            problems.append(f"excess of {over} chars in {order_id} is read as {reading.get('reading')!r}, not irreducible")
    if problems:
        raise Unmet("; ".join(problems))


CHECKS = {"orders": check_labelled, "stage-briefs": check_labelled, "plan-reviews": check_labelled,
          "small-tasks": check_small_tasks, "link-reading": check_link_reading, "length": check_length}


def check_set_rows(results: dict, args) -> None:
    """Refuse a result whose rows are not the set's items with the set's labels: a dropped, an added or a
    relabelled row would turn the thresholds into a statement about another set."""
    set_items = {it.get("id"): it for it in _items(Path(args.set))}
    if args.kind == "length":
        runs = results.get("runs")
        row_sets = [(f"run {i}", run.get("orders", [])) for i, run in enumerate(runs or [], start=1)]
    else:
        row_sets = [("results", results.get("results") or [])]
    for where, rows in row_sets:
        ids = [r.get("id") for r in rows]
        if len(ids) != len(set(ids)) or set(ids) != set(set_items):
            missing = sorted(map(str, set(set_items) - set(ids)))
            extra = sorted(map(str, set(ids) - set(set_items)))
            raise Unmet(f"{where} are not the items of {args.set} (missing {missing}, not in the set {extra})")
        for r in rows:
            item = set_items[r["id"]]
            for key in ("label", "expected"):
                if key in item and r.get(key) != item[key]:
                    raise Unmet(f"{where} item {r['id']} has {key} {r.get(key)!r}, the set has {item[key]!r}")
            if args.kind == "length" and bool(r.get("included")) != bool(item.get("included", True)):
                raise Unmet(f"{where} item {r['id']} differs from the set in `included`")


def run_check(args) -> list:
    results = load_json(args.results)
    if results.get("kind") != args.kind:
        raise Unmet(f"the result records kind {results.get('kind')!r}, not {args.kind!r}")
    check_pins(results, args)
    if args.set:
        check_set_rows(results, args)
    out: list = []
    CHECKS[args.kind](results, args, out)
    return out


# --- command line ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="method-statement-eval.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="judge a labelled set N times and record the replies")
    run.add_argument("--kind", required=True, choices=sorted(CHECKS))
    run.add_argument("--set", required=True)
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--out", required=True)
    run.add_argument("--config-root")
    run.add_argument("--commit")
    check = sub.add_parser("check", help="compare a recorded result with thresholds and pins")
    check.add_argument("--kind", required=True, choices=sorted(CHECKS))
    check.add_argument("--results", required=True)
    check.add_argument("--min-found", type=float)
    check.add_argument("--max-flagged", type=float)
    check.add_argument("--min-runs", type=int, default=1)
    check.add_argument("--min-share", type=float)
    check.add_argument("--min-items", type=int)
    check.add_argument("--config-root")
    check.add_argument("--tolerance-chars", type=int)
    check.add_argument("--excess-readings")
    check.add_argument("--min-orders", type=int)
    check.add_argument("--set")
    check.add_argument("--set-digest-file")
    check.add_argument("--commit")
    return parser


def main(argv=None, *, runner=None, out=None) -> int:
    out = out if out is not None else sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            if args.runs < 1:
                raise Usage("--runs must be at least 1")
            result = run_kind(args, runner)
            Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"recorded {args.runs} runs of {args.kind} in {args.out}", file=out)
            return 0
        for line in run_check(args):
            print(line, file=out)
        print(f"met: {args.kind}", file=out)
        return 0
    except Unmet as exc:
        print(f"not met: {exc}", file=out)
        return 1
    except Usage as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return 2
    except (KeyError, TypeError, AttributeError) as exc:
        print(f"usage: unreadable input ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
