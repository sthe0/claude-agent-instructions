#!/usr/bin/env python3
"""Spawn cost log summary; entry point for `agent-report.py cost`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import run
from lib.agent_report.cost import (  # noqa: F401 -- loaded by path by other scripts
    COST_LOG,
    INTERRUPT_SENTINEL,
    PRICING_SHA,
    PRICING_USD_PER_MTOK,
    _CORRECTION_JUDGE_RUNNER,
    _is_tool_result,
    _iter_jsonl,
    _msg_text,
    budget_report,
    csv_out,
    detail,
    filter_by_time,
    fmt_usd,
    parse_entries,
    parse_transcripts,
    parse_ts,
    resolve_project,
    split_events,
    summary,
    token_cost,
)


def main(argv=None):
    return run("cost", argv)


if __name__ == "__main__":
    sys.exit(main())
