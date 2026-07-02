#!/usr/bin/env python3
"""Report on prewrite-plan-check fallback hook firings.

Reads the JSONL ledger written by hook-prewrite-plan-check.py when it
emits its "missing plan" nudge and aggregates: total firings, unique
sessions, and a per-project breakdown. Useful for deciding when to
retire the hook (zero firings over N days after agentctl auto-start).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

LEDGER = config_root.agentctl_dir() / "prewrite-fallback.jsonl"
LEGACY_LEDGER = config_root.agentctl_legacy_state_dir().parent / "prewrite-fallback.jsonl"


def load_ledger(path: Path) -> list[dict]:
    """Load JSONL rows; tolerate missing file or malformed lines."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def aggregate(rows: list[dict], days: int | None) -> dict:
    """Aggregate rows within an optional time window."""
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
                if ts >= cutoff:
                    filtered.append(r)
            except (ValueError, TypeError):
                continue
        rows = filtered

    cwd_counts: Counter = Counter(r.get("cwd", "") for r in rows)
    session_ids = {r.get("session_id", "") for r in rows}

    return {
        "total": len(rows),
        "unique_sessions": len(session_ids),
        "by_cwd": dict(cwd_counts.most_common()),
    }


def _print_digest(agg: dict, days: int | None) -> None:
    window = f"last {days}d" if days is not None else "all time"
    if agg["total"] == 0:
        print(f"## prewrite-fallback firings ({window})\n\nNo fallback firings recorded.")
        return

    print(f"## prewrite-fallback firings ({window})\n")
    print(f"- **Total firings:** {agg['total']}")
    print(f"- **Unique sessions:** {agg['unique_sessions']}")

    if agg["by_cwd"]:
        print("\n### By project (cwd)\n")
        print(f"| {'Project':<60} | Count |")
        print(f"|{'-'*62}|-------|")
        for cwd, count in agg["by_cwd"].items():
            print(f"| {cwd:<60} | {count:>5} |")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=None,
                   help="restrict to last N days (default: all time)")
    p.add_argument("--ledger", type=Path, default=LEDGER,
                   help="path to the JSONL ledger (default: %(default)s)")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)

    rows = load_ledger(a.ledger)
    agg = aggregate(rows, a.days)
    _print_digest(agg, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
