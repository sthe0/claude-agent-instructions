#!/usr/bin/env python3
"""Query + stamp CLI over the durable edit-ledger (agentctl/edit_ledger.py).

Answers the two forensic questions the ledger exists to make cheap:
"what did session X edit" (``by-session``) and "who edited file Y"
(``by-file``) — without reading a transcript. It also exposes a ``stamp``
subcommand: the shell entry point to ``edit_ledger.stamp()``, the same
primitive a Python direct-IO canon writer imports directly — so a shell
writer (apply-settings.sh, install-reminder-hooks.sh, ...) can record its own
attribution with one line, with no second implementation to keep in sync.

Each ledger row carries two session ids (see edit_ledger.py's module
docstring): ``session_id`` (the hook-stdin id of the agent that actually made
the edit — may be a subagent) and ``env_session_id`` (the root session's
CLAUDE_CODE_SESSION_ID, which a commit trailer keys on). ``by-session``
matches a row when EITHER id equals the query, so a query keyed on a commit
trailer's Agent-Session value also surfaces the subagent edits made under it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import edit_ledger  # noqa: E402


def _ledger_path(arg: "str | None") -> "Path | None":
    """None means: let edit_ledger resolve its own default (config_root /
    $AGENTCTL_EDIT_LEDGER)."""
    return Path(arg).expanduser() if arg else None


def by_session(records: "list[dict]", session_id: str) -> "list[dict]":
    return [
        r for r in records
        if r.get("session_id") == session_id or r.get("env_session_id") == session_id
    ]


def by_file(records: "list[dict]", file_path: str) -> "list[dict]":
    target = os.path.realpath(file_path)
    return [r for r in records if r.get("file") == target]


def _render_human(records: "list[dict]") -> str:
    lines = [
        f"{r.get('ts')}  {r.get('session_id')}  {r.get('env_session_id')}  {r.get('tool')}  {r.get('file')}"
        for r in records
    ]
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ledger", help="ledger jsonl path (default: config_root.agentctl_edit_log(), honoring $AGENTCTL_EDIT_LEDGER)")
    p.add_argument("--json", action="store_true", help="emit a JSON array of raw records instead of human-readable lines")
    sub = p.add_subparsers(dest="command", required=True)

    p_session = sub.add_parser("by-session", help="all records for a session id (matches session_id OR env_session_id)")
    p_session.add_argument("session_id")

    p_file = sub.add_parser("by-file", help="all records touching a file (matched by realpath)")
    p_file.add_argument("path")

    p_stamp = sub.add_parser("stamp", help="append one attribution row for a direct-IO canon write (shell entry point to edit_ledger.stamp())")
    p_stamp.add_argument("--file", required=True, help="path of the file that was written")
    p_stamp.add_argument("--tool", required=True, help="synthetic writer marker, e.g. 'script:apply-settings'")
    p_stamp.add_argument("--session", default=None, help="explicit session id, overriding $CLAUDE_CODE_SESSION_ID for session_id")

    args = p.parse_args(argv)

    if args.command == "stamp":
        # Always returns 0: a shell writer's own canon write must never be
        # failed by its attribution call (stamp() is itself fail-open, but the
        # CLI wraps it again so even an argparse-adjacent surprise can't
        # propagate a nonzero exit into a caller's `|| true`-free path).
        try:
            edit_ledger.stamp(args.file, args.tool, session=args.session, path=_ledger_path(args.ledger))
        except Exception:
            pass
        return 0

    records = sorted(edit_ledger.read_records(_ledger_path(args.ledger)), key=lambda r: r.get("ts", 0))

    if args.command == "by-session":
        matched = by_session(records, args.session_id)
    else:
        matched = by_file(records, args.path)

    if args.json:
        print(json.dumps(matched, ensure_ascii=False))
    else:
        out = _render_human(matched)
        if out:
            print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
