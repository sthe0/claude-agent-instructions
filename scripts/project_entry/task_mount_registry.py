"""Persisted ticket -> mount-name binding for `enter-task.sh --key`.

Difficulty removed: `--key <TICKET>` derives the mount name by slugifying the
ticket's CURRENT title (tracker_resolve -> slugify.py) on every invocation, with
no persisted record of which name was chosen. Renaming the ticket in the
tracker then silently changes the derived name on the next `--key` call, which
`backend_ensure_workspace` treats as a brand-new mount request — creating a
second, disconnected working copy instead of reusing the one already in
progress. This module lets the launcher bind a ticket key to its mount name
once and reuse that binding regardless of later title drift.

One JSON record per ticket key, `{"name": "<mount-name>"}`, under a
machine-local root (mirrors `projects.d` — not versioned, one registry per
machine). Pure functions take the root as a parameter; host I/O (default root
resolution, actual file read/write) is confined to __main__ so tests run
offline against a temp root.

CLI:
  task_mount_registry.py get ROOT KEY           -> print the bound name, exit 1 if unbound
  task_mount_registry.py set ROOT KEY NAME      -> write the binding, print its path
  task_mount_registry.py get KEY                -> same, ROOT defaults per _default_root()
  task_mount_registry.py set KEY NAME           -> same
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def _record_path(root: str, key: str) -> str:
    # Ticket keys (PROJ-440) are already filename-safe; sanitize defensively
    # so an unexpected selector (a URL, a key with a slash) can't escape root.
    safe = _SAFE_KEY.sub("_", key) or "_"
    return os.path.join(root, f"{safe}.json")


def get_name(root: str, key: str, read_file: Callable[[str], "str | None"]) -> "str | None":
    """Return the bound mount name for `key`, or None if unbound/unreadable."""
    text = read_file(_record_path(root, key))
    if text is None:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    return name if isinstance(name, str) and name else None


def set_name(
    root: str,
    key: str,
    name: str,
    write_file: Callable[[str, str], None],
) -> str:
    """Write the binding for `key` -> `name`; return the record path."""
    path = _record_path(root, key)
    write_file(path, json.dumps({"name": name}, indent=2, sort_keys=True) + "\n")
    return path


# ── Host I/O — confined to __main__ below ───────────────────────────────────

def _real_read(path: str) -> "str | None":
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _real_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _default_root(getenv: Callable[[str], "str | None"]) -> str:
    """Machine-local registry root: CLAUDE_TASK_MOUNTS_DIR, else <config root>/task-mounts.d.

    Mirrors projects.sh::_projects_local_dir's config-root resolution (override
    -> isolated -> legacy), kept inline so this stays usable standalone.
    """
    explicit = getenv("CLAUDE_TASK_MOUNTS_DIR")
    if explicit:
        return explicit
    config_home = getenv("CLAUDE_AGENT_HOME") or getenv("CLAUDE_CONFIG_DIR")
    if not config_home:
        home = getenv("HOME") or os.path.expanduser("~")
        isolated = os.path.join(home, ".claude-agent")
        config_home = isolated if os.path.isdir(isolated) else os.path.join(home, ".claude")
    return os.path.join(config_home, "task-mounts.d")


def main(argv: "list[str]") -> int:
    if not argv:
        print("usage: task_mount_registry.py {get|set} [ROOT] KEY [NAME]", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]

    if cmd == "get":
        if len(rest) == 1:
            root, key = _default_root(os.environ.get), rest[0]
        elif len(rest) == 2:
            root, key = rest[0], rest[1]
        else:
            print("usage: task_mount_registry.py get [ROOT] KEY", file=sys.stderr)
            return 2
        name = get_name(root, key, _real_read)
        if name is None:
            return 1
        print(name)
        return 0

    if cmd == "set":
        if len(rest) == 2:
            root, key, name = _default_root(os.environ.get), rest[0], rest[1]
        elif len(rest) == 3:
            root, key, name = rest[0], rest[1], rest[2]
        else:
            print("usage: task_mount_registry.py set [ROOT] KEY NAME", file=sys.stderr)
            return 2
        path = set_name(root, key, name, _real_write)
        print(path)
        return 0

    print(f"task_mount_registry: unknown command '{cmd}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
