"""Open enumeration tables of the telemetry record (roles, phases, loss modes, cause categories,
severity bands, functional places, the mechanism registry).

An enumeration is a versioned data table under `tables/<name>.toml`, never a literal
list in code: a member carries the table version it exists from (`since`) and, once
retired, the version it is retired from (`retired_since`); it is never deleted.
`tables.snapshot.json` records every version of every table with its members, so a
version stored on a row stays resolvable. A table's version is an integer that starts at
1 and grows by exactly 1 on every add or retire (`add_member` / `retire_member` are the
only editors). Every row that stores an enumerated value also stores the table version
`validate` returned for it.

Tests and maintenance point `AGENTCTL_TELEMETRY_TABLES_ROOT` at a copy of the directory
holding `tables/` and `tables.snapshot.json`.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT_ENV = "AGENTCTL_TELEMETRY_TABLES_ROOT"
TABLE_NAMES = ("roles", "phases", "loss_modes", "categories", "severity", "places", "registry")
_RESERVED_KEYS = ("name", "since", "retired_since")


class TableError(ValueError):
    """A value or version the table does not accept."""


@dataclass(frozen=True)
class Member:
    name: str
    since: int
    retired_since: "int | None" = None
    attrs: dict = field(default_factory=dict, compare=False)

    def live_at(self, version: int) -> bool:
        return self.since <= version and (self.retired_since is None or self.retired_since > version)


@dataclass(frozen=True)
class Table:
    name: str
    version: int
    members: "dict[str, Member]"


def data_root() -> Path:
    override = os.environ.get(ROOT_ENV)
    return Path(override) if override else Path(__file__).resolve().parent


def _table_path(name: str, root: "Path | None") -> Path:
    if name not in TABLE_NAMES:
        raise TableError(f"unknown table {name!r}")
    return (root or data_root()) / "tables" / f"{name}.toml"


def _snapshot_path(root: "Path | None") -> Path:
    return (root or data_root()) / "tables.snapshot.json"


def load(name: str, root: "Path | None" = None) -> Table:
    with _table_path(name, root).open("rb") as fh:
        data = tomllib.load(fh)
    members = {}
    for entry in data.get("members", []):
        attrs = {k: v for k, v in entry.items() if k not in _RESERVED_KEYS}
        members[entry["name"]] = Member(entry["name"], int(entry["since"]), entry.get("retired_since"), attrs)
    return Table(name, int(data["version"]), members)


def snapshot(root: "Path | None" = None) -> dict:
    return json.loads(_snapshot_path(root).read_text(encoding="utf-8"))


def known_version(name: str, version, root: "Path | None" = None) -> bool:
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return str(version) in snapshot(root).get(name, {})


def resolves(name: str, value, root: "Path | None" = None) -> bool:
    """True for every member ever present, retired ones included: old rows keep resolving."""
    return isinstance(value, str) and value in load(name, root).members


def validate(name: str, value, core_version: "int | None" = None, root: "Path | None" = None) -> int:
    """Check a new row's value against the table and return the table version it was
    validated against. `core_version` pins an integer table version; None means current.
    A retired, unknown or not-yet-added member is refused."""
    table = load(name, root)
    if core_version is None:
        version = table.version
    elif isinstance(core_version, int) and not isinstance(core_version, bool):
        if not known_version(name, core_version, root):
            raise TableError(f"{name} has no version {core_version}")
        version = core_version
    else:
        raise TableError(f"core_version must be an integer table version or None, got {core_version!r}")
    member = table.members.get(value) if isinstance(value, str) else None
    if member is None:
        raise TableError(f"{value!r} is not a member of {name}")
    if not member.live_at(version):
        raise TableError(f"{value!r} is not a live member of {name} at version {version}")
    return version


def _toml_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _render(version: int, members: "list[Member]") -> str:
    lines = [f"version = {version}"]
    for m in members:
        lines += ["", "[[members]]", f'name = "{m.name}"', f"since = {m.since}"]
        if m.retired_since is not None:
            lines.append(f"retired_since = {m.retired_since}")
        lines += [f"{key} = {_toml_value(value)}" for key, value in m.attrs.items()]
    return "\n".join(lines) + "\n"


def _state(table: Table, version: int) -> dict:
    return {
        "members": sorted(m.name for m in table.members.values() if m.since <= version),
        "retired": sorted(m.name for m in table.members.values()
                          if m.retired_since is not None and m.retired_since <= version),
    }


def _write(name: str, table: Table, root: "Path | None") -> None:
    ordered = sorted(table.members.values(), key=lambda m: (m.since, m.name))
    _table_path(name, root).write_text(_render(table.version, ordered), encoding="utf-8")
    snap = snapshot(root)
    snap.setdefault(name, {})[str(table.version)] = _state(table, table.version)
    _snapshot_path(root).write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def add_member(name: str, member: str, root: "Path | None" = None, attrs: "dict | None" = None) -> int:
    """Add a member (with its extra attributes, for a table whose members carry some) and bump
    the table version by one; returns the new version."""
    table = load(name, root)
    if member in table.members:
        raise TableError(f"{member!r} is already a member of {name}")
    version = table.version + 1
    members = dict(table.members)
    members[member] = Member(member, version, None, dict(attrs or {}))
    _write(name, Table(name, version, members), root)
    return version


def retire_member(name: str, member: str, root: "Path | None" = None) -> int:
    """Retire a member (never deleted) and bump the table version by one."""
    table = load(name, root)
    current = table.members.get(member)
    if current is None or current.retired_since is not None:
        raise TableError(f"{member!r} is not a live member of {name}")
    version = table.version + 1
    members = dict(table.members)
    members[member] = Member(member, current.since, version, current.attrs)
    _write(name, Table(name, version, members), root)
    return version


def check_consistency(root: "Path | None" = None) -> "list[str]":
    """Problems between each table and its snapshot history; an empty list is consistent."""
    problems = []
    snap = snapshot(root)
    for name in TABLE_NAMES:
        table = load(name, root)
        history = snap.get(name, {})
        versions = sorted(int(v) for v in history)
        if versions != list(range(1, table.version + 1)):
            problems.append(f"{name}: history versions {versions} are not 1..{table.version}")
            continue
        if history[str(table.version)] != _state(table, table.version):
            problems.append(f"{name}: version {table.version} differs from the table file")
        for version in versions[1:]:
            before, after = history[str(version - 1)], history[str(version)]
            added = set(after["members"]) - set(before["members"])
            removed = set(before["members"]) - set(after["members"])
            retired = set(after["retired"]) - set(before["retired"])
            unretired = set(before["retired"]) - set(after["retired"])
            if removed or unretired or len(added) + len(retired) != 1:
                problems.append(f"{name}: version {version} is not exactly one add or retire of version {version - 1}")
    return problems
