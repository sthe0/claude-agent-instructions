"""Named, two-root project registry for the task-entry subsystem.

Difficulty removed: the launcher used to hardcode one consumer's tracker queue
and guess the workspace from PWD magic. This module replaces both with a named
registry of records. Each ``agent-project.json`` under a registry root describes
one project; the record drives workspace subpath/root, tracker backend, and
tracker queue. A record's KEY is the directory path that holds it, relative to
its root (e.g. ``team/web``) — the location IS the name.

Two roots, merged by key:
  1. shared / versioned root(s) first — portable definitions distributed to the
     team (no machine-local absolute paths);
  2. machine-local ``~/.claude/projects.d`` last — absolute git checkout paths
     written by ``--register`` on this machine.
A later root completes/overrides an earlier one field-by-field, so a local
record can attach a machine-specific ``workspace_path`` to a portable record.

Every function that resolves records is PURE — all host I/O (directory walk,
file read/write, stderr) is injected via callbacks, so tests run offline against
temp roots. This mirrors detect_backend.py's injected-probe idiom. The module is
org-neutral: it carries no project, tracker, or org-specific constants — only the
mechanism that finds and resolves records by name.

CLI (host I/O lives only here, in __main__):
  projects.py list                        -> print the registry table
  projects.py resolve [SELECTOR]          -> print the resolved key (exit 1 if none)
  projects.py fields [SELECTOR]           -> print resolved record as key=value lines (exit 1 if none)
  projects.py register ROOT KEY [k=v ...] -> write a machine-local record, print its path
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Iterable

# Record schema fields this module understands. Unknown keys are passed through
# unchanged (e.g. instruction_queue), so a record can carry data other tools own.
_KNOWN_FIELDS = (
    "workspace_subpath",   # explicit, relative — NOT derived from the key
    "workspace_path",      # optional, absolute, machine-local only
    "workspace_backend",   # optional backend NAME
    "tracker_backend",     # tracker backend NAME
    "tracker_queue",       # queue the tracker creates tasks in
)

# Each merged record carries its key under this reserved field.
KEY_FIELD = "_key"


def _noop_warn(_msg: str) -> None:
    """Default warn sink — pure no-op so load_records stays side-effect-free."""


def load_records(
    roots: "list[str]",
    read_file: Callable[[str], "str | None"],
    walk: Callable[[str], "Iterable[tuple[str, str]]"],
    on_warn: Callable[[str], None] = _noop_warn,
) -> "list[dict]":
    """Load and merge records across an ORDERED list of roots.

    roots      -- shared root(s) first, machine-local last. Later roots
                  complete/override earlier ones, field by field.
    walk(root) -- yields (key, path) for each agent-project.json found under
                  root, where key is the POSIX directory path relative to root.
    read_file  -- (path) -> text, or None when the file is unreadable.
    on_warn    -- (message) sink for malformed/skipped records; default no-op.

    Returns a list of merged record dicts, each carrying its key under KEY_FIELD,
    in first-seen key order. Never raises on a bad record — it is skipped with a
    warning so one broken file cannot hide the rest of the registry.
    """
    merged: "dict[str, dict]" = {}
    order: "list[str]" = []
    for root in roots:
        for key, path in walk(root):
            text = read_file(path)
            if text is None:
                on_warn(f"projects: cannot read {path}, skipping")
                continue
            try:
                raw = json.loads(text)
            except ValueError:
                on_warn(f"projects: malformed JSON in {path}, skipping")
                continue
            if not isinstance(raw, dict):
                on_warn(f"projects: {path} is not a JSON object, skipping")
                continue
            if key not in merged:
                merged[key] = {}
                order.append(key)
            for field, value in raw.items():
                if field == KEY_FIELD:
                    continue
                merged[key][field] = value
            merged[key][KEY_FIELD] = key
    return [merged[k] for k in order]


def _is_path_suffix(pwd: str, cand: str) -> bool:
    """True when cand is a path-component suffix of pwd (boundary-aware).

    'team/web' is a suffix of '/home/x/team/web' but 'eb' is NOT a suffix of
    '.../web'. Trailing slashes are ignored.
    """
    pwd = pwd.rstrip("/")
    cand = cand.rstrip("/")
    if not cand:
        return False
    return pwd == cand or pwd.endswith("/" + cand)


def resolve(
    records: "list[dict]",
    selector: "str | None" = None,
    pwd: "str | None" = None,
) -> "dict | None":
    """Resolve a single record.

    With a selector: match it against the record key first, then workspace_path.
    Without a selector: pick the record whose workspace_subpath or workspace_path
    is the LONGEST path-suffix of pwd. Returns None when nothing matches.
    """
    if selector:
        for rec in records:
            if rec.get(KEY_FIELD) == selector:
                return rec
        for rec in records:
            if rec.get("workspace_path") == selector:
                return rec
        return None

    if not pwd:
        return None

    best: "dict | None" = None
    best_len = -1
    for rec in records:
        for field in ("workspace_subpath", "workspace_path"):
            cand = rec.get(field)
            if isinstance(cand, str) and _is_path_suffix(pwd, cand) and len(cand) > best_len:
                best = rec
                best_len = len(cand)
    return best


def format_table(records: "list[dict]") -> str:
    """Render records as a keyed, aligned table for --list-projects / usage."""
    if not records:
        return "(no projects registered)"
    headers = ("KEY", "BACKEND", "TRACKER", "QUEUE", "SUBPATH")
    rows = []
    for rec in sorted(records, key=lambda r: r.get(KEY_FIELD, "")):
        rows.append(
            (
                rec.get(KEY_FIELD, "") or "-",
                rec.get("workspace_backend", "") or "-",
                rec.get("tracker_backend", "") or "-",
                rec.get("tracker_queue", "") or "-",
                rec.get("workspace_subpath", "") or "-",
            )
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers).rstrip()]
    lines += [fmt.format(*row).rstrip() for row in rows]
    return "\n".join(lines)


def register(
    root: str,
    key: str,
    fields: "dict",
    write_file: Callable[[str, str], None],
) -> str:
    """Write a machine-local record for `key` under `root`; return its path.

    fields is serialized as pretty JSON. write_file(path, text) owns directory
    creation and the actual write (host I/O), keeping this function pure.
    """
    path = os.path.join(root, *key.split("/"), "agent-project.json")
    payload = {f: v for f, v in fields.items() if f != KEY_FIELD}
    write_file(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


# ── Host I/O — confined to __main__ below ───────────────────────────────────

def _real_walk(root: str) -> "list[tuple[str, str]]":
    found: "list[tuple[str, str]]" = []
    if not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        if "agent-project.json" in filenames:
            rel = os.path.relpath(dirpath, root)
            key = "" if rel == "." else rel.replace(os.sep, "/")
            found.append((key, os.path.join(dirpath, "agent-project.json")))
    return found


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


def _stderr_warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _default_roots(getenv: Callable[[str], "str | None"]) -> "list[str]":
    """Ordered registry roots from the environment.

    CLAUDE_PROJECT_ROOTS (os.pathsep-joined) wins when set — projects.sh builds
    the full ordered list (shared > identity, then machine-local) and exports it.
    Otherwise fall back to [CLAUDE_PROJECTS_DIR?, ~/.claude/projects.d] so the
    Python CLI is usable standalone.
    """
    explicit = getenv("CLAUDE_PROJECT_ROOTS")
    if explicit:
        return [r for r in explicit.split(os.pathsep) if r]
    roots: "list[str]" = []
    shared = getenv("CLAUDE_PROJECTS_DIR")
    if shared:
        roots.append(shared)
    home = getenv("HOME") or os.path.expanduser("~")
    roots.append(os.path.join(home, ".claude", "projects.d"))
    return roots


def _load_default() -> "list[dict]":
    roots = _default_roots(os.environ.get)
    return load_records(roots, _real_read, _real_walk, _stderr_warn)


def main(argv: "list[str]") -> int:
    if not argv:
        print("usage: projects.py {list|resolve|register} ...", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]

    if cmd in ("list", "table"):
        print(format_table(_load_default()))
        return 0

    if cmd == "resolve":
        selector = rest[0] if rest else None
        rec = resolve(_load_default(), selector=selector, pwd=os.getcwd())
        if rec is None:
            return 1
        print(rec.get(KEY_FIELD, ""))
        return 0

    if cmd == "fields":
        # Output resolved record as key=value lines; exit 1 if nothing resolves.
        selector = rest[0] if rest else None
        rec = resolve(_load_default(), selector=selector, pwd=os.getcwd())
        if rec is None:
            return 1
        print(f"{KEY_FIELD}={rec.get(KEY_FIELD, '')}")
        for field in _KNOWN_FIELDS:
            val = rec.get(field)
            if val is not None and isinstance(val, str):
                print(f"{field}={val}")
        return 0

    if cmd == "register":
        if len(rest) < 2:
            print("usage: projects.py register ROOT KEY [field=value ...]", file=sys.stderr)
            return 2
        root, key = rest[0], rest[1]
        fields: "dict" = {}
        for pair in rest[2:]:
            if "=" not in pair:
                print(f"projects: ignoring malformed field '{pair}'", file=sys.stderr)
                continue
            field, value = pair.split("=", 1)
            fields[field] = value
        path = register(root, key, fields, _real_write)
        print(path)
        return 0

    print(f"projects: unknown command '{cmd}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
