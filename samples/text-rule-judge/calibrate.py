#!/usr/bin/env python3
"""Live calibration of the published-text rule judge (advisor.judge_published_text_rules).

  calibrate.py judge     [--runs 2] [--out calibration.json]
      Judge every item of labelled.jsonl `--runs` times with the live model and
      count how many historical publication bodies the lexical prefilter nominates.
  calibrate.py hook-e2e  [--out hook-e2e.json]
      Feed the real hook script a PreToolUse payload for a violating and a clean
      body whose transcript binds them (POST_WITNESS); record both decisions.

Then `check_calibration.py calibration.json hook-e2e.json` holds every assertion.
See README.md for when to re-run.

Nothing is published: the hook is fed a synthetic payload on stdin and its command
is never executed. Historical publication bodies are read in memory and only
counts leave this process.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentctl import advisor  # noqa: E402
from lib import published_body, writer_rules  # noqa: E402

LABELLED = HERE / "labelled.jsonl"
HOOK = REPO_ROOT / "scripts" / "hook-published-text-writer-gate.py"
PROJECTS_ROOT = Path.home() / ".claude-agent" / "projects"
E2E_POSITIVE_ID = "incident-2026-10-05"
E2E_NEGATIVE_ID = "clean-reply-login"
MAX_RETRIES_PER_RUN = 1


def load_items(path: Path = LABELLED) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def p90_nearest_rank(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def judged_rules(findings: list, candidates: list, body: str) -> list[str]:
    """The rule ids the hook would quote in its deny reason: named by the judge,
    nominated by the prefilter, and carrying a span that occurs in the body."""
    fired = {rule_id for rule_id, _ in candidates}
    lowered = body.lower()
    out: list[str] = []
    for rule_id, span in findings:
        if rule_id in fired and span.strip() and span.lower() in lowered and rule_id not in out:
            out.append(rule_id)
    return out


def judge_once(body: str, candidates: list) -> dict:
    """One live judge call; at most MAX_RETRIES_PER_RUN retries when it fails open."""
    failed_attempts: list[dict] = []
    for _ in range(MAX_RETRIES_PER_RUN + 1):
        start = time.monotonic()
        violated, reason, findings = advisor.judge_published_text_rules(
            body, candidates, advisor.subprocess_runner,
            enabled=True, timeout=advisor._PUBLISHED_TEXT_RULES_TIMEOUT_S,
        )
        latency = round(time.monotonic() - start, 2)
        if not reason:
            return {
                "verdict": bool(violated), "genuine": True,
                "judged_rules": judged_rules(findings, candidates, body),
                "spans": [span for _, span in findings],
                "latency_s": latency, "failed_attempts": failed_attempts,
            }
        failed_attempts.append({"reason": reason, "latency_s": latency})
    return {
        "verdict": False, "genuine": False, "judged_rules": [], "spans": [],
        "latency_s": failed_attempts[-1]["latency_s"], "failed_attempts": failed_attempts,
    }


def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_published_text_writer_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def prefilter_rate(projects_root: Path) -> dict:
    """Count historical publication bodies the prefilter nominates. Counts only."""
    seam = _load_hook()._load_seam()
    transcripts = sorted(projects_root.glob("*/*.jsonl"))
    publications = fired = unresolved = 0
    with tempfile.TemporaryDirectory() as scratch:
        os.environ[published_body.ADVISORY_SINK_ENV] = str(Path(scratch) / "advisories.jsonl")
        for transcript in transcripts:
            try:
                lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if '"Bash"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                blocks = message.get("content") if isinstance(message, dict) else None
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"):
                        continue
                    tool_input = block.get("input") or {}
                    cwd = entry.get("cwd") or str(REPO_ROOT)
                    resolution = published_body.resolve("Bash", tool_input, cwd, seam=seam)
                    if resolution.kind == published_body.UNRESOLVED:
                        unresolved += 1
                    elif resolution.kind == published_body.TEXT and resolution.body:
                        publications += 1
                        if writer_rules.find_candidates(resolution.body):
                            fired += 1
        os.environ.pop(published_body.ADVISORY_SINK_ENV, None)
    return {
        "transcripts": len(transcripts), "publications": publications,
        "unresolved": unresolved, "fired": fired,
        "rate": round(fired / publications, 4) if publications else None,
    }


def cmd_judge(args: argparse.Namespace) -> int:
    items = load_items()
    results = []
    for item in items:
        candidates = writer_rules.find_candidates(item["body"])
        runs = []
        for run_index in range(args.runs):
            run = judge_once(item["body"], candidates)
            runs.append(run)
            print(f"{item['id']} run {run_index + 1}: verdict={run['verdict']} genuine={run['genuine']} "
                  f"rules={run['judged_rules']} {run['latency_s']}s", flush=True)
        results.append({"id": item["id"], "label": item["label"], "label_rules": item["label_rules"],
                        "candidates": [rule_id for rule_id, _ in candidates], "runs": runs})
    latencies = [
        value
        for result in results for run in result["runs"]
        for value in (run["latency_s"], *(a["latency_s"] for a in run["failed_attempts"]))
    ]
    flips = sum(1 for r in results if len({run["verdict"] for run in r["runs"]}) > 1)
    calibration = {
        "n": len(results), "runs_per_item": args.runs, "model": advisor._TEXT_RULES_JUDGE_MODEL,
        "items": results,
        "median_s": round(statistics.median(latencies), 2),
        "p90_s": round(p90_nearest_rank(latencies), 2),
        "max_s": round(max(latencies), 2),
        "flips": flips,
        "prefilter": prefilter_rate(Path(args.projects_root)),
    }
    Path(args.out).write_text(json.dumps(calibration, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: n={calibration['n']} median={calibration['median_s']} "
          f"p90={calibration['p90_s']} max={calibration['max_s']} flips={flips} prefilter={calibration['prefilter']}")
    return 0


def _transcript_binding(body: str, scratch: Path) -> tuple[Path, Path]:
    """A transcript in which a tech-writer pass precedes a Write of exactly `body`
    (POST_WITNESS), plus the file that Write names, which the command posts."""
    body_file = scratch / "comment.md"
    body_file.write_text(body, encoding="utf-8")
    events = [
        {"type": "assistant", "timestamp": "2026-10-06T10:00:00Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w1", "name": "Skill", "input": {"skill": "tech-writer", "args": "draft the comment"}}]}},
        {"type": "user", "timestamp": "2026-10-06T10:00:03Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "w1", "content": [{"type": "text", "text": "Draft ready."}]}]}},
        {"type": "assistant", "timestamp": "2026-10-06T10:00:30Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "wr1", "name": "Write", "input": {"file_path": str(body_file), "content": body}}]}},
        {"type": "user", "timestamp": "2026-10-06T10:00:31Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "wr1", "content": [{"type": "text", "text": "File written."}]}]}},
    ]
    transcript = scratch / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return transcript, body_file


def run_hook_once(body: str) -> dict:
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch = Path(scratch_name)
        transcript, body_file = _transcript_binding(body, scratch)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": f"gh issue comment 1 --repo example/repo --body-file {body_file}"},
            "cwd": str(REPO_ROOT), "transcript_path": str(transcript),
        }
        env = dict(os.environ)
        env[published_body.ADVISORY_SINK_ENV] = str(scratch / "advisories.jsonl")
        env["AGENTCTL_JUDGE_LEDGER"] = str(scratch / "ledger.jsonl")
        for name in ("CLAUDE_PUBLISHED_TEXT_GATE", "CLAUDE_PUBLISHED_TEXT_RULES_SEMANTIC", "AGENTCTL_JUDGE_CHILD"):
            env.pop(name, None)
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        latency = round(time.monotonic() - start, 2)
        decision, reason = "allow", ""
        if proc.stdout.strip():
            out = json.loads(proc.stdout)["hookSpecificOutput"]
            decision, reason = out["permissionDecision"], out["permissionDecisionReason"]
        advisories = []
        sink = scratch / "advisories.jsonl"
        if sink.exists():
            advisories = [json.loads(ln).get("kind") for ln in sink.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return {"decision": decision, "reason": reason, "returncode": proc.returncode,
                "latency_s": latency, "advisories": advisories}


def cmd_hook_e2e(args: argparse.Namespace) -> int:
    by_id = {item["id"]: item for item in load_items()}
    result = {
        "positive": {"id": E2E_POSITIVE_ID, **run_hook_once(by_id[E2E_POSITIVE_ID]["body"])},
        "negative": {"id": E2E_NEGATIVE_ID, **run_hook_once(by_id[E2E_NEGATIVE_ID]["body"])},
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: positive={result['positive']['decision']} negative={result['negative']['decision']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    judge = sub.add_parser("judge")
    judge.add_argument("--runs", type=int, default=2)
    judge.add_argument("--out", required=True)
    judge.add_argument("--projects-root", default=str(PROJECTS_ROOT))
    judge.set_defaults(fn=cmd_judge)
    e2e = sub.add_parser("hook-e2e")
    e2e.add_argument("--out", required=True)
    e2e.set_defaults(fn=cmd_hook_e2e)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
