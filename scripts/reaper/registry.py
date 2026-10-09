"""Reaper discovery across three layers, later layer replacing an earlier same-NAME one.

Difficulty removed: an org layer or a project had no way to register its own cleanup
under the shared rules. This follows the other plugin seams (org-portability.md):
built-in first, machine-local plugin second, project third.

Layers, in order:
  builtin  scripts/reaper/builtin/*.py
  plugin   ${CLAUDE_REAPER_PLUGIN_DIR:-<config root>/reaper-plugins}/reapers/*.py
  project  <project>/.claude/reapers/*.py   (project = $CLAUDE_PROJECT_DIR, else cwd)

A module that fails to import, or lacks NAME, scan or remove, is skipped with one
stderr line; the rest still run.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from lib.plugin_dir import load_plugin_module, resolve_plugin_dir

PLUGIN_ENV_VAR = "CLAUDE_REAPER_PLUGIN_DIR"
PLUGIN_DIRNAME = "reaper-plugins"
REAPERS_SUBDIR = "reapers"
DEFAULT_THROTTLE_HOURS = 24.0
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"

LAYERS = ("builtin", "plugin", "project")


@dataclass
class Reaper:
    name: str
    layer: str
    file: str
    module: ModuleType

    @property
    def throttle_hours(self) -> float:
        return float(getattr(self.module, "THROTTLE_HOURS", DEFAULT_THROTTLE_HOURS))


def _contract_problem(module: ModuleType) -> "str | None":
    name = getattr(module, "NAME", None)
    if not isinstance(name, str) or not name:
        return "no NAME"
    if not callable(getattr(module, "scan", None)):
        return "no scan"
    if not callable(getattr(module, "remove", None)):
        return "no remove"
    throttle = getattr(module, "THROTTLE_HOURS", DEFAULT_THROTTLE_HOURS)
    if isinstance(throttle, bool) or not isinstance(throttle, (int, float)):
        return "THROTTLE_HOURS is not a number"
    return None


def _layer_files(directory: Path) -> "list[Path]":
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith("_"))


def _load_builtin(path: Path, builtin_dir: "Path | None") -> "ModuleType | None":
    if builtin_dir is None:
        return importlib.import_module(f"reaper.builtin.{path.stem}")
    return load_plugin_module(builtin_dir.parent, f"{builtin_dir.name}/{path.name}", "claude_reaper_builtin")


def _load_plugin(path: Path, root: Path, namespace: str) -> "ModuleType | None":
    return load_plugin_module(root, f"{path.parent.name}/{path.name}", namespace)


def discover(
    project_dir: Path,
    *,
    builtin_dir: "Path | None" = None,
    plugin_root: "Path | None" = None,
    warn: "Callable[[str], None]" = lambda line: print(line, file=sys.stderr),
) -> "list[Reaper]":
    """All usable reapers, in discovery order, after same-NAME replacement.

    ``builtin_dir`` and ``plugin_root`` exist so tests can point a layer at a temporary
    directory; production leaves them None.
    """
    plugin_root = plugin_root if plugin_root is not None else resolve_plugin_dir(PLUGIN_ENV_VAR, PLUGIN_DIRNAME)
    project_claude = Path(project_dir) / ".claude"
    layers: "list[tuple[str, Path, Callable[[Path], ModuleType | None]]]" = [
        ("builtin", builtin_dir or BUILTIN_DIR, lambda p: _load_builtin(p, builtin_dir)),
        ("plugin", plugin_root / REAPERS_SUBDIR, lambda p: _load_plugin(p, plugin_root, "claude_reaper_plugin")),
        ("project", project_claude / REAPERS_SUBDIR, lambda p: _load_plugin(p, project_claude, "claude_reaper_project")),
    ]
    found: "dict[str, Reaper]" = {}
    for layer, directory, load in layers:
        for path in _layer_files(directory):
            try:
                module = load(path)
            except (Exception, SystemExit) as exc:  # a plugin may call sys.exit() at import
                warn(f"reaper: skipped {path}: import failed: {type(exc).__name__}: {exc}")
                continue
            if module is None:
                continue
            problem = _contract_problem(module)
            if problem:
                warn(f"reaper: skipped {path}: {problem}")
                continue
            found[module.NAME] = Reaper(module.NAME, layer, str(path), module)
    return list(found.values())
