"""Session-locator seam: what the dashboard learns about harness sessions from the machine.

Core names no bridge, multiplexer or terminal; a machine-local plugin
``<plugin dir>/locators/<name>.py`` supplies that knowledge. It exposes ``NAME`` and
``health() -> bool | (bool, detail)``, which must stay local (it runs with the network and process
spawning stubbed to raise; touching a stub makes it unhealthy), and any of these optional hooks,
each called with a harness session id except ``sessions`` and ``inputs_stamp``:

- ``transcript_path(id) -> str | None``: the session's transcript file;
- ``permission_wait(id) -> bool | None``: a permission prompt is pending, newer than the transcript;
- ``jump_target(id) -> {"argv": [str, ...], "label": str, "return_hint": str} | None``;
- ``sessions() -> [{"harness_session": str, "cwd": str | None, "name": str | None}]``;
- ``inputs_stamp() -> int | str | [int | str, ...]``: changes whenever ``permission_wait``'s inputs
  change, so a waiting mark cached with it can be reused while it holds.

A hook that raises or returns a malformed value counts as absent. Exactly one locator is used per
run; ``jump`` runs a target's argv without a shell and never raises.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
from pathlib import Path

from lib import plugin_dir as plugin_dirs
from plan_dashboard import publish

LOCATOR_SUBDIR = "locators"
HOOKS = ("transcript_path", "permission_wait", "jump_target", "sessions", "inputs_stamp")
JUMP_TIMEOUT_S = 5
TIMEOUT_RETURNCODE = 124
NOT_FOUND_RETURNCODE = 127


class LocatorError(Exception):
    """No locator, several without a name, a plugin that does not load or lacks ``health()``."""


def discover(plugin_dir: Path | None = None) -> list:
    root = plugin_dir if plugin_dir is not None else plugin_dirs.resolve_plugin_dir(publish.PLUGIN_ENV, publish.PLUGIN_DIRNAME)
    directory = Path(root) / LOCATOR_SUBDIR
    found = []
    for path in sorted(directory.glob("*.py")) if directory.is_dir() else []:
        try:
            module = plugin_dirs.load_plugin_module(Path(root), f"{LOCATOR_SUBDIR}/{path.name}", "plan_dashboard_locator")
        except Exception as exc:
            raise LocatorError(f"{path.name} does not load: {type(exc).__name__}: {exc}") from exc
        if module is None or not callable(getattr(module, "health", None)):
            raise LocatorError(f"{path.name} lacks health()")
        found.append(module)
    return found


def name_of(locator) -> str:
    return str(getattr(locator, "NAME", None) or getattr(locator, "__name__", "?"))


def select(plugin_dir: Path | None = None, name: str | None = None):
    found = discover(plugin_dir)
    if name is not None:
        found = [m for m in found if name_of(m) == name]
    if not found:
        raise LocatorError(f"no locator plugin named {name}" if name else "no locator plugin found")
    if len(found) > 1:
        raise LocatorError(f"several locator plugins found ({', '.join(name_of(m) for m in found)}); pick one with --locator")
    return found[0]


def select_or_none(plugin_dir: Path | None = None, name: str | None = None):
    """The locator a view uses: the selected one, or None, in which case the views go on without."""
    try:
        return select(plugin_dir, name)
    except LocatorError:
        return None


def _refuse(*_args, **_kwargs):
    raise OSError("health() must stay local: network and process spawning are off")


@contextlib.contextmanager
def health_stubbed(hits: list):
    """Best effort, not a guarantee: replaces the common network and process entry points with
    functions that raise, and records each call in ``hits`` so a plugin that swallows the error
    still fails the check. A plugin reaching them another way (ctypes, a pre-imported alias) is not stopped.
    The stubs are process-global, so this runs only on the one-shot ``locate --health`` path, never under a server."""
    def stub(*args, **kwargs):
        hits.append(1)
        return _refuse(*args, **kwargs)

    names = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
             (socket, "getaddrinfo"), (subprocess, "Popen"), (os, "system"), (os, "posix_spawn")]
    names += [(os, name) for name in dir(os) if name.startswith("exec")]
    saved = [(owner, name, getattr(owner, name)) for owner, name in names]
    for owner, name in names:
        setattr(owner, name, stub)
    try:
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)


def check_health(locator) -> tuple[bool, str]:
    hits: list = []
    try:
        with health_stubbed(hits):
            result = locator.health()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if hits:
        return False, "health() reached the network or spawned a process"
    ok, detail = result if isinstance(result, tuple) else (result, "")
    return bool(ok), str(detail)


def defined_hooks(locator) -> list[str]:
    return [name for name in HOOKS if callable(getattr(locator, name, None))]


def _call(locator, hook: str, *args):
    function = getattr(locator, hook, None) if locator is not None else None
    if not callable(function):
        return None
    try:
        return function(*args)
    except Exception:
        return None


def transcript_path(locator, harness_session: str) -> Path | None:
    value = _call(locator, "transcript_path", harness_session)
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def permission_wait(locator, harness_session: str) -> bool | None:
    value = _call(locator, "permission_wait", harness_session)
    return value if isinstance(value, bool) else None


def _stamp_part(value) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def inputs_stamp(locator) -> tuple | None:
    """A hashable value that changes whenever the locator's own inputs change, so a mark cached
    with it is stale once it differs; None when the hook is absent or malformed."""
    value = _call(locator, "inputs_stamp")
    if _stamp_part(value):
        return (value,)
    if isinstance(value, (list, tuple)) and value and all(_stamp_part(v) for v in value):
        return tuple(value)
    return None


def jump_target(locator, harness_session: str) -> dict | None:
    value = _call(locator, "jump_target", harness_session)
    if not isinstance(value, dict):
        return None
    argv, label, hint = value.get("argv"), value.get("label"), value.get("return_hint")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return None
    if not (isinstance(label, str) and isinstance(hint, str)):
        return None
    return {"argv": list(argv), "label": label, "return_hint": hint}


def sessions(locator) -> list[dict]:
    """The sessions the machine knows of, each as ``harness_session``, ``cwd``, ``name``; a malformed
    entry, and a repeat of an id, is skipped."""
    value = _call(locator, "sessions")
    if not isinstance(value, list):
        return []
    rows, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        harness, cwd, name = entry.get("harness_session"), entry.get("cwd"), entry.get("name")
        if not isinstance(harness, str) or not harness or harness in seen:
            continue
        if not (cwd is None or isinstance(cwd, str)) or not (name is None or isinstance(name, str)):
            continue
        seen.add(harness)
        rows.append({"harness_session": harness, "cwd": cwd, "name": name})
    return rows


def _run(argv):
    return subprocess.run(argv, shell=False, timeout=JUMP_TIMEOUT_S, capture_output=True, text=True)


def jump(target: dict, *, runner=None) -> subprocess.CompletedProcess:
    """Run a jump target's argv. Always returns a result: the runner's own, ``124`` after a timeout,
    ``127`` when the program cannot be started."""
    argv = list(target["argv"])
    try:
        return (runner or _run)(argv)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, TIMEOUT_RETURNCODE, "", f"timed out after {JUMP_TIMEOUT_S} s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, NOT_FOUND_RETURNCODE, "", str(exc))
