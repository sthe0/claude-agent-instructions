#!/usr/bin/env python3
"""Single-place usage report over the existing local ledgers.

Reads the three append-only ledgers already written by the coordination
machinery and prints one summary — no new central store:

  - ~/.local/log/claude-policy-ledger.jsonl   (policy-scorecard.py, one row
    per session; carries a `project` field) -> invocations.
  - ~/.local/log/claude-task-quality.jsonl    (agentctl `resolve`; carries
    `session`, `quality`, `tracker_key`) -> resolved tasks, marked
    precedents (a non-null `tracker_key` — the solved_by_007 join key), mean
    quality.
  - ~/.local/log/claude-spawn-costs.jsonl     (spawn-specialist.py; carries
    `session_id`) -> spawns, cost.

Project scope is resolved the same way tool-usage-report.py / policy-
scorecard.py do it: a project is a directory under the transcripts root
(`~/.claude/projects/<project>` by default), and a ledger row belongs to a
project if its session id is one of that directory's transcript filenames
(policy-ledger rows carry their own `project` field directly).

Modes:
  agent-stats.py [--days N] [--project P]   report for one project
                                             (default: current cwd)
  agent-stats.py --global [--days N]        report across all projects
  agent-stats.py --json                     emit a JSON dict instead of
                                             markdown
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path

# --- reuse cost-report.py (hyphenated filename -> load by path, no copy-paste) ---
_CR_PATH = Path(__file__).resolve().parent / "cost-report.py"
_spec = importlib.util.spec_from_file_location("cost_report", _CR_PATH)
cost_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost_report)

_iter_jsonl = cost_report._iter_jsonl
filter_by_time = cost_report.filter_by_time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home

TASK_QUALITY_LOG = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"
POLICY_LEDGER = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
SPAWN_COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"
PROJECTS_DIR = agent_home() / "projects"

DEFAULT_DAYS = 30


def sanitize_cwd(cwd: str) -> str:
    """Map an absolute cwd to the projects/ subdir name Claude Code uses."""
    return cwd.replace("/", "-")


def project_sessions(projects_dir: Path, project: str) -> set[str]:
    """Session ids (transcript filename stems) belonging to a project dir."""
    d = projects_dir / project
    if not d.is_dir():
        return set()
    return {f.stem for f in d.glob("*.jsonl")}


def read_rows(path: Path) -> list[dict]:
    """All well-formed JSON rows in a ledger; malformed lines are skipped,
    a missing ledger file yields no rows."""
    if not path.exists():
        return []
    return list(_iter_jsonl(path))


def scope_rows(
    *,
    task_rows: list[dict],
    policy_rows: list[dict],
    spawn_rows: list[dict],
    project: str | None,
    sessions: set[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Restrict each ledger's rows to one project. `project=None` means global
    (no filtering)."""
    if project is None:
        return task_rows, policy_rows, spawn_rows
    return (
        [r for r in task_rows if r.get("session") in sessions],
        [r for r in policy_rows if r.get("project") == project],
        [r for r in spawn_rows if r.get("session_id") in sessions],
    )


def aggregate(task_rows: list[dict], policy_rows: list[dict], spawn_rows: list[dict]) -> dict:
    """Compute the report's six metrics from already-scoped, already-windowed rows."""
    resolved = len(task_rows)
    marked_precedents = sum(1 for r in task_rows if r.get("tracker_key"))
    qualities = [r["quality"] for r in task_rows if isinstance(r.get("quality"), (int, float))]
    mean_quality = statistics.fmean(qualities) if qualities else None

    invocations = len(policy_rows)

    spawn_events = [r for r in spawn_rows if r.get("event", "spawn") != "refused"]
    spawns = len(spawn_events)
    cost = round(sum((r.get("cost_usd") or 0) for r in spawn_events), 6)

    return {
        "invocations": invocations,
        "resolved": resolved,
        "marked_precedents": marked_precedents,
        "mean_quality": mean_quality,
        "cost": cost,
        "spawns": spawns,
    }


def resolve_since(since_str: str | None, days: int) -> tuple[dt.datetime, str]:
    if since_str:
        return (
            dt.datetime.fromisoformat(since_str).replace(tzinfo=dt.timezone.utc),
            f"since {since_str}",
        )
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return since, f"last {days} days"


def render_md(scope_label: str, window_label: str, stats: dict) -> str:
    mq = f"{stats['mean_quality']:.2f}" if stats["mean_quality"] is not None else "—"
    lines = [
        f"agent-stats: {scope_label}    window: {window_label}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Invocations (distinct sessions) | {stats['invocations']} |",
        f"| Resolved tasks | {stats['resolved']} |",
        f"| Marked precedents (`solved_by_007`) | {stats['marked_precedents']} |",
        f"| Mean quality | {mq} |",
        f"| Cost (spawns, USD) | ${stats['cost']:.4f} |",
        f"| Spawns | {stats['spawns']} |",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--since", help="window start as YYYY-MM-DD (UTC); overrides --days")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"window size in days (default: {DEFAULT_DAYS})")
    p.add_argument("--project", help="project dir name under the transcripts root (default: current cwd, sanitized)")
    p.add_argument("--global", dest="global_scope", action="store_true", help="aggregate across all projects on this machine")
    p.add_argument("--task-log", type=Path, default=TASK_QUALITY_LOG, help=f"task-quality ledger (default: {TASK_QUALITY_LOG})")
    p.add_argument("--policy-log", type=Path, default=POLICY_LEDGER, help=f"policy ledger (default: {POLICY_LEDGER})")
    p.add_argument("--spawn-log", type=Path, default=SPAWN_COST_LOG, help=f"spawn cost log (default: {SPAWN_COST_LOG})")
    p.add_argument("--projects-dir", type=Path, default=PROJECTS_DIR, help=f"transcripts root (default: {PROJECTS_DIR})")
    p.add_argument("--json", action="store_true", help="emit a JSON dict instead of markdown")
    p.add_argument("--cross-machine", action="store_true",
                   help="print the cross-installation rollup (delegates to usage-digest.py pull)")
    args = p.parse_args(argv)

    if args.cross_machine:
        # Lazy path-load usage-digest.py (hyphenated; it imports THIS module, so importing it
        # at top would be circular). Delegates to its read-only `pull` aggregator.
        _ud_spec = importlib.util.spec_from_file_location(
            "usage_digest", Path(__file__).resolve().parent / "usage-digest.py")
        usage_digest = importlib.util.module_from_spec(_ud_spec)
        _ud_spec.loader.exec_module(usage_digest)
        argv2 = ["pull"]
        if args.json:
            argv2.append("--json")
        return usage_digest.main(argv2)

    since, window_label = resolve_since(args.since, args.days)

    task_rows = filter_by_time(read_rows(args.task_log), since)
    policy_rows = filter_by_time(read_rows(args.policy_log), since)
    spawn_rows = filter_by_time(read_rows(args.spawn_log), since)

    if args.global_scope:
        project = None
        sessions: set[str] = set()
        scope_label = "global (all projects)"
    else:
        project = args.project or sanitize_cwd(str(Path(os.getcwd()).resolve()))
        sessions = project_sessions(args.projects_dir, project)
        scope_label = f"project={project}"

    task_rows, policy_rows, spawn_rows = scope_rows(
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        project=project, sessions=sessions,
    )
    stats = aggregate(task_rows, policy_rows, spawn_rows)

    if args.json:
        print(json.dumps({**stats, "scope": scope_label, "window": window_label}, ensure_ascii=False))
        return 0
    print(render_md(scope_label, window_label, stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
