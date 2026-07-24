"""Core commit-authority detection (ADR-0001 § Submission (non-author, no push)).

A machine either holds Core commit authority or it does not. ``is_author()`` decides via a
``git push --dry-run`` capability probe — push access IS the authority. The explicit ``flag``
param is a test seam only; the config-flag branch (``is_author`` row in config.md) was removed
because config.md is git-shared and therefore identical on every clone, making the flag useless
as a per-machine discriminator.

Channel selection: the machine's preferred channel is read from the system config root's
``agent-identity.local`` (resolved via ``scripts/lib/config_root.py`` — ``~/.claude-agent`` when
isolated, ``~/.claude`` on a legacy machine); (``difficulty_channel=<name>``); the default is
``github`` (the public built-in). A machine that needs an org-specific channel installs that
channel's adapter as a plugin (``adapters.load_adapter`` — see
``scripts/difficulty_channel/adapters/__init__.py``) and sets ``difficulty_channel=<name>`` to
that plugin's name; resolution below loads the plugin before looking it up in the registry.

Both the push probe and the HTTP clients are injectable so tests run offline with no real push.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from .port import DifficultyRecord, get_channel, is_registered
from . import adapters as _adapters  # noqa: F401  — registers the built-in adapters (github, external)
from .adapters import load_adapter

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ for lib.config_root
from lib.config_root import identity_file  # noqa: E402

LOCAL_IDENTITY_PATH = identity_file()
DEFAULT_CHANNEL = "github"


def probe_push_capability(
    runner: Callable[[list[str]], int] | None = None,
    remote: str = "origin",
    ref: str = "HEAD",
) -> bool:
    """`git push --dry-run` capability probe. No actual push (dry-run). Mockable via runner."""
    def _default_runner(cmd: list[str]) -> int:
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True).returncode

    run = runner or _default_runner
    return run(["git", "push", "--dry-run", remote, ref]) == 0


def is_author(
    probe: Callable[[], bool] | None = None,
    flag: bool | None = None,
) -> bool:
    """Push-capability is the authoritative source. ``flag`` is a test seam only."""
    if flag is not None:
        return flag
    probe_fn = probe or probe_push_capability
    return probe_fn()


# The two routing outcomes for a Core-target difficulty.
ROUTE_EDIT_CORE = "edit-core"        # author: normal planner -> approval -> developer spine
ROUTE_TO_CHANNEL = "route-to-channel"  # non-author: file a DifficultyRecord, never edit Core


def route_for_core_difficulty(author: bool) -> str:
    return ROUTE_EDIT_CORE if author else ROUTE_TO_CHANNEL


def read_local_identity(path: Path = LOCAL_IDENTITY_PATH) -> dict[str, str]:
    """Parse key=value lines from the machine-local identity file; skip blank lines and comments."""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return result


def read_configured_channel(path: Path = LOCAL_IDENTITY_PATH) -> str:
    """Return the machine's preferred channel from agent-identity.local; default: github."""
    return read_local_identity(path).get("difficulty_channel", DEFAULT_CHANNEL)


def file_core_difficulty(record: DifficultyRecord, channel: str | None = None, **kwargs) -> str:
    """Non-author path: submit the difficulty to a channel the machine already has write to.
    Returns the channel-native handle (tracker key or GitHub issue URL). NEVER edits Core.

    ``load_adapter`` is a no-op for a built-in channel and loads+registers a machine-local
    plugin adapter for any other configured name, so a non-built-in channel still resolves.
    Skipped entirely when ``ch`` is already registered (a built-in, an already-loaded plugin,
    or a channel a test registered directly) — it is not a plugin file on disk.
    """
    ch = channel if channel is not None else read_configured_channel()
    if not is_registered(ch):
        load_adapter(ch)
    return get_channel(ch, **kwargs).submit(record)
