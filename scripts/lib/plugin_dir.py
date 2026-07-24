"""Machine-local plugin directory resolution and module loading.

Difficulty removed: Core keeps the mechanism and the neutral default, while every
org-specific implementation attaches from a machine-local plugin directory outside
the public repo. Three seams now resolve such a directory and import a file out of
it — the difficulty-channel adapters, the difficulty-channel detect hook, and the
project-entry backend detect hook — and each hand-rolling the same
``spec_from_file_location`` dance would let them drift apart (in particular on the
subtle part: the plugin dir must be part of the cached module's identity).

This is the Python analog of ``scripts/project_entry/registry.sh``'s
``_plugin_dir`` / ``_registry_resolve`` pair: built-in first (the caller's own
concern), machine-local plugin second.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from .config_root import agent_home


def resolve_plugin_dir(env_var: str, dirname: str) -> Path:
    """Machine-local plugin root for one seam: ``$<env_var>`` else ``<config root>/<dirname>``.

    ``agent_home()`` already probes isolated-vs-legacy at the root level, so a
    not-yet-migrated machine resolves to ``~/.claude/<dirname>``.
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    return agent_home() / dirname


def load_plugin_module(plugin_dir: Path, relpath: str, namespace: str) -> ModuleType | None:
    """Import ``<plugin_dir>/<relpath>`` under a synthetic module name, or None if absent.

    The plugin dir is part of the module identity: keyed on ``relpath`` alone, a later
    call against a different plugin dir would silently reuse the first dir's module.

    A plugin module must use ABSOLUTE imports — it is executed under a synthetic
    package name whose parents are never imported, so a relative import raises.
    """
    tag = hashlib.sha1(str(plugin_dir).encode("utf-8")).hexdigest()[:8]
    stem = relpath[:-3] if relpath.endswith(".py") else relpath
    module_name = f"{namespace}.{tag}.{stem.replace('/', '.')}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    plugin_file = plugin_dir / relpath
    if not plugin_file.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A half-executed module left in the cache would be served to the next caller.
        sys.modules.pop(module_name, None)
        raise
    return module
