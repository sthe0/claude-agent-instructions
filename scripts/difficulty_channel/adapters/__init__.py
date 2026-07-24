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

import sys
from pathlib import Path

from .github import GitHubChannel
from .external import ExternalChannel  # back-compat alias for GitHubChannel

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2])
if _SCRIPTS_DIR not in sys.path:  # scripts/ for lib.plugin_dir
    sys.path.insert(0, _SCRIPTS_DIR)
from lib.plugin_dir import load_plugin_module, resolve_plugin_dir  # noqa: E402

__all__ = ["GitHubChannel", "ExternalChannel", "load_adapter"]

BUILTIN_NAMES = {"github", "external"}

PLUGIN_DIR_ENV = "CLAUDE_DIFFICULTY_PLUGIN_DIR"
PLUGIN_DIR_NAME = "difficulty-channel-plugins"
_PLUGIN_NAMESPACE = "difficulty_channel._plugin_adapters"


def _plugin_dir() -> Path:
    """Machine-local adapter-plugin root (read-time resolution; overridable for tests)."""
    return resolve_plugin_dir(PLUGIN_DIR_ENV, PLUGIN_DIR_NAME)


def load_adapter(name: str):
    """Load and register a non-built-in adapter by name from the machine-local plugin dir.

    Returns the loaded plugin module (cached per plugin dir), or ``None`` for a built-in name
    (already registered at package-import time — there is no lazily-loaded module to hand back).
    Raises FileNotFoundError naming the plugin file searched and the built-in names that need no
    plugin — mirroring registry.sh's ``_registry_resolve`` error shape — if no plugin provides
    ``name``.

    Plugin contract. A plugin module lives at ``<plugin dir>/adapters/<name>.py`` and must:

    * call ``difficulty_channel.port.register_channel(<name>, <factory>)`` at import time, with a
      factory returning a ``DifficultyChannel``; that call is what makes ``get_channel(<name>)``
      resolve — loading alone registers nothing;
    * use ABSOLUTE imports only. The module is executed under the synthetic package name
      ``difficulty_channel._plugin_adapters.<tag>.<name>``, whose parents are never imported, so
      a relative import (``from ..port import ...``) raises;
    * expose whatever module-level surface its consumers reach for beyond the port. The in-tree
      consumers use ``QUEUE`` and ``BACKLOG_QUEUE`` (stream identifiers), ``add_tag(key, tag)``,
      ``add_comment(key, body, http=None)`` and ``list_comments(key, http=None)``; omitting one
      breaks only the consumer that calls it.
    """
    if name in BUILTIN_NAMES:
        return None
    plugin_dir = _plugin_dir()
    relpath = f"adapters/{name}.py"
    module = load_plugin_module(plugin_dir, relpath, _PLUGIN_NAMESPACE)
    if module is None:
        raise FileNotFoundError(
            f"difficulty_channel: no adapter plugin named {name!r} "
            f"(looked in {plugin_dir / relpath}; "
            f"built-in names need no plugin: {', '.join(sorted(BUILTIN_NAMES))})"
        )
    return module
