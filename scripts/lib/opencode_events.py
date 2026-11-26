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
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

from lib import config_root

# Any deny among the rows of one event refuses it, with the reason of the first deny in row order.
DENY_WINS = True
# OpenCode has no loop guard on the idle hook (E6): the shim stops continuing a session after this many.
IDLE_MAX_CONTINUES = 5

# OpenCode reads the project's own config (opencode.json, .opencode/plugin, .opencode/tool) at start, before
# any tool call, and a gated session must not run what the project brings; probe A6 (project_routes) records
# that this variable switches each of the three routes off. Only the value the probe set is accepted, and only
# while ``OPENCODE_CONFIG_PROJECT_DISABLE`` is unset: the pinned binary reads that one first
# (``CONFIG_PROJECT_DISABLE ?? DISABLE_PROJECT_CONFIG``), so any value of it replaces this variable's.
PROJECT_CONFIG_OFF_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"
PROJECT_CONFIG_PRECEDENCE_ENV = "OPENCODE_CONFIG_PROJECT_DISABLE"
PROJECT_CONFIG_OFF = {PROJECT_CONFIG_OFF_ENV: "1"}

RUNTIME_HOST_ENV = "AGENTCTL_RUNTIME_HOST"
HARNESS_SESSION_ENV = "AGENTCTL_HARNESS_SESSION_ID"
OWN_SESSION_ENV = "AGENTCTL_OWN_SESSION"
INTEGRATION_ENV = "AGENT_OPENCODE_HOME"
EXTRA_ROWS_ENV = "AGENT_GATE_EXTRA_ROWS"
OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG"
OPENCODE_CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"
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
# Paths a model family reaches under another tool id (probe A5 lists the ids each family is offered).
FAMILY_TOOL_PATHS = {"root:patch": "patch"}
PLUGIN_TOOLS_OF_PATHS = ("probe_tool",)
# A built-in whose effect the bridge cannot work out from the call's input the way OpenCode will carry it out
# (OpenCode applies a patch over its own in-memory view of the files, which the bridge would have to imitate
# to the byte). Such a tool is not mapped and every call of it is refused. It cannot be switched off alone: the
# permission action ``patch`` removes nothing, and ``edit`` removes ``edit``, ``write`` and ``patch`` together
# for every model family (probe A5, ``patch_switch_off``), which would take file editing from the families
# whose edit and write calls the rows do see.
REFUSED_TOOLS: dict[str, str] = {
    "patch": "the file editor OpenCode offers the gpt family applies a multi-file patch over its own in-memory "
             "view of the files, so no gate can be shown the change it will make",
}
REFUSED_PATHS: dict[str, str] = {
    path_id: REFUSED_TOOLS[tool] for path_id, tool in FAMILY_TOOL_PATHS.items() if tool in REFUSED_TOOLS}

# What the adapter cannot do and says so, by id; ``integrations/opencode/README.md`` § Open limits carries
# each id with its consequence for the user.
OPEN_LIMITS: dict[str, str] = {
    "patch-refused": "every call of the gpt family's multi-file patch editor is refused: its effect cannot be "
                     "shown to the rows, and it cannot be switched off without also removing edit and write",
    "project-routes-unobserved": "a route by which a project brings code into OpenCode that probe A6 never saw "
                                 "load in its control run cannot be shown switched off; the variable "
                                 "OPENCODE_DISABLE_PROJECT_CONFIG is set for the session regardless",
    "alternate-spellings": "a read whose file name ends in a form OpenCode would retry under another spelling "
                           "(no-break space, curly quote, decomposed accent) is refused",
    "fuzzy-edit": "an edit whose oldString does not occur in the file exactly is refused, though OpenCode would "
                  "apply it fuzzily",
    "mcp-name-reserved": "an MCP server named opencode, or after a built-in or a refused tool, is refused: its "
                         "tools cannot be told from OpenCode's own",
    "interpreter-writes": "a write into the session's own config directory or the integration directory made by "
                          "code an interpreter runs (python -c, node -e) is not seen: only write, edit and the "
                          "write targets of a shell command line (redirects, cp, mv, install, tee) are checked",
}

# Every argument of every built-in the adapter maps, as the pinned OpenCode version's tool schemas name it
# (probe A5 records them): ``path`` is resolved like a path (``resolve_path``) into the named Claude input
# field, ``mapped`` is copied into it, ``irrelevant`` is dropped, for the stated reason. An argument that
# decides where a tool reads, writes or runs, or what it writes or runs, is never irrelevant. A call carrying
# an argument not listed here is refused.
ARGUMENT_CLASSES: dict[str, dict[str, tuple[str, str]]] = {
    "read": {"path": ("path", "file_path"), "offset": ("mapped", "offset"), "limit": ("mapped", "limit")},
    "write": {"path": ("path", "file_path"), "content": ("mapped", "content")},
    "edit": {"path": ("path", "file_path"), "oldString": ("mapped", "old_string"),
             "newString": ("mapped", "new_string"), "replaceAll": ("mapped", "replace_all")},
    "glob": {"pattern": ("mapped", "pattern"), "path": ("path", "path"),
             "hidden": ("irrelevant", "lists dot-files under the search root that path already names"),
             "limit": ("irrelevant", "caps how many matches are listed")},
    "grep": {"pattern": ("mapped", "pattern"), "path": ("path", "path"),
             "include": ("irrelevant", "narrows the files searched inside the root that path already names"),
             "literal": ("irrelevant", "how the pattern is read, not which files are"),
             "caseSensitive": ("irrelevant", "how the pattern is matched, not which files are read"),
             "limit": ("irrelevant", "caps how many matches are listed")},
    "shell": {"command": ("mapped", "command"), "workdir": ("path", "cwd"),
              "background": ("mapped", "run_in_background"),
              "timeout": ("irrelevant", "bounds how long the command may run, not what it runs")},
    "execute": {"code": ("mapped", "command")},
    "webfetch": {"url": ("mapped", "url"),
                 "format": ("irrelevant", "the form the fetched page is returned in"),
                 "timeout": ("irrelevant", "bounds how long the fetch may run")},
    "websearch": {"query": ("mapped", "query")},
    "question": {"questions": ("mapped", "questions")},
    "subagent": {"agent": ("mapped", "subagent_type"), "description": ("mapped", "description"),
                 "prompt": ("mapped", "prompt"),
                 "model": ("irrelevant", "picks the model of a child session whose own calls the gates see"),
                 "sessionID": ("irrelevant", "resumes a child session whose own calls the gates see")},
    "skill": {"id": ("mapped", "skill")},
}
OPTIONAL_ARGUMENTS = frozenset({"offset", "limit", "replaceAll", "background", "hidden", "include", "literal",
                                "caseSensitive", "timeout", "format", "model", "sessionID", "workdir"})
# The arguments a call must carry (probe A5, ``tool_schemas``, records the schema's own ``required``) and the
# JSON type of each argument name; a call of a built-in that leaves a required one out or gives one the wrong
# type is refused, since OpenCode's own handling of it was never recorded.
REQUIRED_ARGUMENTS = {
    "read": {"path"}, "write": {"path", "content"}, "edit": {"path", "oldString", "newString"},
    "glob": {"pattern"}, "grep": {"pattern"}, "shell": {"command"}, "execute": {"code"}, "webfetch": {"url"},
    "websearch": {"query"}, "question": {"questions"}, "subagent": {"description", "prompt", "agent"},
    "skill": {"id"},
}
STRING_ARGUMENTS = frozenset({"path", "content", "oldString", "newString", "pattern", "command", "workdir", "code",
                              "url", "format", "query", "description", "prompt", "agent", "model", "sessionID",
                              "id", "include"})
BOOLEAN_ARGUMENTS = frozenset({"replaceAll", "background", "hidden", "literal", "caseSensitive"})
NUMBER_ARGUMENTS = frozenset({"offset", "limit", "timeout"})
ARRAY_ARGUMENTS = frozenset({"questions"})
# glob reads these two spellings of a path as no path at all (A6 ``spellings``); grep reads them as the
# directory of that name.
GLOB_ABSENT_PATHS = frozenset({"undefined", "null"})

EVENT_OF = {
    "tool.execute.before": "PreToolUse", "tool.execute.after": "PostToolUse",
    "session.prompt": "UserPromptSubmit", "session.created": "SessionStart",
    "session.deleted": "SessionEnd", "experimental.idle": "Stop",
}
# The events whose verdict can refuse something: a tool call, a prompt, the end of a turn.
DECIDING_EVENTS = frozenset({"tool.execute.before", "session.prompt", "experimental.idle"})

ROW_TIMEOUT_DEFAULT_S = 10

REFUSAL_CLASSES = ("unclassified", "argument", "refused-tool", "path-form", "directory", "project-config",
                   "config-dir", "rows-snapshot", "gate-setup")


def refusal_reason(cls: str, detail: str) -> str:
    """The text of a refusal the bridge or the shim makes itself; a hook's deny reads
    ``deny: <script> <reason>`` instead, so the reader sees whose refusal it is."""
    assert cls in REFUSAL_CLASSES, cls
    return f"refused: {cls} {detail}"


def hook_deny_reason(script: str, reason: str) -> str:
    return f"deny: {script} {reason}"


def project_config_off(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(PROJECT_CONFIG_OFF_ENV) == PROJECT_CONFIG_OFF[PROJECT_CONFIG_OFF_ENV]
            and PROJECT_CONFIG_PRECEDENCE_ENV not in env)


def require_project_config_off(environ=None) -> None:
    if not project_config_off(environ):
        raise ProjectConfigNotOff(f"{PROJECT_CONFIG_OFF_ENV}={PROJECT_CONFIG_OFF[PROJECT_CONFIG_OFF_ENV]} is not "
                                  f"set (or {PROJECT_CONFIG_PRECEDENCE_ENV} is), so the session may have loaded "
                                  f"code from the project's own OpenCode config")


def integration_dir() -> Path:
    """The integration directory: ``AGENT_OPENCODE_HOME`` when set, else ``opencode`` under the agent home."""
    override = os.environ.get(INTEGRATION_ENV)
    return Path(override) if override else config_root.agent_home() / "opencode"


def transcripts_dir(directory: Path) -> Path:
    return Path(directory) / "state" / "transcripts"


def snapshots_dir(directory: Path) -> Path:
    return Path(directory) / "state" / "rows-snapshots"


def shadow_transcript_path(directory: Path, session: str) -> Path:
    return transcripts_dir(directory) / f"{config_root.sanitize_session_id(session)}.jsonl"


@contextmanager
def _transcript_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def append_transcript(path: Path, records: list[dict]) -> None:
    with _transcript_lock(path), path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def redact_transcript_call(path: Path, tool_use_id) -> None:
    """Take the input of one recorded tool call out of the shadow transcript: the call was refused after the
    rows were shown it, and the transcript must not keep the file content or command of a refused call."""
    with _transcript_lock(path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        kept = []
        for line in lines:
            try:
                record = json.loads(line)
                for part in (record.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("id") == tool_use_id:
                        part["input"] = {}
                        part["refused"] = True
                        line = json.dumps(record)
            except (ValueError, AttributeError):
                pass
            kept.append(line)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temporary, path)


def payload_session_id(opencode_session: str, environ=None) -> str:
    """The session id Core's hooks see: the engine session id of a linked child
    (``AGENTCTL_OWN_SESSION``), else the OpenCode session id."""
    env = os.environ if environ is None else environ
    return env.get(OWN_SESSION_ENV) or opencode_session


PLUGIN_SPEC_KEYS = ("plugin", "plugins")
FILE_SPEC_PREFIX = "file://"


def _plugin_spec_path(spec, source: Path) -> str | None:
    """The file or directory a plugin spec of a config file names, ``None`` for a package name. A spec is a
    string or ``[string, options]``."""
    if isinstance(spec, list) and spec:
        spec = spec[0]
    if not isinstance(spec, str):
        return None
    if spec.startswith(FILE_SPEC_PREFIX):
        return unquote(spec[len(FILE_SPEC_PREFIX):])
    if spec.startswith("/"):
        return spec
    if spec.startswith("."):
        return str(source.parent / spec)
    return None


def declared_config(environ=None) -> dict:
    """What the session's own OpenCode config declares, read from the file ``OPENCODE_CONFIG`` names and from
    ``opencode.json`` of ``OPENCODE_CONFIG_DIR``: ``mcp_servers``, the keys of ``mcp`` (probe A5,
    ``mcp_names_from_config``, records that a server's tools are offered as ``<key>_<tool>``), and
    ``plugin_paths``, the files and directories its plugin specs name, which a session must not write. A config
    that cannot be read declares none, so the tools of its servers are refused. A session reads it once, into
    its snapshot (``rows_snapshot``)."""
    env = os.environ if environ is None else environ
    sources = [env.get(OPENCODE_CONFIG_ENV)]
    if env.get(OPENCODE_CONFIG_DIR_ENV):
        sources.append(str(Path(env[OPENCODE_CONFIG_DIR_ENV]) / "opencode.json"))
    servers: set[str] = set()
    plugin_paths: set[str] = set()
    for source in filter(None, sources):
        try:
            table = json.loads(Path(source).read_text(encoding="utf-8"))
            mcp = table.get("mcp")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(mcp, dict):
            servers.update(str(key) for key in mcp)
        for key in PLUGIN_SPEC_KEYS:
            specs = table.get(key)
            for spec in specs if isinstance(specs, list) else []:
                path = _plugin_spec_path(spec, Path(source))
                if path:
                    plugin_paths.add(path)
    return {"mcp_servers": sorted(servers), "plugin_paths": sorted(plugin_paths)}


def declared_mcp_servers(environ=None) -> frozenset[str]:
    return frozenset(declared_config(environ)["mcp_servers"])


def declared_plugin_tools(extra_rows_file: str | None = None) -> frozenset[str]:
    """The plugin tool ids the parity table declares (``plugin_tools.ids``, shipped empty) and, for a
    session, the table-shaped file ``AGENT_GATE_EXTRA_ROWS`` names."""
    from lib import hook_parity

    tables = [hook_parity.load_table(hook_parity.CORE_TABLE)]
    if extra_rows_file:
        tables.append(tomllib.loads(Path(extra_rows_file).read_text(encoding="utf-8")))
    ids: set[str] = set()
    for table in tables:
        ids.update(str(i) for i in (table.get("plugin_tools") or {}).get("ids") or [])
    return frozenset(ids)


# A server of this name would take the ids OpenCode's own tools carry (``opencode_worktree_create``) or a
# built-in's, so its tools cannot be told from them.
RESERVED_SERVER_NAMES = frozenset({"opencode"}) | frozenset(BUILTIN_TOOLS) | frozenset(REFUSED_TOOLS)


class UnshowableCall(ValueError):
    """A tool call the gates cannot be shown as it will run; ``cls`` is its refusal class."""

    cls = "argument"


class UnclassifiedTool(UnshowableCall):
    """A tool id that is no built-in, no tool of a declared MCP server and no declared plugin tool, or fits
    two of these, or sits under a reserved server name."""

    cls = "unclassified"


class RefusedTool(UnshowableCall):
    """A built-in whose effect no gate can be shown (``REFUSED_TOOLS``), or one denied by name."""

    cls = "refused-tool"


class UnresolvablePath(UnshowableCall):
    """A path the way OpenCode resolves it cannot be reproduced: a form outside the recorded set."""

    cls = "path-form"


class UnknownDirectory(UnresolvablePath):
    """A path or event that depends on the session directory, and that directory is not known."""

    cls = "directory"


class UnclassifiedArgument(UnshowableCall):
    """A built-in's call carries an argument ``ARGUMENT_CLASSES`` does not list, lacks a required one or gives
    one of the wrong type, or an ``edit`` whose ``oldString`` is not in the file as written."""

    cls = "argument"


class RowsSnapshotRefused(UnshowableCall):
    """The rows of a session are not the ones its first event froze: no snapshot, or one that changed."""

    cls = "rows-snapshot"


class ProjectConfigNotOff(UnshowableCall):
    cls = "project-config"


class ConfigDirWrite(UnshowableCall):
    """A write into the session's own config directory or the integration directory: the plugin that gates the
    session and the config that loads it live there."""

    cls = "config-dir"


SYSTEM_CONFIG_DIR = "/etc/opencode"


def global_config_locations(environ=None) -> tuple[str, ...]:
    """The directories OpenCode loads plugin code from whatever the project config says. Probe A6
    ``global_plugin_dirs`` (pinned 2.3.2, session started without ``OPENCODE_CONFIG_DIR``) observed ``on`` for
    ``$XDG_CONFIG_HOME/opencode`` (its ``plugin`` and ``plugins``, with the project config switched off), ``absent``
    for ``$HOME/.agents``, ``$HOME/.claude``, ``$HOME/.config/opencode`` beside another ``XDG_CONFIG_HOME`` and the
    directory above the binary, and ``unobserved`` for the system directory, which the probe cannot place. The
    protected set is each location not ``absent``: the system directory and the XDG config directory
    (``$HOME/.config/opencode`` when ``XDG_CONFIG_HOME`` is unset, and always, for the passwd home as well as
    ``$HOME``, which a command can set for itself). Resolved from the environment the session really runs in."""
    env = os.environ if environ is None else environ
    homes = [h for h in dict.fromkeys((env.get("HOME") or "", os.path.expanduser("~"))) if h and os.path.isabs(h)]
    xdg = env.get("XDG_CONFIG_HOME") or ""
    found = [SYSTEM_CONFIG_DIR]
    found += [os.path.join(xdg, "opencode")] if xdg else []
    found += [os.path.join(home, ".config", "opencode") for home in homes]
    return tuple(found)


_LIB_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = _LIB_DIR.parent.parent
GATE_CODE_PATHS = (str(REPOSITORY_ROOT),)
GATE_PYTHON_ENV = "AGENT_GATE_PYTHON"
GATE_BRIDGE_ENV = "AGENT_GATE_BRIDGE"


def protected_paths(environ=None, directory: str = "", plugin_paths=(), extra=()) -> tuple[str, ...]:
    """The files and directories a gated session must not write: the config directory OpenCode was started
    with (``OPENCODE_CONFIG_DIR``), the integration directory (the shadow transcripts, the rows snapshots,
    OpenCode's data), the ``OPENCODE_CONFIG`` file, the ``AGENT_GATE_EXTRA_ROWS`` file, each plugin spec of the
    declared config (the file, and its directory when it is a file: the shim and the directory that holds it),
    the whole repository the shim and the bridge run from (``GATE_CODE_PATHS``: the bridge, the hooks, every
    library and table they import), the interpreter that runs them (the bridge's own, the
    ``AGENT_GATE_PYTHON`` value) and the bridge script named by ``AGENT_GATE_BRIDGE``, ``extra`` (each row's
    script), the global plugin locations (``global_config_locations``), and the settings files of the session
    directory's own ``.claude``, whose hooks the bridge reads as layer rows. Real paths, so a link to one names
    it."""
    env = os.environ if environ is None else environ
    found = [env.get(OPENCODE_CONFIG_DIR_ENV), env.get(INTEGRATION_ENV) or str(config_root.agent_home() / "opencode"),
             env.get(OPENCODE_CONFIG_ENV), env.get(EXTRA_ROWS_ENV), *GATE_CODE_PATHS, sys.executable,
             env.get(GATE_PYTHON_ENV), env.get(GATE_BRIDGE_ENV), *extra, *global_config_locations(env)]
    for path in plugin_paths:
        found.append(path)
        if not os.path.isdir(path):
            found.append(os.path.dirname(path))
    if directory:
        found += [os.path.join(directory, ".claude", name) for name in LAYER_SETTINGS_NAMES]
    return tuple(dict.fromkeys(os.path.realpath(p) for p in found if p))


LAYER_SETTINGS_NAMES = ("settings.json", "settings.local.json")


def _inside(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


class WriteScopeHits(NamedTuple):
    protected: list[str]
    unreadable: list[str]


def config_dir_write_targets(tool: str, tool_input: dict, cwd: str, environ=None, *, directory: str = "",
                             plugin_paths=(), extra=()) -> WriteScopeHits:
    """The protected paths a call would write (``protected``) and the write targets of a shell command line
    that cannot be shown to lie outside the protected set (``unreadable``: a ``$`` the environment does not
    expand, a ``cd`` to an unreadable place, a command line that does not tokenize). ``tool`` is the Claude
    tool name and ``tool_input`` its input. A ``write`` or ``edit`` path was resolved by OpenCode's own
    resolver, which removes ``..`` before it follows a link, and is checked as it names; a shell target is
    resolved as the Claude hooks resolve one, the real path of the raw path, since the shell follows the link
    first (``lib/shell_write_scope.py``)."""
    from lib import shell_write_scope

    env = os.environ if environ is None else environ
    protected = protected_paths(env, directory, plugin_paths, extra)
    if tool in ("Write", "Edit"):
        normal = os.path.normpath(str(tool_input.get("file_path") or ""))
        candidates = [normal, os.path.realpath(normal)] if tool_input.get("file_path") else []
        unreadable: list[str] = []
        merged_into: tuple[str, ...] = ()
    elif tool == "Bash":
        try:
            scope = shell_write_scope.write_scope(str(tool_input.get("command") or ""), cwd, env)
        except shell_write_scope.UnreadableCommand as error:
            return WriteScopeHits([], [str(error)])
        candidates, unreadable = list(scope.resolved), list(scope.unreadable)
        merged_into = scope.whole
    else:
        return WriteScopeHits([], [])
    hits = [c for c in dict.fromkeys(candidates) if any(_inside(c, d) for d in protected)]
    hits += [d for d in protected if any(_inside(d, whole) and d != whole for whole in merged_into) and d not in hits]
    return WriteScopeHits(hits, unreadable)


def require_no_config_dir_write(tool: str, tool_input: dict, cwd: str, environ=None, *, directory: str = "",
                                plugin_paths=(), extra=()) -> None:
    hits = config_dir_write_targets(tool, tool_input, cwd, environ, directory=directory, plugin_paths=plugin_paths,
                                    extra=extra)
    if hits.protected:
        raise ConfigDirWrite(f"{tool} would write {hits.protected[0]}, a file or directory that loads code into "
                             f"the session or holds the gates' own state (the session's config directory, the "
                             f"integration directory, the plugin, the rows files, a global plugin directory)")
    if hits.unreadable:
        raise ConfigDirWrite(f"{tool} writes to {hits.unreadable[0]!r}, which cannot be shown to lie outside the "
                             f"directories that load code into the session or hold the gates' own state")


def classify_tool(name: str, plugin: frozenset[str] = frozenset(),
                  servers: frozenset[str] = frozenset()) -> str:
    """The Claude tool name for an OpenCode tool id, by declared names only.

    A built-in (``BUILTIN_TOOLS``) maps by its table; a tool in ``REFUSED_TOOLS`` or denied by name is refused;
    otherwise the prefix before ``_`` is an MCP server declared in the session's config (the longest wins),
    giving ``mcp__<server>__<tool>``; otherwise the id is a declared plugin tool, giving ``plugin__<id>``. An id
    that fits both an MCP server and a plugin id is ambiguous, and one that fits neither is not known: both are
    refused, as is any id under a reserved server name."""
    if name in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[name]
    if name in REFUSED_TOOLS or denied_by_name(name):
        raise RefusedTool(f"{name} is not a tool the gates can be shown: "
                          f"{REFUSED_TOOLS.get(name, 'OpenCode manages worktrees and sessions itself')}")
    for server in sorted(RESERVED_SERVER_NAMES & servers):
        if name.startswith(server + "_"):
            raise UnclassifiedTool(f"{name} is under the MCP server name {server!r}, which is reserved")
    named = [s for s in sorted(servers - RESERVED_SERVER_NAMES, key=len, reverse=True)
             if name.startswith(s + "_") and name[len(s) + 1:]]
    if named and name in plugin:
        raise UnclassifiedTool(f"{name} is both a tool of the MCP server {named[0]!r} and a declared plugin tool")
    if named:
        return f"mcp__{named[0]}__{name[len(named[0]) + 1:]}"
    if name in plugin:
        return f"plugin__{name}"
    raise UnclassifiedTool(f"{name} is no built-in, no tool of an MCP server the session's config declares and "
                           f"no plugin tool the parity table declares")


def claude_tool(name: str, plugin: frozenset[str] = frozenset(),
                servers: frozenset[str] = frozenset()) -> str | None:
    """``classify_tool``, or None when the tool is not one the session knows."""
    try:
        return classify_tool(name, plugin, servers)
    except UnshowableCall:
        return None


def _spelling_form(name: str) -> str:
    """What OpenCode's read tool takes alternate spellings of a file name to be the same as: NFC, no-break and
    narrow no-break space as a space, curly as straight single quotes (A6 ``spellings``)."""
    return (unicodedata.normalize("NFC", name).replace(" ", " ").replace(" ", " ")
            .replace("‘", "'").replace("’", "'"))


def resolve_path(path, directory: str):
    """The absolute, normalised path OpenCode 2.3.2 takes a path argument to name, for exactly the forms probe
    A6 recorded (``path_forms``): the empty string is the session ``directory``; ``~`` and ``~/...`` are the
    home directory (``$HOME``, the one home source; ``~//...`` was not recorded and is refused); a leading ``/``
    (also ``//``) is absolute; every other form, ``~user``, ``$HOME``, ``${HOME}``, ``file://...``, ``..``,
    ``./``, is relative to the session directory. ``None`` (an argument not given) stays ``None``. Any other
    value, a NUL byte, or a directory or home that is needed and not an absolute path is refused. So is a path
    whose last component is not its own spelling form (``_spelling_form``): the read tool opens the file under
    any alternate spelling of a missing name, which no compare of the written name covers (A6 ``spellings``)."""
    if path is None:
        return None
    if not isinstance(path, str) or "\0" in path:
        raise UnresolvablePath(f"the path {path!r} is not a form OpenCode's resolver was recorded to handle")
    if path == "~" or path.startswith("~/"):
        base = os.environ.get("HOME", "")
        if not base or not os.path.isabs(base) or path.startswith("~//"):
            raise UnresolvablePath(f"the path {path!r} starts at the home directory and it is unknown, or is a "
                                   f"form that was not recorded")
        joined = os.path.join(base, path[2:])
    elif path.startswith("/"):
        joined = path
    else:
        if not directory or not os.path.isabs(directory):
            raise UnknownDirectory(f"the path {path!r} is relative and the session directory is unknown")
        joined = os.path.join(directory, path)
    normal = os.path.normpath(joined)
    normal = normal[1:] if normal.startswith("//") else normal
    last = os.path.basename(normal)
    if _spelling_form(last) != last:
        raise UnresolvablePath(f"the last component of {path!r} is not its own spelling form (no-break space, "
                               f"curly quote or decomposed accent): OpenCode would open the file under another "
                               f"spelling than the one the gates compare")
    return normal


def run_directory(tool: str, args: dict, directory: str, *, strict: bool = True) -> str:
    """The directory a call runs in, which Claude's payload names ``cwd``: the session ``directory``, but for a
    shell call with a ``workdir`` the one that names (OpenCode resolves it like any path, probe A6). With
    ``strict`` false a ``workdir`` that cannot be resolved leaves the session directory (events no gate
    decides on)."""
    workdir = (args if isinstance(args, dict) else {}).get("workdir") if tool == "shell" else None
    if workdir is None:
        return directory
    try:
        return resolve_path(workdir, directory)
    except UnresolvablePath:
        if strict:
            raise
        return directory


def _type_problem(name: str, value) -> str | None:
    if name in STRING_ARGUMENTS and not isinstance(value, str):
        return "a string"
    if name in BOOLEAN_ARGUMENTS and not isinstance(value, bool):
        return "a boolean"
    if name in NUMBER_ARGUMENTS and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return "a number"
    if name in ARRAY_ARGUMENTS and not isinstance(value, list):
        return "a list"
    return None


def claude_input(tool: str, args: dict, directory: str = "", *, strict: bool = True) -> dict:
    """The Claude tool input of an OpenCode tool input, by ``ARGUMENT_CLASSES``. A path is resolved by
    ``resolve_path`` (refused when it cannot be; with ``strict`` false it is kept as written, and an unlisted
    argument is dropped, for events no gate decides on). With ``strict`` an argument of the wrong type, and a
    required one that is absent (or JSON null, which OpenCode drops), is refused too. A glob ``path`` of the text
    ``undefined`` or ``null`` is no path, as OpenCode takes it. A tool the table does not list (plugin and MCP
    tools) passes its input as it is."""
    args = args if isinstance(args, dict) else {}
    classes = ARGUMENT_CLASSES.get(tool)
    if classes is None:
        return dict(args)
    mapped = {target: None for name, (kind, target) in classes.items()
              if kind != "irrelevant" and target != "cwd" and name not in OPTIONAL_ARGUMENTS}
    for name, value in args.items():
        if name not in classes:
            if strict:
                raise UnclassifiedArgument(f"{tool} was called with the argument {name!r}, which nobody classified")
            continue
        if value is None:
            continue
        problem = _type_problem(name, value) if strict else None
        if problem:
            raise UnclassifiedArgument(f"{tool} argument {name!r} is not {problem}")
        kind, target = classes[name]
        if kind == "irrelevant" or target == "cwd":
            continue
        if kind == "path":
            if tool == "glob" and value in GLOB_ABSENT_PATHS:
                continue
            try:
                value = resolve_path(value, directory)
            except UnresolvablePath:
                if strict:
                    raise
        mapped[target] = value
    if strict:
        absent = sorted(n for n in REQUIRED_ARGUMENTS.get(tool, ()) if args.get(n) is None)
        if absent:
            raise UnclassifiedArgument(f"{tool} was called without the required argument {absent[0]!r}")
    return mapped


EDIT_FILE_MAX_BYTES = 4 * 1024 * 1024


def require_exact_edit(file_path: str, old_string: str, replace_all: bool = False) -> None:
    """Refuse an ``edit`` whose ``oldString`` is not in the file as written, or occurs more than once without
    ``replaceAll``. OpenCode matches it fuzzily (curly quotes, indentation, trailing space; A6 ``spellings``)
    and, when the text is not unique, tries its next replacer, so the rows would be shown another edit than the
    one made. Occurrences count as OpenCode's own uniqueness test does (first index equals last index), so
    overlapping ones are several. The file is opened only here, never a link or a device, and nothing of its
    content leaves this function. An empty ``oldString`` is OpenCode's own write of the whole file."""
    if not old_string:
        return
    unreadable = UnclassifiedArgument(f"edit of {file_path}: the bridge cannot read the file to check that "
                                      f"oldString occurs in it exactly")
    try:
        fd = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        raise unreadable from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise unreadable
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(EDIT_FILE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > EDIT_FILE_MAX_BYTES:
        raise unreadable
    needle = old_string.encode("utf-8")
    first = data.find(needle)
    if first < 0:
        raise UnclassifiedArgument(f"edit of {file_path}: oldString does not occur in the file exactly, and "
                                   f"OpenCode would match it fuzzily and make another edit than the rows see")
    if not replace_all and first != data.rfind(needle):
        raise UnclassifiedArgument(f"edit of {file_path}: oldString occurs more than once and replaceAll is not "
                                   f"set, so OpenCode would try a looser match and make another edit than the "
                                   f"rows see")


def _schema_type(spec) -> str:
    kind = spec.get("type") if isinstance(spec, dict) else None
    return "number" if kind == "integer" else str(kind)


def _expected_type(name: str) -> str | None:
    for expected, names in (("string", STRING_ARGUMENTS), ("boolean", BOOLEAN_ARGUMENTS),
                            ("number", NUMBER_ARGUMENTS), ("array", ARRAY_ARGUMENTS)):
        if name in names:
            return expected
    return None


def unclassified_arguments(schema: dict[str, dict], plugin: frozenset[str] = frozenset(),
                           servers: frozenset[str] = frozenset()) -> list[str]:
    """What differs between the tool schemas the pinned OpenCode offered (``{tool: {"properties": {name: spec},
    "required": [...]}}``, probe A5 ``tool_schemas``) and what the adapter classified: ``tool.argument`` for an
    argument ``ARGUMENT_CLASSES`` does not list, ``tool.argument (stale)`` for a listed one the schema no longer
    has, ``tool.argument (type)`` for another JSON type than ``_expected_type``, ``tool (required)`` for another
    required set than ``REQUIRED_ARGUMENTS``, and ``tool`` alone for a tool that is no built-in, no refused tool
    and no declared plugin or MCP tool."""
    problems = []
    for tool, spec in sorted(schema.items()):
        properties = spec.get("properties") or {} if isinstance(spec, dict) else {}
        classes = ARGUMENT_CLASSES.get(tool)
        if classes is None:
            if tool not in REFUSED_TOOLS and claude_tool(tool, plugin, servers) is None:
                problems.append(tool)
            continue
        problems += [f"{tool}.{n}" for n in properties if n not in classes]
        problems += [f"{tool}.{n} (stale)" for n in classes if n not in properties]
        problems += [f"{tool}.{n} (type)" for n, sub in properties.items()
                     if n in classes and _expected_type(n) not in (None, _schema_type(sub))]
        if set(spec.get("required") or []) != REQUIRED_ARGUMENTS.get(tool, set()):
            problems.append(f"{tool} (required)")
    return problems


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
        if not self.matcher or self.matcher == "*":  # Claude: an empty or "*" matcher matches every tool
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
    A layer root only adds rows; Core's are never replaced. This reads the three sources; a session reads
    them once, into its snapshot (``rows_snapshot``)."""
    rows = _core_rows()
    home = config_root.agent_home().resolve()
    for root in layer_roots or []:
        if Path(root).resolve() != home:
            rows += _layer_rows(Path(root))
    return rows + _extra_rows(extra_rows_file)


def snapshot_digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class SessionSnapshot(NamedTuple):
    rows: list
    plugin_tools: frozenset
    digest: str
    declared: dict


def rows_snapshot(integration: Path, session: str, digest: str | None, make_roots, extra_rows_file: str | None,
                  environ=None) -> SessionSnapshot:
    """The rows, the declared plugin tool ids and the declared config (``declared_config``: MCP server names,
    plugin paths) of a session, and the digest of the snapshot they come from.

    A session's rows are read from the three sources once, into a snapshot file under the integration state
    keyed by the session id. With no ``digest`` (the first event of the session, or a new process of an existing
    one) the snapshot is built from the sources (``make_roots`` gives the layer roots) and replaces the stored
    one. With a ``digest`` the stored snapshot is read and never rebuilt: a snapshot that is missing,
    unreadable or whose digest differs is refused, so a row added or changed after the session began does not
    take effect in it and one removed does not drop out."""
    if not session:
        raise RowsSnapshotRefused("the session has no id to key its rows snapshot by")
    path = snapshots_dir(integration) / f"{config_root.sanitize_session_id(session)}.json"
    if digest:
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RowsSnapshotRefused("the stored rows snapshot of this session is missing or unreadable") from None
        if not isinstance(snapshot, dict) or snapshot_digest(snapshot) != digest:
            raise RowsSnapshotRefused("the stored rows snapshot of this session is not the one its digest names")
    else:
        snapshot = {"rows": [asdict(r) for r in load_rows(make_roots(), extra_rows_file)],
                    "plugin_tools": sorted(declared_plugin_tools(extra_rows_file)),
                    "declared": declared_config(environ)}
        digest = snapshot_digest(snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    try:
        rows = [Row(**row) for row in snapshot["rows"]]
        declared = {"mcp_servers": [str(n) for n in snapshot["declared"]["mcp_servers"]],
                    "plugin_paths": [str(p) for p in snapshot["declared"]["plugin_paths"]]}
        return SessionSnapshot(rows, frozenset(str(i) for i in snapshot["plugin_tools"]), digest, declared)
    except (KeyError, TypeError):
        raise RowsSnapshotRefused("the stored rows snapshot of this session has a shape no snapshot has") from None


def rows_for(rows: list[Row], event: str, tool_name: str | None) -> list[Row]:
    return [r for r in rows if r.event == event and r.matches(tool_name)]


class EmptyParityTable(Exception):
    """The Core parity table yields no PreToolUse row for the OpenCode host: an empty table is a broken
    install, and a tool call that no row sees must not pass as allowed."""


def require_pretool_rows(rows: list[Row]) -> None:
    if not any(r.origin == "core" and r.event == "PreToolUse" for r in rows):
        raise EmptyParityTable("the parity table has no PreToolUse row for the opencode host")


@dataclass
class Decision:
    deny: bool = False
    reason: str = ""
    script: str = ""
    context: list[str] = field(default_factory=list)
    cls: str = ""


def refusal(cls: str, detail: str) -> Decision:
    """A refusal the bridge makes itself, of refusal class ``cls``."""
    return Decision(True, refusal_reason(cls, detail), "opencode-gate-bridge", cls=cls)


def parse_hook_output(returncode: int, stdout: str, stderr: str) -> Decision:
    """A hook's verdict by Claude's protocol: exit 2 refuses with stderr as the reason; exit 0 with a
    JSON object refuses on ``permissionDecision`` deny or ask, or ``decision`` block; other exits
    are non-blocking. ``additionalContext`` and ``systemMessage`` are carried as context."""
    if returncode == 2:
        return Decision(True, (stderr or stdout).strip() or "hook exited 2")
    if returncode != 0:
        return Decision()  # Claude Code: any exit other than 0 and 2 is a non-blocking error, the call runs
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


# ``-E`` ignores PYTHON* variables and ``-s`` the user site. Not ``-I``: it implies ``-P`` (safe path), which
# drops the script directory, and a hook imports its sibling ``lib`` from there.
HOOK_INTERPRETER_FLAGS = ("-E", "-s")


def command_of(row: Row) -> list[str]:
    path = Path(row.script)
    if not path.is_file():
        raise MissingScript(str(path))
    argv = [sys.executable, *HOOK_INTERPRETER_FLAGS, str(path)] if path.suffix == ".py" else [str(path)]
    return argv + shlex.split(row.args)


def hook_env(session_id: str, cwd: str) -> dict:
    env = dict(os.environ)
    env.update({RUNTIME_HOST_ENV: "opencode", HARNESS_SESSION_ENV: session_id, "CLAUDE_PROJECT_DIR": cwd})
    return env


def run_row(row: Row, payload: dict, env: dict) -> Decision:
    argv = command_of(row)
    try:
        proc = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                              timeout=row.timeout_s, env=env, cwd=payload["cwd"])
    except subprocess.TimeoutExpired:
        return Decision()  # Claude Code treats a timed-out hook as a non-blocking error: the call runs
    decision = parse_hook_output(proc.returncode, proc.stdout, proc.stderr)
    decision.script = os.path.basename(row.script)
    return decision


def run_rows(rows: list[Row], payload: dict) -> Decision:
    """Run ``rows`` against ``payload`` side by side and combine their decisions in row order. The hooks run in
    the payload's ``cwd``, which must be a directory: this process's own is never a stand-in."""
    for row in rows:
        command_of(row)
    if not rows:
        return Decision()
    if not payload.get("cwd"):
        raise UnknownDirectory("the directory the session runs in is unknown, so no hook can be run in it")
    env = hook_env(payload.get("session_id", ""), payload["cwd"])
    with ThreadPoolExecutor(max_workers=min(len(rows), 8)) as pool:
        decisions = list(pool.map(lambda r: run_row(r, payload, env), rows))
    return combine(decisions)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_payload(event: str, data: dict, *, transcript_path: Path, plugin: frozenset[str],
                  environ=None, servers: frozenset[str] = frozenset(), directory: str = "",
                  declared: dict | None = None, row_scripts=()) -> dict:
    """The Claude-shaped payload of an OpenCode event ``data`` (the hook input, as the probe recorded it).
    ``row_scripts`` are the scripts of the rows that will judge the call; a write into one is refused.
    ``cwd`` is the session ``directory``, or for a shell call with a ``workdir`` the directory the command will
    run in; paths of the tool input are resolved by ``resolve_path``. A before-event whose tool is not one the
    session knows, or whose call cannot be shown, raises ``UnshowableCall``; an after-event, which no gate can
    still prevent, keeps the tool name None and the path as written."""
    opencode_session = data.get("sessionID") or ""
    session = payload_session_id(opencode_session, environ)
    payload = {"session_id": session, "transcript_path": str(transcript_path), "cwd": directory,
               "hook_event_name": EVENT_OF[event]}
    if event in ("tool.execute.before", "tool.execute.after"):
        tool, args = data.get("tool") or "", data.get("input") or {}
        strict = event == "tool.execute.before"
        payload["tool_name"] = classify_tool(tool, plugin, servers) if strict else claude_tool(tool, plugin, servers)
        payload["cwd"] = run_directory(tool, args, directory, strict=strict)
        payload["tool_input"] = claude_input(tool, args, directory, strict=strict)
        if strict:
            if payload["tool_name"] == "Edit":
                require_exact_edit(payload["tool_input"]["file_path"], payload["tool_input"]["old_string"],
                                   payload["tool_input"].get("replace_all") is True)
            plugin_paths = (declared if declared is not None else declared_config(environ))["plugin_paths"]
            require_no_config_dir_write(payload["tool_name"], payload["tool_input"], payload["cwd"], environ,
                                        directory=directory, plugin_paths=plugin_paths, extra=tuple(row_scripts))
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


def transcript_records(event: str, data: dict, session: str, *, plugin: frozenset[str] = frozenset(),
                       servers: frozenset[str] = frozenset()) -> list[dict]:
    """The Claude-format transcript lines an event adds to the shadow transcript."""
    base = {"sessionId": session, "timestamp": now(), "cwd": data.get("cwd", "")}
    if event == "session.prompt":
        text = ((data.get("prompt") or {}).get("text")) or ""
        return [{**base, "type": "user", "uuid": str(uuid.uuid4()),
                 "message": {"role": "user", "content": [{"type": "text", "text": text}]}}]
    tool, args = data.get("tool") or "", data.get("input") or {}
    name = claude_tool(tool, plugin, servers) or tool
    if event == "tool.execute.before":
        return [{**base, "type": "assistant", "uuid": str(uuid.uuid4()),
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": data.get("id"), "name": name,
                      "input": claude_input(tool, args, data.get("cwd") or "", strict=False)}]}}]
    if event == "tool.execute.after":
        response = claude_response(tool, args, data.get("result") or {})
        text = response.get("content") if isinstance(response, dict) and "content" in response \
            else json.dumps(response)
        return [{**base, "type": "user", "uuid": str(uuid.uuid4()),
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": data.get("id"), "content": text}]}}]
    return []


def credential_paths(integration: Path, agent_home: Path, credential_file: str | None) -> tuple[Path, ...]:
    """The paths that hold credentials or what the gates saw, resolved absolute: OpenCode's auth store in the
    integration directory, the shadow transcripts directory (every tool call and result the gates were
    shown, which a model that read it would see unfiltered), the rows snapshots (which name every script the
    gates run), the per-machine settings file under the agent home, and the credential file the settings name.
    Reads no setting itself."""
    found = [Path(integration) / "data" / "opencode" / "auth.json", transcripts_dir(integration),
             snapshots_dir(integration), Path(agent_home) / SETTINGS_FILE]
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
