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

import hashlib
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
    scripts_dir = str(Path(__file__).resolve().parents[2])  # scripts/ for lib.config_root
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from lib.config_root import agent_home  # noqa: E402

    return agent_home() / "difficulty-channel-plugins"


def _module_name(plugin_dir: Path, name: str) -> str:
    # The plugin dir is part of the module identity: keyed on ``name`` alone, a later call with a
    # different CLAUDE_DIFFICULTY_PLUGIN_DIR would silently reuse the first dir's module.
    tag = hashlib.sha1(str(plugin_dir).encode("utf-8")).hexdigest()[:8]
    return f"difficulty_channel._plugin_adapters.{tag}.{name}"


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
    module_name = _module_name(plugin_dir, name)
    if module_name in sys.modules:
        return sys.modules[module_name]
    plugin_file = plugin_dir / "adapters" / f"{name}.py"
    if not plugin_file.is_file():
        raise FileNotFoundError(
            f"difficulty_channel: no adapter plugin named {name!r} (looked in {plugin_file}; "
            f"built-in names need no plugin: {', '.join(sorted(BUILTIN_NAMES))})"
        )
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
