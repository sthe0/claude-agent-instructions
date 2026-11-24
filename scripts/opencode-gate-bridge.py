"""Bridge from OpenCode hook events to Core's unchanged Claude-shaped hook scripts.

    opencode-gate-bridge.py event <name>          one OpenCode hook input (JSON) on stdin, one verdict on stdout
    opencode-gate-bridge.py replay --event E --payload FILE [--layer-root DIR]...
    opencode-gate-bridge.py credential-paths      the credential path list, as one JSON list
    opencode-gate-bridge.py idle-cap              the cap on idle continues of one session

``event`` answers ``{"decision": "allow"|"deny", "reason": ..., "script": ..., "context": [...]}`` and
exits 0; it exits 2 with ``missing-script: <path>`` or the error text when it cannot decide, and the
shim then refuses a tool call rather than let it pass. ``replay`` runs a recorded input through the
rows without a session or a transcript: exit 1 on deny, printing ``deny: <script> <reason>``; exit 0 on
allow; exit 2 with ``missing-script: <path>`` when a row's script does not resolve.

The rows are the Core parity table's, plus each non-home layer root's, plus the table-shaped file named
by ``AGENT_GATE_EXTRA_ROWS``. Plugin shim, rows and combination: ``lib/opencode_events.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root, opencode_events as oe  # noqa: E402


def _default_layer_roots(cwd: str) -> list[Path]:
    roots = [config_root.agent_home()]
    project = Path(cwd) / ".claude"
    if project.is_dir():
        roots.append(project)
    return roots


def _read_json(source) -> dict:
    data = json.loads(source.read())
    if not isinstance(data, dict):
        raise ValueError("the event input must be a JSON object")
    return data


def _decide(event: str, data: dict, roots: list[Path], transcript: Path | None) -> oe.Decision:
    cwd = data.get("cwd") or os.getcwd()
    plugin = oe.plugin_tools()
    payload = oe.build_payload(event, data, transcript_path=transcript or Path("/dev/null"), cwd=cwd,
                               plugin=plugin)
    if transcript is not None:
        oe.append_transcript(transcript, oe.transcript_records(event, {**data, "cwd": cwd}, payload["session_id"]))
    rows = oe.load_rows(roots, os.environ.get(oe.EXTRA_ROWS_ENV))
    if event == "tool.execute.before":
        tool = data.get("tool") or ""
        if oe.denied_by_name(tool):
            return oe.Decision(True, f"{tool} is denied by the OpenCode gate adapter", "opencode-gate-bridge")
        if payload["tool_name"] is None and oe.rows_for(rows, "PreToolUse", None):
            return oe.Decision(True, f"tool {tool!r} has no Claude name and a denying row exists; refused",
                               "opencode-gate-bridge")
    decision = oe.run_rows(oe.rows_for(rows, payload["hook_event_name"], payload.get("tool_name")), payload)
    if event == "session.created":
        from lib import hook_parity

        decision.context += hook_parity.start_notice(roots)
    return decision


def _output(decision: oe.Decision) -> dict:
    return {"decision": "deny" if decision.deny else "allow", "reason": decision.reason,
            "script": decision.script, "context": decision.context}


def cmd_event(args) -> int:
    data = _read_json(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    roots = [Path(r) for r in args.layer_root] or _default_layer_roots(cwd)
    session = oe.payload_session_id(data.get("sessionID") or "")
    transcript = oe.shadow_transcript_path(oe.integration_dir(), session) if session else None
    try:
        decision = _decide(args.name, data, roots, transcript)
    except oe.MissingScript as error:
        print(f"missing-script: {error.path}", file=sys.stderr)
        return 2
    print(json.dumps(_output(decision)))
    return 0


def cmd_replay(args) -> int:
    with open(args.payload, encoding="utf-8") as handle:
        data = _read_json(handle)
    try:
        decision = _decide(args.event, data, [Path(r) for r in args.layer_root], None)
    except oe.MissingScript as error:
        print(f"missing-script: {error.path}")
        return 2
    if decision.deny:
        print(f"deny: {decision.script} {decision.reason}")
        return 1
    return 0


def cmd_credential_paths(args) -> int:
    home = config_root.agent_home()
    paths = oe.credential_paths(oe.integration_dir(), home, oe.configured_credential_file(home))
    print(json.dumps([str(p) for p in paths]))
    return 0


def cmd_idle_cap(args) -> int:
    print(json.dumps(oe.IDLE_MAX_CONTINUES))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    event = sub.add_parser("event")
    event.add_argument("name", choices=sorted(oe.EVENT_OF))
    event.add_argument("--layer-root", action="append", default=[])
    event.set_defaults(func=cmd_event)
    replay = sub.add_parser("replay")
    replay.add_argument("--event", required=True, choices=sorted(oe.EVENT_OF))
    replay.add_argument("--payload", required=True)
    replay.add_argument("--layer-root", action="append", default=[])
    replay.set_defaults(func=cmd_replay)
    sub.add_parser("credential-paths").set_defaults(func=cmd_credential_paths)
    sub.add_parser("idle-cap").set_defaults(func=cmd_idle_cap)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:  # the shim must see a failure, never an empty allow
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
