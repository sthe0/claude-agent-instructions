#!/usr/bin/env python3
"""Drive a topological plan review: one thinker spawn per base-service pair, level by level.

Difficulty removed: a plan too large for one whole-plan review spawn has no single
command that walks every reliance pair, records each verdict and composes the pass; the
operator would hand-run `plan-review-walk`, one `spawn-specialist.py --review-topo`
per pair and one `plan-review` per verdict, and could neither see what the review cost
nor whether the reviewers ever pulled the service file.

The driver orchestrates and never judges. Order and readiness come from
`agentctl plan-review-walk`, verdicts are recorded and composed by `agentctl`, and the
only text it parses is the fixed protocol tokens of a pair review. Cost, duration and
pull counts are printed as telemetry; nothing is gated on them.

  plan-review-topological.py --session <sid> --plan <plan.toml>
      [--complexity low|medium|high | --model <m>] [--dry-run] [--no-early-stop]
      [--parallel <k>] [--pairs <p,...>] [--ledger <path>]

Output lines: TOPO-PAIR, TOPO-REFUSED, TOPO-CURRENT, TOPO-WAITING, TOPO-DRY,
TOPO-SUMMARY, COMPOSE. Exit codes: 0 composed pass (or scoped run whose pairs passed),
1 blocked (a revise, stale, missing or waiting pair remains), 2 a TOPO-REFUSED occurred
anywhere in the run or a usage error, 3 nothing to review (every pair already current).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import plan  # noqa: E402 - protocol tokens are read as plan.X at call time
from agentctl.render import topo_pair_view_dirname  # noqa: E402
from lib import planner_plan_check  # noqa: E402
from lib.config_root import agentctl_topo_units_dir  # noqa: E402

SPAWNER = SCRIPTS_DIR / "spawn-specialist.py"
AGENTCTL_CLI = SCRIPTS_DIR / "agentctl-cli.py"
COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"

SATISFIED = ("current", "override")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
NUMBERING_RE = re.compile(r"^\d+[.)]\s*")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]\s|\d+[.)]\s)")
SUMMARY_MARKER_RE = re.compile(r"\bmarker=(\S+)")
SUMMARY_COST_RE = re.compile(r"\bcost_usd=([0-9.]+)")
SUMMARY_DURATION_RE = re.compile(r"\bduration_ms=(\d+)")
DRY_CHARS_RE = re.compile(r"^# stdin: <prompt (\d+) chars>", re.MULTILINE)
DRY_VIEW_RE = re.compile(r"^TOPO-VIEW: \S+ files=(\S*)", re.MULTILINE)
CEILING_HINT = "exits: split the pair's plan content or dispatch this pair by hand (user override)"


class TopoRefused(Exception):
    """A pair whose spawn or verdict must not be recorded; the message is the reason."""


@dataclass
class ParsedReview:
    verdict: str
    digest: str
    concerns: list[str]


@dataclass
class Launched:
    rc: int
    stdout: str
    stderr: str
    log_offset: int
    plan_sha: str


@dataclass
class PairResult:
    pair: str
    verdict: str
    digest: str
    cost_usd: "float | None"
    duration_ms: "int | None"
    pulls: "int | None"
    transcript: "str | None"


def emit(line: str) -> None:
    print(line, flush=True)


# --- engine and spawner subprocesses (the seams the tests replace) -------------


def spawn_pair(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(SPAWNER), *argv], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_agentctl(argv: list[str], env: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(AGENTCTL_CLI), *argv], capture_output=True, text=True, env=env
    )
    try:
        directive = json.loads(proc.stdout)
    except ValueError:
        directive = {"ok": False, "detail": (proc.stderr or proc.stdout).strip()[:500], "data": {}}
    return directive


# --- spawn argv ----------------------------------------------------------------


def done_criterion(pair: str) -> str:
    conditions = f"{plan.CONDITION_MARKERS[0].rstrip(':')}-{plan.CONDITION_MARKERS[-1].rstrip(':')}"
    return (
        f"Review the base-service pair {pair}: decide conditions {conditions} for this one pair "
        f"and answer with the {plan.REVIEW_MARKER} block your starting prompt specifies."
    )


def build_spawn_argv(args, pair: str, plan_path: str, *, dry_run: bool) -> list[str]:
    selector = ["--model", args.model] if args.model else ["--complexity", args.complexity or "high"]
    argv = [
        "--kind", "thinker", *selector, "--effort", "high", "--plan-brief",
        "--review-topo", pair, "--plan", plan_path,
        "--done-criterion", done_criterion(pair), "--criterion-type", "acceptance-review",
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


def record_argv(sid: str, plan_path: str, pair: str, verdict: str, digest: str,
                concerns: list[str]) -> list[str]:
    argv = [
        "plan-review", "--session", sid, "--target", plan_path, "--scope", f"topo:{pair}",
        "--reviewer", "thinker", "--verdict", verdict, "--plan-digest", digest,
    ]
    for concern in concerns:
        argv += ["--concern", concern]
    return argv


# --- parsing the spawner's output ----------------------------------------------


def _lead_clean(raw: str) -> str:
    text = raw.lstrip(planner_plan_check._DECORATION_CHARS)
    text = NUMBERING_RE.sub("", text)
    return text.lstrip(planner_plan_check._DECORATION_CHARS)


def _clean_value(text: str) -> str:
    trimmed = text.strip(planner_plan_check.CONCERN_VALUE_DECORATION_CHARS)
    return re.sub(r"\*+", "", trimmed.replace("`", ""))


def _classify(raw: str) -> tuple[str, str]:
    """(kind, value) of one raw line; kind is review, verdict, digest, condition or other.
    A condition's value is its recorded concern text."""
    strip = planner_plan_check.strip_decoration
    cleaned = strip(NUMBERING_RE.sub("", strip(raw)))
    for kind, marker in (
        ("review", plan.REVIEW_MARKER),
        ("verdict", plan.VERDICT_MARKER),
        ("digest", plan.PLAN_DIGEST_MARKER),
    ):
        if cleaned.startswith(marker):
            return kind, strip(cleaned[len(marker):])
    for marker in plan.CONDITION_MARKERS:
        if cleaned.startswith(marker):
            leading = _lead_clean(raw)
            return "condition", f"{marker} {_clean_value(leading[len(marker):])}".rstrip()
    return "other", ""


def strip_envelope(stdout: str) -> str:
    """The child's original output: canonicalize() prepends a bare marker line and
    Digest:/Plan: lines up to the first blank line, which are not the child's words."""
    lines = stdout.splitlines()
    if not lines or lines[0].strip() != plan.REVIEW_MARKER:
        return stdout
    for i, line in enumerate(lines[1:], start=1):
        if not line.strip():
            return "\n".join(lines[i + 1:])
        if not line.startswith(("Digest:", "Plan:")):
            return stdout
    return stdout


def parse_review_output(stdout: str) -> ParsedReview:
    lines = [ln for ln in strip_envelope(stdout).splitlines() if ln.strip()]
    classified = [(_classify(ln), ln) for ln in lines]
    anchor = None
    for i, ((kind, _), _) in enumerate(classified):
        if kind == "review" and any(k == "verdict" for (k, _), _ in classified[i + 1:]):
            anchor = i
    if anchor is None:
        raise TopoRefused(f"no {plan.REVIEW_MARKER} block with a {plan.VERDICT_MARKER} line")
    block = classified[anchor + 1:]
    verdict_at = next((i for i, ((k, _), _) in enumerate(block) if k == "verdict"), None)
    digest_at = next((i for i, ((k, _), _) in enumerate(block) if k == "digest"), None)
    if digest_at is None:
        raise TopoRefused(f"no {plan.PLAN_DIGEST_MARKER} line in the review block")
    verdict = block[verdict_at][0][1]
    digest = block[digest_at][0][1]
    if verdict not in ("pass", "revise"):
        raise TopoRefused(f"verdict must be pass or revise, got {verdict!r}")
    if not DIGEST_RE.fullmatch(digest):
        raise TopoRefused(f"{plan.PLAN_DIGEST_MARKER} is not 64 lowercase hex characters: {digest!r}")
    region = block[max(verdict_at, digest_at) + 1:]
    concerns = _parse_concerns(region, verdict) if verdict == "revise" else []
    return ParsedReview(verdict, digest, concerns)


def _parse_concerns(region, verdict: str) -> list[str]:
    concerns: list[str] = []
    before_close = []
    for entry in region:
        if entry[0][0] == "review":
            break
        before_close.append(entry)
    if not any(kind == "condition" for (kind, _), _ in before_close):
        raise TopoRefused("no condition-prefixed concerns")
    for (kind, value), raw in region:
        if raw.strip().startswith("```"):
            continue
        if kind == "review":
            if value and value != verdict:
                raise TopoRefused("marker disagrees with verdict")
            break
        if kind == "condition":
            concerns.append(value)
        elif LIST_ITEM_RE.match(raw) or not concerns:
            raise TopoRefused("unprefixed concern")
        else:
            concerns[-1] = f"{concerns[-1]} {_clean_value(raw.strip())}"
    if not concerns:
        raise TopoRefused("no condition-prefixed concerns")
    return concerns


def read_spawn_result(rc: int, stdout: str, stderr: str) -> ParsedReview:
    if rc != 0:
        detail = " ".join(
            " ".join(ln.split()) for ln in stderr.splitlines() if ln.strip().startswith("error:")
        ) or " ".join(stderr.split())[-300:]
        raise TopoRefused(f"spawner exited {rc}: {detail} ({CEILING_HINT})")
    if stdout.lstrip().startswith("MALFORMED:"):
        raise TopoRefused("spawner rejected the child's output (MALFORMED)")
    summary = _summary_line(stderr)
    found = SUMMARY_MARKER_RE.search(summary) if summary else None
    if found is None:
        raise TopoRefused("spawner summary carries no marker= field")
    expected = plan.REVIEW_MARKER.rstrip(":")
    if found.group(1) != expected:
        raise TopoRefused(f"spawner marker={found.group(1)} is not {expected}")
    return parse_review_output(stdout)


def _summary_line(stderr: str) -> "str | None":
    return next((ln for ln in stderr.splitlines() if ln.startswith("spawn-specialist: kind=")), None)


def stderr_transcript(stderr: str) -> "str | None":
    for line in stderr.splitlines():
        if line.startswith("spawn-specialist: transcript="):
            return line.split("=", 1)[1].strip()
    return None


# --- cost and pull telemetry ---------------------------------------------------


def cost_log_size() -> int:
    try:
        return COST_LOG.stat().st_size
    except OSError:
        return 0


def _cost_rows(offset: int) -> list[dict]:
    try:
        data = COST_LOG.read_bytes()[offset:]
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") != "refused":
            rows.append(row)
    return rows


def select_cost_row(pair: str, sha: str, transcript: "str | None", offset: int) -> "dict | None":
    if transcript and transcript != "<not-found-within-10s>":
        exact = [r for r in _cost_rows(0) if r.get("transcript_path") == transcript]
        if exact:
            return exact[-1]
    window = [r for r in _cost_rows(offset) if r.get("review_pair") == pair and r.get("plan_sha256") == sha]
    if len(window) > 1:
        raise TopoRefused("ambiguous cost row: more than one spawn row for this pair and plan version")
    return window[0] if window else None


def _under(path: str, root: str) -> bool:
    norm = os.path.normpath(path)
    return norm == root or norm.startswith(root + os.sep)


def _names(text: str, root: str) -> bool:
    return re.search(rf"(?<![\w.\-]){re.escape(root)}(?![\w.\-])", text) is not None


def _touches(tool: str, tool_input: dict, root: str) -> bool:
    if tool == "Read":
        return _under(str(tool_input.get("file_path", "")), root)
    if tool in ("Grep", "Glob"):
        return _under(str(tool_input.get("path", "")), root) or _names(str(tool_input.get("pattern", "")), root)
    if tool == "Bash":
        return _names(str(tool_input.get("command", "")), root)
    return False


def count_pulls(transcript: "str | None", view_dir: str) -> "int | None":
    if not transcript or not os.path.isfile(transcript):
        return None
    root = os.path.normpath(view_dir)
    seen: set = set()
    parsed = 0
    try:
        with open(transcript, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        parsed += 1
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            key = block.get("id") or id(block)
            if key not in seen and _touches(block.get("name", ""), block.get("input") or {}, root):
                seen.add(key)
    return len(seen) if parsed or not lines else None


def view_dir_for(pair: str, sha: str) -> str:
    override = os.environ.get("AGENTCTL_TOPO_UNITS_DIR")
    root = Path(override) if override else agentctl_topo_units_dir()
    return str(root / sha / topo_pair_view_dirname(pair))


def pair_telemetry(launched: Launched, pair: str) -> tuple["float | None", "int | None", "int | None", "str | None"]:
    stderr_path = stderr_transcript(launched.stderr)
    row = select_cost_row(pair, launched.plan_sha, stderr_path, launched.log_offset)
    if row is not None:
        cost, duration = row.get("cost_usd"), row.get("duration_ms")
    else:
        summary = _summary_line(launched.stderr) or ""
        cost_found, duration_found = SUMMARY_COST_RE.search(summary), SUMMARY_DURATION_RE.search(summary)
        cost = float(cost_found.group(1)) if cost_found else None
        duration = int(duration_found.group(1)) if duration_found else None
    candidates = [stderr_path, row.get("transcript_path") if row else None]
    transcript = next((c for c in candidates if c and os.path.isfile(c)), None)
    pulls = count_pulls(transcript, view_dir_for(pair, launched.plan_sha))
    return (round(float(cost), 4) if cost is not None else None,
            int(duration) if duration is not None else None, pulls, transcript)


# --- the walk ------------------------------------------------------------------


class Driver:
    def __init__(self, args, plan_path: str, env: dict):
        self.args = args
        self.plan_path = plan_path
        self.env = env
        self.sid = args.session
        self.named = set(args.pairs.split(",")) if args.pairs else None
        self.spawned: set[str] = set()
        self.results: list[PairResult] = []
        self.refused = False
        self.level_revise = False
        self.level_refused = False

    def agentctl(self, verb: str, *extra: str) -> dict:
        return run_agentctl([verb, "--session", self.sid, "--target", self.plan_path, *extra], self.env)

    def read_walk(self) -> dict:
        directive = self.agentctl("plan-review-walk", "--format", "json")
        if not directive.get("ok"):
            raise RuntimeError(f"plan-review-walk failed: {directive.get('detail', '')}")
        return directive["data"]

    def refuse(self, pair: str, reason: str) -> None:
        self.refused = True
        self.level_refused = True
        emit(f"TOPO-REFUSED: pair={pair} {' '.join(reason.split())}")

    def launch(self, pair: str) -> Launched:
        self.spawned.add(pair)
        sha = hashlib.sha256(Path(self.plan_path).read_bytes()).hexdigest()
        offset = cost_log_size()
        rc, out, err = spawn_pair(build_spawn_argv(self.args, pair, self.plan_path, dry_run=False))
        return Launched(rc, out, err, offset, sha)

    def settle(self, pair: str, launched: Launched) -> None:
        try:
            parsed = read_spawn_result(launched.rc, launched.stdout, launched.stderr)
            if parsed.digest != launched.plan_sha:
                raise TopoRefused(
                    f"digest mismatch: reviewer read {parsed.digest}, driver computed {launched.plan_sha}"
                )
            cost, duration, pulls, transcript = pair_telemetry(launched, pair)
        except TopoRefused as exc:
            self.refuse(pair, str(exc))
            return
        self.results.append(PairResult(pair, parsed.verdict, parsed.digest, cost, duration, pulls, transcript))
        emit(
            f"TOPO-PAIR: pair={pair} verdict={parsed.verdict} digest={parsed.digest} "
            f"cost_usd={'NA' if cost is None else f'{cost:.4f}'} "
            f"duration_ms={'NA' if duration is None else duration} "
            f"pulls={'NA' if pulls is None else pulls} transcript={transcript or 'NA'}"
        )
        recorded = run_agentctl(
            record_argv(self.sid, self.plan_path, pair, parsed.verdict, parsed.digest, parsed.concerns),
            self.env,
        )
        if not recorded.get("ok"):
            self.refuse(pair, f"engine refused the record: {recorded.get('detail', '')}")
        elif parsed.verdict == "revise":
            self.level_revise = True

    def run_level(self, rows: list[dict]) -> None:
        batch: list[dict] = []
        for row in rows:
            pair = row["pair"]
            if self.named is not None and pair not in self.named:
                continue
            if pair in self.spawned:
                continue
            if row["status"] in SATISFIED:
                emit(f"TOPO-CURRENT: pair={pair}")
            elif row["ready"] or self.named is not None and pair in self.named or self.args.no_early_stop:
                batch.append(row)
            else:
                emit(f"TOPO-WAITING: pair={pair} waiting={','.join(row['waiting'])}")
        if self.args.parallel <= 1:
            for row in batch:
                self.settle(row["pair"], self.launch(row["pair"]))
            return
        with ThreadPoolExecutor(max_workers=self.args.parallel) as pool:
            launched = list(pool.map(lambda row: self.launch(row["pair"]), batch))
        for row, outcome in zip(batch, launched):
            self.settle(row["pair"], outcome)

    def dry_run(self, walk: dict) -> None:
        for depth, rows in enumerate(walk["levels"]):
            emit(f"level {depth}:")
            for row in rows:
                pair = row["pair"]
                if self.named is not None and pair not in self.named:
                    continue
                if row["status"] in SATISFIED:
                    emit(f"TOPO-CURRENT: pair={pair}")
                    continue
                spawn_cmd = build_spawn_argv(self.args, pair, self.plan_path, dry_run=False)
                emit("  spawn: " + shlex.join([sys.executable, str(SPAWNER), *spawn_cmd]))
                emit("  record: " + shlex.join(
                    [sys.executable, str(AGENTCTL_CLI),
                     *record_argv(self.sid, self.plan_path, pair, "<pass|revise>", "<sha256>", [])]))
                rc, out, err = spawn_pair(build_spawn_argv(self.args, pair, self.plan_path, dry_run=True))
                chars, files = DRY_CHARS_RE.search(out), DRY_VIEW_RE.search(out)
                if rc != 0 or chars is None or files is None:
                    emit(f"TOPO-REFUSED: pair={pair} spawner --dry-run exited {rc} ({CEILING_HINT})")
                    continue
                emit(f"TOPO-DRY: pair={pair} prompt_chars={chars.group(1)} view_files={files.group(1)}")

    def compose(self) -> str:
        directive = self.agentctl("plan-review-compose")
        if directive.get("ok"):
            emit("COMPOSE: pass")
            return "pass"
        failing = (directive.get("data") or {}).get("failing")
        emit("COMPOSE: blocked " + (",".join(failing) if failing else " ".join(str(directive.get("detail", "")).split())))
        return "blocked"

    def summary(self) -> None:
        known_cost = [r.cost_usd for r in self.results if r.cost_usd is not None]
        known_duration = [r.duration_ms for r in self.results if r.duration_ms is not None]
        pulled = [r for r in self.results if r.pulls is not None and r.pulls >= 1]
        emit(
            f"TOPO-SUMMARY: pairs={len(self.results)} cost_usd={sum(known_cost):.4f} "
            f"duration_ms={sum(known_duration)} pairs_pulled={len(pulled)} "
            f"cost_unknown={sum(r.cost_usd is None for r in self.results)} "
            f"pulls_unknown={sum(r.pulls is None for r in self.results)}"
        )

    def run(self, parser: argparse.ArgumentParser) -> int:
        try:
            walk = self.read_walk()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        known = {row["pair"] for level in walk["levels"] for row in level}
        if self.named is not None and not self.named <= known:
            parser.error(f"--pairs names unknown pair(s): {', '.join(sorted(self.named - known))}")
        if self.args.dry_run:
            self.dry_run(walk)
            return 0
        in_scope = [row for level in walk["levels"] for row in level
                    if self.named is None or row["pair"] in self.named]
        all_current = all(row["status"] in SATISFIED for row in in_scope)
        stopped_by_refusal = False
        try:
            for depth in range(len(walk["levels"])):
                if depth:
                    walk = self.read_walk()
                self.level_revise = self.level_refused = False
                self.run_level(walk["levels"][depth] if depth < len(walk["levels"]) else [])
                if (self.level_revise or self.level_refused) and not self.args.no_early_stop:
                    stopped_by_refusal = self.level_refused
                    break
            composed = self.finish(stopped_by_refusal)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        self.summary()
        return self.exit_code(composed, all_current)

    def finish(self, stopped_by_refusal: bool) -> str:
        if stopped_by_refusal:
            return "none"
        if self.named is not None:
            walk = self.read_walk()
            open_pairs = [row["pair"] for level in walk["levels"] for row in level
                          if row["status"] not in SATISFIED]
            if open_pairs:
                emit("COMPOSE: skipped scoped run; not current: " + ",".join(open_pairs))
                named_open = [p for p in open_pairs if p in self.named]
                return "skipped-blocked" if named_open else "skipped"
        return self.compose()

    def exit_code(self, composed: str, all_current: bool) -> int:
        if self.refused:
            return 2
        if composed == "blocked" or composed == "skipped-blocked":
            return 1
        if not self.spawned and all_current:
            return 3
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--session", required=True, help="engine session id owning the plan review")
    p.add_argument("--plan", required=True, help="plan file to review (the walk target)")
    selector = p.add_mutually_exclusive_group()
    selector.add_argument("--complexity", choices=("low", "medium", "high"), default=None,
                          help="reviewer model tier (default high)")
    selector.add_argument("--model", default=None, help="explicit reviewer model alias")
    p.add_argument("--dry-run", action="store_true",
                   help="list per-level commands and each pair's prompt size; spawn nothing")
    p.add_argument("--no-early-stop", action="store_true",
                   help="review every pair even after a revise or a refusal (measurement runs)")
    p.add_argument("--parallel", type=int, default=1,
                   help="concurrent spawns within one level (default 1: sequential)")
    p.add_argument("--pairs", default=None, help="comma list of pair ids to review, in walk order")
    p.add_argument("--ledger", default=None,
                   help="condition-4 gap ledger path (sets AGENTCTL_ESCALATION_LEDGER)")
    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")
    plan_path = Path(args.plan).resolve()
    if not plan_path.is_file():
        parser.error(f"--plan {args.plan}: no such file")
    env = dict(os.environ)
    if args.ledger:
        ledger = Path(args.ledger).resolve()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.touch()
        env["AGENTCTL_ESCALATION_LEDGER"] = str(ledger)
    return Driver(args, str(plan_path), env).run(parser)


if __name__ == "__main__":
    sys.exit(main())
