"""OpenCode hook events as the Claude-shaped hook calls Core's gates are written against.

Difficulty removed: the Core hook scripts read a Claude payload (tool name, tool input, session id,
transcript path), answer with an exit code or a JSON decision, and rely on Claude's stop semantics.
OpenCode 2.3.2 hands a plugin its own event inputs. This module is the one place that turns an
OpenCode event into the payload, picks the rows of the parity table that apply, runs them and
combines their decisions; ``opencode-gate-bridge.py`` is its command-line face and the plugin shim
``integrations/opencode/plugin/agent-gates.ts`` only forwards events to it and carries out the verdict.

Every fact about OpenCode's inputs here (field names, the tool names the hooks see, the answer shape
of a question) comes from the pinned mechanisms probe record, not from documentation.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from lib import config_root

# Any deny among the rows of one event refuses it, with the reason of the first deny in row order.
DENY_WINS = True
# OpenCode has no loop guard on the idle hook (E6): the shim stops continuing a session after this many.
IDLE_MAX_CONTINUES = 5

RUNTIME_HOST_ENV = "AGENTCTL_RUNTIME_HOST"
HARNESS_SESSION_ENV = "AGENTCTL_HARNESS_SESSION_ID"
OWN_SESSION_ENV = "AGENTCTL_OWN_SESSION"
INTEGRATION_ENV = "AGENT_OPENCODE_HOME"
EXTRA_ROWS_ENV = "AGENT_GATE_EXTRA_ROWS"
PLUGIN_TOOLS_ENV = "AGENT_GATE_PLUGIN_TOOLS"
SETTINGS_CREDENTIAL_KEY = "opencode-credential-file"
SETTINGS_FILE = "opencode.local.toml"

BUILTIN_TOOLS = {
    "read": "Read", "edit": "Edit", "write": "Write", "shell": "Bash", "glob": "Glob", "grep": "Grep",
    "webfetch": "WebFetch", "websearch": "WebSearch", "question": "AskUserQuestion", "subagent": "Agent",
    "skill": "Skill", "execute": "Bash",
}
DENIED_BY_NAME = frozenset({"opencode_worktree_create", "opencode_worktree_remove", "opencode_session_move"})

# Probe path id -> the tool name the hooks see for it (stage 1's P1 record). A path whose tool is
# listed in DISABLED_PATHS is switched off by the integration's own config instead of mapped.
PATH_TOOL_NAMES = {
    "root:shell": "shell", "root:write": "write", "root:edit": "edit", "root:read": "read",
    "root:glob": "glob", "root:grep": "grep", "root:webfetch": "webfetch", "subagent:task": "subagent",
    "root:execute": "execute", "root:execute-inner": "probecm_touch", "root:skill": "skill",
    "mcp:probesrv:touch": "probesrv_touch", "plugin-tool:probe_tool": "probe_tool",
    "child-run": "shell", "root:question": "question",
}
PLUGIN_TOOLS_OF_PATHS = ("probe_tool",)
DISABLED_PATHS: dict[str, str] = {}

EVENT_OF = {
    "tool.execute.before": "PreToolUse", "tool.execute.after": "PostToolUse",
    "session.prompt": "UserPromptSubmit", "session.created": "SessionStart",
    "session.deleted": "SessionEnd", "experimental.idle": "Stop",
}

ROW_TIMEOUT_DEFAULT_S = 10


def integration_dir() -> Path:
    """The integration directory: ``AGENT_OPENCODE_HOME`` when set, else ``opencode`` under the agent home."""
    override = os.environ.get(INTEGRATION_ENV)
    return Path(override) if override else config_root.agent_home() / "opencode"


def shadow_transcript_path(directory: Path, session: str) -> Path:
    return Path(directory) / "state" / "transcripts" / f"{config_root.sanitize_session_id(session)}.jsonl"


def append_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def payload_session_id(opencode_session: str, environ=None) -> str:
    """The session id Core's hooks see: the engine session id of a linked child
    (``AGENTCTL_OWN_SESSION``), else the OpenCode session id."""
    env = os.environ if environ is None else environ
    return env.get(OWN_SESSION_ENV) or opencode_session


def plugin_tools(environ=None) -> frozenset[str]:
    env = os.environ if environ is None else environ
    return frozenset(t for t in (env.get(PLUGIN_TOOLS_ENV) or "").split(",") if t)


def claude_tool(name: str, plugin: frozenset[str] = frozenset()) -> str | None:
    """The Claude tool name for an OpenCode tool, or None when no name is known.

    ``<server>_<tool>`` is an MCP tool and a tool the plugin registers is ``plugin__<id>``; the two
    differ only by whether the name is in ``plugin``."""
    if name in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[name]
    if name in plugin:
        return f"plugin__{name}"
    if "_" in name:
        server, tool = name.split("_", 1)
        if server and tool:
            return f"mcp__{server}__{tool}"
    return None


def claude_input(tool: str, args: dict) -> dict:
    args = args if isinstance(args, dict) else {}
    if tool in ("read", "write", "edit"):
        mapped = {"file_path": args.get("path")}
        if tool == "write":
            mapped["content"] = args.get("content")
        if tool == "edit":
            mapped.update(old_string=args.get("oldString"), new_string=args.get("newString"))
        return mapped
    if tool in ("glob", "grep"):
        return {"pattern": args.get("pattern"), "path": args.get("path")}
    if tool == "shell":
        return {"command": args.get("command")}
    if tool == "execute":
        return {"command": args.get("code")}
    if tool == "webfetch":
        return {"url": args.get("url")}
    if tool == "websearch":
        return {"query": args.get("query")}
    if tool == "question":
        return {"questions": args.get("questions")}
    if tool == "subagent":
        return {"subagent_type": args.get("agent"), "description": args.get("description"),
                "prompt": args.get("prompt")}
    if tool == "skill":
        return {"skill": args.get("id")}
    return dict(args)


def answers_of(args: dict, output) -> dict:
    """``{question text: answer}`` of a question tool call: OpenCode's ``result.output.answers`` is one
    list of chosen labels per question, in the order of ``input.questions``."""
    questions = (args or {}).get("questions") or []
    picked = (output or {}).get("answers") if isinstance(output, dict) else None
    if not isinstance(picked, list):
        return {}
    out = {}
    for question, chosen in zip(questions, picked):
        text = (question or {}).get("question")
        if text:
            out[text] = ", ".join(str(c) for c in chosen) if isinstance(chosen, list) else str(chosen)
    return out


def claude_response(tool: str, args: dict, result: dict) -> object:
    result = result if isinstance(result, dict) else {}
    text = "\n".join(p.get("text", "") for p in result.get("content") or []
                     if isinstance(p, dict) and p.get("type") == "text")
    if tool == "question":
        return {"answers": answers_of(args, result.get("output"))}
    if tool in ("shell", "execute"):
        return {"stdout": text or str(result.get("output") or ""), "stderr": "", "interrupted": False}
    response = {"content": text}
    if tool in ("read", "write", "edit"):
        response["filePath"] = (args or {}).get("path")
    return response


@dataclass(frozen=True)
class Row:
    event: str
    matcher: str | None
    script: str
    args: str
    timeout_s: int
    origin: str

    def matches(self, tool_name: str | None) -> bool:
        if not self.matcher:
            return True
        return tool_name is not None and re.fullmatch(self.matcher, tool_name) is not None


class MissingScript(Exception):
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


def _core_rows() -> list[Row]:
    from lib import hook_parity

    table = hook_parity.load_table(hook_parity.CORE_TABLE)
    scripts = hook_parity.REPO_ROOT / "scripts"
    return [Row(r["event"], r.get("matcher") or None, str(scripts / r["script"]), r.get("args") or "",
                int(r.get("timeout_s") or ROW_TIMEOUT_DEFAULT_S), "core")
            for r in table.get("row", []) if r.get("opencode") != hook_parity.NO_MECHANISM]


def _layer_rows(root: Path) -> list[Row]:
    from lib import hook_parity, hook_wiring

    rows = []
    for unit in hook_parity._resolved_units(root):
        hooks = json.loads(unit.read_text(encoding="utf-8")).get("hooks") or {}
        for event, groups in hooks.items():
            for group in groups:
                for hook in group.get("hooks", []):
                    command = (hook.get("command") or "").strip()
                    if hook.get("type", "command") != "command" or not command:
                        continue
                    script = os.path.expandvars(os.path.expanduser(hook_wiring.script_path(command)))
                    if not os.path.isabs(script):
                        script = str(unit.parent / script)
                    tokens = command.split()
                    rest = " ".join(tokens[1:]) if tokens and hook_wiring.script_path(command) == tokens[0] else ""
                    rows.append(Row(event, group.get("matcher") or None, script, rest,
                                    int(hook.get("timeout") or ROW_TIMEOUT_DEFAULT_S), str(root)))
    return rows


def _extra_rows(path: str | None) -> list[Row]:
    if not path:
        return []
    table = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [Row(r["event"], r.get("matcher") or None, r["script"], r.get("args") or "",
                int(r.get("timeout_s") or ROW_TIMEOUT_DEFAULT_S), "extra")
            for r in table.get("row", [])]


def load_rows(layer_roots: list[Path] | None = None, extra_rows_file: str | None = None) -> list[Row]:
    """Core's rows, then each non-home layer root's rows, then the per-session rows file's.
    A layer root only adds rows; Core's are never replaced."""
    rows = _core_rows()
    home = config_root.agent_home().resolve()
    for root in layer_roots or []:
        if Path(root).resolve() != home:
            rows += _layer_rows(Path(root))
    return rows + _extra_rows(extra_rows_file)


def rows_for(rows: list[Row], event: str, tool_name: str | None) -> list[Row]:
    return [r for r in rows if r.event == event and r.matches(tool_name)]


@dataclass
class Decision:
    deny: bool = False
    reason: str = ""
    script: str = ""
    context: list[str] = field(default_factory=list)


def parse_hook_output(returncode: int, stdout: str, stderr: str) -> Decision:
    """A hook's verdict by Claude's protocol: exit 2 refuses with stderr as the reason; exit 0 with a
    JSON object refuses on ``permissionDecision`` deny or ask, or ``decision`` block; other exits
    are non-blocking. ``additionalContext`` and ``systemMessage`` are carried as context."""
    if returncode == 2:
        return Decision(True, (stderr or stdout).strip() or "hook exited 2")
    if returncode != 0:
        return Decision()
    text = stdout.strip()
    if not text.startswith("{"):
        return Decision(context=[text] if text else [])
    try:
        data = json.loads(text)
    except ValueError:
        return Decision(context=[text])
    if not isinstance(data, dict):
        return Decision()
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    context = [str(v) for v in (specific.get("additionalContext"), data.get("systemMessage")) if v]
    verdict = specific.get("permissionDecision") or data.get("permissionDecision")
    if verdict in ("deny", "ask"):
        reason = specific.get("permissionDecisionReason") or data.get("permissionDecisionReason") or data.get("reason")
        return Decision(True, str(reason or f"hook decided {verdict}"), context=context)
    if data.get("decision") == "block":
        return Decision(True, str(data.get("reason") or "hook blocked"), context=context)
    return Decision(context=context)


def combine(decisions: list[Decision]) -> Decision:
    """One decision from the rows' decisions: any deny wins, with the first deny's reason and its
    script; the context of every row is kept."""
    context = [c for d in decisions for c in d.context]
    if DENY_WINS:
        deny = next((d for d in decisions if d.deny), None)
        chosen = deny or Decision()
    else:
        chosen = decisions[-1] if decisions else Decision()
    return Decision(chosen.deny, chosen.reason, chosen.script, context)


def command_of(row: Row) -> list[str]:
    path = Path(row.script)
    if not path.is_file():
        raise MissingScript(str(path))
    argv = [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
    return argv + shlex.split(row.args)


def hook_env(session_id: str, cwd: str) -> dict:
    env = dict(os.environ)
    env.update({RUNTIME_HOST_ENV: "opencode", HARNESS_SESSION_ENV: session_id, "CLAUDE_PROJECT_DIR": cwd})
    return env


def run_row(row: Row, payload: dict, env: dict) -> Decision:
    argv = command_of(row)
    try:
        proc = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                              timeout=row.timeout_s, env=env, cwd=payload.get("cwd") or None)
    except subprocess.TimeoutExpired:
        return Decision()
    decision = parse_hook_output(proc.returncode, proc.stdout, proc.stderr)
    decision.script = os.path.basename(row.script)
    return decision


def run_rows(rows: list[Row], payload: dict) -> Decision:
    """Run ``rows`` against ``payload`` side by side and combine their decisions in row order."""
    for row in rows:
        command_of(row)
    if not rows:
        return Decision()
    env = hook_env(payload.get("session_id", ""), payload.get("cwd", ""))
    with ThreadPoolExecutor(max_workers=min(len(rows), 8)) as pool:
        decisions = list(pool.map(lambda r: run_row(r, payload, env), rows))
    return combine(decisions)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_payload(event: str, data: dict, *, transcript_path: Path, cwd: str, plugin: frozenset[str],
                  environ=None) -> dict:
    """The Claude-shaped payload of an OpenCode event ``data`` (the hook input, as the probe recorded it).
    The tool name is None when no Claude name is known for the tool."""
    opencode_session = data.get("sessionID") or ""
    session = payload_session_id(opencode_session, environ)
    payload = {"session_id": session, "transcript_path": str(transcript_path), "cwd": cwd,
               "hook_event_name": EVENT_OF[event]}
    if event in ("tool.execute.before", "tool.execute.after"):
        tool, args = data.get("tool") or "", data.get("input") or {}
        payload["tool_name"] = claude_tool(tool, plugin)
        payload["tool_input"] = claude_input(tool, args)
        payload["tool_use_id"] = data.get("id")
        if event == "tool.execute.after":
            payload["tool_response"] = claude_response(tool, args, data.get("result") or {})
    elif event == "session.prompt":
        payload["prompt"] = ((data.get("prompt") or {}).get("text")) or ""
    elif event == "session.created":
        payload["source"] = "startup"
    elif event == "session.deleted":
        payload["reason"] = "other"
    elif event == "experimental.idle":
        payload["stop_hook_active"] = bool(data.get("stop_hook_active"))
    return payload


def transcript_records(event: str, data: dict, session: str) -> list[dict]:
    """The Claude-format transcript lines an event adds to the shadow transcript."""
    base = {"sessionId": session, "timestamp": now(), "cwd": data.get("cwd", "")}
    if event == "session.prompt":
        text = ((data.get("prompt") or {}).get("text")) or ""
        return [{**base, "type": "user", "uuid": str(uuid.uuid4()),
                 "message": {"role": "user", "content": [{"type": "text", "text": text}]}}]
    tool, args = data.get("tool") or "", data.get("input") or {}
    name = claude_tool(tool, plugin_tools()) or tool
    if event == "tool.execute.before":
        return [{**base, "type": "assistant", "uuid": str(uuid.uuid4()),
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": data.get("id"), "name": name,
                      "input": claude_input(tool, args)}]}}]
    if event == "tool.execute.after":
        response = claude_response(tool, args, data.get("result") or {})
        text = response.get("content") if isinstance(response, dict) and "content" in response \
            else json.dumps(response)
        return [{**base, "type": "user", "uuid": str(uuid.uuid4()),
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": data.get("id"), "content": text}]}}]
    return []


def credential_paths(integration: Path, agent_home: Path, credential_file: str | None) -> tuple[Path, ...]:
    """The files that hold credentials, resolved absolute: OpenCode's auth store in the integration
    directory, the per-machine settings file under the agent home, and the credential file the
    settings name. Reads no setting itself."""
    found = [Path(integration) / "data" / "opencode" / "auth.json", Path(agent_home) / SETTINGS_FILE]
    if credential_file:
        found.append(Path(credential_file).expanduser())
    return tuple(dict.fromkeys(p.resolve() for p in found))


def configured_credential_file(agent_home: Path) -> str | None:
    try:
        table = tomllib.loads((Path(agent_home) / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = table.get(SETTINGS_CREDENTIAL_KEY)
    return value if isinstance(value, str) and value else None


def denied_by_name(tool: str) -> bool:
    return tool in DENIED_BY_NAME
