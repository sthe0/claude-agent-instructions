"""Landed-check provider seam: how a non-git delivery proves it reached trunk.

Difficulty removed: "did this delivery reach trunk" is mechanically decidable for any
VCS that can answer it, but Core may not name an org's VCS. The git check is built in;
every other VCS attaches through a machine-local plugin, so the decision stays
mechanical without the org leaking into Core.

A plan's ``[*.landed]`` table names ``provider = "<name>"`` (default ``"git"``). A
non-git provider is a module at ``<plugin dir>/providers/<name>.py``, where the plugin
dir is ``$CLAUDE_LANDED_CHECK_PLUGIN_DIR`` else ``<config root>/landed-check-plugins``.

Provider contract (absolute imports only — the module runs under a synthetic package
name whose parents are never imported):

* ``freeze(venue: str) -> str | None`` — an opaque token naming the delivered work at
  ``venue`` (the delivery venue's directory); ``None`` means it cannot be frozen. The
  engine stamps the token on the delivered stage and never re-derives it.
* ``is_landed(token: str, target: str) -> bool | None`` — ``True`` landed, ``False`` not
  yet, ``None`` cannot decide. Monotone: once ``True`` for a token it must stay ``True``.
  The provider owns proving the token belongs to this task's delivery (the git check's
  ``Task:`` trailer rule is the git form of that obligation).

Loading is lazy: nothing here touches the plugin dir at import time.
"""
from __future__ import annotations

import re
from pathlib import Path

from lib.plugin_dir import load_plugin_module, resolve_plugin_dir

PLUGIN_DIR_ENV = "CLAUDE_LANDED_CHECK_PLUGIN_DIR"
PLUGIN_DIR_NAME = "landed-check-plugins"
BUILTIN_PROVIDER = "git"
_PLUGIN_NAMESPACE = "agentctl._plugin_landed_providers"
PROVIDER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class LandedProviderBroken(Exception):
    """A provider plugin file exists but failed to import or lacks the contract.

    Distinct from FileNotFoundError (no plugin installed) so a caller can tell "the
    provider is broken" from "no provider was configured".
    """


def _plugin_dir() -> Path:
    return resolve_plugin_dir(PLUGIN_DIR_ENV, PLUGIN_DIR_NAME)


def load_provider(name: str):
    """Load the non-git provider ``name`` from the machine-local plugin dir.

    Raises ValueError for a name that is not a plain identifier or is the built-in
    ``git`` (never loaded from disk), FileNotFoundError when no plugin provides it,
    LandedProviderBroken when the file is there but fails to import or lacks
    ``freeze`` / ``is_landed``.
    """
    if not isinstance(name, str) or not PROVIDER_NAME_RE.match(name):
        raise ValueError(f"landed provider name {name!r} is not a plain identifier")
    if name == BUILTIN_PROVIDER:
        raise ValueError("the git provider is built in and is never loaded from a plugin")
    plugin_dir = _plugin_dir()
    relpath = f"providers/{name}.py"
    try:
        module = load_plugin_module(plugin_dir, relpath, _PLUGIN_NAMESPACE)
    except Exception as exc:
        raise LandedProviderBroken(
            f"landed provider plugin {plugin_dir / relpath} failed to import: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if module is None:
        raise FileNotFoundError(
            f"no landed provider plugin for {name!r}: looked for {plugin_dir / relpath} "
            f"(the built-in provider is {BUILTIN_PROVIDER!r}, which needs no plugin)"
        )
    for attr in ("freeze", "is_landed"):
        if not callable(getattr(module, attr, None)):
            raise LandedProviderBroken(
                f"landed provider plugin {plugin_dir / relpath} lacks a callable {attr}()"
            )
    return module
