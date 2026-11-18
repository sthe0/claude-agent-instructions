"""Per-machine opt-in for a reaper that deletes things nobody else asked it to.

Difficulty removed: a reaper that proves residue dead is still a deletion on a machine whose
owner has not yet seen what it would remove. The machine keeps one file,
``<agent home>/reaper/opt-in.toml``, with ``<reaper NAME> = true`` for each reaper its owner
enabled; plugin-layer reapers may read the same file. A missing, unreadable or malformed
file, a missing key and any value other than the boolean ``true`` all mean off.

The same file may carry a ``[params.NAME]`` table of per-reaper parameters (``params``); a table
never turns a boolean key off or on, and a file that mixes both is well-formed.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from lib import config_root


def opt_in_path() -> Path:
    return config_root.agent_home() / "reaper" / "opt-in.toml"


def _load(path: "Path | None") -> "dict[str, object]":
    with open(path or opt_in_path(), "rb") as handle:
        return tomllib.load(handle)


def enabled(name: str, path: "Path | None" = None) -> bool:
    try:
        return _load(path).get(name) is True
    except (OSError, ValueError):
        return False


def params(name: str, path: "Path | None" = None) -> "dict[str, object]":
    """The ``[params.<name>]`` table, or {} when there is none, the file is missing, or it is malformed
    (a malformed file is reported on stderr)."""
    try:
        table = _load(path).get("params", {})
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"reaper: opt-in file unreadable ({type(exc).__name__}: {exc}); no parameters", file=sys.stderr)
        return {}
    entry = table.get(name) if isinstance(table, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}
