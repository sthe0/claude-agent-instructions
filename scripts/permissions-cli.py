#!/usr/bin/env python3
"""CLI for workflow-level permission grants.

Reads / writes permissions/*.json files (default: this repo's
permissions/global.json). Replaces the free-form Markdown table the
manager used to scan by eye.

See permissions/README.md for schema and file conventions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = REPO_ROOT / "permissions" / "global.json"

REQUIRED_FIELDS = ("pattern", "granted_at", "context")


def load(path: Path) -> dict:
    if not path.exists():
        return {"permissions": []}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("permissions", [])
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def matches(pattern: str, action: str) -> bool:
    """Return True if `action` satisfies `pattern`.

    Glob form (`*` / `?` present) → fnmatch case-insensitive.
    Otherwise → case-insensitive substring match.
    """
    pat = pattern.strip()
    act = action.strip()
    if any(c in pat for c in "*?["):
        return fnmatch.fnmatchcase(act.lower(), pat.lower())
    return pat.lower() in act.lower()


def cmd_list(args: argparse.Namespace) -> int:
    data = load(args.file)
    perms = data["permissions"]
    if not perms:
        print(f"(no grants in {args.file.relative_to(REPO_ROOT) if args.file.is_relative_to(REPO_ROOT) else args.file})")
        return 0
    width = max(len(p["pattern"]) for p in perms)
    for p in perms:
        print(f"  {p['pattern']:<{width}}  {p['granted_at']}  {p['context']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    data = load(args.file)
    for p in data["permissions"]:
        if matches(p["pattern"], args.action):
            print(f"GRANTED: {p['pattern']!r} matches {args.action!r} "
                  f"(granted {p['granted_at']}, {p['context']})")
            return 0
    print(f"NOT GRANTED: {args.action!r}")
    return 1


def cmd_grant(args: argparse.Namespace) -> int:
    data = load(args.file)
    granted_at = args.date or dt.date.today().isoformat()
    # Idempotent on exact pattern: update context/date if same pattern exists.
    for p in data["permissions"]:
        if p["pattern"] == args.pattern:
            p["granted_at"] = granted_at
            p["context"] = args.context
            save(args.file, data)
            print(f"UPDATED: {args.pattern!r}")
            return 0
    data["permissions"].append({
        "pattern": args.pattern,
        "granted_at": granted_at,
        "context": args.context,
    })
    save(args.file, data)
    print(f"GRANTED: {args.pattern!r} (granted {granted_at}, {args.context})")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    data = load(args.file)
    before = len(data["permissions"])
    data["permissions"] = [p for p in data["permissions"] if p["pattern"] != args.pattern]
    after = len(data["permissions"])
    if before == after:
        print(f"NOT FOUND: {args.pattern!r}")
        return 1
    save(args.file, data)
    print(f"REVOKED: {args.pattern!r}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Print a multi-line digest suitable for embedding in a specialist spawn prompt."""
    data = load(args.file)
    perms = data["permissions"]
    if not perms:
        return 0  # silent: nothing to embed
    print("Permissions previously granted (apply during your work):")
    for p in perms:
        print(f"  - {p['pattern']} — {p['context']} (since {p['granted_at']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_FILE,
        help=f"permissions JSON file (default: {DEFAULT_FILE.relative_to(REPO_ROOT)})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show all grants").set_defaults(func=cmd_list)

    c = sub.add_parser("check", help="exit 0 if action is granted")
    c.add_argument("action")
    c.set_defaults(func=cmd_check)

    g = sub.add_parser("grant", help="add a new grant (idempotent on pattern)")
    g.add_argument("pattern")
    g.add_argument("--context", required=True, help="one-line reason for the grant")
    g.add_argument("--date", help="grant date YYYY-MM-DD (default: today)")
    g.set_defaults(func=cmd_grant)

    r = sub.add_parser("revoke", help="remove a grant by exact pattern")
    r.add_argument("pattern")
    r.set_defaults(func=cmd_revoke)

    sub.add_parser(
        "digest",
        help="human-readable digest for embedding in a specialist spawn prompt",
    ).set_defaults(func=cmd_digest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
