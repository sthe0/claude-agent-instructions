"""Ledger and transcript readers shared by the agent-report views.

Difficulty removed: the five report scripts each carried their own copy of the
ledger paths, timestamp parsing, JSONL row reading and project-transcript
lookup, so a fix to one reader had to be repeated five times. They live here
once; each view imports what it needs.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2]

LOG_DIR = Path.home() / ".local" / "log"
SPAWN_COST_LOG = LOG_DIR / "claude-spawn-costs.jsonl"
TASK_QUALITY_LOG = LOG_DIR / "claude-task-quality.jsonl"
POLICY_LEDGER = LOG_DIR / "claude-policy-ledger.jsonl"


def sanitize_cwd(cwd: str) -> str:
    """Map an absolute cwd to the projects/ subdir name Claude Code uses."""
    return cwd.replace("/", "-")


def parse_ts(s: str) -> dt.datetime:
    """Parse an ISO timestamp, "Z" or "+00:00"; raises ValueError when malformed."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def parse_ts_utc(s: str) -> dt.datetime:
    """`parse_ts`, with a naive result read as UTC."""
    d = parse_ts(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def parse_ts_or_none(s: str | None) -> dt.datetime | None:
    """`parse_ts` for a field that may be absent or malformed."""
    if not s:
        return None
    try:
        return parse_ts(s)
    except ValueError:
        return None


def read_jsonl_rows(path: Path, warn: bool = True) -> list[dict]:
    """Every well-formed JSON line of a ledger; a missing file yields no rows.

    A malformed line is skipped, with a stderr warning unless `warn` is false.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if warn:
                    print(f"warning: skipping malformed jsonl line: {line[:120]}", file=sys.stderr)
    return rows


def project_transcripts(name: str, roots: list[Path]) -> list[Path]:
    """Session transcripts of project dir `name`, unioned across `roots`: the
    same name can exist under both config roots and each holds different
    sessions."""
    return sorted(t for root in roots for t in (root / name).glob("*.jsonl"))
