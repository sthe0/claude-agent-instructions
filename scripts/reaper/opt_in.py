"""Per-machine opt-in for a reaper that deletes things nobody else asked it to.

Difficulty removed: a reaper that proves residue dead is still a deletion on a machine whose
owner has not yet seen what it would remove. The machine keeps one file,
``<agent home>/reaper/opt-in.toml``, with ``<reaper NAME> = true`` for each reaper its owner
enabled; plugin-layer reapers may read the same file. A missing, unreadable or malformed
file, a missing key and any value other than the boolean ``true`` all mean off.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from lib import config_root


def opt_in_path() -> Path:
    return config_root.agent_home() / "reaper" / "opt-in.toml"


def enabled(name: str, path: "Path | None" = None) -> bool:
    try:
        with open(path or opt_in_path(), "rb") as handle:
            return tomllib.load(handle).get(name) is True
    except (OSError, ValueError):
        return False
