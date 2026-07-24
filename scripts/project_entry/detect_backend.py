"""Auto-detect the (workspace, tracker) backend pair for this machine.

detect_backends() is a pure function — every host-access operation is injected via
the three probe parameters, so tests run offline with no real host inspection. This
mirrors scripts/difficulty_channel/detect.py exactly (same injected-probe idiom, same
hook seam).

Core carries only the probe interface and the ORG-NEUTRAL precedence:
  1. an optional machine-local detect hook decides first (contract below)
  2. `gh` on PATH (GitHub CLI present) -> ('git', 'github')
  3. otherwise                         -> ('git', 'none')

The else-branch IS the org-neutral default, so an unconfigured public machine resolves
deterministically to git/none. Which toolchain identifies an *organization*'s backends
is org-specific DATA and lives in the machine-local plugin dir, never in this repo.

Plugin contract. ``<plugin dir>/detect.py`` — plugin dir: ``$CLAUDE_PROJECT_PLUGIN_DIR``
else ``<config root>/project-entry-plugins``, the same dir whose ``backends/`` and
``trackers/`` subdirs registry.sh resolves — exposes::

    detect(has_command, path_exists, getenv) -> (workspace, tracker) | None

called with the three probes as KEYWORD arguments. Returning a pair of backend NAMES
decides; returning None defers to the neutral rules above. A decided name must resolve
through registry.sh, i.e. the same plugin dir must also carry its backend/tracker script.
The hook module must use ABSOLUTE imports (see ``lib.plugin_dir.load_plugin_module``).

``detect_backends`` never loads the hook itself — that would make it impure. Impure
callers resolve it once via ``load_detect_hook()`` and pass it in; ``detect_this_machine()``
below is the reference wiring.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:  # scripts/ for lib.plugin_dir
    sys.path.insert(0, _SCRIPTS_DIR)
from lib.plugin_dir import load_plugin_module, resolve_plugin_dir  # noqa: E402

DEFAULT_BACKENDS = ("git", "none")

PLUGIN_DIR_ENV = "CLAUDE_PROJECT_PLUGIN_DIR"
PLUGIN_DIR_NAME = "project-entry-plugins"
_PLUGIN_NAMESPACE = "project_entry._plugin_detect"


def detect_backends(
    has_command: Callable[[str], bool],
    path_exists: Callable[[str], bool],
    getenv: Callable[[str], "str | None"],
    hook: "Callable[..., tuple[str, str] | None] | None" = None,
) -> "tuple[str, str]":
    """Pure detection — all host I/O via the three injected probes, org rules via `hook`.

    Returns (workspace, tracker) backend NAMES. path_exists / getenv are accepted for
    probe-signature parity with difficulty_channel.detect (and for the hook, which may
    key on them); today's neutral rule needs only has_command.
    """
    if hook is not None:
        decided = hook(has_command=has_command, path_exists=path_exists, getenv=getenv)
        if decided is not None:
            return decided
    if has_command("gh"):
        return ("git", "github")
    return DEFAULT_BACKENDS


def load_detect_hook() -> "Callable[..., tuple[str, str] | None] | None":
    """Resolve this machine's detect hook, or None when no plugin dir / hook is installed.

    Impure by construction (reads the filesystem) — kept out of `detect_backends` so that
    stays a pure function over its probes.
    """
    module = load_plugin_module(
        resolve_plugin_dir(PLUGIN_DIR_ENV, PLUGIN_DIR_NAME), "detect.py", _PLUGIN_NAMESPACE
    )
    return getattr(module, "detect", None) if module is not None else None


def _real_path_exists(p: str) -> bool:
    return os.path.exists(os.path.expanduser(p))


def detect_this_machine() -> "tuple[str, str]":
    """Reference wiring: the real probes plus this machine's detect hook."""
    return detect_backends(
        has_command=lambda cmd: shutil.which(cmd) is not None,
        path_exists=_real_path_exists,
        getenv=os.environ.get,
        hook=load_detect_hook(),
    )


if __name__ == "__main__":
    ws, tr = detect_this_machine()
    print(f"{ws} {tr}")
