"""Register machine-defined MCP servers where Claude Code actually reads them.

Difficulty removed: ``apply-mcp-local.sh`` merged ``mcp-local/*.json`` into a
settings file, but Claude Code takes user-scope MCP servers only from the
top-level ``mcpServers`` of ``<config root>/.claude.json`` — so setup succeeded
and ``claude mcp list`` never showed the server. This module writes to the file
the CLI documents as its user-scope store, and ``check`` lets ``doctor.sh`` name
a defined-but-unregistered server without running the CLI.

Sources (each ``<name>.json`` is one server, name = file stem):
  * personal:  ``$CLAUDE_MCP_LOCAL_DIR`` else ``<repo>/mcp-local``
  * org layer: ``$CLAUDE_MCP_PLUGIN_DIR`` else ``<config root>/mcp-plugins``
A name present in both resolves to the personal definition.

Target: ``<root>/.claude.json`` with ``root`` = ``$CLAUDE_AGENT_HOME`` else the
resolved agent home. Registration is a direct atomic merge, never
``claude mcp add-json``, whose server JSON (env tokens included) is a positional
argument readable from ``/proc/<pid>/cmdline`` by other local users. Output
carries server names and source kind only — never a definition value.

Exit codes: 0 ok / nothing to do / not logged in; 1 server missing (``check``)
or not confirmed after the write (``apply``); 2 malformed definition, unreadable
target, or checker error.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib.config_root import agent_home  # noqa: E402
from lib.plugin_dir import resolve_plugin_dir  # noqa: E402

LOCAL = "mcp-local"
PLUGIN = "mcp-plugins"
_NOT_LOGGED_IN = (
    "config root is not logged in yet: log in once "
    "(CLAUDE_CONFIG_DIR=<root> claude auth login), then re-run apply-mcp-local.sh"
)


class RegistrationError(Exception):
    """Exit-2 failure; the message names a file, never a definition value."""


def target_file() -> Path:
    override = os.environ.get("CLAUDE_AGENT_HOME")
    root = Path(override).expanduser() if override else agent_home()
    return root / ".claude.json"


def _local_dir() -> Path:
    override = os.environ.get("CLAUDE_MCP_LOCAL_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "mcp-local"


def _read_definitions(directory: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RegistrationError(f"malformed definition {path}: {exc.msg}") from None
        except (OSError, UnicodeDecodeError):
            raise RegistrationError(f"unreadable definition {path}") from None
        if not isinstance(cfg, dict):
            raise RegistrationError(f"definition {path} is not a JSON object")
        out[path.stem] = cfg
    return out


def collect_sources() -> tuple[dict[str, tuple[str, dict]], list[str]]:
    """(name -> (kind, definition), notices). Parses every source before returning."""
    local = _read_definitions(_local_dir())
    plugin = _read_definitions(resolve_plugin_dir("CLAUDE_MCP_PLUGIN_DIR", PLUGIN))
    notices = [
        f"{name} is defined in both {LOCAL} and {PLUGIN}; using the {LOCAL} definition"
        for name in sorted(set(local) & set(plugin))
    ]
    merged = {name: (PLUGIN, cfg) for name, cfg in plugin.items()}
    merged.update({name: (LOCAL, cfg) for name, cfg in local.items()})
    return dict(sorted(merged.items())), notices


def _load_target(path: Path) -> dict | None:
    """Parsed ``.claude.json``, or None when absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise RegistrationError(f"unreadable {path}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistrationError(f"{path} is not valid JSON: {exc.msg}") from None
    if not isinstance(doc, dict):
        raise RegistrationError(f"{path} is not a JSON object")
    return doc


def _logged_in(doc: dict | None) -> bool:
    return bool(doc) and "oauthAccount" in doc


def _registered(doc: dict) -> dict:
    servers = doc.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _write_atomic(path: Path, doc: dict) -> None:
    real = Path(os.path.realpath(path))
    mode = real.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply() -> int:
    sources, notices = collect_sources()
    for line in notices:
        print(f"notice: {line}")
    if not sources:
        print("no MCP server definitions found, nothing to do")
        return 0
    path = target_file()
    # Read straight before the merge: a running session may rewrite this file.
    doc = _load_target(path)
    if not _logged_in(doc):
        print(f"notice: {_NOT_LOGGED_IN}")
        return 0
    servers = _registered(doc)
    changed = [name for name, (_, cfg) in sources.items() if servers.get(name) != cfg]
    if not changed:
        print(f"up to date: {len(sources)} MCP server(s) already registered in {path}")
        return 0
    merged = dict(servers)
    for name in changed:
        merged[name] = sources[name][1]
    doc["mcpServers"] = merged
    _write_atomic(path, doc)

    confirmed = _registered(_load_target(path) or {})
    lost = [name for name in changed if confirmed.get(name) != sources[name][1]]
    if lost:
        print(f"error: not confirmed after write: {', '.join(lost)}", file=sys.stderr)
        return 1
    for name in changed:
        print(f"registered: {name} ({sources[name][0]})")
    print(f"Done. Updated {path}")
    return 0


def check() -> int:
    sources, notices = collect_sources()
    for line in notices:
        print(f"notice: {line}")
    if not sources:
        return 0
    doc = _load_target(target_file())
    if not _logged_in(doc):
        print(f"notice: {_NOT_LOGGED_IN}")
        return 0
    servers = _registered(doc)
    missing = [name for name in sources if name not in servers]
    if missing:
        print(f"missing: {', '.join(missing)}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("apply", "check"):
        print("usage: mcp_registration.py apply|check", file=sys.stderr)
        return 2
    try:
        return apply() if argv[0] == "apply" else check()
    except RegistrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {type(exc).__name__}: {exc.strerror or 'I/O failure'}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
