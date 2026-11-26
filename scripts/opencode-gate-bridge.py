"""Bridge from OpenCode hook events to Core's unchanged Claude-shaped hook scripts.

    opencode-gate-bridge.py event <name>          one OpenCode hook input (JSON) on stdin, one verdict on stdout
    opencode-gate-bridge.py replay --event E --payload FILE [--layer-root DIR]...
    opencode-gate-bridge.py credential-paths      the credential path list, as one JSON list
    opencode-gate-bridge.py idle-cap              the cap on idle continues of one session

``event`` answers ``{"decision": "allow"|"deny", "reason": ..., "script": ..., "context": [...], "digest": ...}``
and exits 0; it exits 2 with ``missing-script: <path>`` or the error text when it cannot decide, and the shim
then refuses a tool call rather than let it pass. A hook's deny reads ``deny: <script> <reason>``; a refusal the
bridge makes itself reads ``refused: <class> <detail>`` (``oe.REFUSAL_CLASSES``): a tool nobody classified, an
argument nobody classified, ``patch``, a path in a form OpenCode's resolver was not recorded to handle, an unknown
session directory, a session started without ``OPENCODE_DISABLE_PROJECT_CONFIG``, a write into the session's own
config directory or the integration directory, a rows snapshot that is missing or changed.

The rows are the Core parity table's, plus each non-home layer root's, plus the table-shaped file named by
``AGENT_GATE_EXTRA_ROWS``, read once at the session's first event into a snapshot; the answer's ``digest`` names
it and the shim sends it back with every later event (``rowsDigest``). A parity table without a PreToolUse row
for the OpenCode host is an error (exit 2), not an allow-all. Plugin shim, rows and combination:
``lib/opencode_events.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The shim starts this script with ``-I``, which leaves the script directory off ``sys.path``. Appended, so
# the standard library and the installed packages win over a module of the same name in this directory.
sys.path.append(str(Path(__file__).resolve().parent))
from lib import config_root, opencode_events as oe  # noqa: E402

REFUSING_EVENTS = ("tool.execute.before", "session.prompt")


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


def _decide_call(event: str, data: dict, rows: list[oe.Row], plugin: frozenset[str], transcript: Path | None,
                 environ=None, declared: dict | None = None) -> oe.Decision:
    directory = data.get("cwd") or ""
    declared = oe.declared_config(environ) if declared is None else declared
    servers = frozenset(declared["mcp_servers"])
    if not directory:
        if event == "tool.execute.before":
            oe.classify_tool(str(data.get("tool") or ""), plugin, servers)
            raise oe.UnknownDirectory("the directory the session runs in is unknown, so no path or command "
                                      "can be placed")
        if event == "tool.execute.after":
            return oe.Decision()
    payload = oe.build_payload(event, data, transcript_path=transcript or Path("/dev/null"), plugin=plugin,
                               environ=environ, servers=servers, directory=directory, declared=declared,
                               row_scripts=[row.script for row in rows])
    if transcript is not None:
        oe.append_transcript(transcript, oe.transcript_records(event, {**data, "cwd": directory},
                                                                payload["session_id"], plugin=plugin,
                                                                servers=servers))
    decision = oe.run_rows(oe.rows_for(rows, payload["hook_event_name"], payload.get("tool_name")), payload)
    if decision.deny and event == "tool.execute.before" and transcript is not None:
        oe.redact_transcript_call(transcript, data.get("id"))
    return decision


def _decide(event: str, data: dict, rows: list[oe.Row], plugin: frozenset[str], roots: list[Path],
            transcript: Path | None, environ=None, declared: dict | None = None) -> oe.Decision:
    if event == "tool.execute.before":
        oe.require_pretool_rows(rows)
    try:
        decision = _decide_call(event, data, rows, plugin, transcript, environ, declared)
    except oe.UnshowableCall as error:
        if event in REFUSING_EVENTS:
            return oe.refusal(error.cls, str(error))
        return oe.Decision()
    if decision.deny and event in REFUSING_EVENTS:
        decision.reason = oe.hook_deny_reason(decision.script, decision.reason)
    if event == "session.created":
        from lib import hook_parity

        decision.context += hook_parity.start_notice(roots)
    return decision


def _output(decision: oe.Decision, digest: str | None) -> dict:
    return {"decision": "deny" if decision.deny else "allow", "reason": decision.reason,
            "script": decision.script, "context": decision.context, "digest": digest}


def bridge_event(name: str, data: dict, roots: list[Path], *, environ=None) -> dict:
    """The verdict of one event of a session. ``data["rowsDigest"]`` is the digest of the session's rows snapshot
    the shim holds; without it the snapshot is built from the sources and replaces the stored one."""
    env = os.environ if environ is None else environ
    data = dict(data)
    digest = data.pop("rowsDigest", None)
    session = oe.payload_session_id(data.get("sessionID") or "", env)
    transcript = oe.shadow_transcript_path(oe.integration_dir(), session) if session else None
    try:
        if name in REFUSING_EVENTS:
            oe.require_project_config_off(env)
        snapshot = oe.rows_snapshot(oe.integration_dir(), data.get("sessionID") or session, digest, lambda: roots,
                                    env.get(oe.EXTRA_ROWS_ENV), env)
    except oe.UnshowableCall as error:
        if name in REFUSING_EVENTS:
            return _output(oe.refusal(error.cls, str(error)), digest)
        return _output(oe.Decision(), digest)
    return _output(_decide(name, data, snapshot.rows, snapshot.plugin_tools, roots, transcript, env,
                           snapshot.declared), snapshot.digest)


def cmd_event(args) -> int:
    data = _read_json(sys.stdin)
    roots = [Path(r) for r in args.layer_root] or _default_layer_roots(data.get("cwd") or os.getcwd())
    try:
        output = bridge_event(args.name, data, roots)
    except oe.MissingScript as error:
        print(f"missing-script: {error.path}", file=sys.stderr)
        return 2
    print(json.dumps(output))
    return 0


def cmd_replay(args) -> int:
    with open(args.payload, encoding="utf-8") as handle:
        data = _read_json(handle)
    extra = os.environ.get(oe.EXTRA_ROWS_ENV)
    roots = [Path(r) for r in args.layer_root]
    try:
        decision = _decide(args.event, data, oe.load_rows(roots, extra), oe.declared_plugin_tools(extra), roots, None)
    except oe.MissingScript as error:
        print(f"missing-script: {error.path}")
        return 2
    if decision.deny:
        print(decision.reason if decision.cls else oe.hook_deny_reason(decision.script, decision.reason))
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
