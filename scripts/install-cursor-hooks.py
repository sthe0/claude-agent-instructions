#!/usr/bin/env python3
"""Install managed Cursor hooks from the shared registry into hooks.json.

Preserves unrelated hook entries (for example the existing Arcadia shell
guards) and only adds or updates rows owned by this installer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_registry  # noqa: E402

DEFAULT_CURSOR_HOOKS = Path.home() / ".cursor" / "hooks.json"


def _managed_markers() -> tuple[str, ...]:
    return hook_registry.cursor_managed_markers()


def _managed_command() -> str:
    return str((SCRIPTS_DIR / "hook-cursor-adapt.py").resolve())


def _direct_command(command: str) -> str:
    parts = command.split()
    resolved = str((SCRIPTS_DIR / parts[0]).resolve())
    if len(parts) > 1:
        return resolved + " " + " ".join(parts[1:])
    return resolved


def _desired_adapter_rows() -> list[dict]:
    return hook_registry.cursor_adapter_install_expectations()


def _desired_direct_rows() -> list[dict]:
    return hook_registry.cursor_direct_install_expectations()


def _is_managed(entry: dict) -> bool:
    command = str(entry.get("command") or "")
    return any(marker in command for marker in _managed_markers())


def _is_flat_hook(entry: dict) -> bool:
    return "command" in entry and "hooks" not in entry


def _is_group_hook(entry: dict) -> bool:
    return isinstance(entry.get("hooks"), list)


def _build_hook(row: dict, command: str) -> dict:
    hook: dict = {"command": command, "type": "command", "timeout": row["timeout"]}
    if row.get("matcher"):
        hook["matcher"] = row["matcher"]
    if row.get("failClosed"):
        hook["failClosed"] = True
    return hook


def _prune_managed_entries(hooks: dict) -> None:
    for event, entries in list(hooks.items()):
        kept: list[dict] = []
        for entry in entries:
            if _is_flat_hook(entry):
                if not _is_managed(entry):
                    kept.append(entry)
            elif _is_group_hook(entry):
                inner = [hook for hook in entry.get("hooks", []) if not _is_managed(hook)]
                if inner:
                    group = dict(entry)
                    group["hooks"] = inner
                    kept.append(group)
            else:
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def _upsert_hook(
    hooks: dict,
    row: dict,
    command: str,
    *,
    marker: str,
) -> None:
    event = row["event"]
    matcher = row.get("matcher")
    entries = hooks.setdefault(event, [])
    managed = [
        entry
        for entry in entries
        if _is_flat_hook(entry)
        and marker in str(entry.get("command") or "")
        and (entry.get("matcher") or None) == (matcher or None)
    ]
    hook = _build_hook(row, command)
    if managed:
        managed[0].update(hook)
    else:
        entries.append(hook)


def merge_hooks(existing: dict, desired_rows: list[dict], command: str) -> dict:
    merged = json.loads(json.dumps(existing))
    hooks = merged.setdefault("hooks", {})
    _prune_managed_entries(hooks)
    for row in desired_rows:
        _upsert_hook(hooks, row, command, marker="hook-cursor-adapt.py")
    merged["version"] = 1
    return merged


def merge_direct_hooks(existing: dict, direct_rows: list[dict]) -> dict:
    merged = json.loads(json.dumps(existing))
    hooks = merged.setdefault("hooks", {})
    _prune_managed_entries(hooks)
    for row in direct_rows:
        command = _direct_command(row["command"])
        _upsert_hook(hooks, row, command, marker=row["managed_marker"])
    merged["version"] = 1
    return merged


def install(path: Path | None = None, dry_run: bool = False) -> dict:
    target = path or DEFAULT_CURSOR_HOOKS
    adapter_rows = _desired_adapter_rows()
    direct_rows = _desired_direct_rows()
    adapter_command = _managed_command()
    if target.is_file():
        existing = json.loads(target.read_text(encoding="utf-8"))
    else:
        existing = {"version": 1, "hooks": {}}
    merged = json.loads(json.dumps(existing))
    hooks = merged.setdefault("hooks", {})
    _prune_managed_entries(hooks)
    for row in adapter_rows:
        _upsert_hook(hooks, row, adapter_command, marker="hook-cursor-adapt.py")
    for row in direct_rows:
        _upsert_hook(hooks, row, _direct_command(row["command"]), marker=row["managed_marker"])
    merged["version"] = 1
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return merged


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_CURSOR_HOOKS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    merged = install(args.path, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
    else:
        print(f"install-cursor-hooks: wrote {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
