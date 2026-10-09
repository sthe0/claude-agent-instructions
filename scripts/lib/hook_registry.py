"""Load the shared hook registry (scripts/hooks/desired.json).

Claude installer consumes rows with claude_event set. Cursor-only rows are
skipped there so they cannot change generated Claude wiring.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from lib import cursor_hook_contract

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "hooks" / "desired.json"

ClaudeRow = tuple[str, str | None, str, int]

METADATA_ROW_ID = "native-auto-memory-extraction"

REQUIRED_CURSOR_MAPPED_COMMANDS = (
    "hook-experience-record-reminder.py",
    "hook-self-improvement-reminder.py",
    "hook-turn-end-gate.py",
    "hook-skill-first.py",
)

CURSOR_GUARDIAN_HOOKS = (
    "hook-state-gate.py",
    "hook-turn-end-gate.py",
)


def _registry_path(path: Path | None) -> Path:
    if path is not None:
        return path
    env_path = os.environ.get("HOOK_REGISTRY_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return REGISTRY_PATH


def load_registry(path: Path | None = None) -> dict[str, Any]:
    target = _registry_path(path)
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), list):
        raise ValueError(f"hook registry {target} is not an object with a hooks list")
    return data


def _is_metadata_row(entry: dict[str, Any]) -> bool:
    return entry.get("id") == METADATA_ROW_ID


def _has_value(field: Any) -> bool:
    if field is None:
        return False
    if isinstance(field, str):
        return bool(field.strip())
    return True


def _command_basename(entry: dict[str, Any]) -> str | None:
    command = entry.get("command")
    if not command:
        return None
    return Path(str(command).split()[0]).name


def _row_matcher(entry: dict[str, Any]) -> str | None:
    matcher = entry.get("cursor_matcher")
    if matcher:
        return matcher
    return cursor_hook_contract.cursor_matcher_for(entry.get("claude_matcher"))


def validate_registry_row(entry: dict[str, Any], index: int) -> list[str]:
    """Return structural problems for one registry row."""
    problems: list[str] = []
    label = f"hooks[{index}]"
    if _is_metadata_row(entry):
        if entry.get("command"):
            problems.append(f"{label}: metadata row {METADATA_ROW_ID!r} must not carry command")
        for field in ("claude_skip_reason", "cursor_skip_reason"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{label}: metadata row must have non-empty {field}")
        return problems

    command = entry.get("command")
    if not command:
        problems.append(f"{label}: hook row missing command")
        return problems

    claude_event = entry.get("claude_event")
    claude_skip = entry.get("claude_skip_reason")
    has_claude_event = _has_value(claude_event)
    has_claude_skip = _has_value(claude_skip)
    if has_claude_event == has_claude_skip:
        problems.append(
            f"{label} ({command}): exactly one of claude_event or claude_skip_reason "
            f"must be set (event={claude_event!r}, skip={claude_skip!r})"
        )
    if has_claude_skip and isinstance(claude_skip, str) and not claude_skip.strip():
        problems.append(f"{label} ({command}): claude_skip_reason must be non-empty when present")

    cursor_event = entry.get("cursor_event")
    cursor_skip = entry.get("cursor_skip_reason")
    has_cursor_event = _has_value(cursor_event)
    has_cursor_skip = _has_value(cursor_skip)
    if has_cursor_event == has_cursor_skip:
        problems.append(
            f"{label} ({command}): exactly one of cursor_event or cursor_skip_reason "
            f"must be set (event={cursor_event!r}, skip={cursor_skip!r})"
        )
    if has_cursor_skip and isinstance(cursor_skip, str) and not cursor_skip.strip():
        problems.append(f"{label} ({command}): cursor_skip_reason must be non-empty when present")

    if entry.get("role") == "gate" and "cursor_fail_closed" not in entry:
        problems.append(
            f"{label} ({command}): gate row must declare cursor_fail_closed true or false"
        )

    basename = _command_basename(entry)
    if basename in REQUIRED_CURSOR_MAPPED_COMMANDS and not has_cursor_event:
        problems.append(
            f"{label} ({command}): {basename!r} is REQUIRED_CURSOR_MAPPED but has no cursor_event"
        )

    return problems


def validate_registry(data: dict[str, Any] | None = None, path: Path | None = None) -> list[str]:
    """Validate every row in the shared hook registry."""
    registry = data if data is not None else load_registry(path)
    problems: list[str] = []
    hooks = registry.get("hooks", [])
    if not isinstance(hooks, list):
        return ["registry hooks is not a list"]

    metadata_rows = [entry for entry in hooks if _is_metadata_row(entry)]
    if len(metadata_rows) != 1:
        problems.append(
            f"registry must contain exactly one metadata row id={METADATA_ROW_ID!r}, "
            f"found {len(metadata_rows)}"
        )

    for index, entry in enumerate(hooks):
        if not isinstance(entry, dict):
            problems.append(f"hooks[{index}] is not an object")
            continue
        problems.extend(validate_registry_row(entry, index))

    return problems


def _is_cursor_direct_row(entry: dict[str, Any]) -> bool:
    return bool(entry.get("cursor_event")) and not entry.get("claude_event")


def cursor_adapter_install_expectations(path: Path | None = None) -> list[dict[str, Any]]:
    """Grouped hook-cursor-adapt.py rows derived from Claude-mapped registry entries."""
    grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
    for entry in load_registry(path)["hooks"]:
        if _is_metadata_row(entry) or _is_cursor_direct_row(entry):
            continue
        cursor_event = entry.get("cursor_event")
        if not cursor_event:
            continue
        matcher = _row_matcher(entry)
        key = (cursor_event, matcher)
        timeout = int(entry.get("timeout") or 30)
        fail_closed = bool(entry.get("cursor_fail_closed"))
        if key not in grouped:
            grouped[key] = {
                "event": cursor_event,
                "matcher": matcher,
                "timeout": timeout,
                "failClosed": fail_closed,
            }
        else:
            grouped[key]["timeout"] = max(grouped[key]["timeout"], timeout)
            if fail_closed:
                grouped[key]["failClosed"] = True
    return list(grouped.values())


def cursor_direct_install_expectations(path: Path | None = None) -> list[dict[str, Any]]:
    """Cursor-only registry rows installed as direct command hooks (not via adapter)."""
    rows: list[dict[str, Any]] = []
    for entry in load_registry(path)["hooks"]:
        if _is_metadata_row(entry) or not _is_cursor_direct_row(entry):
            continue
        command = str(entry["command"])
        rows.append(
            {
                "event": entry["cursor_event"],
                "matcher": _row_matcher(entry),
                "timeout": int(entry.get("timeout") or 30),
                "failClosed": bool(entry.get("cursor_fail_closed")),
                "command": command,
                "managed_marker": Path(command.split()[0]).name,
            }
        )
    return rows


def cursor_install_expectations(path: Path | None = None) -> list[dict[str, Any]]:
    """Adapter-grouped rows install-cursor-hooks.py materializes from the registry."""
    return cursor_adapter_install_expectations(path)


def cursor_managed_markers(path: Path | None = None) -> tuple[str, ...]:
    """Command substrings that identify managed Cursor hook installs."""
    markers = ["hook-cursor-adapt.py"]
    for row in cursor_direct_install_expectations(path):
        marker = row["managed_marker"]
        if marker not in markers:
            markers.append(marker)
    return tuple(markers)


def cursor_mapped_command_basenames(
    path: Path | None = None, data: dict[str, Any] | None = None
) -> set[str]:
    """Hook script basenames with a Cursor event mapping (not skipped)."""
    hooks = (data if data is not None else load_registry(path)).get("hooks", [])
    names: set[str] = set()
    for entry in hooks:
        if _is_metadata_row(entry) or not entry.get("cursor_event"):
            continue
        basename = _command_basename(entry)
        if basename:
            names.add(basename)
    return names


def check_cursor_guardians(
    path: Path | None = None, data: dict[str, Any] | None = None
) -> list[str]:
    """Each engine guardian must be Cursor-mapped in the registry."""
    mapped = cursor_mapped_command_basenames(path=path, data=data)
    problems: list[str] = []
    for hook in CURSOR_GUARDIAN_HOOKS:
        if hook not in mapped:
            problems.append(
                f"Cursor guardian {hook!r} is not registered with cursor_event in the hook registry"
            )
    return problems


def claude_desired_tuples(path: Path | None = None) -> list[ClaudeRow]:
    rows: list[ClaudeRow] = []
    for entry in load_registry(path)["hooks"]:
        if _is_metadata_row(entry):
            continue
        event = entry.get("claude_event")
        if not event:
            continue
        matcher = entry.get("claude_matcher")
        command = entry["command"]
        timeout = entry["timeout"]
        rows.append((event, matcher, command, int(timeout)))
    return rows


def claude_hook_basenames(path: Path | None = None) -> set[str]:
    names: set[str] = set()
    for _event, _matcher, command, _timeout in claude_desired_tuples(path):
        names.add(Path(command.split()[0]).name)
    return names


def _managed_hooks_for_event(
    hooks_doc: dict[str, Any],
    event: str,
    managed_markers: tuple[str, ...],
    *,
    required_marker: str | None = None,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for entry in hooks_doc.get("hooks", {}).get(event, []):
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command") or "")
        if "hooks" in entry:
            continue
        if required_marker is not None:
            if required_marker not in command:
                continue
        elif not any(marker in command for marker in managed_markers):
            continue
        found.append(entry)
    return found


def _check_expected_hook_row(
    hooks_doc: dict[str, Any],
    row: dict[str, Any],
    managed_markers: tuple[str, ...],
    *,
    required_marker: str | None = None,
) -> list[str]:
    event = row["event"]
    matcher = row.get("matcher")
    candidates = _managed_hooks_for_event(
        hooks_doc,
        event,
        managed_markers,
        required_marker=required_marker,
    )
    matched = [
        hook for hook in candidates if (hook.get("matcher") or None) == (matcher or None)
    ]
    problems: list[str] = []
    if not matched:
        problems.append(
            f"installed hooks.json missing managed entry for event={event!r} "
            f"matcher={matcher!r}"
        )
        return problems
    hook = matched[0]
    if int(hook.get("timeout") or 0) < int(row["timeout"]):
        problems.append(
            f"installed hook event={event!r} matcher={matcher!r} timeout "
            f"{hook.get('timeout')!r} < expected {row['timeout']!r}"
        )
    if row.get("failClosed") and not hook.get("failClosed"):
        problems.append(
            f"installed hook event={event!r} matcher={matcher!r} missing failClosed=true"
        )
    return problems


def check_cursor_installation(
    hooks_path: Path,
    managed_marker: str | None = None,
    path: Path | None = None,
) -> list[str]:
    """Verify a live hooks.json contains every registry-derived managed row."""
    if not hooks_path.is_file():
        return [f"Cursor hooks file missing: {hooks_path}"]

    try:
        hooks_doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read Cursor hooks file {hooks_path}: {exc}"]

    managed_markers = cursor_managed_markers(path)
    if managed_marker is not None and managed_marker not in managed_markers:
        managed_markers = (managed_marker, *managed_markers)

    problems: list[str] = []
    for row in cursor_adapter_install_expectations(path):
        problems.extend(
            _check_expected_hook_row(
                hooks_doc,
                row,
                managed_markers,
                required_marker="hook-cursor-adapt.py",
            )
        )
    for row in cursor_direct_install_expectations(path):
        problems.extend(
            _check_expected_hook_row(
                hooks_doc,
                row,
                managed_markers,
                required_marker=row["managed_marker"],
            )
        )
    return problems
