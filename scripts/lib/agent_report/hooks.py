"""Decision telemetry per mechanism: how often each hook or engine gate evaluated, and how
often it denied, nudged or stayed silent.

Reads the decision log (`config_root.decision_log()`, one JSON row per evaluation; row
schema and the writers in docs/operations/decision-telemetry.md). Rows written by a test
run (`test: true`) are left out unless `--include-test` is given, so the default view
describes real sessions only.

Default window: all rows. Override with `--since YYYY-MM-DD`. Default log: the live one.
Override with `--log PATH`. A mechanism that never denies or nudges over a long window is
a candidate for removal; one that always denies is a candidate for a closer look.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from lib import config_root

DECISIONS = ("deny", "nudge", "silent")


def read_rows(log: Path):
    """Yield the decision rows of `log`, skipping lines that are not a JSON object."""
    with log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def summarize(rows, *, since: str | None, include_test: bool) -> dict:
    """Map (mechanism, layer, event) -> {decision: count, "last": newest ts}."""
    table: dict = defaultdict(lambda: {**dict.fromkeys(DECISIONS, 0), "last": ""})
    for row in rows:
        ts = str(row.get("ts", ""))
        if since and ts[:len(since)] < since:
            continue
        if row.get("test") and not include_test:
            continue
        decision = row.get("decision")
        if decision not in DECISIONS:
            continue
        entry = table[(str(row.get("mechanism", "?")), str(row.get("layer", "?")),
                       str(row.get("event", "?")))]
        entry[decision] += 1
        entry["last"] = max(entry["last"], ts)
    return table


def render(table: dict, *, since: str | None, include_test: bool) -> str:
    window = f"since {since}" if since else "all time"
    scope = "including test runs" if include_test else "real sessions only"
    lines = [f"# Hook and gate decisions ({window}, {scope})", ""]
    if not table:
        lines.append("No decision rows.")
        return "\n".join(lines)
    lines += ["| mechanism | layer | event | evaluations | deny | nudge | silent | deny % | last |",
              "|---|---|---|---:|---:|---:|---:|---:|---|"]
    ordered = sorted(table.items(), key=lambda kv: (-sum(kv[1][d] for d in DECISIONS), kv[0]))
    for (mechanism, layer, event), counts in ordered:
        total = sum(counts[d] for d in DECISIONS)
        lines.append(
            f"| `{mechanism}` | {layer} | {event} | {total} | {counts['deny']} | "
            f"{counts['nudge']} | {counts['silent']} | {100 * counts['deny'] / total:.1f} | "
            f"{counts['last']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, help="Decision log to read (default: the live log)")
    ap.add_argument("--since", help="Window start (YYYY-MM-DD or an ISO timestamp prefix)")
    ap.add_argument("--include-test", action="store_true", help="Also count rows written by test runs")
    args = ap.parse_args(argv)

    log = args.log or config_root.decision_log()
    if not log.is_file():
        print(f"No decision log at {log}.", file=sys.stderr)
        return 1
    table = summarize(read_rows(log), since=args.since, include_test=args.include_test)
    print(render(table, since=args.since, include_test=args.include_test))
    return 0


if __name__ == "__main__":
    sys.exit(main())
