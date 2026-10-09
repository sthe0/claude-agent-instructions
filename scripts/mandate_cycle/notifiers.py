"""Digest delivery for the background debt cycle: the notifier seam.

Difficulty removed: a label the cycle applied becomes eligible only after the user has had a
veto window to see it, so the digest has to reach the user by a channel they actually read.
Which channel that is differs per machine and is not Core's business, so Core ships one
built-in notifier (`file`, which the rules refuse to count as delivered) and a plugin seam a
machine-local layer fills.

A plugin is ``<plugin dir>/notifiers/<name>.py`` exposing ``NAME`` (str) and ``send(text) ->
bool``. The plugin dir is ``$CLAUDE_MANDATE_PLUGIN_DIR`` else ``<agent home>/mandate-plugins``.
Plugins are tried in filename order and the first one that loads is the notifier; a plugin
that fails to import or lacks ``send`` is skipped. With none loaded the `file` notifier is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lib import plugin_dir as plugin_dirs

PLUGIN_ENV = "CLAUDE_MANDATE_PLUGIN_DIR"
PLUGIN_DIRNAME = "mandate-plugins"
NOTIFIER_SUBDIR = "notifiers"
FILE_NOTIFIER = "file"


@dataclass(frozen=True)
class Notifier:
    name: str
    send: Callable[[str], bool]


@dataclass(frozen=True)
class Delivery:
    notifier: str
    ok: bool
    detail: str = ""


def plugin_root(override: "Path | None" = None) -> Path:
    return override if override is not None else plugin_dirs.resolve_plugin_dir(PLUGIN_ENV, PLUGIN_DIRNAME)


def discover_plugins(root: "Path | None" = None) -> "list[Notifier]":
    """Loadable notifier plugins in filename order; a broken one is skipped, not fatal."""
    directory = plugin_root(root) / NOTIFIER_SUBDIR
    if not directory.is_dir():
        return []
    found: "list[Notifier]" = []
    for path in sorted(directory.glob("*.py")):
        try:
            module = plugin_dirs.load_plugin_module(
                plugin_root(root), f"{NOTIFIER_SUBDIR}/{path.name}", "mandate_plugin",
            )
        except Exception:
            continue
        send = getattr(module, "send", None)
        if module is None or not callable(send):
            continue
        name = getattr(module, "NAME", None)
        found.append(Notifier(name if isinstance(name, str) and name else path.stem, send))
    return found


def file_notifier(digests_dir: Path, cycle_id: str) -> Notifier:
    def send(text: str) -> bool:
        digests_dir.mkdir(parents=True, exist_ok=True)
        (digests_dir / f"{cycle_id}.md").write_text(text, encoding="utf-8")
        return True

    return Notifier(FILE_NOTIFIER, send)


def select_notifier(digests_dir: Path, cycle_id: str, root: "Path | None" = None) -> Notifier:
    """The first loadable plugin, else the built-in `file` notifier."""
    plugins = discover_plugins(root)
    return plugins[0] if plugins else file_notifier(digests_dir, cycle_id)


def deliver(
    text: str, *, digests_dir: Path, cycle_id: str, root: "Path | None" = None,
) -> Delivery:
    """Deliver `text` through the selected notifier and say which one carried it.

    A copy always lands in `digests_dir`, so a failed or file-only delivery still leaves the
    digest where the user can read it. The returned name is the notifier that was actually
    used, because the eligibility rule reads it: `file` never counts as delivered.
    """
    notifier = select_notifier(digests_dir, cycle_id, root)
    copy = file_notifier(digests_dir, cycle_id)
    try:
        copy.send(text)
    except OSError as exc:
        if notifier.name == FILE_NOTIFIER:
            return Delivery(FILE_NOTIFIER, False, f"{type(exc).__name__}: {exc}")
    if notifier.name == FILE_NOTIFIER:
        return Delivery(FILE_NOTIFIER, True)
    try:
        ok = bool(notifier.send(text))
    except Exception as exc:
        return Delivery(notifier.name, False, f"{type(exc).__name__}: {exc}")
    return Delivery(notifier.name, ok, "" if ok else "send returned false")
