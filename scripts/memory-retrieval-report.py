#!/usr/bin/env python3
"""Report hit/miss counts and the missed queries from the memory-retrieval ledger.

Difficulty removed: the retrieval hook is improvable only from evidence of what it failed to
surface. Each `extend`/`search` row of ~/.local/log/claude-memory-retrieval.jsonl is labelled
hit or miss against the leaves surfaced earlier in the same session; this prints the totals
and the missed queries (most recent first) so a reviewer sees what the ranking did not find.

`new` rows are unclassified (the leaf did not exist) and rows without a session are
unattributed; neither is a miss. Read-only.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import memory_retrieval_ledger  # noqa: E402

MISSES_SHOWN = 20


def summarize(rows: list[dict]) -> dict:
    out = {"surfaces": 0, "hits": 0, "misses": 0, "unclassified": 0, "unattributed": 0,
           "missed": []}
    for row in rows:
        event, outcome = row.get("event"), row.get("outcome")
        if event == "surface":
            out["surfaces"] += 1
        elif event not in ("extend", "search", "new"):
            continue
        elif not row.get("session"):
            out["unattributed"] += 1
        elif outcome == "hit":
            out["hits"] += 1
        elif outcome == "miss":
            out["misses"] += 1
            out["missed"].append(row)
        else:
            out["unclassified"] += 1
    out["missed"].sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=None,
                        help="ledger file (default: the live ledger)")
    parser.add_argument("--days", type=float, default=None,
                        help="only rows from the last N days")
    args = parser.parse_args(argv)
    rows = memory_retrieval_ledger.read_rows(args.ledger)
    if args.days is not None:
        cutoff = time.time() - args.days * 86400
        rows = [r for r in rows if (r.get("ts") or 0) >= cutoff]
    summary = summarize(rows)
    for key in ("surfaces", "hits", "misses", "unclassified", "unattributed"):
        print(f"{key}: {summary[key]}")
    for row in summary["missed"][:MISSES_SHOWN]:
        query = " ".join(str(row.get("query") or "").split())
        print(f"miss [{row.get('event')}] {query!r} -> {row.get('target')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
