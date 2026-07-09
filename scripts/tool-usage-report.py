#!/usr/bin/env python3
"""Per-task report of specialization spawns and skill / subagent invocations.

Source data:
  - `~/.claude/projects/<cwd-hash>/*.jsonl` — session transcripts written by the
    Claude Code harness. Each line is a JSON message. Assistant `tool_use`
    entries with `name in {Skill, Agent, Task}` give us inline skill and
    subagent invocations along with their inputs.
  - `~/.local/log/claude-spawn-costs.jsonl` — written by
    `scripts/spawn-specialist.py` on every `claude -p` spawn. Gives us spawned
    specializations (one row per spawn) keyed by `kind`.

Default window: the last 7 days. Override with `--since YYYY-MM-DD` or
`--days N`. Default cwd: the current working directory's project dir
(sanitized as Claude Code does it: each `/` becomes `-`, leading dash kept).
Override with `--cwd <abs-path>` or `--transcript <path>` (scan exactly one
jsonl).

Default output: a markdown table the caller pastes into the experience leaf's
`Cost, effort, and tool usage` section. `--csv` switches to CSV.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home

COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"
PROJECTS_ROOT = agent_home() / "projects"  # system root (isolated or legacy)

# Tool names whose tool_use entries are counted as inline invocations.
SKILL_TOOL = "Skill"
AGENT_TOOLS = {"Agent", "Task"}  # historic alias kept for safety

PURPOSE_MAX_CHARS = 80
DEFAULT_MAX_PURPOSES = 3


def sanitize_cwd(cwd: str) -> str:
    """Map an absolute cwd to the projects/ subdir name Claude Code uses."""
    return cwd.replace("/", "-")


def parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def iter_transcript_lines(paths: list[Path]):
    for p in paths:
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            print(f"warning: cannot read {p}: {exc}", file=sys.stderr)


def squash_purpose(raw: str | None) -> str:
    if not raw:
        return ""
    first = raw.strip().splitlines()[0].strip()
    if len(first) > PURPOSE_MAX_CHARS:
        first = first[: PURPOSE_MAX_CHARS - 1].rstrip() + "…"
    return first


def collect_invocations(
    transcripts: list[Path], since: dt.datetime | None
) -> dict[tuple[str, str], list[str]]:
    """Return {(kind, name) -> [purpose, ...]} for every Skill / Agent / Task
    tool_use found in the given transcripts within the time window."""
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in iter_transcript_lines(transcripts):
        ts = parse_ts(entry.get("timestamp"))
        if since is not None and ts is not None and ts < since:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            tool_name = item.get("name")
            tool_input = item.get("input") or {}
            if tool_name == SKILL_TOOL:
                skill = tool_input.get("skill") or "?"
                purpose = squash_purpose(tool_input.get("args"))
                buckets[("Skill", skill)].append(purpose)
            elif tool_name in AGENT_TOOLS:
                sub = tool_input.get("subagent_type") or "default"
                purpose = squash_purpose(
                    tool_input.get("description") or tool_input.get("prompt")
                )
                buckets[("Agent", sub)].append(purpose)
    return buckets


def collect_spawns(
    log_path: Path, since: dt.datetime | None
) -> dict[tuple[str, str], list[str]]:
    """Return {('spawn', kind) -> [marker_or_status, ...]}."""
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    if not log_path.exists():
        return buckets
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(e.get("ts"))
            if since is not None and ts is not None and ts < since:
                continue
            if e.get("event") == "spawn_start":
                continue  # paired with the child's completion row; counting both doubles it
            kind = e.get("kind") or "?"
            if e.get("event", "spawn") == "refused":
                reason = e.get("reason") or "?"
                buckets[("spawn-refused", kind)].append(f"refused: {reason}")
            else:
                marker = e.get("return_marker") or "no-marker"
                exit_code = e.get("exit_code")
                tag = marker if exit_code in (0, None) else f"{marker} (exit={exit_code})"
                buckets[("spawn", kind)].append(tag)
    return buckets


def render_md(buckets: dict[tuple[str, str], list[str]], max_purposes: int) -> str:
    rows = sorted(buckets.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1]))
    if not rows:
        return "(no specializations or skills used in the selected window)"
    lines = ["| Kind | Name | Count | Purposes |", "|---|---|---|---|"]
    for (kind, name), purposes in rows:
        seen: list[str] = []
        for p in purposes:
            if p and p not in seen:
                seen.append(p)
            if len(seen) >= max_purposes:
                break
        more = len(purposes) - len(seen)
        joined = "; ".join(f'"{p}"' for p in seen) if seen else "—"
        if more > 0:
            joined += f"; +{more} more"
        lines.append(f"| {kind} | `{name}` | {len(purposes)} | {joined} |")
    total_inline = sum(
        len(v) for (k, _), v in buckets.items() if k in {"Skill", "Agent"}
    )
    total_spawn = sum(
        len(v) for (k, _), v in buckets.items() if k == "spawn"
    )
    total_refused = sum(
        len(v) for (k, _), v in buckets.items() if k == "spawn-refused"
    )
    lines.append("")
    lines.append(
        f"_Total: {total_inline} inline invocations, "
        f"{total_spawn} spawns, {total_refused} refused._"
    )
    return "\n".join(lines)


def render_csv(buckets: dict[tuple[str, str], list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kind", "name", "count", "purpose"])
    for (kind, name), purposes in sorted(buckets.items()):
        if not purposes:
            w.writerow([kind, name, 0, ""])
            continue
        for p in purposes:
            w.writerow([kind, name, len(purposes), p])
    return buf.getvalue().rstrip()


def resolve_transcripts(args) -> list[Path]:
    if args.transcript:
        return [Path(args.transcript).expanduser().resolve()]
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path(os.getcwd()).resolve()
    proj_dir = PROJECTS_ROOT / sanitize_cwd(str(cwd))
    if not proj_dir.is_dir():
        print(
            f"warning: no transcripts dir for cwd={cwd}: {proj_dir} does not exist",
            file=sys.stderr,
        )
        return []
    return sorted(proj_dir.glob("*.jsonl"))


def resolve_since(args) -> tuple[dt.datetime | None, str]:
    if args.since:
        since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
        return since, f"since {args.since}"
    if args.days <= 0:
        return None, "all time"
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    return since, f"last {args.days} days"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--since", help="window start as YYYY-MM-DD (UTC); overrides --days")
    p.add_argument("--days", type=int, default=7, help="window size in days (default: 7); 0 = all time")
    p.add_argument("--cwd", help="absolute cwd to look up (default: current working directory)")
    p.add_argument("--transcript", help="scan exactly one jsonl transcript (overrides --cwd)")
    p.add_argument("--log", type=Path, default=COST_LOG, help=f"spawn cost log (default: {COST_LOG})")
    p.add_argument("--no-spawns", action="store_true", help="skip the spawn cost log; report only inline Skill / Agent invocations")
    p.add_argument("--csv", action="store_true", help="emit CSV instead of markdown")
    p.add_argument("--max-purposes", type=int, default=DEFAULT_MAX_PURPOSES, help=f"max distinct purposes shown per row (default: {DEFAULT_MAX_PURPOSES})")
    args = p.parse_args(argv)

    since, window_label = resolve_since(args)
    transcripts = resolve_transcripts(args)
    inline = collect_invocations(transcripts, since)
    spawns: dict[tuple[str, str], list[str]] = {}
    if not args.no_spawns:
        spawns = collect_spawns(args.log, since)

    combined: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, vals in inline.items():
        combined[key].extend(vals)
    for key, vals in spawns.items():
        combined[key].extend(vals)

    if args.csv:
        print(render_csv(combined))
        return 0

    target = args.transcript or args.cwd or os.getcwd()
    print(f"tool-usage-report: {window_label}    target: {target}")
    print()
    print(render_md(combined, args.max_purposes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
