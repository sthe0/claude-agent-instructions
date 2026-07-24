"""Concrete DifficultyChannel adapters (ADR-0001 adapter table).

Each adapter maps the one common DifficultyRecord onto a tracker's native fields, so a
non-author submits to a surface they already have write access to — never the protected Core.
Importing this package registers the built-in adapters with the port registry.

A non-built-in channel name is not shipped here: it is resolved at request time from a
machine-local plugin directory via ``load_adapter``, mirroring
``scripts/project_entry/registry.sh``'s backend resolution (built-in first, machine-local
plugin second) — an org-specific adapter (e.g. an internal tracker) attaches to Core through
this seam without ever living in the public repo.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from .github import GitHubChannel
from .external import ExternalChannel  # back-compat alias for GitHubChannel

__all__ = ["GitHubChannel", "ExternalChannel", "load_adapter"]

BUILTIN_NAMES = {"github", "external"}


def _plugin_dir() -> Path:
    """Machine-local adapter-plugin root (read-time resolution; overridable for tests)."""
    override = os.environ.get("CLAUDE_DIFFICULTY_PLUGIN_DIR")
    if override:
        return Path(override).expanduser()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/ for lib.config_root
    from lib.config_root import agent_home  # noqa: E402

    return agent_home() / "difficulty-channel-plugins"


def load_adapter(name: str):
    """Load and register a non-built-in adapter by name from the machine-local plugin dir.

    Returns the loaded plugin module (cached across calls), or ``None`` for a built-in name
    (already registered at package-import time — there is no lazily-loaded module to hand back).
    Raises FileNotFoundError naming both the plugin-dir root and the missing file — mirroring
    registry.sh's ``_registry_resolve`` error shape — if no plugin provides ``name``.
    """
    if name in BUILTIN_NAMES:
        return None
    module_name = f"difficulty_channel._plugin_adapters.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    plugin_file = _plugin_dir() / "adapters" / f"{name}.py"
    if not plugin_file.is_file():
        raise FileNotFoundError(
            f"difficulty_channel: no adapter plugin named {name!r} (looked in {plugin_file})"
        )
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
